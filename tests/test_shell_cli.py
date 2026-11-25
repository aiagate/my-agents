import json
from pathlib import Path
from unittest.mock import patch

from my_agents.shell.cli import main

_ENV = {"CHAT_BACKEND": "local", "MEMORY_PATH": "/tmp/my-agents-cli-test"}


def _inputs(values: list[str]):
    def read(_prompt: str) -> str:
        if not values:
            raise EOFError
        return values.pop(0)

    return read


def test_help_returns_zero() -> None:
    assert main(["--help"]) == 0


def test_invalid_arguments_return_two() -> None:
    assert main(["--unknown"]) == 2


def test_repl_chats_and_exits(tmp_path: Path) -> None:
    outputs: list[str] = []
    code = main(
        [],
        env={**_ENV, "MEMORY_PATH": str(tmp_path)},
        read_input=_inputs(["hello", "exit"]),
        write_output=outputs.append,
    )
    assert code == 0
    assert any("AI > ローカル応答: hello" in line for line in outputs)


def test_repl_reports_empty_message_and_eof(tmp_path: Path) -> None:
    outputs: list[str] = []
    code = main(
        [],
        env={**_ENV, "MEMORY_PATH": str(tmp_path)},
        read_input=_inputs(["   ", "hello"]),
        write_output=outputs.append,
    )
    assert code == 0
    assert any("Error: Message must not be empty" in line for line in outputs)
    assert any("AI >" in line for line in outputs)


def test_repl_handles_eof_immediately(tmp_path: Path) -> None:
    outputs: list[str] = []
    code = main(
        [],
        env={**_ENV, "MEMORY_PATH": str(tmp_path)},
        read_input=_inputs([]),
        write_output=outputs.append,
    )
    assert code == 0
    assert outputs[0].startswith("my-agents local chat")


def test_log_file_is_written(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    code = main(
        ["--log-file", str(log)],
        env={**_ENV, "MEMORY_PATH": str(tmp_path)},
        read_input=_inputs(["exit"]),
        write_output=lambda _line: None,
    )
    assert code == 0
    assert "cli_exit" in log.read_text(encoding="utf-8")


def test_unwritable_explicit_log_file_exits_one(tmp_path: Path) -> None:
    errors: list[str] = []
    code = main(
        ["--log-file", str(tmp_path)],
        env={**_ENV, "MEMORY_PATH": str(tmp_path)},
        read_input=_inputs(["exit"]),
        write_output=lambda _line: None,
        write_error=errors.append,
    )
    assert code == 1
    assert any("Error: cannot open log file" in line for line in errors)


def test_unwritable_env_log_file_warns_and_continues(tmp_path: Path) -> None:
    errors: list[str] = []
    code = main(
        [],
        env={**_ENV, "MEMORY_PATH": str(tmp_path), "LOG_FILE": str(tmp_path)},
        read_input=_inputs(["exit"]),
        write_output=lambda _line: None,
        write_error=errors.append,
    )
    assert code == 0
    assert any("Warning: cannot open log file" in line for line in errors)


def test_debug_prints_the_resolved_log_path(tmp_path: Path) -> None:
    errors: list[str] = []
    code = main(
        ["--debug", "--log-file", str(tmp_path / "agent.jsonl")],
        env={**_ENV, "MEMORY_PATH": str(tmp_path)},
        read_input=_inputs(["exit"]),
        write_output=lambda _line: None,
        write_error=errors.append,
    )
    assert code == 0
    assert any(line.startswith("Debug: log file = ") for line in errors)


def test_persona_error_returns_one(tmp_path: Path) -> None:
    errors: list[str] = []
    code = main(
        [],
        env={
            **_ENV,
            "MEMORY_PATH": str(tmp_path),
            "PERSONA_PATH": str(tmp_path / "missing.json"),
        },
        read_input=_inputs(["exit"]),
        write_output=lambda _line: None,
        write_error=errors.append,
    )
    assert code == 1
    assert "Cannot read persona" in errors[0]


def test_openai_without_key_returns_one(tmp_path: Path) -> None:
    errors: list[str] = []
    code = main(
        [],
        env={"CHAT_BACKEND": "openai", "MEMORY_PATH": str(tmp_path)},
        read_input=_inputs(["exit"]),
        write_output=lambda _line: None,
        write_error=errors.append,
    )
    assert code == 1
    assert "MAGPIE_API_KEY" in errors[0]


def test_serve_delegates_to_server(tmp_path: Path) -> None:
    outputs: list[str] = []
    with patch("my_agents.shell.cli.serve") as serve:
        code = main(
            ["serve"],
            env={
                **_ENV,
                "MEMORY_PATH": str(tmp_path),
                "SERVER_HOST": "127.0.0.1",
                "SERVER_PORT": "9999",
            },
            write_output=outputs.append,
        )
    assert code == 0
    serve.assert_called_once()
    assert serve.call_args.args[1:] == ("127.0.0.1", 9999)
    assert "127.0.0.1:9999" in outputs[0]


def test_invalid_settings_use_default_error_writer(capsys) -> None:
    assert main([], env={"CHAT_BACKEND": "bad"}) == 1
    assert "Error:" in capsys.readouterr().err


def test_repl_exits_one_when_llm_start_fails(tmp_path: Path) -> None:
    errors: list[str] = []
    outputs: list[str] = []
    read_calls: list[str] = []

    def read(prompt: str) -> str:
        read_calls.append(prompt)
        raise AssertionError("REPL must not prompt after a startup failure")

    code = main(
        [],
        env={
            **_ENV,
            "MEMORY_PATH": str(tmp_path),
            "MCP_SERVERS_JSON": json.dumps(
                {"broken": {"command": str(tmp_path / "missing-mcp")}}
            ),
        },
        read_input=read,
        write_output=outputs.append,
        write_error=errors.append,
    )
    assert code == 1
    assert read_calls == []
    assert outputs == []
    assert any("MCP startup failed" in line for line in errors)
