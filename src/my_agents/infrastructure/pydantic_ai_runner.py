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
from my_agents.contracts.ports.association_expander import (
    AssociationExpandError,
    AssociationExpandErrorType,
    AssociationExpandRequest,
    IAssociationExpander,
)
from my_agents.contracts.ports.chat_recall import (
    ChatRecallError,
    ChatRecallErrorType,
    ChatSearchHit,
    IChatRecall,
    RecalledEpisode,
)
from my_agents.contracts.ports.core_profile_updater import (
    CoreMemoryUpdateAction,
    CoreMemoryUpdateCommand,
    CoreMemoryUpdateError,
    CoreMemoryUpdateErrorType,
    ICoreProfileUpdater,
)
from my_agents.contracts.secret import SecretStr
from my_agents.infrastructure.error_formatting import format_exception, redact_secret
from my_agents.infrastructure.settings import AppSettings

DEFAULT_REQUEST_LIMIT = 6
DEFAULT_TOOL_CALLS_LIMIT = 4
DEFAULT_USAGE_LIMITS = UsageLimits(
    request_limit=DEFAULT_REQUEST_LIMIT,
    tool_calls_limit=DEFAULT_TOOL_CALLS_LIMIT,
)


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


def _format_association_error(error: AssociationExpandError) -> str:
    match error.type:
        case AssociationExpandErrorType.INVALID_INPUT:
            return (
                f"Association expansion input was invalid: {error.message} "
                "Provide a non-empty topic and retry."
            )
        case AssociationExpandErrorType.EXPANSION_FAILED:
            return (
                f"Association expansion failed: {error.message} "
                "Answer without this tool instead."
            )
        case _:
            assert_never(error.type)


def _build_association_toolset(
    expander: IAssociationExpander,
) -> FunctionToolset[None]:
    async def expand_associations(
        topic: Annotated[
            str,
            Field(
                description=(
                    "Concrete object, place, problem, or theme to explore, such as "
                    "a shopping item, an everyday annoyance, or a trip idea."
                )
            ),
        ],
        context: Annotated[
            str,
            Field(
                description=(
                    "Optional constraints, preferences, or current situation that "
                    "should steer the associations. Leave empty when irrelevant."
                )
            ),
        ] = "",
    ) -> str:
        result = await expander.expand(
            AssociationExpandRequest(topic=topic, context=context)
        )
        if is_err(result):
            return _format_association_error(result.error)
        outcome = result.value
        ideas = "\n".join(f"- {idea}" for idea in outcome.lateral_ideas)
        return (
            f"3-level Magical Banana association tree for {outcome.root}:\n"
            f"{outcome.tree_summary}\n\n"
            f"Lateral ideas:\n{ideas}"
        )

    tool: Tool[None] = Tool(
        expand_associations,
        name="expand_associations",
        description=(
            "Compute a 3-level cross-category Magical Banana association tree "
            "for a consultation, shopping, lifestyle, planning, or brainstorming "
            "topic."
        ),
        takes_ctx=False,
    )
    return FunctionToolset(tools=[tool])


def _format_recall_error(error: ChatRecallError) -> str:
    match error.type:
        case ChatRecallErrorType.INVALID_INPUT:
            return f"Invalid chat history request: {error.message}"
        case ChatRecallErrorType.NOT_FOUND:
            return f"Conversation not found: {error.message}"
        case ChatRecallErrorType.STORAGE_ERROR:
            return f"Could not read conversation history: {error.message}"
        case _:
            assert_never(error.type)


