from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pydantic_ai
import pytest
from flow_res import Err, Ok, Result, is_ok
from pydantic_ai import Agent, UsageLimits
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
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ObjectJsonSchema

from my_agents.contracts.messages import AgentReply, ChatMessage
from my_agents.contracts.ports.agent_runner import (
    AgentRunnerError,
    AgentRunnerErrorType,
)
from my_agents.contracts.ports.association_expander import (
    AssociationExpandError,
    AssociationExpandErrorType,
    AssociationExpandOutcome,
    AssociationExpandRequest,
    IAssociationExpander,
)
from my_agents.contracts.ports.core_profile_updater import (
    CoreMemoryUpdateCommand,
    CoreMemoryUpdateError,
    CoreMemoryUpdateErrorType,
    CoreMemoryUpdateOutcome,
    ICoreProfileUpdater,
)
from my_agents.contracts.secret import SecretStr
from my_agents.infrastructure.pydantic_ai_runner import (
    DEFAULT_REQUEST_LIMIT,
    DEFAULT_TOOL_CALLS_LIMIT,
    DEFAULT_USAGE_LIMITS,
    PydanticAIAgentRunner,
)
from my_agents.infrastructure.settings import AppSettings
from tests.helpers import assert_err


def _raising_function_model(exc: Exception) -> FunctionModel:
    def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise exc

    return FunctionModel(respond)


class FakeUpdater(ICoreProfileUpdater):
    def __init__(
        self,
        result: Result[CoreMemoryUpdateOutcome, CoreMemoryUpdateError],
    ) -> None:
        self._result = result
        self.commands: list[CoreMemoryUpdateCommand] = []

    def update(
        self,
        command: CoreMemoryUpdateCommand,
    ) -> Result[CoreMemoryUpdateOutcome, CoreMemoryUpdateError]:
        self.commands.append(command)
        return self._result


class SequenceUpdater(ICoreProfileUpdater):
    def __init__(
        self,
        results: Sequence[Result[CoreMemoryUpdateOutcome, CoreMemoryUpdateError]],
    ) -> None:
        self._results = results
        self.commands: list[CoreMemoryUpdateCommand] = []

    def update(
        self,
        command: CoreMemoryUpdateCommand,
    ) -> Result[CoreMemoryUpdateOutcome, CoreMemoryUpdateError]:
        self.commands.append(command)
        return self._results[len(self.commands) - 1]


class FakeExpander(IAssociationExpander):
    def __init__(
        self,
        result: Result[AssociationExpandOutcome, AssociationExpandError],
    ) -> None:
        self._result = result
        self.requests: list[AssociationExpandRequest] = []

    async def expand(
        self,
        request: AssociationExpandRequest,
    ) -> Result[AssociationExpandOutcome, AssociationExpandError]:
        self.requests.append(request)
        return self._result


def _association_calling_model(
    captured: dict[str, object],
    schemas: list[ObjectJsonSchema] | None = None,
) -> FunctionModel:
    calls = 0

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            captured["tools"] = [
                (tool.name, tool.description) for tool in info.function_tools
            ]
            if schemas is not None:
                schemas.append(info.function_tools[0].parameters_json_schema)
            return ModelResponse(
                [
                    ToolCallPart(
                        "expand_associations",
                        {"topic": "コースター", "context": "1LDK・水滴対策"},
                    )
                ]
            )
        for message in messages:
            for part in message.parts:
                if isinstance(part, ToolReturnPart):
                    captured["tool_result"] = part.content
        return ModelResponse([TextPart("done")])

    return FunctionModel(respond)


def _tool_calling_model(
    args: dict[str, str],
    captured: dict[str, object],
    schemas: list[ObjectJsonSchema] | None = None,
) -> FunctionModel:
    calls = 0

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            captured["tools"] = [
                (tool.name, tool.description) for tool in info.function_tools
            ]
            if schemas is not None:
                schemas.append(info.function_tools[0].parameters_json_schema)
            return ModelResponse([ToolCallPart("update_core_memory", args)])
        for message in messages:
            for part in message.parts:
                if isinstance(part, ToolReturnPart):
                    captured["tool_result"] = part.content
        return ModelResponse([TextPart("done")])

    return FunctionModel(respond)


