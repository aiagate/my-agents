import pydantic_ai
import pytest
from flow_res import is_ok
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
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from my_agents.contracts.messages import AgentReply, ChatMessage
from my_agents.contracts.ports.agent_runner import (
    AgentRunnerError,
    AgentRunnerErrorType,
)
from my_agents.contracts.secret import SecretStr
from my_agents.infrastructure.pydantic_ai_runner import PydanticAIAgentRunner
from my_agents.infrastructure.settings import AppSettings
from tests.helpers import assert_err


def _raising_function_model(exc: Exception) -> FunctionModel:
    def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise exc

    return FunctionModel(respond)


@pytest.mark.anyio
async def test_returns_agent_reply() -> None:
    runner = PydanticAIAgentRunner(TestModel(custom_output_text="こんにちは"))

    result = await runner.run("hello", history=[], instructions="")

    assert is_ok(result)
    assert result.unwrap() == AgentReply(content="こんにちは")


@pytest.mark.anyio
async def test_converts_history_to_model_messages() -> None:
    captured: list[ModelMessage] = []

    def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        captured.extend(messages)
        return ModelResponse([TextPart("ok")])

    runner = PydanticAIAgentRunner(FunctionModel(respond))
    history = [
        ChatMessage(role="user", content="前回の質問"),
        ChatMessage(role="assistant", content="前回の回答"),
    ]

    result = await runner.run("今回の質問", history=history, instructions="")

    assert is_ok(result)
    assert len(captured) == 3

    assert isinstance(captured[0], ModelRequest)
    assert len(captured[0].parts) == 1
    user_history_part = captured[0].parts[0]
    assert isinstance(user_history_part, UserPromptPart)
    assert user_history_part.content == "前回の質問"

    assert isinstance(captured[1], ModelResponse)
    assert len(captured[1].parts) == 1
    assistant_history_part = captured[1].parts[0]
    assert isinstance(assistant_history_part, TextPart)
    assert assistant_history_part.content == "前回の回答"

    assert isinstance(captured[2], ModelRequest)
    assert len(captured[2].parts) == 1
    user_prompt_part = captured[2].parts[0]
    assert isinstance(user_prompt_part, UserPromptPart)
    assert user_prompt_part.content == "今回の質問"


@pytest.mark.anyio
async def test_propagates_instructions() -> None:
    captured: str | None = None

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal captured
        captured = info.instructions
        return ModelResponse([TextPart("ok")])

    runner = PydanticAIAgentRunner(FunctionModel(respond))

    result = await runner.run(
        "hello",
        history=[],
        instructions="簡潔に答えてください。",
    )

    assert is_ok(result)
    assert captured == "簡潔に答えてください。"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("exc", "expected_type", "expected_message"),
    [
        pytest.param(
            ModelAPIError("test", "API is unavailable"),
            AgentRunnerErrorType.API_ERROR,
            "API is unavailable",
            id="api-error",
        ),
        pytest.param(
            UnexpectedModelBehavior("unexpected model output"),
            AgentRunnerErrorType.UNEXPECTED_BEHAVIOR,
            "unexpected model output",
            id="unexpected-behavior",
        ),
        pytest.param(
            UsageLimitExceeded("usage limit exceeded"),
            AgentRunnerErrorType.USAGE_LIMIT_EXCEEDED,
            "usage limit exceeded",
            id="usage-limit-exceeded",
        ),
        pytest.param(
            RuntimeError("boom"),
            AgentRunnerErrorType.UNEXPECTED,
            "boom",
            id="unexpected-exception",
        ),
    ],
)
async def test_maps_model_errors_to_port_errors(
    exc: Exception,
    expected_type: AgentRunnerErrorType,
    expected_message: str,
) -> None:
    runner = PydanticAIAgentRunner(_raising_function_model(exc))

    result = await runner.run("hello", history=[], instructions="")

    error = assert_err(
        result,
        expected_type=expected_type,
        message_contains=expected_message,
    )
    assert isinstance(error, AgentRunnerError)
    assert not isinstance(error, Exception)


@pytest.mark.anyio
async def test_from_settings_builds_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    sentinel_provider = object()
    monkeypatch.setattr(pydantic_ai, "BANNER_ENABLED", True)

    def fake_provider(*, base_url: str, api_key: str) -> object:
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        return sentinel_provider

    def fake_model(model_name: str, *, provider: object) -> TestModel:
        captured["model_name"] = model_name
        captured["provider"] = provider
        return TestModel(custom_output_text="wired")

    monkeypatch.setattr(
        "my_agents.infrastructure.pydantic_ai_runner.OpenAIProvider",
        fake_provider,
    )
    monkeypatch.setattr(
        "my_agents.infrastructure.pydantic_ai_runner.OpenAIChatModel",
        fake_model,
    )

    settings = AppSettings(
        model_name="test-model",
        base_url="https://example.com/v1",
        api_key=SecretStr("secret"),
    )

    runner = PydanticAIAgentRunner.from_settings(settings)
    result = await runner.run("hello", history=[], instructions="")

    assert isinstance(runner, PydanticAIAgentRunner)
    assert captured["model_name"] == "test-model"
    assert captured["base_url"] == "https://example.com/v1"
    assert captured["api_key"] == "secret"
    assert captured["provider"] is sentinel_provider
    assert pydantic_ai.BANNER_ENABLED is False
    assert is_ok(result)
    assert result.unwrap() == AgentReply(content="wired")


@pytest.mark.anyio
async def test_accepts_agent_instance() -> None:
    agent: Agent[None, str] = Agent(TestModel(custom_output_text="こんにちは"))

    runner = PydanticAIAgentRunner(agent)

    result = await runner.run("hello", history=[], instructions="")

    assert is_ok(result)
    assert result.unwrap() == AgentReply(content="こんにちは")
