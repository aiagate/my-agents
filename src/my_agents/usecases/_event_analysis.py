from typing import assert_never

from flow_res import Err, Ok, Result

from my_agents.contracts.events import (
    EmailReceived,
    EventPayload,
    InboundEvent,
    TemperatureThresholdExceeded,
    WorkerFault,
)
from my_agents.domain.event_context import (
    decide_event_delivery,
    sanitize_single_line,
    sanitize_untrusted_text,
)
from my_agents.usecases._event_rules import EventAnalysis, EventAnalysisError

URGENT_EMAIL_TERMS = ("至急", "障害", "請求", "期限")
EMAIL_BODY_EXCERPT_CHARS = 200


def _temperature_analysis(
    payload: TemperatureThresholdExceeded,
) -> EventAnalysis:
    sensor = sanitize_single_line(payload.sensor_id)
    delta = payload.celsius - payload.threshold_celsius
    if delta > 0:
        return EventAnalysis(
            summary=f"{sensor} の温度が閾値を {delta:.1f}°C 超過しています。",
            findings=(
                f"計測値: {payload.celsius:.1f}°C",
                f"閾値: {payload.threshold_celsius:.1f}°C",
            ),
            recommended_action="換気・空調設定を確認し、継続する場合は設備担当へ連絡する。",
            delivery=decide_event_delivery(temperature_delta=delta),
        )
    return EventAnalysis(
        summary=f"{sensor} の温度は閾値内です。",
        findings=(
            f"計測値: {payload.celsius:.1f}°C",
            f"閾値: {payload.threshold_celsius:.1f}°C",
        ),
        recommended_action="追加の対応は不要です。次回の計測を待つ。",
        delivery=decide_event_delivery(temperature_delta=delta),
    )


def _email_analysis(payload: EmailReceived) -> EventAnalysis:
    searchable = f"{payload.subject}\n{payload.body}"
    urgent = any(term in searchable for term in URGENT_EMAIL_TERMS)
    sender = sanitize_single_line(payload.sender)
    subject = sanitize_single_line(payload.subject)
    body = sanitize_untrusted_text(payload.body)[:EMAIL_BODY_EXCERPT_CHARS]
    findings = (
        f"件名（データ）: {subject}",
        f"本文（データ）: {body}",
    )
    if urgent:
        return EventAnalysis(
            summary=f"要対応の可能性があるメールを {sender} から受信しました。",
            findings=(
                *findings,
                "件名または本文に緊急語（至急・障害・請求・期限）を含みます。",
            ),
            recommended_action="内容を確認し、必要なら返信・起票・関係者共有を行う。",
            delivery=decide_event_delivery(urgent_email=True),
        )
    return EventAnalysis(
        summary=f"{sender} からメールを受信しました。",
        findings=findings,
        recommended_action="通常の受信箱として内容を確認する。",
        delivery=decide_event_delivery(urgent_email=False),
    )


def _worker_fault_analysis(
    payload: WorkerFault,
) -> Result[EventAnalysis, EventAnalysisError]:
    reason = sanitize_untrusted_text(payload.reason)
    return Err(
        EventAnalysisError(
            message=f"Injected worker failure: {reason}",
            display_message="イベント解析ワーカーが失敗しました。",
        )
    )


def analyze_event(
    event: InboundEvent,
) -> Result[EventAnalysis, EventAnalysisError]:
    payload: EventPayload = event.payload
    match payload:
        case TemperatureThresholdExceeded():
            return Ok(_temperature_analysis(payload))
        case EmailReceived():
            return Ok(_email_analysis(payload))
        case WorkerFault():
            return _worker_fault_analysis(payload)
        case _:
            assert_never(payload)
