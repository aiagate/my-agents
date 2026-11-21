import json
from collections.abc import Callable
from pathlib import Path

import pytest
from flow_res import is_ok
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from my_agents.composition import build_cli, run_cli
from my_agents.contracts.ports.logger import NOOP_LOGGER
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
        argv=(),
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

    exit_code = run_cli(argv=(), env={}, env_path=tmp_path / ".env")

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
        argv=(),
        env={"MAGPIE_MODEL": "test-model"},
        env_path=tmp_path / ".env",
        write_error=errors.append,
    )

    assert exit_code == 1
    assert len(errors) == 1
    assert errors[0].startswith("Error: ")
    assert "MAGPIE_BASE_URL" in errors[0]


def test_run_cli_reads_sys_argv_when_argv_is_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "my_agents.composition.sys.argv",
        ["my-agents", "--help"],
    )
    outputs: list[str] = []
    errors: list[str] = []

    exit_code = run_cli(
        env={},
        env_path=tmp_path / ".env",
        write_output=outputs.append,
        write_error=errors.append,
    )

    assert exit_code == 0
    assert errors == []
    assert len(outputs) == 1
    assert "--debug" in outputs[0]
    assert "--log-file" in outputs[0]


def test_run_cli_returns_exit_2_on_invalid_arguments(tmp_path: Path) -> None:
    outputs: list[str] = []
    errors: list[str] = []

    exit_code = run_cli(
        argv=("--invalid-option",),
        env=_VALID_ENV,
        env_path=tmp_path / ".env",
        write_output=outputs.append,
        write_error=errors.append,
    )

    assert exit_code == 2
    assert outputs == []
    assert len(errors) == 1
    assert "--invalid-option" in errors[0]


def test_run_cli_debug_and_explicit_log_file_record_runtime_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def failing_respond(
        _messages: list[ModelMessage],
        _info: AgentInfo,
    ) -> ModelResponse:
        raise ModelAPIError("test", "upstream 502 error")

    class FailingStubRunner:
        @classmethod
        def from_settings(cls, settings: AppSettings) -> PydanticAIAgentRunner:
            return PydanticAIAgentRunner(
                FunctionModel(failing_respond),
                api_key=settings.api_key,
            )

    monkeypatch.setattr(
        "my_agents.composition.PydanticAIAgentRunner", FailingStubRunner
    )
    log_path = tmp_path / "logs" / "session.jsonl"
    outputs: list[str] = []
    errors: list[str] = []

    exit_code = run_cli(
        argv=("--debug", "--log-file", str(log_path)),
        env=_VALID_ENV,
        env_path=tmp_path / ".env",
        read_input=_read_inputs(["質問", "exit"]),
        write_output=outputs.append,
        write_error=errors.append,
    )

    assert exit_code == 0
    assert errors[0] == f"Debug: log file = {log_path}"
    assert errors[1].startswith("\nError: ")
    assert "Debug [API_ERROR]: ModelAPIError: upstream 502 error" in errors[2]

    records = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["level"] == "ERROR"
    assert records[0]["event"] == "send_message_failed"
    assert records[0]["error_type"] == "API_ERROR"
    assert "upstream 502 error" in records[0]["message"]


def test_run_cli_debug_with_injected_logger_skips_log_path_banner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    _patch_runner(monkeypatch, captured, "ok")
    outputs: list[str] = []
    errors: list[str] = []

    exit_code = run_cli(
        argv=("--debug",),
        env=_VALID_ENV,
        env_path=tmp_path / ".env",
        read_input=_read_inputs(["hi", "exit"]),
        write_output=outputs.append,
        write_error=errors.append,
        logger=NOOP_LOGGER,
    )

    assert exit_code == 0
    assert errors == []
    assert "\nAI > ok" in outputs


def test_run_cli_logs_settings_error_to_log_file(tmp_path: Path) -> None:
    log_path = tmp_path / "settings.jsonl"
    errors: list[str] = []

    exit_code = run_cli(
        argv=("--log-file", str(log_path)),
        env={"MAGPIE_MODEL": "test-model"},
        env_path=tmp_path / ".env",
        write_error=errors.append,
    )

    assert exit_code == 1
    assert errors[0].startswith("Error: ")
    records = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["event"] == "settings_load_failed"
    assert "MAGPIE_API_KEY" in records[0]["message"]


def test_run_cli_fails_when_explicit_log_file_cannot_be_opened(
    tmp_path: Path,
) -> None:
    errors: list[str] = []

    exit_code = run_cli(
        argv=("--log-file", str(tmp_path)),
        env=_VALID_ENV,
        env_path=tmp_path / ".env",
        write_error=errors.append,
    )

    assert exit_code == 1
    assert len(errors) == 1
    assert errors[0].startswith("Error: Failed to open log file")


def test_run_cli_warns_and_continues_when_default_log_path_unwritable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    _patch_runner(monkeypatch, captured, "続行できた")
    blocked_state_home = tmp_path / "blocked-state-home"
    blocked_state_home.write_text("not a directory", encoding="utf-8")
    outputs: list[str] = []
    errors: list[str] = []

    exit_code = run_cli(
        argv=(),
        env={
            **_VALID_ENV,
            "XDG_STATE_HOME": str(blocked_state_home),
        },
        env_path=tmp_path / ".env",
        read_input=_read_inputs(["質問", "exit"]),
        write_output=outputs.append,
        write_error=errors.append,
    )

    assert exit_code == 0
    assert len(errors) == 1
    assert errors[0].startswith("Warning: Failed to open log file")
    assert "\nAI > 続行できた" in outputs
