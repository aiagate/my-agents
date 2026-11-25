from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto
from typing import Protocol

from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.events import InboundEvent
from my_agents.contracts.ports.event_chat import (
    EventChatRecordError,
    IEventChatRecorder,
)
from my_agents.contracts.ports.event_notifier import (
    EventNotification,
    IEventNotifier,
)
from my_agents.usecases._event_analysis import analyze_event
from my_agents.usecases._event_rules import (
    format_event_completion,
    format_event_failure,
)


class EventProcessingErrorType(Enum):
    ANALYSIS_FAILED = auto()
    RECORD_FAILED = auto()
    UNEXPECTED = auto()


@dataclass(frozen=True, slots=True)
class EventProcessingError:
    type: EventProcessingErrorType
    message: str
    display_message: str


@dataclass(frozen=True, slots=True)
class EventCompletion:
    event_id: str
    content: str


class HandleEventUseCase(Protocol):
    def execute(
        self, event: InboundEvent
    ) -> Result[EventCompletion, EventProcessingError]: ...


class HandleEventHandler:
    def __init__(
        self,
        recorder: IEventChatRecorder,
        notifier: IEventNotifier,
    ) -> None:
        self._recorder = recorder
        self._notifier = notifier

    def _record(self, event: InboundEvent, content: str) -> EventChatRecordError | None:
        result = self._recorder.record(
            event.event_id, content, occurred_at=datetime.now(UTC)
        )
        return result.error if is_err(result) else None

    def _notify_failure(
        self,
        event: InboundEvent,
        failure: str,
        record_error: EventChatRecordError | None,
    ) -> None:
        notified = (
            failure
            if record_error is None
            else f"{failure}\n（履歴保存にも失敗しました: {record_error.message}）"
        )
        self._notifier.notify(EventNotification(event.event_id, notified, failed=True))

    def execute(
        self, event: InboundEvent
    ) -> Result[EventCompletion, EventProcessingError]:
        try:
            return self._execute(event)
        except Exception as exc:
            return self._unexpected_failure(event, exc)

    def _unexpected_failure(
        self, event: InboundEvent, exc: Exception
    ) -> Result[EventCompletion, EventProcessingError]:
        failure = format_event_failure(
            event.event_id,
            event.name,
            f"予期しないワーカー例外: {type(exc).__name__}: {exc}",
        )
        try:
            record_error = self._record(event, failure)
        except Exception as record_exc:
            record_error = EventChatRecordError(str(record_exc))
        self._notify_failure(event, failure, record_error)
        return Err(
            EventProcessingError(
                type=EventProcessingErrorType.UNEXPECTED,
                message=str(exc),
                display_message="イベント処理中に予期しないエラーが発生しました。",
            )
        )

    def _execute(
        self, event: InboundEvent
    ) -> Result[EventCompletion, EventProcessingError]:
        analysis = analyze_event(event)
        if is_err(analysis):
            failure = format_event_failure(
                event.event_id, event.name, analysis.error.message
            )
            record_error = self._record(event, failure)
            self._notify_failure(event, failure, record_error)
            return Err(
                EventProcessingError(
                    type=EventProcessingErrorType.ANALYSIS_FAILED,
                    message=analysis.error.message,
                    display_message=analysis.error.display_message,
                )
            )

        content = format_event_completion(event, analysis.value)
        record_error = self._record(event, content)
        if record_error is not None:
            failure = format_event_failure(
                event.event_id,
                event.name,
                f"完了結果を会話履歴に保存できませんでした: {record_error.message}",
            )
            self._notify_failure(event, failure, None)
            return Err(
                EventProcessingError(
                    type=EventProcessingErrorType.RECORD_FAILED,
                    message=record_error.message,
                    display_message="イベント結果を会話履歴に保存できませんでした。",
                )
            )
        self._notifier.notify(EventNotification(event.event_id, content, failed=False))
        return Ok(EventCompletion(event.event_id, content))
