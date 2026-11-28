import json
from pathlib import Path

from pydantic import SecretStr

from my_agents.shell.logger import (
    NOOP_LOGGER,
    JsonlLogger,
    LogEntry,
    LogLevel,
    write_stderr,
    write_stdout,
)


def test_noop_logger_accepts_entries() -> None:
    NOOP_LOGGER.log(LogEntry(LogLevel.INFO, "event"))
    assert LogLevel.ERROR.value == "error"


def test_jsonl_logger_writes_structured_lines_and_permissions(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "agent.jsonl"
    logger = JsonlLogger.open(path)
    logger.log(LogEntry(LogLevel.INFO, "informed", "message", "TYPE", "detail"))
    logger.log(LogEntry(LogLevel.ERROR, "plain"))
    logger.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert first["level"] == "info"
    assert (first["event"], first["error_type"], first["detail"]) == (
        "informed",
        "TYPE",
        "detail",
    )
    assert "error_type" not in json.loads(lines[1])
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_jsonl_logger_stops_after_a_write_failure(tmp_path: Path) -> None:
    logger = JsonlLogger.open(tmp_path / "agent.jsonl")
    logger.close()
    logger.log(LogEntry(LogLevel.INFO, "first"))
    logger.log(LogEntry(LogLevel.INFO, "second"))
    assert (tmp_path / "agent.jsonl").read_text(encoding="utf-8") == ""


def test_jsonl_logger_redacts_message_error_type_and_detail(tmp_path: Path) -> None:
    path = tmp_path / "agent.jsonl"
    logger = JsonlLogger.open(
        path,
        redactions=(SecretStr("message-secret"), SecretStr("type-secret")),
    )
    logger.log(
        LogEntry(
            LogLevel.ERROR,
            "redacted",
            "message-secret at https://user:pw@host/x",
            "type-secret",
            "detail-secret is not configured for redaction",
        )
    )
    logger.close()
    entry = json.loads(path.read_text(encoding="utf-8"))
    assert entry["message"] == "*** at https://***@host/x"
    assert entry["error_type"] == "***"
    assert entry["detail"] == "detail-secret is not configured for redaction"


def test_write_stdout_and_stderr_flush_lines(capsys) -> None:
    write_stdout("hello")
    write_stderr("problem")

    captured = capsys.readouterr()
    assert captured.out == "hello\n"
    assert captured.err == "problem\n"