@pytest.mark.anyio
async def test_returns_agent_reply() -> None:
    runner = PydanticAIAgentRunner(TestModel(custom_output_text="こんにちは"))

    result = await runner.run("hello", history=[], instructions="")

    assert is_ok(result)
    assert result.unwrap() == AgentReply(content="こんにちは")
    assert result.unwrap().core_memory_updates == ()


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
    ("exc", "expected_type", "expected_message", "expect_detail"),
    [
        pytest.param(
            ModelAPIError("test", "API is unavailable"),
            AgentRunnerErrorType.API_ERROR,
            "ModelAPIError: API is unavailable",
            False,
            id="api-error",
        ),
        pytest.param(
            UnexpectedModelBehavior("unexpected model output"),
            AgentRunnerErrorType.UNEXPECTED_BEHAVIOR,
            "UnexpectedModelBehavior: unexpected model output",
            False,
            id="unexpected-behavior",
        ),
        pytest.param(
            UsageLimitExceeded("usage limit exceeded"),
            AgentRunnerErrorType.USAGE_LIMIT_EXCEEDED,
            "UsageLimitExceeded: usage limit exceeded",
            False,
            id="usage-limit-exceeded",
        ),
        pytest.param(
            RuntimeError("boom"),
            AgentRunnerErrorType.UNEXPECTED,
            "RuntimeError: boom",
            True,
            id="unexpected-exception",
        ),
        pytest.param(
            TimeoutError(),
            AgentRunnerErrorType.UNEXPECTED,
            "TimeoutError",
            True,
            id="empty-message-exception",
        ),
    ],
)
async def test_maps_model_errors_to_port_errors(
    exc: Exception,
    expected_type: AgentRunnerErrorType,
    expected_message: str,
    expect_detail: bool,
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
    if expect_detail:
        assert error.detail is not None
        assert "Traceback" in error.detail
        assert type(exc).__name__ in error.detail
    else:
        assert error.detail is None


@pytest.mark.anyio
async def test_redacts_api_key_in_error_message_and_detail() -> None:
    secret_key = SecretStr("sk-super-secret-value")
    runner = PydanticAIAgentRunner(
        _raising_function_model(
            RuntimeError("upstream failure using key sk-super-secret-value")
        ),
        api_key=secret_key,
    )

    result = await runner.run("hello", history=[], instructions="")

    error = assert_err(
        result,
        expected_type=AgentRunnerErrorType.UNEXPECTED,
        message_contains="RuntimeError: upstream failure using key ***",
    )
    assert "sk-super-secret-value" not in error.message
    assert error.detail is not None
    assert "sk-super-secret-value" not in error.detail
    assert "***" in error.detail


@pytest.mark.anyio
async def test_empty_secret_does_not_corrupt_error_message() -> None:
    runner = PydanticAIAgentRunner(
        _raising_function_model(ModelAPIError("test", "plain failure")),
        api_key=SecretStr(""),
    )

    result = await runner.run("hello", history=[], instructions="")

    error = assert_err(
        result,
        expected_type=AgentRunnerErrorType.API_ERROR,
        message_contains="ModelAPIError: plain failure",
    )
    assert "***" not in error.message


def test_default_usage_limits() -> None:
    assert DEFAULT_REQUEST_LIMIT == 6
    assert DEFAULT_TOOL_CALLS_LIMIT == 4
    assert DEFAULT_USAGE_LIMITS.request_limit == 6
    assert DEFAULT_USAGE_LIMITS.tool_calls_limit == 4


@pytest.mark.anyio
async def test_core_memory_tool_updates_and_records_reason() -> None:
    captured: dict[str, object] = {}
    schemas: list[ObjectJsonSchema] = []
    updater = FakeUpdater(
        Ok(
            CoreMemoryUpdateOutcome(
                char_count=42,
                max_chars=4096,
                reason="価値観を記録",
            )
        )
    )
    runner = PydanticAIAgentRunner(
        _tool_calling_model(
            {
                "action": "append",
                "reason": "価値観を記録",
                "content": "## 価値観\n- 誠実さ",
            },
            captured,
            schemas,
        ),
        core_profile_updater=updater,
    )

    result = await runner.run("覚えて", history=[], instructions="")

    assert is_ok(result)
    assert result.unwrap() == AgentReply(
        content="done",
        core_memory_updates=("価値観を記録",),
    )
    assert updater.commands == [
        CoreMemoryUpdateCommand(
            action="append",
            reason="価値観を記録",
            content="## 価値観\n- 誠実さ",
            old_text="",
        )
    ]
    assert captured["tools"] == [
        (
            "update_core_memory",
            (
                "Update the persistent Core Memory when the user shares a durable "
                "value, decision axis, constraint, active theme, or preferred "
                "dialogue style. Do not use it for transient one-off topics."
            ),
        )
    ]
    assert len(schemas) == 1
    schema = schemas[0]
    assert schema["type"] == "object"
    properties = schema["properties"]
    assert properties["action"]["enum"] == ["append", "replace", "rewrite"]
    for name in ("action", "reason", "content", "old_text"):
        description = properties[name]["description"]
        assert isinstance(description, str)
        assert description.strip()
    required = schema["required"]
    assert sorted(required) == ["action", "reason"]
    assert captured["tool_result"] == (
        "Core Memory updated (42/4096 chars): 価値観を記録"
    )


@pytest.mark.anyio
async def test_core_memory_tool_reports_error_to_model() -> None:
    captured: dict[str, object] = {}
    error = CoreMemoryUpdateError(
        type=CoreMemoryUpdateErrorType.TARGET_NOT_FOUND,
        message="target missing",
    )
    updater = FakeUpdater(Err(error))
    runner = PydanticAIAgentRunner(
        _tool_calling_model(
            {
                "action": "replace",
                "reason": "修正",
                "content": "new",
                "old_text": "missing",
            },
            captured,
        ),
        core_profile_updater=updater,
    )

    result = await runner.run("直して", history=[], instructions="")

    assert is_ok(result)
    assert result.unwrap() == AgentReply(content="done", core_memory_updates=())
    assert updater.commands == [
        CoreMemoryUpdateCommand(
            action="replace",
            reason="修正",
            content="new",
            old_text="missing",
        )
    ]
    assert captured["tool_result"] == (
        "Core Memory update target was not found: target missing Use replace only "
        "with an exact snippet that exists in the current Core Memory, or use "
        "append for new content."
    )
    assert "exact snippet" in str(captured["tool_result"])


@pytest.mark.anyio
async def test_core_memory_tool_retries_and_records_only_success() -> None:
    updater = SequenceUpdater(
        [
            Err(
                CoreMemoryUpdateError(
                    type=CoreMemoryUpdateErrorType.MAX_LENGTH_EXCEEDED,
                    message="too long",
                )
            ),
            Ok(
                CoreMemoryUpdateOutcome(
                    char_count=20,
                    max_chars=4096,
                    reason="再構成",
                )
            ),
        ]
    )
    calls = 0

    def respond(
        _messages: list[ModelMessage],
        _info: AgentInfo,
    ) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                [
                    ToolCallPart(
                        "update_core_memory",
                        {
                            "action": "append",
                            "reason": "too long",
                            "content": "x" * 5000,
                        },
                    )
                ]
            )
        if calls == 2:
            return ModelResponse(
                [
                    ToolCallPart(
                        "update_core_memory",
                        {
                            "action": "rewrite",
                            "reason": "再構成",
                            "content": "## 価値観\n- 誠実さ",
                        },
                    )
                ]
            )
        return ModelResponse([TextPart("done")])

    runner = PydanticAIAgentRunner(
        FunctionModel(respond),
        core_profile_updater=updater,
    )

    result = await runner.run("整理して", history=[], instructions="")

    assert is_ok(result)
    assert result.unwrap() == AgentReply(
        content="done",
        core_memory_updates=("再構成",),
    )
    assert [command.action for command in updater.commands] == ["append", "rewrite"]
    assert updater.commands[1].reason == "再構成"


