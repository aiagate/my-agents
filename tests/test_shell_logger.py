import json
from pathlib import Path

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
    assert LogLevel.DEBUG.value == "debug"


def test_jsonl_logger_writes_structured_lines_and_permissions(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "agent.jsonl"
    logger = JsonlLogger.open(path)
    logger.log(LogEntry(LogLevel.WARNING, "warned", "message", "TYPE", "detail"))
    logger.log(LogEntry(LogLevel.ERROR, "plain"))
    logger.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert first["level"] == "warning"
    assert (first["event"], first["error_type"], first["detail"]) == (
        "warned",
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


def test_write_stdout_and_stderr_flush_lines(capsys) -> None:
    write_stdout("hello")
    write_stderr("problem")

    captured = capsys.readouterr()
    assert captured.out == "hello\n"
    assert captured.err == "problem\n"
