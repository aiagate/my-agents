import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass
from typing import Annotated, Literal
from uuid import uuid4

from flow_res import Err, Ok, Result
from pydantic import Field
from pydantic_ai import Agent, AgentRunResult, FunctionToolset, Tool, UsageLimits
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.toolsets import AbstractToolset

from my_agents.core.context import AssembledContext
from my_agents.core.events import build_event_prompt, fence_untrusted
from my_agents.core.memory import edit_memory
from my_agents.core.types import (
    AppError,
    AppErrorKind,
    ChatMessage,
    EventDecision,
    EventEnvelope,
    MemoryBlock,
    MemoryBlocks,
    redact_secrets,
)
from my_agents.shell.logger import IAppLogger, LogEntry, LogLevel
from my_agents.shell.mcp import build_toolsets
from my_agents.shell.settings import Settings
from my_agents.shell.store import Store

_REQUEST_LIMITS = UsageLimits(request_limit=6, tool_calls_limit=8)


@dataclass(frozen=True, slots=True)
class ChatReply:
    text: str
    memory_updates: tuple[str, ...]


def _history(messages: Sequence[ChatMessage]) -> list[ModelMessage]:
    return [
        ModelRequest([UserPromptPart(item.content)])
        if item.role == "user"
        else ModelResponse([TextPart(item.content)])
        for item in messages
    ]


class ChatToolbox:
    def __init__(
        self,
        store: Store,
        conversation_id: str,
        recorded_updates: list[str],
    ) -> None:
        self._store = store
        self._conversation_id = conversation_id
        self._recorded_updates = recorded_updates

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
        payload = json.dumps(asdict(result.value), ensure_ascii=False, default=str)
        return fence_untrusted(payload, uuid4().hex)

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
        def edit(blocks: MemoryBlocks) -> Result[MemoryBlocks, AppError]:
            return edit_memory(blocks, action, block, content, old_text)

        saved = await asyncio.to_thread(self._store.update_memory, edit)
        if isinstance(saved, Err):
            return f"Memory update rejected: {saved.error.message}"
        self._recorded_updates.append(f"{action} {block.value}")
        return "Core memory updated."

    def toolset(self, *, allow_memory_writes: bool) -> FunctionToolset[None]:
        tools = [
            Tool(
                self.read_event,
                description="Read one event ledger record by ID.",
                takes_ctx=False,
            ),
        ]
        if allow_memory_writes:
            tools.append(
                Tool(
                    self.update_core_memory,
                    description="Edit a core memory block immediately.",
                    takes_ctx=False,
                )
            )
        return FunctionToolset(tools=tools)


class Llm:
    def __init__(
        self,
        model: Model,
        *,
        store: Store,
        logger: IAppLogger,
        external_toolsets: Sequence[AbstractToolset[None]] = (),
        redactions: tuple[str, ...] = (),
    ) -> None:
        self._store = store
        self._external_toolsets = tuple(external_toolsets)
        self._logger = logger
        self._redactions = redactions
        self._stack: AsyncExitStack | None = None
        self._chat: Agent[None, str] = Agent(model)
        self._evaluator: Agent[None, EventDecision] = Agent(
            model, output_type=EventDecision
        )

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
        ) + tuple(
            spec.url.get_secret_value()
            for spec in servers.value
            if spec.url is not None
        )
        api_key = settings.llm_api_key
        if api_key is None:
            return Err(AppError(AppErrorKind.VALIDATION, "LLM_API_KEY is required."))
        model_name = settings.llm_model
        if model_name is None:
            return Err(AppError(AppErrorKind.VALIDATION, "LLM_MODEL is required."))
        redactions += (api_key.get_secret_value(),)
        model = OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(
                base_url=settings.llm_base_url,
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
        event: str,
        invoke: Callable[[], Awaitable[AgentRunResult[T]]],
    ) -> Result[T, AppError]:
        try:
            result = await invoke()
        except Exception as exc:
            return Err(
                AppError(
                    AppErrorKind.LLM,
                    self.redact(f"{label} failed: {exc}"),
                )
            )
        self._logger.log(
            LogEntry(
                LogLevel.INFO,
                event,
                "llm call completed",
                detail=f"cache_read_tokens={result.usage.cache_read_tokens}",
            )
        )
        return Ok(result.output)

    async def chat(
        self,
        conversation_id: str,
        user_text: str,
        history: Sequence[ChatMessage],
        context: AssembledContext,
    ) -> Result[ChatReply, AppError]:
        recorded: list[str] = []
        toolbox = ChatToolbox(self._store, conversation_id, recorded)
        result = await self._run(
            "Chat",
            "chat_completed",
            lambda: self._chat.run(
                user_text,
                message_history=_history(history),
                instructions=context.instructions,
                toolsets=[
                    toolbox.toolset(allow_memory_writes=not context.event_section),
                    *self._external_toolsets,
                ],
                usage_limits=_REQUEST_LIMITS,
            ),
        )
        if isinstance(result, Err):
            return result
        return Ok(ChatReply(result.value, tuple(recorded)))

    async def evaluate_event(
        self, envelope: EventEnvelope, stable_prefix: str
    ) -> Result[EventDecision, AppError]:
        return await self._run(
            "Event evaluation",
            "event_evaluated",
            lambda: self._evaluator.run(
                build_event_prompt(envelope, uuid4().hex),
                instructions=stable_prefix,
                usage_limits=_REQUEST_LIMITS,
            ),
        )
