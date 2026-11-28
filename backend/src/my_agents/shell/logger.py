import json
import os
import sys
from collections.abc import Sequence
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
        self._failed = False
        self._redactions: tuple[SecretStr, ...] = tuple(redactions)

    @classmethod
    def open(cls, path: Path, *, redactions: Sequence[SecretStr] = ()) -> JsonlLogger:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        return cls(os.fdopen(descriptor, "a", encoding="utf-8"), redactions=redactions)

    def log(self, entry: LogEntry, /) -> None:
        if self._failed:
            return
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": entry.level.value,
            "event": entry.event,
            "message": redact_secrets(entry.message, self._redactions),
        }
        if entry.error_type is not None:
            payload["error_type"] = redact_secrets(entry.error_type, self._redactions)
        if entry.detail is not None:
            payload["detail"] = redact_secrets(entry.detail, self._redactions)
        try:
            self._stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._stream.flush()
        except OSError, ValueError:
            self._failed = True

    def close(self) -> None:
        self._stream.close()