def _build_chat_recall_toolset(recall: IChatRecall) -> FunctionToolset[None]:
    def recall_conversation(
        episode_id: Annotated[
            str,
            Field(description="Episode ID from the summary index or search results."),
        ],
        limit: Annotated[
            int,
            Field(description="Maximum number of recent dialogue messages to return."),
        ] = 40,
    ) -> RecalledEpisode | str:
        result = recall.recall_episode(episode_id, limit=limit)
        return _format_recall_error(result.error) if is_err(result) else result.value

    def search_conversations(
        query: Annotated[
            str,
            Field(
                description="Literal term to look for in titles, summaries, and dialogue."
            ),
        ],
        limit: Annotated[
            int, Field(description="Maximum number of matching episodes to return.")
        ] = 10,
    ) -> tuple[ChatSearchHit, ...] | str:
        result = recall.search_episodes(query, limit=limit)
        return _format_recall_error(result.error) if is_err(result) else result.value

    recall_tool: Tool[None] = Tool(
        recall_conversation,
        description="Retrieve summary and recent dialogue for a specific past conversation by episode ID.",
        takes_ctx=False,
    )
    search_tool: Tool[None] = Tool(
        search_conversations,
        description="Find past conversations that mention a term and return compact results with episode IDs.",
        takes_ctx=False,
    )
    return FunctionToolset(tools=[recall_tool, search_tool])


class PydanticAIAgentRunner(IAgentRunner):
    def __init__(
        self,
        model_or_agent: Model | Agent[None, str],
        *,
        api_key: SecretStr | None = None,
        core_profile_updater: ICoreProfileUpdater | None = None,
        association_expander: IAssociationExpander | None = None,
        chat_recall: IChatRecall | None = None,
        usage_limits: UsageLimits = DEFAULT_USAGE_LIMITS,
    ) -> None:
        self._agent: Agent[None, str]
        if isinstance(model_or_agent, Agent):
            self._agent = model_or_agent
        else:
            self._agent = Agent(model_or_agent)
        self._api_key = api_key
        self._core_profile_updater = core_profile_updater
        self._association_expander = association_expander
        self._chat_recall = chat_recall
        self._usage_limits = usage_limits

    @classmethod
    def from_settings(
        cls,
        settings: AppSettings,
        *,
        core_profile_updater: ICoreProfileUpdater | None = None,
        association_expander: IAssociationExpander | None = None,
        chat_recall: IChatRecall | None = None,
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
            association_expander=association_expander,
            chat_recall=chat_recall,
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
        toolsets: list[FunctionToolset[None]] = []
        if self._core_profile_updater is not None:
            toolsets.append(
                _build_core_memory_toolset(
                    self._core_profile_updater,
                    recorded_updates,
                )
            )
        if self._association_expander is not None:
            toolsets.append(_build_association_toolset(self._association_expander))
        if self._chat_recall is not None:
            toolsets.append(_build_chat_recall_toolset(self._chat_recall))
        try:
            result = await self._agent.run(
                user_input,
                message_history=message_history,
                instructions=instructions,
                toolsets=toolsets if toolsets else None,
                usage_limits=self._usage_limits,
            )
        except ModelAPIError as exc:
            return Err(
                AgentRunnerError(
                    type=AgentRunnerErrorType.API_ERROR,
                    message=redact_secret(format_exception(exc), self._api_key),
                )
            )
        except UnexpectedModelBehavior as exc:
            return Err(
                AgentRunnerError(
                    type=AgentRunnerErrorType.UNEXPECTED_BEHAVIOR,
                    message=redact_secret(format_exception(exc), self._api_key),
                )
            )
        except UsageLimitExceeded as exc:
            return Err(
                AgentRunnerError(
                    type=AgentRunnerErrorType.USAGE_LIMIT_EXCEEDED,
                    message=redact_secret(format_exception(exc), self._api_key),
                )
            )
        except Exception as exc:
            return Err(
                AgentRunnerError(
                    type=AgentRunnerErrorType.UNEXPECTED,
                    message=redact_secret(format_exception(exc), self._api_key),
                    detail=redact_secret(traceback.format_exc(), self._api_key),
                )
            )
        return Ok(
            AgentReply(
                content=result.output,
                core_memory_updates=tuple(recorded_updates),
            )
        )
