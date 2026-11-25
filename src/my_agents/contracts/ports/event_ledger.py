from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto

from flow_res import Result

from my_agents.contracts.events import EventRecord


class EventLedgerErrorType(Enum):
    READ_FAILED = auto()
    WRITE_FAILED = auto()
    CORRUPT_DATA = auto()


@dataclass(frozen=True, slots=True)
class EventLedgerError:
    type: EventLedgerErrorType
    message: str


class IEventLedger(ABC):
    @abstractmethod
    def record(self, entry: EventRecord) -> Result[None, EventLedgerError]: ...

    @abstractmethod
    def list_recent(
        self, *, limit: int = 10, unsurfaced_only: bool = False
    ) -> Result[tuple[EventRecord, ...], EventLedgerError]: ...

    @abstractmethod
    def get_event(
        self, event_id: str
    ) -> Result[EventRecord | None, EventLedgerError]: ...

    @abstractmethod
    def mark_surfaced(
        self, event_ids: Sequence[str], *, surfaced_at: datetime
    ) -> Result[None, EventLedgerError]: ...
