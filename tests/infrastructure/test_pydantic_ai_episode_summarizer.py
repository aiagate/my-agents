from pathlib import Path

import pytest
from pydantic_ai import UsageLimits
from pydantic_ai.exceptions import (
    ModelAPIError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from my_agents.contracts.messages import ChatMessage
from my_agents.contracts.ports.episode_summarizer import (
    EpisodeSummarizerErrorType,
    EpisodeSummaryOutcome,
    EpisodeSummaryRequest,
)
from my_agents.contracts.secret import SecretStr
from my_agents.infrastructure.pydantic_ai_episode_summarizer import (
    PydanticAIEpisodeSummarizer,
)
from my_agents.infrastructure.settings import AppSettings
from tests.helpers import assert_err

_REQUEST = EpisodeSummaryRequest(
    "以前の題名",
    "既存の決定",
    (ChatMessage("user", "質問"), ChatMessage("assistant", "回答")),
)


@pytest.mark.anyio
async def test_summary_prompt_and_structured_output() -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert "重要な情報を残してください" in (info.instructions or "")
        message = messages[-1]
        assert isinstance(message, ModelRequest)
        part = message.parts[0]
        assert isinstance(part, UserPromptPart)
        assert (
            part.content
            == "既存のタイトル: 以前の題名\n\n既存の要約:\n既存の決定\n\n新しい会話:\nuser: 質問\n\nassistant: 回答"
        )
        tool = info.output_tools[0]
        assert set(tool.parameters_json_schema["properties"]) == {
            "title",
            "overview",
            "summary",
        }
        return ModelResponse(
            [
                ToolCallPart(
                    tool.name,
                    {"title": "題名", "overview": "概要", "summary": "- 要点"},
                )
            ]
        )

    model = FunctionModel(respond)
    summarizer = PydanticAIEpisodeSummarizer(model)
    assert (await summarizer.summarize(_REQUEST)).unwrap() == EpisodeSummaryOutcome(
        "題名", "概要", "- 要点"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (ModelAPIError("test", "raw-credential"), EpisodeSummarizerErrorType.API_ERROR),
        (
            UnexpectedModelBehavior("raw-credential"),
            EpisodeSummarizerErrorType.UNEXPECTED_BEHAVIOR,
        ),
        (
            UsageLimitExceeded("raw-credential"),
            EpisodeSummarizerErrorType.UNEXPECTED_BEHAVIOR,
        ),
        (RuntimeError("raw-credential"), EpisodeSummarizerErrorType.UNEXPECTED),
    ],
)
async def test_summary_failures_redact_messages_and_details(
    failure: Exception, kind: EpisodeSummarizerErrorType
) -> None:
    def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise failure

    summarizer = PydanticAIEpisodeSummarizer(
        FunctionModel(respond), api_key=SecretStr("raw-credential")
    )
    error = assert_err(
        await summarizer.summarize(_REQUEST),
        expected_type=kind,
        message_contains=f"{type(failure).__name__}: ***",
    )
    assert "raw-credential" not in error.message
    if kind == EpisodeSummarizerErrorType.UNEXPECTED:
        assert error.detail is not None
        assert "RuntimeError: ***" in error.detail
        assert "raw-credential" not in error.detail
    else:
        assert error.detail is None


@pytest.mark.anyio
async def test_summary_factory_applies_settings_and_usage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def provider(**options: object) -> str:
        captured.update(options)
        return "fake provider"

    def model(model_name: str, *, provider: str) -> FunctionModel:
        captured.update(model_name=model_name, provider=provider)

        def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            pytest.fail("Request budget should prevent model calls")

        return FunctionModel(respond)

    monkeypatch.setattr(
        "my_agents.infrastructure.pydantic_ai_episode_summarizer.OpenAIProvider",
        provider,
    )
    monkeypatch.setattr(
        "my_agents.infrastructure.pydantic_ai_episode_summarizer.OpenAIChatModel", model
    )
    settings = AppSettings(
        "chat",
        "https://chat.example",
        SecretStr("chat-credential"),
        tmp_path,
        summary_model_name="summary",
        summary_base_url="https://summary.example",
        summary_api_key=SecretStr("summary-credential"),
    )
    summarizer = PydanticAIEpisodeSummarizer.from_settings(
        settings, usage_limits=UsageLimits(request_limit=0)
    )
    assert captured == {
        "model_name": "summary",
        "base_url": "https://summary.example",
        "api_key": "summary-credential",
        "provider": "fake provider",
    }
    assert_err(
        await summarizer.summarize(_REQUEST),
        expected_type=EpisodeSummarizerErrorType.UNEXPECTED_BEHAVIOR,
        message_contains="request_limit",
    )


@pytest.mark.anyio
async def test_summary_rejects_whitespace_only_output() -> None:
    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            [
                ToolCallPart(
                    info.output_tools[0].name,
                    {"title": "title", "overview": "overview", "summary": " \n\t"},
                )
            ]
        )

    summarizer = PydanticAIEpisodeSummarizer(FunctionModel(respond))
    assert_err(
        await summarizer.summarize(_REQUEST),
        expected_type=EpisodeSummarizerErrorType.UNEXPECTED_BEHAVIOR,
        message_contains="Episode summarizer returned an empty summary.",
    )
