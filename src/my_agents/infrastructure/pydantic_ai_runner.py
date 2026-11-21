from collections.abc import Sequence
from typing import assert_never

import pydantic_ai
from flow_res import Err, Ok, Result
from pydantic_ai import Agent
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
from my_agents.infrastructure.settings import AppSettings


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


class PydanticAIAgentRunner(IAgentRunner):
    def __init__(self, model_or_agent: Model | Agent[None, str]) -> None:
        self._agent: Agent[None, str]
        if isinstance(model_or_agent, Agent):
            self._agent = model_or_agent
        else:
            self._agent = Agent(model_or_agent)

    @classmethod
    def from_settings(cls, settings: AppSettings) -> PydanticAIAgentRunner:
        pydantic_ai.BANNER_ENABLED = False
        model = OpenAIChatModel(
            settings.model_name,
            provider=OpenAIProvider(
                base_url=settings.base_url,
                api_key=settings.api_key.get_secret_value(),
            ),
        )
        return cls(model)

    async def run(
        self,
        user_input: str,
        *,
        history: Sequence[ChatMessage],
        instructions: str,
    ) -> Result[AgentReply, AgentRunnerError]:
        message_history = _to_model_messages(history)
        try:
            result = await self._agent.run(
                user_input,
                message_history=message_history,
                instructions=instructions,
            )
        except ModelAPIError as exc:
            return Err(
                AgentRunnerError(
                    type=AgentRunnerErrorType.API_ERROR,
                    message=str(exc),
                )
            )
        except UnexpectedModelBehavior as exc:
            return Err(
                AgentRunnerError(
                    type=AgentRunnerErrorType.UNEXPECTED_BEHAVIOR,
                    message=str(exc),
                )
            )
        except UsageLimitExceeded as exc:
            return Err(
                AgentRunnerError(
                    type=AgentRunnerErrorType.USAGE_LIMIT_EXCEEDED,
                    message=str(exc),
                )
            )
        except Exception as exc:
            return Err(
                AgentRunnerError(
                    type=AgentRunnerErrorType.UNEXPECTED,
                    message=str(exc),
                )
            )
        return Ok(AgentReply(content=result.output))
