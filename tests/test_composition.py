from collections.abc import Callable
from pathlib import Path

import pytest
from flow_res import is_ok
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from my_agents.composition import build_cli, run_cli
from my_agents.contracts.secret import SecretStr
from my_agents.infrastructure.pydantic_ai_runner import PydanticAIAgentRunner
from my_agents.infrastructure.settings import AppSettings, SettingsError
from my_agents.usecases.send_message import DEFAULT_INSTRUCTIONS
from tests.helpers import assert_err

_VALID_ENV = {
    "MAGPIE_MODEL": "test-model",
    "MAGPIE_BASE_URL": "https://example.com/v1",
    "MAGPIE_API_KEY": "secret",
}


def _patch_runner(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, object],
    reply: str,
) -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["instructions"] = info.instructions
        last_message = messages[-1]
        assert isinstance(last_message, ModelRequest)
        last_part = last_message.parts[0]
        assert isinstance(last_part, UserPromptPart)
        captured["prompt"] = last_part.content
        return ModelResponse([TextPart(reply)])

    class StubRunner:
        @classmethod
        def from_settings(cls, settings: AppSettings) -> PydanticAIAgentRunner:
            captured["model_name"] = settings.model_name
            captured["base_url"] = settings.base_url
            captured["api_key"] = settings.api_key
            return PydanticAIAgentRunner(FunctionModel(respond))

    monkeypatch.setattr("my_agents.composition.PydanticAIAgentRunner", StubRunner)


def _read_inputs(inputs: list[str]) -> Callable[[str], str]:
    def read_input(_prompt: str) -> str:
        return inputs.pop(0)

    return read_input


def test_build_cli_reports_settings_error(tmp_path: Path) -> None:
    result = build_cli(
        env={"MAGPIE_MODEL": "test-model"},
        env_path=tmp_path / ".env",
    )

    error = assert_err(result, message_contains="MAGPIE_API_KEY")
    assert isinstance(error, SettingsError)
    assert "MAGPIE_BASE_URL" in error.message


def test_build_cli_wires_runner_into_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    _patch_runner(monkeypatch, captured, "配線済み")
    outputs: list[str] = []

    result = build_cli(
        env=_VALID_ENV,
        env_path=tmp_path / ".env",
        read_input=_read_inputs(["こんにちは", "exit"]),
        write_output=outputs.append,
    )

    assert is_ok(result)
    result.unwrap().run()
    assert captured["model_name"] == "test-model"
    assert captured["base_url"] == "https://example.com/v1"
    assert captured["api_key"] == SecretStr("secret")
    assert captured["instructions"] == DEFAULT_INSTRUCTIONS
    assert captured["prompt"] == "こんにちは"
    assert "\nAI > 配線済み" in outputs


def test_run_cli_returns_zero_and_uses_default_env_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "MAGPIE_MODEL=file-model\n"
        "MAGPIE_BASE_URL=https://file.example.com/v1\n"
        "MAGPIE_API_KEY=file-secret\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}
    _patch_runner(monkeypatch, captured, "応答")
    outputs: list[str] = []

    exit_code = run_cli(
        env={},
        read_input=_read_inputs(["質問", "exit"]),
        write_output=outputs.append,
    )

    assert exit_code == 0
    assert captured["model_name"] == "file-model"
    assert captured["base_url"] == "https://file.example.com/v1"
    assert captured["api_key"] == SecretStr("file-secret")
    assert captured["instructions"] == DEFAULT_INSTRUCTIONS
    assert captured["prompt"] == "質問"
    assert "\nAI > 応答" in outputs


def test_run_cli_reports_settings_error_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = run_cli(env={}, env_path=tmp_path / ".env")

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.startswith("Error: ")
    assert "MAGPIE_MODEL" in captured.err


def test_run_cli_uses_injected_error_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    errors: list[str] = []

    exit_code = run_cli(
        env={"MAGPIE_MODEL": "test-model"},
        env_path=tmp_path / ".env",
        write_error=errors.append,
    )

    assert exit_code == 1
    assert len(errors) == 1
    assert errors[0].startswith("Error: ")
    assert "MAGPIE_BASE_URL" in errors[0]
