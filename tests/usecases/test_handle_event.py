from datetime import datetime

import pytest
from flow_res import Err, Ok, Result, is_ok

from my_agents.contracts.events import InboundEvent
from my_agents.contracts.ports.event_chat import (
    EventChatRecordError,
    IEventChatRecorder,
)
from my_agents.contracts.ports.event_notifier import (
    EventNotification,
    IEventNotifier,
)
from my_agents.usecases.handle_event import (
    EventProcessingErrorType,
    HandleEventHandler,
)
from tests.event_fixtures import (
    email_event,
    temperature_event,
    validated,
    worker_fault_event,
)
from tests.helpers import assert_err


class FakeRecorder(IEventChatRecorder):
    def __init__(
        self, result: Result[None, EventChatRecordError] | None = None
    ) -> None:
        self.result = Ok(None) if result is None else result
        self.calls: list[tuple[str, str, datetime]] = []

    def record(
        self, event_id: str, content: str, *, occurred_at: datetime
    ) -> Result[None, EventChatRecordError]:
        self.calls.append((event_id, content, occurred_at))
        return self.result


class RecordingNotifier(IEventNotifier):
    def __init__(self) -> None:
        self.notifications: list[EventNotification] = []

    def notify(self, notification: EventNotification, /) -> None:
        self.notifications.append(notification)


def test_completed_event_is_recorded_and_notified() -> None:
    recorder = FakeRecorder()
    notifier = RecordingNotifier()
    event = validated(temperature_event())

    result = HandleEventHandler(recorder, notifier).execute(event)

    assert is_ok(result)
    assert result.value.event_id == event.event_id
    assert recorder.calls[0][0] == event.event_id
    assert "[event:evt_fixture] completed" in recorder.calls[0][1]
    assert len(recorder.calls[0]) == 3
    assert notifier.notifications == [
        EventNotification(event.event_id, recorder.calls[0][1], failed=False)
    ]


def test_analysis_failure_is_recorded_and_notified() -> None:
    recorder = FakeRecorder()
    notifier = RecordingNotifier()
    event = validated(worker_fault_event(reason="boom"))

    result = HandleEventHandler(recorder, notifier).execute(event)

    error = assert_err(
        result,
        expected_type=EventProcessingErrorType.ANALYSIS_FAILED,
        message_contains="Injected worker failure: boom",
        display_message_contains="ワーカーが失敗",
    )
    assert error.type is EventProcessingErrorType.ANALYSIS_FAILED
    assert "[event:evt_fixture] failed" in recorder.calls[0][1]
    assert notifier.notifications[0].failed is True
    assert "boom" in notifier.notifications[0].content


def test_analysis_failure_still_notifies_when_recording_fails() -> None:
    recorder = FakeRecorder(Err(EventChatRecordError("disk full")))
    notifier = RecordingNotifier()

    result = HandleEventHandler(recorder, notifier).execute(
        validated(worker_fault_event(reason="boom"))
    )

    assert_err(
        result,
        expected_type=EventProcessingErrorType.ANALYSIS_FAILED,
        message_contains="boom",
        display_message_contains="ワーカーが失敗",
    )
    assert notifier.notifications[0].failed is True
    assert "履歴保存にも失敗しました: disk full" in notifier.notifications[0].content


def test_record_failure_after_analysis_is_notified_as_failed() -> None:
    recorder = FakeRecorder(Err(EventChatRecordError("read-only database")))
    notifier = RecordingNotifier()

    result = HandleEventHandler(recorder, notifier).execute(validated(email_event()))

    assert_err(
        result,
        expected_type=EventProcessingErrorType.RECORD_FAILED,
        message_contains="read-only database",
        display_message_contains="保存できませんでした",
    )
    assert notifier.notifications[0].failed is True
    assert "完了結果を会話履歴に保存できませんでした" in (
        notifier.notifications[0].content
    )


class RaisingRecorder(IEventChatRecorder):
    def record(
        self, event_id: str, content: str, *, occurred_at: datetime
    ) -> Result[None, EventChatRecordError]:
        _ = (event_id, content, occurred_at)
        raise RuntimeError("write exploded")


def test_unexpected_worker_exception_is_recorded_and_notified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(_event: InboundEvent) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("my_agents.usecases.handle_event.analyze_event", explode)
    recorder = FakeRecorder()
    notifier = RecordingNotifier()

    result = HandleEventHandler(recorder, notifier).execute(
        validated(temperature_event())
    )

    assert_err(
        result,
        expected_type=EventProcessingErrorType.UNEXPECTED,
        message_contains="boom",
        display_message_contains="予期しないエラー",
    )
    assert "[event:evt_fixture] failed" in recorder.calls[0][1]
    assert "予期しないワーカー例外: RuntimeError: boom" in (
        notifier.notifications[0].content
    )
    assert notifier.notifications[0].failed is True


def test_unexpected_failure_reports_recording_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(_event: InboundEvent) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("my_agents.usecases.handle_event.analyze_event", explode)
    notifier = RecordingNotifier()

    result = HandleEventHandler(RaisingRecorder(), notifier).execute(
        validated(temperature_event())
    )

    assert_err(
        result,
        expected_type=EventProcessingErrorType.UNEXPECTED,
        message_contains="boom",
        display_message_contains="予期しないエラー",
    )
    assert "履歴保存にも失敗しました: write exploded" in (
        notifier.notifications[0].content
    )
