import pytest
from flow_res import Err, Ok, Result

from my_agents.contracts.events import InboundEvent
from my_agents.contracts.ports.logger import IAppLogger, LogEntry, LogLevel
from my_agents.presentation.webhook.background_scheduler import (
    BackgroundEventScheduler,
)
from my_agents.usecases.handle_event import (
    EventCompletion,
    EventProcessingError,
    EventProcessingErrorType,
)
from tests.event_fixtures import validated, worker_fault_event


class FakeHandleEvent:
    def __init__(
        self,
        result: Result[EventCompletion, EventProcessingError] | None = None,
        *,
        exc: Exception | None = None,
    ) -> None:
        self.result = result
        self.exc = exc
        self.events: list[InboundEvent] = []

    def execute(
        self, event: InboundEvent
    ) -> Result[EventCompletion, EventProcessingError]:
        self.events.append(event)
        if self.exc is not None:
            raise self.exc
        assert self.result is not None
        return self.result


class RecordingLogger(IAppLogger):
    def __init__(self) -> None:
        self.entries: list[LogEntry] = []

    def log(self, entry: LogEntry, /) -> None:
        self.entries.append(entry)


def _event() -> InboundEvent:
    return validated(worker_fault_event())


def test_processes_scheduled_event_on_background_thread() -> None:
    handle = FakeHandleEvent(Ok(EventCompletion("evt_fixture", "done")))
    scheduler = BackgroundEventScheduler(handle)

    scheduler.start()
    scheduler.start()
    scheduler.schedule(_event())
    scheduler.stop()
    scheduler.stop()

    assert len(handle.events) == 1
    assert handle.events[0].event_id == "evt_fixture"


def test_delays_worker_without_blocking_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "my_agents.presentation.webhook.background_scheduler.time.sleep",
        sleeps.append,
    )
    handle = FakeHandleEvent(Ok(EventCompletion("evt_fixture", "done")))
    scheduler = BackgroundEventScheduler(handle, delay_ms=1500)

    scheduler.start()
    scheduler.schedule(_event())
    scheduler.stop()

    assert sleeps == [1.5]
    assert len(handle.events) == 1


def test_logs_typed_worker_failure() -> None:
    logger = RecordingLogger()
    failure = EventProcessingError(
        EventProcessingErrorType.ANALYSIS_FAILED, "boom", "failed"
    )
    scheduler = BackgroundEventScheduler(FakeHandleEvent(Err(failure)), logger=logger)

    scheduler.start()
    scheduler.schedule(_event())
    scheduler.stop()

    assert logger.entries == [
        LogEntry(
            level=LogLevel.WARNING,
            event="event_worker_failed",
            error_type="ANALYSIS_FAILED",
            message="boom",
        )
    ]


def test_logs_and_survives_unexpected_worker_exception() -> None:
    logger = RecordingLogger()
    handle = FakeHandleEvent(exc=ValueError("unexpected"))
    scheduler = BackgroundEventScheduler(handle, logger=logger)

    scheduler.start()
    scheduler.schedule(_event())
    scheduler.schedule(_event())
    scheduler.stop()

    assert len(handle.events) == 2
    assert len(logger.entries) == 2
    assert logger.entries[0] == LogEntry(
        level=LogLevel.ERROR,
        event="event_worker_crashed",
        error_type="ValueError",
        message="unexpected",
    )