@dataclass(frozen=True, slots=True)
class _FakeRunResult:
    output: str


def _install_recording_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    recorded: dict[str, object] = {}

    class RecordingAgent:
        def __init__(self, _model: object) -> None:
            self._model = _model

        async def run(
            self,
            _user_input: str,
            **kwargs: object,
        ) -> _FakeRunResult:
            recorded["usage_limits"] = kwargs["usage_limits"]
            recorded["toolsets"] = kwargs.get("toolsets")
            return _FakeRunResult("ok")

    monkeypatch.setattr(
        "my_agents.infrastructure.pydantic_ai_runner.Agent",
        RecordingAgent,
    )
    return recorded


@pytest.mark.anyio
async def test_forwards_default_usage_limits_to_agent_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _install_recording_agent(monkeypatch)
    runner = PydanticAIAgentRunner(TestModel())

    result = await runner.run("hello", history=[], instructions="")

    assert is_ok(result)
    assert result.unwrap() == AgentReply(content="ok")
    assert recorded["usage_limits"] is DEFAULT_USAGE_LIMITS
    assert recorded["toolsets"] is None


@pytest.mark.anyio
async def test_forwards_custom_usage_limits_from_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _install_recording_agent(monkeypatch)
    limits = UsageLimits(request_limit=9, tool_calls_limit=2)
    runner = PydanticAIAgentRunner(TestModel(), usage_limits=limits)

    result = await runner.run("hello", history=[], instructions="")

    assert is_ok(result)
    assert recorded["usage_limits"] is limits


