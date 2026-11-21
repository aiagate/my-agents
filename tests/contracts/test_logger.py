from dataclasses import FrozenInstanceError

import pytest

from my_agents.contracts.ports.logger import (
    NOOP_LOGGER,
    IAppLogger,
    LogEntry,
    LogLevel,
    NoopLogger,
)


def test_log_entry_holds_fields_and_is_frozen_and_hashable() -> None:
    entry = LogEntry(
        level=LogLevel.ERROR,
        event="send_message_failed",
        message="RuntimeError: boom",
        error_type="UNEXPECTED",
        detail="traceback",
    )

    assert entry.level is LogLevel.ERROR
    assert entry.event == "send_message_failed"
    assert entry.message == "RuntimeError: boom"
    assert entry.error_type == "UNEXPECTED"
    assert entry.detail == "traceback"
    assert hash(entry) == hash(
        LogEntry(
            level=LogLevel.ERROR,
            event="send_message_failed",
            message="RuntimeError: boom",
            error_type="UNEXPECTED",
            detail="traceback",
        )
    )

    field_name = "message"
    with pytest.raises(FrozenInstanceError):
        setattr(entry, field_name, "changed")


def test_log_entry_optional_fields_default_to_none() -> None:
    entry = LogEntry(
        level=LogLevel.INFO,
        event="started",
        message="ok",
    )

    assert entry.error_type is None
    assert entry.detail is None
    assert entry == LogEntry(
        level=LogLevel.INFO,
        event="started",
        message="ok",
        error_type=None,
        detail=None,
    )


def test_noop_logger_satisfies_port_and_accepts_entries() -> None:
    logger = NoopLogger()
    entry = LogEntry(level=LogLevel.DEBUG, event="test", message="hello")

    assert [level.name for level in LogLevel] == ["DEBUG", "INFO", "WARNING", "ERROR"]
    assert isinstance(logger, IAppLogger)
    assert isinstance(NOOP_LOGGER, NoopLogger)
    assert logger.log(entry) is None
    assert NOOP_LOGGER.log(entry) is None
    assert entry == LogEntry(level=LogLevel.DEBUG, event="test", message="hello")
