from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto
from typing import Protocol
from uuid import uuid4

from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.events import EventReceipt
from my_agents.contracts.ports.event_scheduler import IEventScheduler
from my_agents.usecases._event_rules import validate_event


class EventAcceptErrorType(Enum):
    SCHEMA_VIOLATION = auto()


@dataclass(frozen=True, slots=True)
class EventAcceptError:
    type: EventAcceptErrorType
    message: str
    display_message: str


class AcceptEventUseCase(Protocol):
    def execute(
        self, raw: Mapping[str, object]
    ) -> Result[EventReceipt, EventAcceptError]: ...


def _new_event_id() -> str:
    return f"evt_{uuid4().hex[:12]}"


class AcceptEventHandler:
    def __init__(
        self,
        scheduler: IEventScheduler,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        event_id_factory: Callable[[], str] = _new_event_id,
    ) -> None:
        self._scheduler = scheduler
        self._clock = clock
        self._event_id_factory = event_id_factory

    def execute(
        self, raw: Mapping[str, object]
    ) -> Result[EventReceipt, EventAcceptError]:
        event_id = self._event_id_factory()
        validated = validate_event(raw, event_id=event_id, now=self._clock())
        if is_err(validated):
            return Err(
                EventAcceptError(
                    type=EventAcceptErrorType.SCHEMA_VIOLATION,
                    message=(
                        f"Invalid event field '{validated.error.field}': "
                        f"{validated.error.message}"
                    ),
                    display_message="イベントの形式が不正です。",
                )
            )
        self._scheduler.schedule(validated.value)
        return Ok(EventReceipt(event_id))
