from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import Literal

from flow_res import Result

CoreMemoryUpdateAction = Literal["append", "replace", "rewrite"]


@dataclass(frozen=True, slots=True)
class CoreMemoryUpdateCommand:
    action: CoreMemoryUpdateAction
    reason: str
    content: str = ""
    old_text: str = ""


@dataclass(frozen=True, slots=True)
class CoreMemoryUpdateOutcome:
    char_count: int
    max_chars: int
    reason: str


class CoreMemoryUpdateErrorType(Enum):
    INVALID_INPUT = auto()
    TARGET_NOT_FOUND = auto()
    AMBIGUOUS_TARGET = auto()
    MAX_LENGTH_EXCEEDED = auto()
    STORAGE_ERROR = auto()


@dataclass(frozen=True, slots=True)
class CoreMemoryUpdateError:
    type: CoreMemoryUpdateErrorType
    message: str


class ICoreProfileUpdater(ABC):
    @abstractmethod
    def update(
        self,
        command: CoreMemoryUpdateCommand,
    ) -> Result[CoreMemoryUpdateOutcome, CoreMemoryUpdateError]: ...
