from pathlib import Path
from unittest.mock import patch

from flow_res import Err, Ok

from my_agents.core.types import AppError, AppErrorKind, TurnOutcome
from my_agents.shell.cli import main

_ENV = {
    "LLM_MODEL": "gpt-4o-mini",
    "LLM_API_KEY": "sk-test-key",
    "DATA_DIR": "/tmp/my-agents-cli-test",
}


def _inputs(values: list[str]):
    def read(_prompt: str) -> str:
        if not values:
            raise EOFError
        return values.pop(0)

    return read


def test_help_and_invalid_arguments(capsys) -> None:
    assert main(["--help"]) == 0
    assert main(["--unknown"]) == 2
    assert main([], env={"SERVER_PORT": "bad"}) == 1
    assert "SERVER_PORT" in capsys.readouterr().err


def test_settings_and_persona_validation_errors(tmp_path: Path) -> None:
    errors: list[str] = []
    assert (
        main(
            [],
            env={"SERVER_PORT": "bad", "DATA_DIR": str(tmp_path)},
            write_error=errors.append,
        )
        == 1
    )
    assert any("SERVER_PORT" in line for line in errors)

    errors.clear()
    assert (
        main(
            [],
            env={"DATA_DIR": str(tmp_path), "LLM_API_KEY": "", "LLM_MODEL": ""},
            write_error=errors.append,
        )
        == 1
    )
    assert any("LLM_API_KEY" in line for line in errors)

    errors.clear()
    assert (
        main(
            [],
            env={
                **_ENV,
                "DATA_DIR": str(tmp_path),
                "PERSONA_PATH": str(tmp_path / "missing.json"),
            },
            write_error=errors.append,
        )
        == 1
    )
    assert any("Cannot read persona" in line for line in errors)


def test_logging_paths_and_debug_output(tmp_path: Path) -> None:
    errors: list[str] = []
    assert (
        main(
            ["--log-file", str(tmp_path)],
            env={**_ENV, "DATA_DIR": str(tmp_path)},
            write_error=errors.append,
        )
        == 1
    )
    assert any("cannot open log file" in line for line in errors)

    explicit_log = tmp_path / "explicit.jsonl"
    errors.clear()
    with patch("my_agents.shell.cli._repl", return_value=0):
        assert (
            main(
                ["--log-file", str(explicit_log), "--debug"],
                env={**_ENV, "DATA_DIR": str(tmp_path)},
                write_error=errors.append,
            )
            == 0
        )
    assert any(f"Debug: log file = {explicit_log}" in line for line in errors)
    assert "cli_exit" in explicit_log.read_text(encoding="utf-8")

    env_log = tmp_path / "env.jsonl"
    errors.clear()
    with patch("my_agents.shell.cli._repl", return_value=0):
        assert (
            main(
                ["--debug"],
                env={**_ENV, "DATA_DIR": str(tmp_path), "LOG_PATH": str(env_log)},
                write_error=errors.append,
            )
            == 0
        )
    assert any(f"Debug: log file = {env_log}" in line for line in errors)
    assert env_log.exists()

    errors.clear()
    with patch("my_agents.shell.cli._repl", return_value=0):
        assert (
            main(
                ["--debug"],
                env={**_ENV, "DATA_DIR": str(tmp_path), "LOG_PATH": str(tmp_path)},
                write_error=errors.append,
            )
            == 0
        )
    assert any("Warning: cannot open log file" in line for line in errors)
    assert any("Debug: log file =" in line for line in errors)

    errors.clear()
    with patch("my_agents.shell.cli._repl", return_value=0):
        assert (
            main(
                ["--debug"],
                env={**_ENV, "DATA_DIR": str(tmp_path)},
                write_error=errors.append,
            )
            == 0
        )
    assert any("Debug: log file = <none>" in line for line in errors)


def test_repl_interaction_errors_and_serve_command(tmp_path: Path) -> None:
    class FakeEngine:
        def __init__(self, *, fail_start: bool = False) -> None:
            self.fail_start = fail_start
            self.closed = False

        async def start(self):
            if self.fail_start:
                return Err(AppError(AppErrorKind.LLM, "mcp start failed"))
            return Ok(None)

        async def submit_message(self, conversation_id: str, text: str):
            assert conversation_id == "default"
            if not text.strip():
                return Err(
                    AppError(AppErrorKind.VALIDATION, "Message must not be empty.")
                )
            return Ok(TurnOutcome("default", f"echo:{text}", 1, (), ()))

        async def aclose(self) -> None:
            self.closed = True

    outputs: list[str] = []
    errors: list[str] = []
    failing_engine = FakeEngine(fail_start=True)
    with patch("my_agents.shell.cli._engine", return_value=failing_engine):
        assert (
            main(
                [],
                env={**_ENV, "DATA_DIR": str(tmp_path)},
                read_input=_inputs(["hi"]),
                write_output=outputs.append,
                write_error=errors.append,
            )
            == 1
        )
    assert failing_engine.closed is True
    assert any("Error: mcp start failed" in line for line in errors)

    outputs.clear()
    working_engine = FakeEngine()
    with patch("my_agents.shell.cli._engine", return_value=working_engine):
        assert (
            main(
                [],
                env={**_ENV, "DATA_DIR": str(tmp_path)},
                read_input=_inputs(["   ", "hello", "exit"]),
                write_output=outputs.append,
            )
            == 0
        )
    assert working_engine.closed is True
    assert any("Error: Message must not be empty." in line for line in outputs)
    assert any("AI > echo:hello" in line for line in outputs)

    eof_engine = FakeEngine()
    with patch("my_agents.shell.cli._engine", return_value=eof_engine):
        assert (
            main(
                [],
                env={**_ENV, "DATA_DIR": str(tmp_path)},
                read_input=_inputs([]),
                write_output=outputs.append,
            )
            == 0
        )
    assert eof_engine.closed is True

    outputs.clear()
    captured: list[tuple[object, str, int]] = []
    with patch(
        "my_agents.shell.cli.serve",
        side_effect=lambda engine, host, port: captured.append((engine, host, port)),
    ):
        assert (
            main(
                ["serve"],
                env={
                    **_ENV,
                    "DATA_DIR": str(tmp_path),
                    "SERVER_HOST": "0.0.0.0",
                    "SERVER_PORT": "9001",
                },
                write_output=outputs.append,
            )
            == 0
        )
    assert captured[0][1:] == ("0.0.0.0", 9001)
    assert any("http://0.0.0.0:9001" in line for line in outputs)
