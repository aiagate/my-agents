from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto

from flow_res import Result


class CoreProfileStoreErrorType(Enum):
    READ_FAILED = auto()
    WRITE_FAILED = auto()


@dataclass(frozen=True, slots=True)
class CoreProfileStoreError:
    type: CoreProfileStoreErrorType
    message: str


class ICoreProfileStore(ABC):
    @property
    @abstractmethod
    def location(self) -> str: ...

    @abstractmethod
    def load(self) -> Result[str | None, CoreProfileStoreError]: ...

    @abstractmethod
    def save(self, content: str) -> Result[None, CoreProfileStoreError]: ...
