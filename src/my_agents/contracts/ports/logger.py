from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto


class LogLevel(Enum):
    DEBUG = auto()
    INFO = auto()
    WARNING = auto()
    ERROR = auto()


@dataclass(frozen=True, slots=True)
class LogEntry:
    level: LogLevel
    event: str
    message: str
    error_type: str | None = None
    detail: str | None = None


class IAppLogger(ABC):
    @abstractmethod
    def log(self, entry: LogEntry, /) -> None: ...


class NoopLogger(IAppLogger):
    def log(self, _entry: LogEntry, /) -> None:
        return


NOOP_LOGGER: IAppLogger = NoopLogger()
