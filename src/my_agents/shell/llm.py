import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from flow_res import Err, Ok, Result
from pydantic import BaseModel, Field, SecretStr
from pydantic_ai import Agent, AgentRunResult, FunctionToolset, Tool, UsageLimits
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.toolsets import AbstractToolset

from my_agents.core.context import AssembledContext, build_proactive_prompt
from my_agents.core.events import (
    build_distill_prompt,
    fence_untrusted,
    validate_proactive_message,
)
from my_agents.core.memory import build_summary_prompt, edit_memory, normalize_summary
from my_agents.core.types import (
    AppError,
    AppErrorKind,
    DialogueTurn,
    EventBrief,
    EventEnvelope,
    EventRecord,
    MemoryBlock,
    MemoryBlocks,
    MemoryCandidate,
    redact_secrets,
)
from my_agents.shell.logger import IAppLogger, LogEntry, LogLevel
from my_agents.shell.mcp import build_toolsets
from my_agents.shell.settings import Settings
from my_agents.shell.store import Store

_REQUEST_LIMITS = UsageLimits(request_limit=6, tool_calls_limit=8)
_EVENT_ID = re.compile(r"\[event:(evt_[0-9a-zA-Z_]+)\]")
_EPISODE_ID = re.compile(r"ep_[0-9a-f]+")
_URGENT = ("至急", "障害", "請求", "期限")


@dataclass(frozen=True, slots=True)
class ChatReply:
    text: str
    memory_updates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SummaryOutput:
    title: str
    overview: str
    summary: str


class _Summary(BaseModel):
    title: str = Field(description="Short topic title")
    overview: str = Field(description="One or two sentence overview")
    summary: str = Field(description="Structured recall notes")


def _last_user(messages: Sequence[ModelMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart):
                    return str(part.content)
    return ""


def _fenced_payload(instructions: str) -> str:
    _, marker, rest = instructions.partition(">>\n")
    body, _, _ = rest.partition("\n<<END_UNTRUSTED_EVENT")
    return body if marker else "{}"


def _event_name(instructions: str) -> str:
    for line in instructions.splitlines():
        if line.startswith("event_name: "):
            return line.removeprefix("event_name: ").strip()
    return "unknown.event"


def _local_brief(instructions: str) -> EventBrief:
    try:
        data = json.loads(_fenced_payload(instructions))
    except json.JSONDecodeError:
        data = {}
    payload = data if isinstance(data, dict) else {}
    name = str(payload.get("kind", _event_name(instructions)))
    raw_values = payload.get("data", {})
    values = raw_values if isinstance(raw_values, dict) else {}
    if name == "temperature.threshold_exceeded":
        return EventBrief(
            headline="室内の温度が設定値を上回っています",
            facts=(
                f"計測値 {values.get('celsius', 0)}°C",
                f"閾値 {values.get('threshold_celsius', 0)}°C",
            ),
            impact="このままだと作業環境がつらくなるかもしれません。",
            suggested_angle="換気や空調をやさしく気づかう",
            urgency=8,
            relevance=8,
            confidence=0.92,
            requires_user_action=True,
            sensitive=False,
            dedupe_key=name,
        )
    if name == "email.received":
        subject = str(values.get("subject", ""))
        return EventBrief(
            headline=f"{values.get('sender', '差出人')} からメールが届きました",
            facts=(f"件名: {subject}",),
            impact="内容によっては返信や共有が必要かもしれません。",
            suggested_angle="用件を確認して、必要なら一緒に段取りを考える",
            urgency=7,
            relevance=7,
            confidence=0.8,
            requires_user_action=any(term in subject for term in _URGENT),
            sensitive=False,
            dedupe_key=name,
        )
    return EventBrief(
        headline="外部イベントを検知しました",
        facts=(name,),
        impact="必要に応じて内容を確認してください。",
        suggested_angle="さりげなく知らせる",
        urgency=6,
        relevance=5,
        confidence=0.7,
        requires_user_action=False,
        sensitive=False,
        dedupe_key=name,
    )


def _local_summary(instructions: str) -> _Summary:
    body = instructions.partition("new_turns:\n")[2]
    first = body.splitlines()[0] if body.splitlines() else "会話"
    return _Summary(
        title=first.split(": ", 1)[-1][:40] or "会話",
        overview="ローカルモードの決定論的要約",
        summary=body[:2000],
    )


def _local_chat(instructions: str, prompt: str) -> str:
    event_ids = _EVENT_ID.findall(instructions)
    if "イベント" in prompt or "event" in prompt.lower():
        if event_ids:
            return f"参照できるイベントは {event_ids[0]} です。背景も踏まえてお話ししますね。"
        return "ローカル応答: イベント文脈にイベントはありません。"
    return f"ローカル応答: {prompt}"


def _local_proactive(instructions: str) -> str:
    headline = "少し共有したいことがあります"
    try:
        data = json.loads(_fenced_payload(instructions))
    except json.JSONDecodeError:
        data = {}
    if isinstance(data, dict) and data.get("headline"):
        headline = str(data["headline"])
    return f"{headline}。少し気になったので共有しますね。"


def _last_tool_return(messages: Sequence[ModelMessage]) -> ToolReturnPart | None:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, ToolReturnPart):
                    return part
    return None