@pytest.mark.anyio
async def test_from_settings_forwards_usage_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _install_recording_agent(monkeypatch)

    def fake_provider(**_kwargs: object) -> object:
        return object()

    def fake_chat_model(*_args: object, **_kwargs: object) -> TestModel:
        return TestModel()

    monkeypatch.setattr(
        "my_agents.infrastructure.pydantic_ai_runner.OpenAIProvider",
        fake_provider,
    )
    monkeypatch.setattr(
        "my_agents.infrastructure.pydantic_ai_runner.OpenAIChatModel",
        fake_chat_model,
    )
    limits = UsageLimits(request_limit=7, tool_calls_limit=4)
    settings = AppSettings(
        model_name="test-model",
        base_url="https://example.com/v1",
        api_key=SecretStr("secret"),
        memory_dir=Path("/tmp/memory"),
    )

    runner = PydanticAIAgentRunner.from_settings(settings, usage_limits=limits)
    result = await runner.run("hello", history=[], instructions="")

    assert is_ok(result)
    assert recorded["usage_limits"] is limits


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            CoreMemoryUpdateError(
                type=CoreMemoryUpdateErrorType.INVALID_INPUT,
                message="reason required",
            ),
            "Invalid Core Memory update: reason required Provide a non-empty "
            "reason and the required content or old_text.",
            id="invalid-input",
        ),
        pytest.param(
            CoreMemoryUpdateError(
                type=CoreMemoryUpdateErrorType.TARGET_NOT_FOUND,
                message="missing",
            ),
            "Core Memory update target was not found: missing Use replace only "
            "with an exact snippet that exists in the current Core Memory, or "
            "use append for new content.",
            id="target-not-found",
        ),
        pytest.param(
            CoreMemoryUpdateError(
                type=CoreMemoryUpdateErrorType.AMBIGUOUS_TARGET,
                message="multiple",
            ),
            "Core Memory update target is ambiguous: multiple Include more "
            "surrounding lines in old_text so it matches exactly once, then retry.",
            id="ambiguous-target",
        ),
        pytest.param(
            CoreMemoryUpdateError(
                type=CoreMemoryUpdateErrorType.MAX_LENGTH_EXCEEDED,
                message="too long",
            ),
            "Core Memory update exceeds the limit: too long Condense the content "
            "and retry with rewrite or replace so the profile stays within the "
            "character limit.",
            id="max-length-exceeded",
        ),
        pytest.param(
            CoreMemoryUpdateError(
                type=CoreMemoryUpdateErrorType.STORAGE_ERROR,
                message="disk full",
            ),
            "Core Memory could not be persisted: disk full Check the memory "
            "directory and retry the update.",
            id="storage-error",
        ),
    ],
)
async def test_core_memory_tool_formats_error_for_model(
    error: CoreMemoryUpdateError,
    expected: str,
) -> None:
    captured: dict[str, object] = {}
    updater = FakeUpdater(Err(error))
    runner = PydanticAIAgentRunner(
        _tool_calling_model(
            {
                "action": "append",
                "reason": "記録",
                "content": "content",
            },
            captured,
        ),
        core_profile_updater=updater,
    )

    result = await runner.run("hello", history=[], instructions="")

    assert is_ok(result)
    assert result.unwrap() == AgentReply(content="done", core_memory_updates=())
    assert captured["tool_result"] == expected


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
        memory_dir=Path("/tmp/memory"),
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
async def test_from_settings_forwards_core_profile_updater(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    updater = FakeUpdater(
        Ok(
            CoreMemoryUpdateOutcome(
                char_count=3,
                max_chars=4096,
                reason="wired update",
            )
        )
    )

    def fake_provider(*, base_url: str, api_key: str) -> object:
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        return object()

    def fake_model(model_name: str, *, provider: object) -> FunctionModel:
        captured["model_name"] = model_name
        captured["provider"] = provider
        return _tool_calling_model(
            {
                "action": "append",
                "reason": "wired update",
                "content": "abc",
            },
            captured,
        )

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
        memory_dir=Path("/tmp/memory"),
    )

    runner = PydanticAIAgentRunner.from_settings(
        settings,
        core_profile_updater=updater,
        usage_limits=UsageLimits(request_limit=6, tool_calls_limit=5),
    )
    result = await runner.run("hello", history=[], instructions="")

    assert is_ok(result)
    assert result.unwrap() == AgentReply(
        content="done",
        core_memory_updates=("wired update",),
    )
    assert updater.commands == [
        CoreMemoryUpdateCommand(
            action="append",
            reason="wired update",
            content="abc",
        )
    ]
    assert captured["model_name"] == "test-model"


