import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from flow_res import is_ok

from my_agents.contracts.ports.logger import LogEntry, LogLevel
from my_agents.infrastructure.file_logger import (
    JsonlFileLogger,
    LogFileError,
    LogFileResolution,
    resolve_log_path,
)
from tests.helpers import assert_err


def test_resolve_log_path_prefers_cli_option_over_env(tmp_path: Path) -> None:
    cli_path = tmp_path / "cli.jsonl"

    resolution = resolve_log_path(
        cli_log_file=cli_path,
        env={
            "MY_AGENTS_LOG_FILE": str(tmp_path / "env.jsonl"),
            "XDG_STATE_HOME": str(tmp_path / "xdg"),
            "HOME": str(tmp_path / "home"),
        },
    )

    assert resolution == LogFileResolution(path=cli_path, explicit=True)


def test_resolve_log_path_expands_user_on_explicit_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_home = tmp_path / "custom-home"
    monkeypatch.setenv("HOME", str(fake_home))

    cli_res = resolve_log_path(cli_log_file=Path("~/logs/app.jsonl"), env={})
    env_res = resolve_log_path(env={"MY_AGENTS_LOG_FILE": "  ~/logs/env.jsonl  "})

    assert cli_res == LogFileResolution(
        path=fake_home / "logs" / "app.jsonl",
        explicit=True,
    )
    assert env_res == LogFileResolution(
        path=fake_home / "logs" / "env.jsonl",
        explicit=True,
    )


def test_resolve_log_path_falls_through_xdg_and_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    xdg_res = resolve_log_path(
        env={
            "MY_AGENTS_LOG_FILE": "   ",
            "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
            "HOME": str(tmp_path / "ignored-home"),
        }
    )
    home_res = resolve_log_path(
        env={
            "XDG_STATE_HOME": "  ",
            "HOME": str(tmp_path / "user-home"),
        }
    )
    fallback_home = tmp_path / "fallback-home"
    monkeypatch.setenv("HOME", str(fallback_home))
    empty_env_res = resolve_log_path(env={"HOME": ""})

    assert xdg_res == LogFileResolution(
        path=tmp_path / "xdg-state" / "my-agents" / "my-agents.jsonl",
        explicit=False,
    )
    assert home_res == LogFileResolution(
        path=tmp_path
        / "user-home"
        / ".local"
        / "state"
        / "my-agents"
        / "my-agents.jsonl",
        explicit=False,
    )
    assert empty_env_res == LogFileResolution(
        path=fallback_home / ".local" / "state" / "my-agents" / "my-agents.jsonl",
        explicit=False,
    )


def test_resolve_log_path_reads_process_environ_when_env_is_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_log = tmp_path / "from-environ.jsonl"
    monkeypatch.setenv("MY_AGENTS_LOG_FILE", str(env_log))

    resolution = resolve_log_path()

    assert resolution == LogFileResolution(path=env_log, explicit=True)


def test_jsonl_file_logger_writes_structured_records_with_0600_mode(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "nested" / "my-agents.jsonl"
    warnings: list[str] = []

    open_result = JsonlFileLogger.open(log_path, on_write_error=warnings.append)
    assert is_ok(open_result)

    with open_result.unwrap() as logger:
        logger.log(
            LogEntry(
                level=LogLevel.ERROR,
                event="settings_load_failed",
                message="設定不足",
            )
        )
        logger.log(
            LogEntry(
                level=LogLevel.ERROR,
                event="send_message_failed",
                message="RuntimeError: boom",
                error_type="UNEXPECTED",
                detail="Traceback line 1\nTraceback line 2",
            )
        )

    assert warnings == []
    assert (log_path.stat().st_mode & 0o777) == 0o600

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["level"] == "ERROR"
    assert first["event"] == "settings_load_failed"
    assert first["message"] == "設定不足"
    assert "error_type" not in first
    assert "detail" not in first
    parsed_ts = datetime.fromisoformat(first["timestamp"])
    assert parsed_ts.tzinfo == UTC

    second = json.loads(lines[1])
    assert second["level"] == "ERROR"
    assert second["event"] == "send_message_failed"
    assert second["error_type"] == "UNEXPECTED"
    assert second["message"] == "RuntimeError: boom"
    assert second["detail"] == "Traceback line 1\nTraceback line 2"


def test_jsonl_file_logger_rotates_and_preserves_0600_permissions(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "rotate.jsonl"
    warnings: list[str] = []

    open_result = JsonlFileLogger.open(
        log_path,
        on_write_error=warnings.append,
        max_bytes=120,
        backup_count=2,
    )
    assert is_ok(open_result)

    with open_result.unwrap() as logger:
        for idx in range(4):
            logger.log(
                LogEntry(
                    level=LogLevel.ERROR,
                    event="send_message_failed",
                    error_type="API_ERROR",
                    message=f"error payload number {idx} " + ("x" * 60),
                )
            )

    rotated = tmp_path / "rotate.jsonl.1"
    assert warnings == []
    assert rotated.exists()
    assert (log_path.stat().st_mode & 0o777) == 0o600
    assert (rotated.stat().st_mode & 0o777) == 0o600


def test_jsonl_file_logger_open_returns_err_when_path_is_directory(
    tmp_path: Path,
) -> None:
    warnings: list[str] = []

    result = JsonlFileLogger.open(tmp_path, on_write_error=warnings.append)

    error = assert_err(result, message_contains=str(tmp_path))
    assert isinstance(error, LogFileError)
    assert warnings == []


def test_jsonl_file_logger_reports_write_error_once_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "failing.jsonl"
    warnings: list[str] = []

    def fail_rollover(_self: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(
        "my_agents.infrastructure.file_logger._PrivateRotatingFileHandler.doRollover",
        fail_rollover,
    )

    open_result = JsonlFileLogger.open(
        log_path,
        on_write_error=warnings.append,
        max_bytes=10,
        backup_count=1,
    )
    assert is_ok(open_result)

    with open_result.unwrap() as logger:
        logger.log(
            LogEntry(
                level=LogLevel.ERROR,
                event="send_message_failed",
                message="first error populates file",
            )
        )
        logger.log(
            LogEntry(
                level=LogLevel.ERROR,
                event="send_message_failed",
                message="second error triggers rollover failure",
            )
        )
        logger.log(
            LogEntry(
                level=LogLevel.ERROR,
                event="send_message_failed",
                message="third error also fails without duplicate warning",
            )
        )

    assert len(warnings) == 1
    assert warnings[0].startswith("Warning: Failed to write log file")
    assert "disk full" in warnings[0]