def _tool_names(info: AgentInfo) -> set[str]:
    return {tool.name for tool in info.function_tools}


def _prompt_after(prompt: str, marker: str) -> str:
    tail = prompt.replace(marker, "", 1).lstrip(":：、 ").strip()
    return tail.removeprefix("して").lstrip(":：、 ").strip()


def _local_external_tool(
    prompt: str, names: set[str], returned: ToolReturnPart | None
) -> ModelResponse | None:
    if returned is not None:
        if returned.tool_name == "diverge_associations":
            return ModelResponse([TextPart(f"連想: {returned.content}")])
        if returned.tool_name == "remember":
            return ModelResponse([TextPart("覚えました。")])
        if returned.tool_name == "search_conversations":
            try:
                hits = json.loads(str(returned.content))
            except json.JSONDecodeError:
                hits = []
            if not isinstance(hits, list) or not hits:
                return ModelResponse([TextPart("該当する会話は見つかりませんでした。")])
            first = hits[0]
            identifier = first.get("episode_id") if isinstance(first, dict) else None
            if isinstance(identifier, str):
                return ModelResponse(
                    [
                        ToolCallPart(
                            "recall_conversation",
                            {"episode_id": identifier, "limit": 40},
                        )
                    ]
                )
            return ModelResponse([TextPart("該当する会話は見つかりませんでした。")])
        if returned.tool_name == "recall_conversation":
            return ModelResponse([TextPart(f"思い出しました: {returned.content}")])
    if "diverge_associations" in names and "連想" in prompt:
        return ModelResponse(
            [
                ToolCallPart(
                    "diverge_associations",
                    {
                        "seed": _prompt_after(prompt, "連想") or prompt,
                        "context": "",
                        "limit": 3,
                    },
                )
            ]
        )
    if "search_conversations" in names and "検索" in prompt:
        return ModelResponse(
            [
                ToolCallPart(
                    "search_conversations",
                    {"query": _prompt_after(prompt, "検索") or prompt, "limit": 5},
                )
            ]
        )
    if "recall_conversation" in names and "思い出" in prompt:
        match = _EPISODE_ID.search(prompt)
        if match is not None:
            return ModelResponse(
                [
                    ToolCallPart(
                        "recall_conversation",
                        {"episode_id": match.group(0), "limit": 40},
                    )
                ]
            )
    if "remember" in names and "覚えて" in prompt:
        return ModelResponse(
            [
                ToolCallPart(
                    "remember",
                    {
                        "block": "user",
                        "content": _prompt_after(prompt, "覚えて") or prompt,
                        "reason": "user asked",
                    },
                )
            ]
        )
    return None


