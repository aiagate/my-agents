from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol, assert_never

from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.messages import ChatMessage
from my_agents.contracts.ports.agent_runner import (
    AgentRunnerError,
    AgentRunnerErrorType,
    IAgentRunner,
)
from my_agents.domain.context_assembler import assemble_instructions
from my_agents.usecases.get_core_profile import (
    GetCoreProfileUseCase,
)


class SendMessageErrorType(Enum):
    EMPTY_INPUT = auto()
    CORE_PROFILE_ERROR = auto()
    API_ERROR = auto()
    UNEXPECTED_BEHAVIOR = auto()
    USAGE_LIMIT_EXCEEDED = auto()
    UNEXPECTED = auto()


@dataclass(frozen=True, slots=True)
class SendMessageError:
    type: SendMessageErrorType
    message: str
    display_message: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class SendMessageRequest:
    user_input: str
    history: tuple[ChatMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class SendMessageResponse:
    reply: str
    history: tuple[ChatMessage, ...]
    core_memory_updates: tuple[str, ...] = ()


class SendMessageUseCase(Protocol):
    async def execute(
        self,
        request: SendMessageRequest,
    ) -> Result[SendMessageResponse, SendMessageError]: ...


def _to_send_message_error(error: AgentRunnerError) -> SendMessageError:
    match error.type:
        case AgentRunnerErrorType.API_ERROR:
            return SendMessageError(
                type=SendMessageErrorType.API_ERROR,
                message=error.message,
                display_message=(
                    "APIとの通信でエラーが発生しました。"
                    "しばらくしてから再度お試しください。"
                ),
                detail=error.detail,
            )
        case AgentRunnerErrorType.UNEXPECTED_BEHAVIOR:
            return SendMessageError(
                type=SendMessageErrorType.UNEXPECTED_BEHAVIOR,
                message=error.message,
                display_message="モデルから予期しない応答が返されました。",
                detail=error.detail,
            )
        case AgentRunnerErrorType.USAGE_LIMIT_EXCEEDED:
            return SendMessageError(
                type=SendMessageErrorType.USAGE_LIMIT_EXCEEDED,
                message=error.message,
                display_message=(
                    "利用上限に達しました。しばらくしてから再度お試しください。"
                ),
                detail=error.detail,
            )
        case AgentRunnerErrorType.UNEXPECTED:
            return SendMessageError(
                type=SendMessageErrorType.UNEXPECTED,
                message=error.message,
                display_message="予期しないエラーが発生しました。",
                detail=error.detail,
            )
        case _:
            assert_never(error.type)


class SendMessageHandler:
    def __init__(
        self,
        agent_runner: IAgentRunner,
        get_core_profile: GetCoreProfileUseCase,
    ) -> None:
        self._agent_runner = agent_runner
        self._get_core_profile = get_core_profile

    async def execute(
        self,
        request: SendMessageRequest,
    ) -> Result[SendMessageResponse, SendMessageError]:
        normalized_input = request.user_input.strip()
        if not normalized_input:
            return Err(
                SendMessageError(
                    type=SendMessageErrorType.EMPTY_INPUT,
                    message="User input must not be empty or whitespace only.",
                    display_message="メッセージを入力してください。",
                )
            )

        profile_result = self._get_core_profile.execute()
        if is_err(profile_result):
            return Err(
                SendMessageError(
                    type=SendMessageErrorType.CORE_PROFILE_ERROR,
                    message=profile_result.error.message,
                    display_message=profile_result.error.display_message,
                )
            )

        result = await self._agent_runner.run(
            normalized_input,
            history=request.history,
            instructions=assemble_instructions(profile_result.value.profile),
        )
        if is_err(result):
            return Err(_to_send_message_error(result.error))

        reply = result.value
        return Ok(
            SendMessageResponse(
                reply=reply.content,
                history=(
                    *request.history,
                    ChatMessage(role="user", content=normalized_input),
                    ChatMessage(role="assistant", content=reply.content),
                ),
                core_memory_updates=reply.core_memory_updates,
            )
        )
