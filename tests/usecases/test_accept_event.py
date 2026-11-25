from collections.abc import Mapping
from datetime import UTC, datetime

from flow_res import is_ok

from my_agents.contracts.events import InboundEvent
from my_agents.contracts.ports.event_scheduler import IEventScheduler
from my_agents.usecases.accept_event import (
    AcceptEventHandler,
    EventAcceptErrorType,
)
from tests.event_fixtures import FIXED_NOW, temperature_event
from tests.helpers import assert_err


class RecordingScheduler(IEventScheduler):
    def __init__(self) -> None:
        self.events: list[InboundEvent] = []

    def schedule(self, event: InboundEvent, /) -> None:
        self.events.append(event)


def test_accepts_event_and_schedules_background_work() -> None:
    scheduler = RecordingScheduler()
    handler = AcceptEventHandler(
        scheduler,
        clock=lambda: FIXED_NOW,
        event_id_factory=lambda: "evt_fixed",
    )

    result = handler.execute(temperature_event())

    assert is_ok(result)
    assert result.value.event_id == "evt_fixed"
    assert len(scheduler.events) == 1
    assert scheduler.events[0].event_id == "evt_fixed"
    assert scheduler.events[0].occurred_at == FIXED_NOW


def test_rejects_schema_violation_without_scheduling() -> None:
    scheduler = RecordingScheduler()
    handler = AcceptEventHandler(
        scheduler,
        clock=lambda: FIXED_NOW,
        event_id_factory=lambda: "evt_fixed",
    )
    raw: Mapping[str, object] = {"name": "email.received", "data": {}}

    result = handler.execute(raw)

    error = assert_err(
        result,
        expected_type=EventAcceptErrorType.SCHEMA_VIOLATION,
        message_contains="Invalid event field 'data'",
        display_message_contains="形式が不正",
    )
    assert error.type is EventAcceptErrorType.SCHEMA_VIOLATION
    assert scheduler.events == []


def test_default_clock_and_id_factory_assign_correlatable_event_id() -> None:
    scheduler = RecordingScheduler()
    handler = AcceptEventHandler(scheduler)

    before = datetime.now(UTC)
    result = handler.execute(temperature_event())
    after = datetime.now(UTC)

    assert is_ok(result)
    assert result.value.event_id.startswith("evt_")
    assert len(result.value.event_id) == 16
    assert before <= scheduler.events[0].occurred_at <= after
