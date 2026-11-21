from dataclasses import dataclass
from enum import Enum, auto
from typing import assert_never

from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.messages import ChatMessage
from my_agents.contracts.ports.agent_runner import (
    AgentRunnerError,
    AgentRunnerErrorType,
    IAgentRunner,
)

DEFAULT_INSTRUCTIONS = "日本語で簡潔に応答してください。"


class SendMessageErrorType(Enum):
    EMPTY_INPUT = auto()
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
        *,
        instructions: str = DEFAULT_INSTRUCTIONS,
    ) -> None:
        self._agent_runner = agent_runner
        self._instructions = instructions

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

        result = await self._agent_runner.run(
            normalized_input,
            history=request.history,
            instructions=self._instructions,
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
            )
        )
