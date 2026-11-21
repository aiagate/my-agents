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
from my_agents.domain.context_assembler import assemble_instructions
from my_agents.domain.core_profile import CoreProfile
from my_agents.usecases.get_core_profile import (
    GetCoreProfileError,
    GetCoreProfileErrorType,
    GetCoreProfileResponse,
)
from my_agents.usecases.send_message import (
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


class FakeGetCoreProfile:
    def __init__(
        self,
        result: Result[GetCoreProfileResponse, GetCoreProfileError],
    ) -> None:
        self.result = result
        self.calls = 0

    def execute(self) -> Result[GetCoreProfileResponse, GetCoreProfileError]:
        self.calls += 1
        return self.result


def _profile_response(profile: CoreProfile) -> GetCoreProfileResponse:
    return GetCoreProfileResponse(
        profile=profile,
        location="memory/core.md",
        max_chars=4096,
    )


@pytest.mark.anyio
async def test_execute_returns_reply_and_appends_history() -> None:
    profile = CoreProfile(content="## 価値観\n- 誠実さ")
    runner = FakeAgentRunner(
        Ok(
            AgentReply(
                content="こんにちは",
                core_memory_updates=("価値観を記録",),
            )
        )
    )
    get_core_profile = FakeGetCoreProfile(Ok(_profile_response(profile)))
    handler = SendMessageHandler(runner, get_core_profile)
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
        core_memory_updates=("価値観を記録",),
    )
    assert hash(request) == hash(
        SendMessageRequest(user_input="  今日は？  ", history=(previous,))
    )
    assert get_core_profile.calls == 1
    assert runner.runs == [
        RecordedRun(
            user_input="今日は？",
            history=(previous,),
            instructions=assemble_instructions(profile),
        )
    ]


@pytest.mark.anyio
async def test_execute_uses_empty_profile_without_core_memory_updates() -> None:
    runner = FakeAgentRunner(Ok(AgentReply(content="ok")))
    get_core_profile = FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty())))
    handler = SendMessageHandler(runner, get_core_profile)

    result = await handler.execute(SendMessageRequest(user_input="hello"))

    assert is_ok(result)
    assert result.unwrap() == SendMessageResponse(
        reply="ok",
        history=(
            ChatMessage(role="user", content="hello"),
            ChatMessage(role="assistant", content="ok"),
        ),
        core_memory_updates=(),
    )
    instructions = runner.runs[0].instructions
    assert instructions == assemble_instructions(CoreProfile.empty())
    assert "まだ何も記録されていません。" in instructions


@pytest.mark.anyio
@pytest.mark.parametrize("user_input", ["", "   ", "\n\t "])
async def test_execute_rejects_empty_input(user_input: str) -> None:
    runner = FakeAgentRunner(Ok(AgentReply(content="ok")))
    get_core_profile = FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty())))
    handler = SendMessageHandler(runner, get_core_profile)

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
    assert error.detail is None
    assert get_core_profile.calls == 0
    assert runner.runs == []


@pytest.mark.anyio
async def test_execute_maps_core_profile_error() -> None:
    profile_error = GetCoreProfileError(
        type=GetCoreProfileErrorType.INVALID_PROFILE,
        message="profile too large",
        display_message="Core Memory の内容が不正です。",
    )
    runner = FakeAgentRunner(Ok(AgentReply(content="ok")))
    get_core_profile = FakeGetCoreProfile(Err(profile_error))
    handler = SendMessageHandler(runner, get_core_profile)

    result = await handler.execute(SendMessageRequest(user_input="hello"))

    error = assert_err(
        result,
        expected_type=SendMessageErrorType.CORE_PROFILE_ERROR,
        message_contains="profile too large",
        display_message_contains="Core Memory の内容が不正です。",
    )
    assert isinstance(error, SendMessageError)
    assert isinstance(error, AppError)
    assert isinstance(error, TypedError)
    assert isinstance(error, DisplayableError)
    assert error.message == "profile too large"
    assert error.display_message == "Core Memory の内容が不正です。"
    assert error.detail is None
    assert get_core_profile.calls == 1
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
        detail="traceback detail",
    )
    runner = FakeAgentRunner(Err(runner_error))
    get_core_profile = FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty())))
    handler = SendMessageHandler(runner, get_core_profile)

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
    assert error.detail == "traceback detail"
