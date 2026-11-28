import io
import json
import sys
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


class _FlakyStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next = True

    def write(self, s: str) -> int:
        if self.fail_next:
            self.fail_next = False
            raise OSError("disk full")
        return super().write(s)


def test_noop_logger_accepts_entries() -> None:
    NOOP_LOGGER.log(LogEntry(LogLevel.INFO, "event"))
    assert LogLevel.ERROR.value == "error"


def test_jsonl_logger_writes_structured_lines_and_permissions(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "agent.jsonl"
    logger = JsonlLogger.open(path)
    logger.log(
        LogEntry(
            LogLevel.INFO,
            "informed",
            "message",
            "TYPE",
            "detail",
            duration_ms=12.5,
            conversation_id="conv-1",
            model="gpt-4o-mini",
            input_tokens=10,
            output_tokens=20,
            cache_read_tokens=5,
        )
    )
    logger.log(LogEntry(LogLevel.ERROR, "plain"))
    logger.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert first["level"] == "info"
    assert (
        first["event"],
        first["error_type"],
        first["detail"],
        first["duration_ms"],
        first["conversation_id"],
        first["model"],
        first["input_tokens"],
        first["output_tokens"],
        first["cache_read_tokens"],
    ) == ("informed", "TYPE", "detail", 12.5, "conv-1", "gpt-4o-mini", 10, 20, 5)
    second = json.loads(lines[1])
    for key in ("error_type", "detail", "duration_ms", "conversation_id", "model"):
        assert key not in second
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_jsonl_logger_falls_back_to_stderr_and_recovers(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    path = tmp_path / "agent.jsonl"
    logger = JsonlLogger.open(path, redactions=(SecretStr("s3cr3t"),))
    logger.close()
    logger.log(LogEntry(LogLevel.INFO, "first", "leak s3cr3t"))
    logger.log(LogEntry(LogLevel.INFO, "second"))
    assert path.read_text(encoding="utf-8") == ""
    err_lines = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [entry["event"] for entry in err_lines] == ["first", "second"]
    assert err_lines[0]["message"] == "leak ***"

    flaky_stream = _FlakyStream()
    flaky_logger = JsonlLogger(flaky_stream, redactions=(SecretStr("s3cr3t"),))
    flaky_logger.log(LogEntry(LogLevel.ERROR, "transient", "bad s3cr3t"))
    flaky_logger.log(LogEntry(LogLevel.INFO, "recovered", "ok"))
    flaky_err = json.loads(capsys.readouterr().err.strip())
    assert flaky_err["event"] == "transient"
    assert flaky_err["message"] == "bad ***"
    recovered = json.loads(flaky_stream.getvalue().strip())
    assert recovered["event"] == "recovered"
    assert recovered["message"] == "ok"

    broken_stderr = io.StringIO()
    broken_stderr.close()
    monkeypatch.setattr(sys, "stderr", broken_stderr)
    logger.log(LogEntry(LogLevel.ERROR, "both_failed", "ignored"))


def test_jsonl_logger_redacts_message_error_type_and_detail(tmp_path: Path) -> None:
    path = tmp_path / "agent.jsonl"
    logger = JsonlLogger.open(
        path,
        redactions=(
            SecretStr("message-secret"),
            SecretStr("type-secret"),
            SecretStr("conv-secret"),
            SecretStr("model-secret"),
        ),
    )
    logger.log(
        LogEntry(
            LogLevel.ERROR,
            "redacted",
            "message-secret at https://user:pw@host/x",
            "type-secret",
            "detail-secret is not configured for redaction",
            conversation_id="conv-secret",
            model="model-secret",
        )
    )
    logger.close()
    entry = json.loads(path.read_text(encoding="utf-8"))
    assert entry["message"] == "*** at https://***@host/x"
    assert entry["error_type"] == "***"
    assert entry["detail"] == "detail-secret is not configured for redaction"
    assert entry["conversation_id"] == "***"
    assert entry["model"] == "***"


def test_write_stdout_and_stderr_flush_lines(capsys) -> None:
    write_stdout("hello")
    write_stderr("problem")

    captured = capsys.readouterr()
    assert captured.out == "hello\n"
    assert captured.err == "problem\n"