@pytest.mark.anyio
async def test_accepts_agent_instance() -> None:
    agent: Agent[None, str] = Agent(TestModel(custom_output_text="こんにちは"))

    runner = PydanticAIAgentRunner(agent)

    result = await runner.run("hello", history=[], instructions="")

    assert is_ok(result)
    assert result.unwrap() == AgentReply(content="こんにちは")


_ASSOCIATION_TOOL_DESCRIPTION = (
    "Compute a 3-level cross-category association tree in embedding space "
    "for a consultation, shopping, lifestyle, planning, or brainstorming "
    "topic."
)


@pytest.mark.anyio
async def test_association_tool_returns_tree_and_lateral_ideas_to_model() -> None:
    captured: dict[str, object] = {}
    schemas: list[ObjectJsonSchema] = []
    expander = FakeExpander(
        Ok(
            AssociationExpandOutcome(
                root="コースター",
                tree_summary=(
                    "root: コースター\n"
                    "- L1 コルク [素材・テクスチャ] (0.842)\n"
                    "  - L2 ゴム板 [ホームセンター資材・DIY] (0.731)"
                ),
                lateral_ideas=(
                    "コルク（素材・テクスチャ: 軽くて弾力がある）",
                    "ゴム板（ホームセンター資材・DIY: 滑り止めに使える）",
                ),
            )
        )
    )
    runner = PydanticAIAgentRunner(
        _association_calling_model(captured, schemas),
        association_expander=expander,
    )

    result = await runner.run("コースターが欲しい", history=[], instructions="")

    assert is_ok(result)
    assert result.unwrap() == AgentReply(content="done")
    assert expander.requests == [
        AssociationExpandRequest(
            topic="コースター",
            context="1LDK・水滴対策",
        )
    ]
    assert captured["tools"] == [("expand_associations", _ASSOCIATION_TOOL_DESCRIPTION)]
    assert captured["tool_result"] == (
        "3-level Magical Banana association tree for コースター:\n"
        "root: コースター\n"
        "- L1 コルク [素材・テクスチャ] (0.842)\n"
        "  - L2 ゴム板 [ホームセンター資材・DIY] (0.731)\n"
        "\n"
        "Lateral ideas:\n"
        "- コルク（素材・テクスチャ: 軽くて弾力がある）\n"
        "- ゴム板（ホームセンター資材・DIY: 滑り止めに使える）"
    )
    assert len(schemas) == 1
    schema = schemas[0]
    assert schema["type"] == "object"
    properties = schema["properties"]
    for name in ("topic", "context"):
        description = properties[name]["description"]
        assert isinstance(description, str)
        assert description.strip()
    assert schema["required"] == ["topic"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            AssociationExpandError(
                type=AssociationExpandErrorType.INVALID_INPUT,
                message="topic required",
            ),
            "Association expansion input was invalid: topic required "
            "Provide a non-empty topic and retry.",
            id="invalid-input",
        ),
        pytest.param(
            AssociationExpandError(
                type=AssociationExpandErrorType.EMBEDDING_ERROR,
                message="upstream timeout",
            ),
            "Association expansion failed while computing embeddings: "
            "upstream timeout Answer without this tool instead.",
            id="embedding-error",
        ),
    ],
)
async def test_association_tool_formats_error_for_model(
    error: AssociationExpandError,
    expected: str,
) -> None:
    captured: dict[str, object] = {}
    runner = PydanticAIAgentRunner(
        _association_calling_model(captured),
        association_expander=FakeExpander(Err(error)),
    )

    result = await runner.run("hello", history=[], instructions="")

    assert is_ok(result)
    assert result.unwrap() == AgentReply(content="done")
    assert captured["tool_result"] == expected


