import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from io import FileIO, TextIOWrapper
from logging import ERROR, Formatter, LogRecord
from logging.handlers import RotatingFileHandler
from os import environ
from pathlib import Path
from types import TracebackType

from flow_res import Err, Ok, Result

from my_agents.contracts.ports.logger import IAppLogger, LogEntry

_LOG_FILE_ENV_VAR = "MY_AGENTS_LOG_FILE"
_DEFAULT_LOG_SUBPATH = Path("my-agents") / "my-agents.jsonl"
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 3


@dataclass(frozen=True, slots=True)
class LogFileResolution:
    path: Path
    explicit: bool


@dataclass(frozen=True, slots=True)
class LogFileError:
    message: str


def resolve_log_path(
    *,
    cli_log_file: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> LogFileResolution:
    if cli_log_file is not None:
        return LogFileResolution(path=cli_log_file.expanduser(), explicit=True)

    source: Mapping[str, str] = environ if env is None else env

    env_log_file = source.get(_LOG_FILE_ENV_VAR)
    if env_log_file is not None and env_log_file.strip():
        return LogFileResolution(
            path=Path(env_log_file.strip()).expanduser(),
            explicit=True,
        )

    xdg_state = source.get("XDG_STATE_HOME")
    if xdg_state is not None and xdg_state.strip():
        state_home = Path(xdg_state.strip()).expanduser()
    else:
        home = source.get("HOME")
        base_home = (
            Path(home.strip()).expanduser()
            if home is not None and home.strip()
            else Path.home()
        )
        state_home = base_home / ".local" / "state"

    return LogFileResolution(
        path=state_home / _DEFAULT_LOG_SUBPATH,
        explicit=False,
    )


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def __init__(
        self,
        filename: Path,
        *,
        max_bytes: int,
        backup_count: int,
        on_write_error: Callable[[str], None],
    ) -> None:
        self._on_write_error = on_write_error
        self._write_error_reported = False
        super().__init__(
            filename,
            mode="a",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=False,
        )

    def _open(self) -> TextIOWrapper:
        fd = os.open(
            self.baseFilename,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        return TextIOWrapper(
            FileIO(fd, mode="ab", closefd=True),
            encoding=self.encoding,
            errors=self.errors,
        )

    def handleError(self, record: LogRecord) -> None:
        del record
        if self._write_error_reported:
            return
        self._write_error_reported = True
        self._on_write_error(
            f"Warning: Failed to write log file '{self.baseFilename}': {sys.exc_info()[1]}"
        )


class JsonlFileLogger(IAppLogger):
    def __init__(self, handler: _PrivateRotatingFileHandler) -> None:
        self._handler = handler

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        on_write_error: Callable[[str], None],
        max_bytes: int = _DEFAULT_MAX_BYTES,
        backup_count: int = _DEFAULT_BACKUP_COUNT,
    ) -> Result[JsonlFileLogger, LogFileError]:
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            handler = _PrivateRotatingFileHandler(
                path,
                max_bytes=max_bytes,
                backup_count=backup_count,
                on_write_error=on_write_error,
            )
        except OSError as exc:
            return Err(LogFileError(f"Failed to open log file '{path}': {exc}"))

        handler.setFormatter(Formatter("%(message)s"))
        return Ok(cls(handler))

    def log(self, entry: LogEntry, /) -> None:
        payload: dict[str, str] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": entry.level.name,
            "event": entry.event,
            "message": entry.message,
        }
        if entry.error_type is not None:
            payload["error_type"] = entry.error_type
        if entry.detail is not None:
            payload["detail"] = entry.detail

        line = json.dumps(payload, ensure_ascii=False)
        self._handler.handle(
            LogRecord(
                name="my_agents",
                level=ERROR,
                pathname="",
                lineno=0,
                msg=line,
                args=(),
                exc_info=None,
            )
        )

    def __enter__(self) -> JsonlFileLogger:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        del exc_type, exc_val, exc_tb
        self._handler.close()
