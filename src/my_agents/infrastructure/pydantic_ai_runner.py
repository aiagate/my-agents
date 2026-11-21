import traceback
from collections.abc import Sequence
from typing import Annotated, assert_never

import pydantic_ai
from flow_res import Err, Ok, Result, is_err
from pydantic import Field
from pydantic_ai import Agent, FunctionToolset, Tool, UsageLimits
from pydantic_ai.exceptions import (
    ModelAPIError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
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

from my_agents.contracts.messages import AgentReply, ChatMessage
from my_agents.contracts.ports.agent_runner import (
    AgentRunnerError,
    AgentRunnerErrorType,
    IAgentRunner,
)
from my_agents.contracts.ports.core_profile_updater import (
    CoreMemoryUpdateAction,
    CoreMemoryUpdateCommand,
    CoreMemoryUpdateError,
    CoreMemoryUpdateErrorType,
    ICoreProfileUpdater,
)
from my_agents.contracts.secret import SecretStr
from my_agents.infrastructure.settings import AppSettings

DEFAULT_REQUEST_LIMIT = 4
DEFAULT_TOOL_CALLS_LIMIT = 3
DEFAULT_USAGE_LIMITS = UsageLimits(
    request_limit=DEFAULT_REQUEST_LIMIT,
    tool_calls_limit=DEFAULT_TOOL_CALLS_LIMIT,
)


def _format_exception(exc: Exception) -> str:
    text = str(exc).strip()
    if text:
        return f"{type(exc).__name__}: {text}"
    return type(exc).__name__


def _redact_secret(text: str, secret: SecretStr | None) -> str:
    if secret is None:
        return text
    raw = secret.get_secret_value()
    if not raw:
        return text
    return text.replace(raw, "***")


def _to_model_messages(history: Sequence[ChatMessage]) -> list[ModelMessage]:
    converted: list[ModelMessage] = []
    for message in history:
        match message.role:
            case "user":
                converted.append(ModelRequest([UserPromptPart(message.content)]))
            case "assistant":
                converted.append(ModelResponse([TextPart(message.content)]))
            case _:
                assert_never(message.role)
    return converted


def _format_update_error(error: CoreMemoryUpdateError) -> str:
    match error.type:
        case CoreMemoryUpdateErrorType.INVALID_INPUT:
            return (
                f"Invalid Core Memory update: {error.message} "
                "Provide a non-empty reason and the required content or old_text."
            )
        case CoreMemoryUpdateErrorType.TARGET_NOT_FOUND:
            return (
                f"Core Memory update target was not found: {error.message} "
                "Use replace only with an exact snippet that exists in the current "
                "Core Memory, or use append for new content."
            )
        case CoreMemoryUpdateErrorType.AMBIGUOUS_TARGET:
            return (
                f"Core Memory update target is ambiguous: {error.message} "
                "Include more surrounding lines in old_text so it matches exactly "
                "once, then retry."
            )
        case CoreMemoryUpdateErrorType.MAX_LENGTH_EXCEEDED:
            return (
                f"Core Memory update exceeds the limit: {error.message} "
                "Condense the content and retry with rewrite or replace so the "
                "profile stays within the character limit."
            )
        case CoreMemoryUpdateErrorType.STORAGE_ERROR:
            return (
                f"Core Memory could not be persisted: {error.message} "
                "Check the memory directory and retry the update."
            )
        case _:
            assert_never(error.type)


def _build_core_memory_toolset(
    updater: ICoreProfileUpdater,
    recorded_updates: list[str],
) -> FunctionToolset[None]:
    def update_core_memory(
        action: Annotated[
            CoreMemoryUpdateAction,
            Field(
                description=(
                    "append to add a new durable fact or theme, replace to correct "
                    "or remove one existing item, or rewrite to reorganize the whole "
                    "profile when it is near the character limit."
                )
            ),
        ],
        reason: Annotated[
            str,
            Field(description="Short reason for the update, shown to the user."),
        ],
        content: Annotated[
            str,
            Field(
                description=(
                    "New content for append/rewrite, or the replacement text for "
                    "replace. Empty content deletes the matched text on replace."
                )
            ),
        ] = "",
        old_text: Annotated[
            str,
            Field(
                description=(
                    "Exact existing text to replace. Required for replace and "
                    "ignored for append/rewrite."
                )
            ),
        ] = "",
    ) -> str:
        result = updater.update(
            CoreMemoryUpdateCommand(
                action=action,
                reason=reason,
                content=content,
                old_text=old_text,
            )
        )
        if is_err(result):
            return _format_update_error(result.error)
        outcome = result.value
        recorded_updates.append(outcome.reason)
        return (
            f"Core Memory updated ({outcome.char_count}/{outcome.max_chars} chars): "
            f"{outcome.reason}"
        )

    tool: Tool[None] = Tool(
        update_core_memory,
        name="update_core_memory",
        description=(
            "Update the persistent Core Memory when the user shares a durable value, "
            "decision axis, constraint, active theme, or preferred dialogue style. "
            "Do not use it for transient one-off topics."
        ),
        takes_ctx=False,
    )
    return FunctionToolset(tools=[tool])


class PydanticAIAgentRunner(IAgentRunner):
    def __init__(
        self,
        model_or_agent: Model | Agent[None, str],
        *,
        api_key: SecretStr | None = None,
        core_profile_updater: ICoreProfileUpdater | None = None,
        usage_limits: UsageLimits = DEFAULT_USAGE_LIMITS,
    ) -> None:
        self._agent: Agent[None, str]
        if isinstance(model_or_agent, Agent):
            self._agent = model_or_agent
        else:
            self._agent = Agent(model_or_agent)
        self._api_key = api_key
        self._core_profile_updater = core_profile_updater
        self._usage_limits = usage_limits

    @classmethod
    def from_settings(
        cls,
        settings: AppSettings,
        *,
        core_profile_updater: ICoreProfileUpdater | None = None,
        usage_limits: UsageLimits = DEFAULT_USAGE_LIMITS,
    ) -> PydanticAIAgentRunner:
        pydantic_ai.BANNER_ENABLED = False
        model = OpenAIChatModel(
            settings.model_name,
            provider=OpenAIProvider(
                base_url=settings.base_url,
                api_key=settings.api_key.get_secret_value(),
            ),
        )
        return cls(
            model,
            api_key=settings.api_key,
            core_profile_updater=core_profile_updater,
            usage_limits=usage_limits,
        )

    async def run(
        self,
        user_input: str,
        *,
        history: Sequence[ChatMessage],
        instructions: str,
    ) -> Result[AgentReply, AgentRunnerError]:
        message_history = _to_model_messages(history)
        recorded_updates: list[str] = []
        try:
            if self._core_profile_updater is None:
                result = await self._agent.run(
                    user_input,
                    message_history=message_history,
                    instructions=instructions,
                    usage_limits=self._usage_limits,
                )
            else:
                result = await self._agent.run(
                    user_input,
                    message_history=message_history,
                    instructions=instructions,
                    toolsets=[
                        _build_core_memory_toolset(
                            self._core_profile_updater,
                            recorded_updates,
                        )
                    ],
                    usage_limits=self._usage_limits,
                )
        except ModelAPIError as exc:
            return Err(
                AgentRunnerError(
                    type=AgentRunnerErrorType.API_ERROR,
                    message=_redact_secret(_format_exception(exc), self._api_key),
                )
            )
        except UnexpectedModelBehavior as exc:
            return Err(
                AgentRunnerError(
                    type=AgentRunnerErrorType.UNEXPECTED_BEHAVIOR,
                    message=_redact_secret(_format_exception(exc), self._api_key),
                )
            )
        except UsageLimitExceeded as exc:
            return Err(
                AgentRunnerError(
                    type=AgentRunnerErrorType.USAGE_LIMIT_EXCEEDED,
                    message=_redact_secret(_format_exception(exc), self._api_key),
                )
            )
        except Exception as exc:
            return Err(
                AgentRunnerError(
                    type=AgentRunnerErrorType.UNEXPECTED,
                    message=_redact_secret(_format_exception(exc), self._api_key),
                    detail=_redact_secret(traceback.format_exc(), self._api_key),
                )
            )
        return Ok(
            AgentReply(
                content=result.output,
                core_memory_updates=tuple(recorded_updates),
            )
        )
