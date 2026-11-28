import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass
from typing import Annotated, Literal, assert_never
from uuid import uuid4

import httpx2
from flow_res import Err, Ok, Result
from openai import AsyncOpenAI
from pydantic import Field
from pydantic_ai import Agent, AgentRunResult, FunctionToolset, Tool, UsageLimits
from pydantic_ai.exceptions import (
    ModelConnectionError,
    ModelHTTPError,
    ModelRateLimitError,
    ModelServerError,
    ModelUnavailableError,
)
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
    SecretStr,
    redact_secrets,
)
from my_agents.shell.logger import IAppLogger, LogEntry, LogLevel
from my_agents.shell.mcp import (
    McpServerSpec,
    StdioMcpServer,
    build_toolsets,
)
from my_agents.shell.settings import Settings
from my_agents.shell.store import Store

_REQUEST_LIMITS = UsageLimits(request_limit=6, tool_calls_limit=8)
_DEFAULT_TIMEOUT_SECONDS = 60.0
_DEFAULT_MAX_RETRIES = 2
_DEFAULT_RETRY_BASE_DELAY = 0.5
_TRANSIENT_EXCEPTIONS: tuple[type[Exception], ...] = (
    TimeoutError,
    ConnectionError,
    httpx2.TransportError,
    ModelConnectionError,
    ModelRateLimitError,
    ModelServerError,
    ModelUnavailableError,
)


def _is_transient_error(exc: Exception) -> bool:
    if isinstance(exc, _TRANSIENT_EXCEPTIONS):
        return True
    if isinstance(exc, ModelHTTPError):
        return exc.status_code in {408, 429} or exc.status_code >= 500
    return False


@dataclass(frozen=True, slots=True)
class ChatReply:
    text: str
    memory_updates: tuple[str, ...]


def _spec_secrets(spec: McpServerSpec) -> tuple[SecretStr, ...]:
    if isinstance(spec, StdioMcpServer):
        return tuple(value for _name, value in spec.env)
    return (spec.url, *(value for _name, value in spec.headers))


def static_secrets(settings: Settings) -> tuple[SecretStr, ...]:
    secrets: list[SecretStr] = []
    if settings.llm_api_key is not None:
        secrets.append(settings.llm_api_key)
    if settings.vapid_private_key is not None:
        secrets.append(settings.vapid_private_key)
    servers = settings.mcp_servers
    if not isinstance(servers, Err):
        for spec in servers.value:
            secrets.extend(_spec_secrets(spec))
    return tuple(secrets)


def _history(messages: Sequence[ChatMessage]) -> list[ModelMessage]:
    history: list[ModelMessage] = []
    for item in messages:
        match item.role:
            case "user":
                history.append(ModelRequest([UserPromptPart(item.content)]))
            case "assistant":
                history.append(ModelResponse([TextPart(item.content)]))
            case _:
                assert_never(item.role)
    return history


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
        record_data = asdict(result.value)
        record_data["data"] = json.loads(record_data.pop("payload_json"))
        payload = json.dumps(record_data, ensure_ascii=False, default=str)
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
        redactions: Sequence[SecretStr] = (),
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        retry_base_delay: float = _DEFAULT_RETRY_BASE_DELAY,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._external_toolsets = tuple(external_toolsets)
        self._logger = logger
        self._redactions: tuple[SecretStr, ...] = tuple(redactions)
        self._model_name: str = model.model_name
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._sleep = sleep
        self._stack: AsyncExitStack | None = None
        self._chat: Agent[None, str] = Agent(model)
        self._evaluator: Agent[None, EventDecision] = Agent(
            model, output_type=EventDecision
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        store: Store,
        logger: IAppLogger,
        base_env: Mapping[str, str],
    ) -> Result[Llm, AppError]:
        servers = settings.mcp_servers
        if isinstance(servers, Err):
            return servers
        toolsets = build_toolsets(servers.value, base_env=base_env)
        redactions = tuple(
            value for spec in servers.value for value in _spec_secrets(spec)
        )
        api_key = settings.llm_api_key
        if api_key is None:
            return Err(AppError(AppErrorKind.VALIDATION, "LLM_API_KEY is required."))
        model_name = settings.llm_model
        if model_name is None:
            return Err(AppError(AppErrorKind.VALIDATION, "LLM_MODEL is required."))
        redactions += (api_key,)
        # Disable SDK-level retries so LLM_MAX_RETRIES is the sole retry control and avoids multiplicative retries.
        client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=api_key.get_secret_value(),
            max_retries=0,
        )
        model = OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(openai_client=client),
        )
        return Ok(
            cls(
                model,
                store=store,
                logger=logger,
                external_toolsets=toolsets,
                redactions=redactions,
                timeout_seconds=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
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
        conversation_id: str,
        invoke: Callable[[], Awaitable[AgentRunResult[T]]],
    ) -> Result[T, AppError]:
        started = time.monotonic()
        attempt = 0
        while True:
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    result = await invoke()
            except Exception as exc:
                duration_ms = round((time.monotonic() - started) * 1000.0, 2)
                if attempt < self._max_retries and _is_transient_error(exc):
                    delay = self._retry_base_delay * (2**attempt)
                    self._logger.log(
                        LogEntry(
                            LogLevel.INFO,
                            "llm_retry",
                            self.redact(f"{label} transient error, retrying: {exc}"),
                            error_type=type(exc).__name__,
                            detail=f"attempt={attempt + 1}/{self._max_retries + 1}",
                            duration_ms=duration_ms,
                            conversation_id=conversation_id,
                            model=self._model_name,
                        )
                    )
                    attempt += 1
                    await self._sleep(delay)
                    continue
                if isinstance(exc, TimeoutError):
                    detail = str(exc) or f"timed out after {self._timeout_seconds}s"
                else:
                    detail = str(exc)
                message = self.redact(f"{label} failed: {detail}")
                self._logger.log(
                    LogEntry(
                        LogLevel.ERROR,
                        "llm_failed",
                        message,
                        error_type=type(exc).__name__,
                        detail=f"attempts={attempt + 1}",
                        duration_ms=duration_ms,
                        conversation_id=conversation_id,
                        model=self._model_name,
                    )
                )
                return Err(AppError(AppErrorKind.LLM, message))
            duration_ms = round((time.monotonic() - started) * 1000.0, 2)
            usage = result.usage
            self._logger.log(
                LogEntry(
                    LogLevel.INFO,
                    event,
                    "llm call completed",
                    detail=f"cache_read_tokens={usage.cache_read_tokens}",
                    duration_ms=duration_ms,
                    conversation_id=conversation_id,
                    model=self._model_name,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
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
            conversation_id,
            lambda: self._chat.run(
                user_text,
                message_history=_history(history),
                instructions=context.instructions,
                toolsets=[
                    toolbox.toolset(
                        allow_memory_writes=context.pending_event_count == 0
                    ),
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
            envelope.conversation_id,
            lambda: self._evaluator.run(
                build_event_prompt(envelope, uuid4().hex),
                instructions=stable_prefix,
                usage_limits=_REQUEST_LIMITS,
            ),
        )