@pytest.mark.anyio
async def test_from_settings_forwards_association_expander(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _install_recording_agent(monkeypatch)
    expander = FakeExpander(
        Ok(
            AssociationExpandOutcome(
                root="root",
                tree_summary="root: root",
                lateral_ideas=(),
            )
        )
    )

    def fake_provider(**_kwargs: object) -> object:
        return object()

    def fake_chat_model(*_args: object, **_kwargs: object) -> TestModel:
        return TestModel()

    monkeypatch.setattr(
        "my_agents.infrastructure.pydantic_ai_runner.OpenAIProvider",
        fake_provider,
    )
    monkeypatch.setattr(
        "my_agents.infrastructure.pydantic_ai_runner.OpenAIChatModel",
        fake_chat_model,
    )
    settings = AppSettings(
        model_name="test-model",
        base_url="https://example.com/v1",
        api_key=SecretStr("secret"),
        memory_dir=Path("/tmp/memory"),
    )

    runner = PydanticAIAgentRunner.from_settings(
        settings,
        association_expander=expander,
    )
    result = await runner.run("hello", history=[], instructions="")

    assert is_ok(result)
    assert result.unwrap() == AgentReply(content="ok")
    assert recorded["toolsets"] is not None


@pytest.mark.anyio
async def test_registers_both_toolsets_when_both_collaborators_exist() -> None:
    updater = FakeUpdater(
        Ok(
            CoreMemoryUpdateOutcome(
                char_count=1,
                max_chars=4096,
                reason="reason",
            )
        )
    )
    expander = FakeExpander(
        Ok(
            AssociationExpandOutcome(
                root="root",
                tree_summary="root: root",
                lateral_ideas=(),
            )
        )
    )

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert [tool.name for tool in info.function_tools] == [
            "update_core_memory",
            "expand_associations",
        ]
        return ModelResponse([TextPart("ok")])

    runner = PydanticAIAgentRunner(
        FunctionModel(respond),
        core_profile_updater=updater,
        association_expander=expander,
    )

    result = await runner.run("hello", history=[], instructions="")

    assert is_ok(result)
    assert result.unwrap() == AgentReply(content="ok")