def _local_respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    instructions = info.instructions or ""
    prompt = _last_user(messages)
    source = f"{instructions}\n{prompt}"
    if info.output_tools:
        tool = info.output_tools[0]
        if "headline" in tool.parameters_json_schema.get("properties", {}):
            return ModelResponse(
                [ToolCallPart(tool.name, _local_brief(source).model_dump())]
            )
        return ModelResponse(
            [ToolCallPart(tool.name, _local_summary(source).model_dump())]
        )
    routed = _local_external_tool(
        prompt, _tool_names(info), _last_tool_return(messages)
    )
    if routed is not None:
        return routed
    if "能動発話" in source:
        return ModelResponse([TextPart(_local_proactive(source))])
    return ModelResponse([TextPart(_local_chat(instructions, prompt))])


def _history(turns: Sequence[DialogueTurn]) -> list[ModelMessage]:
    return [
        ModelRequest([UserPromptPart(turn.content)])
        if turn.role == "user"
        else ModelResponse([TextPart(turn.content)])
        for turn in turns
    ]


def _json(value: EventRecord | dict[str, object]) -> str:
    payload = asdict(value) if isinstance(value, EventRecord) else value
    return json.dumps(payload, ensure_ascii=False, default=str)


class ChatToolbox:
    def __init__(
        self,
        store: Store,
        conversation_id: str,
        recorded_updates: list[str],
        now: datetime,
        *,
        allow_memory_writes: bool = True,
    ) -> None:
        self._store = store
        self._conversation_id = conversation_id
        self._recorded_updates = recorded_updates
        self._now = now
        self._allow_memory_writes = allow_memory_writes

    async def read_event(
        self, event_id: Annotated[str, Field(description="Event ledger ID.")]
    ) -> str:
        result = await asyncio.to_thread(
            self._store.get_event_in, self._conversation_id, event_id
        )
        if isinstance(result, Err):
            return f"Could not read the event ledger: {result.error.message}"
        if result.value is None:
            return f"Event not found: {event_id}"
        return fence_untrusted(_json(result.value), uuid4().hex)

    async def search_conversations(
        self,
        query: Annotated[str, Field(description="Term to search for.")],
        limit: Annotated[int, Field(description="Maximum episodes.")] = 10,
    ) -> str:
        result = await asyncio.to_thread(
            self._store.search_episodes,
            self._conversation_id,
            query,
            limit=limit,
        )
        if isinstance(result, Err):
            return f"Could not search conversations: {result.error.message}"
        hits = [
            {
                "episode_id": episode.episode_id,
                "title": episode.title,
                "overview": episode.overview,
            }
            for episode in result.value
            if query in f"{episode.title}\n{episode.overview}\n{episode.summary}"
        ]
        return json.dumps(hits, ensure_ascii=False)

    async def recall_conversation(
        self,
        episode_id: Annotated[str, Field(description="Episode ID.")],
        limit: Annotated[int, Field(description="Maximum turns.")] = 40,
    ) -> str:
        episode = await asyncio.to_thread(
            self._store.get_episode_in, self._conversation_id, episode_id
        )
        if isinstance(episode, Err):
            return f"Could not recall conversation: {episode.error.message}"
        if episode.value is None:
            return f"Conversation not found: {episode_id}"
        turns = await asyncio.to_thread(self._store.list_turns, episode_id, limit=limit)
        if isinstance(turns, Err):
            return f"Could not recall conversation: {turns.error.message}"
        return _json(
            {
                "episode": episode.value.episode_id,
                "title": episode.value.title,
                "turns": [turn.content for turn in turns.value],
            }
        )

    async def remember(
        self,
        block: Annotated[MemoryBlock, Field(description="user, relationship, or arc.")],
        content: Annotated[str, Field(description="Durable fact to remember.")],
        reason: Annotated[str, Field(description="Short reason.")],
    ) -> str:
        candidate = MemoryCandidate(
            f"mc_{uuid4().hex[:12]}",
            self._conversation_id,
            block,
            content,
            reason,
            self._now,
        )
        result = await asyncio.to_thread(self._store.save_candidate, candidate)
        if isinstance(result, Err):
            return f"Could not remember: {result.error.message}"
        self._recorded_updates.append(reason)
        return "Remembered for sleep-time curation."

    async def update_core_memory(
        self,
        action: Annotated[
            Literal["append", "replace", "rewrite"],
            Field(description="append, replace, or rewrite."),
        ],
        block: Annotated[MemoryBlock, Field(description="Memory block.")],
        content: Annotated[str, Field(description="New memory text.")],
        old_text: Annotated[str, Field(description="Exact replace target.")] = "",
    ) -> str:
        memory = await asyncio.to_thread(self._store.read_memory)
        if isinstance(memory, Err):
            return f"Could not read memory: {memory.error.message}"

        def edit(blocks: MemoryBlocks):
            return edit_memory(blocks, action, block, content, old_text)

        updated = edit(memory.value)
        if isinstance(updated, Err):
            return f"Memory update rejected: {updated.error.message}"
        saved = await asyncio.to_thread(self._store.update_memory, edit)
        if isinstance(saved, Err):
            return f"Could not save memory: {saved.error.message}"
        self._recorded_updates.append(f"{action} {block.value}")
        return "Core memory updated."

    def toolset(self) -> FunctionToolset[None]:
        tools = [
            Tool(
                self.read_event,
                description="Read one event ledger record by ID.",
                takes_ctx=False,
            ),
            Tool(
                self.search_conversations,
                description="Search past conversations for a term.",
                takes_ctx=False,
            ),
            Tool(
                self.recall_conversation,
                description="Recall one episode and its recent turns.",
                takes_ctx=False,
            ),
        ]
        if self._allow_memory_writes:
            tools.extend(
                [
                    Tool(
                        self.remember,
                        description="Queue a durable memory candidate.",
                        takes_ctx=False,
                    ),
                    Tool(
                        self.update_core_memory,
                        description="Edit a core memory block immediately.",
                        takes_ctx=False,
                    ),
                ]
            )
        return FunctionToolset(tools=tools)


