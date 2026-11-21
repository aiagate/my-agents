from collections.abc import Sequence
from dataclasses import dataclass

import pytest
from flow_res import Err, Ok, Result, is_ok

from my_agents.contracts.errors import AppError, DisplayableError, TypedError
from my_agents.contracts.messages import AgentReply, ChatMessage
from my_agents.contracts.ports.agent_runner import (
    AgentRunnerError,
    AgentRunnerErrorType,
    IAgentRunner,
)
from my_agents.usecases.send_message import (
    DEFAULT_INSTRUCTIONS,
    SendMessageError,
    SendMessageErrorType,
    SendMessageHandler,
    SendMessageRequest,
    SendMessageResponse,
)
from tests.helpers import assert_err


@dataclass(frozen=True, slots=True)
class RecordedRun:
    user_input: str
    history: tuple[ChatMessage, ...]
    instructions: str


class FakeAgentRunner(IAgentRunner):
    def __init__(self, result: Result[AgentReply, AgentRunnerError]) -> None:
        self.result = result
        self.runs: list[RecordedRun] = []

    async def run(
        self,
        user_input: str,
        *,
        history: Sequence[ChatMessage],
        instructions: str,
    ) -> Result[AgentReply, AgentRunnerError]:
        self.runs.append(
            RecordedRun(
                user_input=user_input,
                history=tuple(history),
                instructions=instructions,
            )
        )
        return self.result


@pytest.mark.anyio
async def test_execute_returns_reply_and_appends_history() -> None:
    runner = FakeAgentRunner(Ok(AgentReply(content="こんにちは")))
    handler = SendMessageHandler(runner)
    previous = ChatMessage(role="user", content="前回の質問")
    request = SendMessageRequest(
        user_input="  今日は？  ",
        history=(previous,),
    )

    result = await handler.execute(request)

    assert is_ok(result)
    assert result.unwrap() == SendMessageResponse(
        reply="こんにちは",
        history=(
            previous,
            ChatMessage(role="user", content="今日は？"),
            ChatMessage(role="assistant", content="こんにちは"),
        ),
    )
    assert hash(request) == hash(
        SendMessageRequest(user_input="  今日は？  ", history=(previous,))
    )
    assert runner.runs == [
        RecordedRun(
            user_input="今日は？",
            history=(previous,),
            instructions=DEFAULT_INSTRUCTIONS,
        )
    ]


@pytest.mark.anyio
async def test_execute_uses_custom_instructions_and_empty_history_by_default() -> None:
    runner = FakeAgentRunner(Ok(AgentReply(content="ok")))
    handler = SendMessageHandler(runner, instructions="関西弁で答えてください。")

    result = await handler.execute(SendMessageRequest(user_input="hello"))

    assert is_ok(result)
    assert result.unwrap().history == (
        ChatMessage(role="user", content="hello"),
        ChatMessage(role="assistant", content="ok"),
    )
    assert runner.runs == [
        RecordedRun(
            user_input="hello",
            history=(),
            instructions="関西弁で答えてください。",
        )
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("user_input", ["", "   ", "\n\t "])
async def test_execute_rejects_empty_input(user_input: str) -> None:
    runner = FakeAgentRunner(Ok(AgentReply(content="ok")))
    handler = SendMessageHandler(runner)

    result = await handler.execute(SendMessageRequest(user_input=user_input))

    error = assert_err(
        result,
        expected_type=SendMessageErrorType.EMPTY_INPUT,
        message_contains="empty",
        display_message_contains="メッセージを入力してください。",
    )
    assert isinstance(error, SendMessageError)
    assert isinstance(error, AppError)
    assert isinstance(error, TypedError)
    assert isinstance(error, DisplayableError)
    assert not isinstance(error, Exception)
    assert error.display_message == "メッセージを入力してください。"
    assert runner.runs == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("runner_error_type", "expected_type", "expected_display_message"),
    [
        pytest.param(
            AgentRunnerErrorType.API_ERROR,
            SendMessageErrorType.API_ERROR,
            "APIとの通信でエラーが発生しました。しばらくしてから再度お試しください。",
            id="api-error",
        ),
        pytest.param(
            AgentRunnerErrorType.UNEXPECTED_BEHAVIOR,
            SendMessageErrorType.UNEXPECTED_BEHAVIOR,
            "モデルから予期しない応答が返されました。",
            id="unexpected-behavior",
        ),
        pytest.param(
            AgentRunnerErrorType.USAGE_LIMIT_EXCEEDED,
            SendMessageErrorType.USAGE_LIMIT_EXCEEDED,
            "利用上限に達しました。しばらくしてから再度お試しください。",
            id="usage-limit-exceeded",
        ),
        pytest.param(
            AgentRunnerErrorType.UNEXPECTED,
            SendMessageErrorType.UNEXPECTED,
            "予期しないエラーが発生しました。",
            id="unexpected",
        ),
    ],
)
async def test_maps_runner_errors(
    runner_error_type: AgentRunnerErrorType,
    expected_type: SendMessageErrorType,
    expected_display_message: str,
) -> None:
    runner_error = AgentRunnerError(
        type=runner_error_type,
        message="runner detail",
    )
    runner = FakeAgentRunner(Err(runner_error))
    handler = SendMessageHandler(runner)

    result = await handler.execute(SendMessageRequest(user_input="hello"))

    error = assert_err(
        result,
        expected_type=expected_type,
        message_contains="runner detail",
        display_message_contains=expected_display_message,
    )
    assert isinstance(error, SendMessageError)
    assert isinstance(error, AppError)
    assert isinstance(error, TypedError)
    assert isinstance(error, DisplayableError)
    assert not isinstance(error, Exception)
    assert error.message == "runner detail"
    assert error.type is expected_type
    assert error.display_message == expected_display_message
