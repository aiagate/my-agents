import json
import os
import sys
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TextIO

from my_agents.core.types import SecretStr, redact_secrets


class LogLevel(StrEnum):
    INFO = "info"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class LogEntry:
    level: LogLevel
    event: str
    message: str = ""
    error_type: str | None = None
    detail: str | None = None
    duration_ms: float | None = None
    conversation_id: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None


class IAppLogger(Protocol):
    def log(self, entry: LogEntry, /) -> None: ...

    def close(self) -> None: ...


class _NoopLogger:
    def log(self, _entry: LogEntry, /) -> None:
        return

    def close(self) -> None:
        return


NOOP_LOGGER: IAppLogger = _NoopLogger()


def write_stdout(message: str) -> None:
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def write_stderr(message: str) -> None:
    sys.stderr.write(f"{message}\n")
    sys.stderr.flush()


class JsonlLogger:
    def __init__(self, stream: TextIO, *, redactions: Sequence[SecretStr] = ()) -> None:
        self._stream = stream
        self._redactions: tuple[SecretStr, ...] = tuple(redactions)

    @classmethod
    def open(cls, path: Path, *, redactions: Sequence[SecretStr] = ()) -> JsonlLogger:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        return cls(os.fdopen(descriptor, "a", encoding="utf-8"), redactions=redactions)

    def log(self, entry: LogEntry, /) -> None:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": entry.level.value,
            "event": entry.event,
            "message": redact_secrets(entry.message, self._redactions),
        }
        for key, text in (
            ("error_type", entry.error_type),
            ("detail", entry.detail),
            ("conversation_id", entry.conversation_id),
            ("model", entry.model),
        ):
            if text is not None:
                payload[key] = redact_secrets(text, self._redactions)
        for key, number in (
            ("duration_ms", entry.duration_ms),
            ("input_tokens", entry.input_tokens),
            ("output_tokens", entry.output_tokens),
            ("cache_read_tokens", entry.cache_read_tokens),
        ):
            if number is not None:
                payload[key] = number
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            self._stream.write(line)
            self._stream.flush()
            return
        except OSError, ValueError:
            pass
        with suppress(OSError, ValueError):
            sys.stderr.write(line)
            sys.stderr.flush()

    def close(self) -> None:
        self._stream.close()