class Llm:
    def __init__(
        self,
        model: Model,
        *,
        store: Store,
        logger: IAppLogger,
        clock: Callable[[], datetime] | None = None,
        external_toolsets: Sequence[AbstractToolset[None]] = (),
        redactions: tuple[str, ...] = (),
    ) -> None:
        self._store = store
        self._external_toolsets = tuple(external_toolsets)
        self._logger = logger
        self._redactions = redactions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stack: AsyncExitStack | None = None
        self._chat: Agent[None, str] = Agent(model)
        self._distill: Agent[None, EventBrief] = Agent(model, output_type=EventBrief)
        self._composer: Agent[None, str] = Agent(model)
        self._summarizer: Agent[None, _Summary] = Agent(model, output_type=_Summary)

    @classmethod
    def from_settings(
        cls, settings: Settings, *, store: Store, logger: IAppLogger
    ) -> Result[Llm, AppError]:
        servers = settings.mcp_servers
        if isinstance(servers, Err):
            return servers
        toolsets = build_toolsets(servers.value)
        redactions = tuple(
            value.get_secret_value()
            for spec in servers.value
            for _name, value in (*spec.env, *spec.headers)
        )
        if settings.magpie_api_key is not None:
            redactions += (settings.magpie_api_key.get_secret_value(),)
        if settings.chat_backend == "local":
            return Ok(
                cls(
                    FunctionModel(_local_respond),
                    store=store,
                    logger=logger,
                    external_toolsets=toolsets,
                    redactions=redactions,
                )
            )
        api_key: SecretStr | None = settings.magpie_api_key
        if api_key is None:
            return Err(
                AppError(
                    AppErrorKind.VALIDATION,
                    "MAGPIE_API_KEY is required for the openai backend.",
                )
            )
        model = OpenAIChatModel(
            settings.magpie_model,
            provider=OpenAIProvider(
                base_url=settings.magpie_base_url,
                api_key=api_key.get_secret_value(),
            ),
        )
        return Ok(
            cls(
                model,
                store=store,
                logger=logger,
                external_toolsets=toolsets,
                redactions=redactions,
            )
        )

    def redact(self, text: str) -> str:
        return redact_secrets(text, self._redactions)

    async def start(self) -> Result[None, AppError]:
        if self._stack is not None or not self._external_toolsets:
            return Ok(None)
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            for toolset in self._external_toolsets:
                await stack.enter_async_context(toolset)
        except Exception as exc:
            await stack.aclose()
            return Err(
                AppError(
                    AppErrorKind.LLM,
                    self.redact(f"MCP startup failed: {exc}"),
                )
            )
        self._stack = stack
        return Ok(None)

    async def aclose(self) -> None:
        if self._stack is None:
            return
        stack = self._stack
        self._stack = None
        await stack.aclose()

    async def _run[T](
        self,
        label: str,
        invoke: Callable[[], Awaitable[AgentRunResult[T]]],
    ) -> Result[AgentRunResult[T], AppError]:
        try:
            return Ok(await invoke())
        except Exception as exc:
            return Err(
                AppError(
                    AppErrorKind.LLM,
                    self.redact(f"{label} failed: {exc}"),
                )
            )

    def _log_usage(self, result: object, event: str) -> None:
        usage = getattr(result, "usage", None)
        cache_read = getattr(usage, "cache_read_tokens", 0) or 0
        self._logger.log(
            LogEntry(
                LogLevel.INFO,
                event,
                "llm call completed",
                detail=f"cache_read_tokens={cache_read}",
            )
        )

    async def chat(
        self,
        conversation_id: str,
        user_text: str,
        history: Sequence[DialogueTurn],
        context: AssembledContext,
    ) -> Result[ChatReply, AppError]:
        recorded: list[str] = []
        toolbox = ChatToolbox(
            self._store,
            conversation_id,
            recorded,
            self._clock(),
            allow_memory_writes=not context.event_section,
        )
        try:
            async with AsyncExitStack() as stack:
                toolsets: list[AbstractToolset[None]] = [toolbox.toolset()]
                for toolset in self._external_toolsets:
                    if self._stack is None:
                        await stack.enter_async_context(toolset)
                    toolsets.append(toolset)
                result = await self._run(
                    "Chat",
                    lambda: self._chat.run(
                        user_text,
                        message_history=_history(history),
                        instructions=context.instructions,
                        toolsets=toolsets,
                        usage_limits=_REQUEST_LIMITS,
                    ),
                )
        except Exception as exc:
            return Err(
                AppError(
                    AppErrorKind.LLM,
                    self.redact(f"Chat toolset failed: {exc}"),
                )
            )
        if isinstance(result, Err):
            return result
        self._log_usage(result.value, "chat_completed")
        return Ok(ChatReply(str(result.value.output), tuple(recorded)))

    async def distill(self, envelope: EventEnvelope) -> Result[EventBrief, AppError]:
        result = await self._run(
            "Distillation",
            lambda: self._distill.run(
                build_distill_prompt(envelope, uuid4().hex),
                usage_limits=_REQUEST_LIMITS,
            ),
        )
        if isinstance(result, Err):
            return result
        self._log_usage(result.value, "distill_completed")
        return Ok(result.value.output)

    async def compose(
        self, brief: EventBrief, context: AssembledContext
    ) -> Result[str, AppError]:
        result = await self._run(
            "Composer",
            lambda: self._composer.run(
                build_proactive_prompt(brief, uuid4().hex),
                instructions=context.instructions,
                usage_limits=_REQUEST_LIMITS,
            ),
        )
        if isinstance(result, Err):
            return result
        self._log_usage(result.value, "compose_completed")
        text = str(result.value.output).strip()
        if not validate_proactive_message(text):
            return Err(
                AppError(
                    AppErrorKind.VALIDATION,
                    "Proactive message contained machine-readable leakage.",
                )
            )
        return Ok(text)

    async def summarize(
        self, turns: Sequence[DialogueTurn], previous_summary: str
    ) -> Result[SummaryOutput, AppError]:
        result = await self._run(
            "Summarizer",
            lambda: self._summarizer.run(
                build_summary_prompt(turns, previous_summary),
                usage_limits=_REQUEST_LIMITS,
            ),
        )
        if isinstance(result, Err):
            return result
        self._log_usage(result.value, "summary_completed")
        title, overview, summary = normalize_summary(
            result.value.output.title,
            result.value.output.overview,
            result.value.output.summary,
            fallback_title="会話",
        )
        return Ok(SummaryOutput(title, overview, summary))
