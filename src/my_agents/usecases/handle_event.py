from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto
from typing import Protocol, assert_never

from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.events import (
    EventDelivery,
    EventRecord,
    EventStatus,
    InboundEvent,
)
from my_agents.contracts.ports.event_ledger import (
    EventLedgerError,
    EventLedgerErrorType,
    IEventLedger,
)
from my_agents.contracts.ports.event_notifier import (
    EventNotification,
    IEventNotifier,
)
from my_agents.domain.event_context import (
    EventDeliveryKind,
    decide_event_delivery,
    sanitize_untrusted_text,
)
from my_agents.usecases._event_analysis import analyze_event
from my_agents.usecases._event_rules import (
    EVENT_FAILURE_REASON_MAX_CHARS,
    EventAnalysis,
    EventAnalysisError,
    serialize_payload,
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


def _completion_content(event: InboundEvent, analysis: EventAnalysis) -> str:
    lines = [
        f"[Event {event.event_id}: completed]",
        f"概要: {analysis.summary}",
        f"推奨: {analysis.recommended_action}",
        f"event_id: {event.event_id}",
    ]
    return "\n".join(lines)


class HandleEventHandler:
    def __init__(
        self,
        ledger: IEventLedger,
        notifier: IEventNotifier,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ledger = ledger
        self._notifier = notifier
        self._clock = clock

    def _record(
        self,
        event: InboundEvent,
        *,
        status: EventStatus,
        delivery: EventDeliveryKind,
        summary: str,
        findings: tuple[str, ...],
        recommended_action: str,
        failure_reason: str,
    ) -> EventLedgerError | None:
        recorded_at = self._clock()
        reason = sanitize_untrusted_text(failure_reason)[
            :EVENT_FAILURE_REASON_MAX_CHARS
        ]
        match delivery:
            case EventDeliveryKind.PROACTIVE:
                surfaced_at = recorded_at
            case EventDeliveryKind.AMBIENT | EventDeliveryKind.SILENT:
                surfaced_at = None
            case _:
                assert_never(delivery)
        result = self._ledger.record(
            EventRecord(
                event_id=event.event_id,
                name=event.name,
                occurred_at=event.occurred_at,
                recorded_at=recorded_at,
                status=status,
                delivery=EventDelivery(delivery.value),
                payload_json=serialize_payload(event.payload),
                summary=summary,
                findings=findings,
                recommended_action=recommended_action,
                failure_reason=reason,
                surfaced_at=surfaced_at,
            )
        )
        return result.error if is_err(result) else None

    def _record_failure(
        self, event: InboundEvent, failure: str
    ) -> EventLedgerError | None:
        return self._record(
            event,
            status=EventStatus.FAILED,
            delivery=decide_event_delivery(worker_failed=True),
            summary="イベント処理に失敗しました。",
            findings=(),
            recommended_action="",
            failure_reason=failure,
        )

    def _notify_failure(
        self,
        event: InboundEvent,
        failure: str,
        record_error: EventLedgerError | None,
    ) -> None:
        reason = sanitize_untrusted_text(failure)[:EVENT_FAILURE_REASON_MAX_CHARS]
        lines = [
            f"[Event {event.event_id}: failed]",
            f"失敗理由: {reason}",
        ]
        if record_error is not None:
            lines.append(f"（台帳保存にも失敗しました: {record_error.message}）")
        lines.append(f"event_id: {event.event_id}")
        self._notifier.notify(
            EventNotification(event.event_id, "\n".join(lines), failed=True)
        )

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
        failure = f"予期しないワーカー例外: {type(exc).__name__}: {exc}"
        try:
            record_error = self._record_failure(event, failure)
        except Exception as record_exc:
            record_error = EventLedgerError(
                EventLedgerErrorType.WRITE_FAILED, str(record_exc)
            )
        self._notify_failure(event, failure, record_error)
        return Err(
            EventProcessingError(
                type=EventProcessingErrorType.UNEXPECTED,
                message=str(exc),
                display_message="イベント処理中に予期しないエラーが発生しました。",
            )
        )

    def _analysis_failure(
        self, event: InboundEvent, error: EventAnalysisError
    ) -> Result[EventCompletion, EventProcessingError]:
        record_error = self._record_failure(event, error.message)
        self._notify_failure(event, error.message, record_error)
        return Err(
            EventProcessingError(
                type=EventProcessingErrorType.ANALYSIS_FAILED,
                message=error.message,
                display_message=error.display_message,
            )
        )

    def _execute(
        self, event: InboundEvent
    ) -> Result[EventCompletion, EventProcessingError]:
        analysis = analyze_event(event)
        if is_err(analysis):
            return self._analysis_failure(event, analysis.error)

        value = analysis.value
        record_error = self._record(
            event,
            status=EventStatus.COMPLETED,
            delivery=value.delivery,
            summary=value.summary,
            findings=value.findings,
            recommended_action=value.recommended_action,
            failure_reason="",
        )
        content = _completion_content(event, value)
        if record_error is not None:
            failure = (
                f"完了結果をイベント台帳に保存できませんでした: {record_error.message}"
            )
            self._notify_failure(event, failure, None)
            return Err(
                EventProcessingError(
                    type=EventProcessingErrorType.RECORD_FAILED,
                    message=record_error.message,
                    display_message="イベント結果を台帳に保存できませんでした。",
                )
            )
        if value.delivery is EventDeliveryKind.PROACTIVE:
            self._notifier.notify(
                EventNotification(event.event_id, content, failed=False)
            )
        return Ok(EventCompletion(event.event_id, content))
