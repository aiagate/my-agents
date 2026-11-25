from flow_res import is_err, is_ok

from my_agents.domain.event_context import EventDeliveryKind
from my_agents.usecases._event_analysis import analyze_event
from tests.event_fixtures import (
    email_event,
    temperature_event,
    validated,
    worker_fault_event,
)


def test_temperature_above_threshold_recommends_ventilation() -> None:
    result = analyze_event(validated(temperature_event(celsius=32.4, threshold=30.0)))

    assert is_ok(result)
    assert "2.4°C 超過" in result.value.summary
    assert "計測値: 32.4°C" in result.value.findings
    assert "換気" in result.value.recommended_action
    assert result.value.delivery is EventDeliveryKind.PROACTIVE


def test_temperature_within_threshold_reports_no_action() -> None:
    result = analyze_event(validated(temperature_event(celsius=28.0, threshold=30.0)))

    assert is_ok(result)
    assert "閾値内" in result.value.summary
    assert "追加の対応は不要" in result.value.recommended_action
    assert result.value.delivery is EventDeliveryKind.AMBIENT


def test_urgent_email_is_flagged() -> None:
    result = analyze_event(
        validated(email_event(subject="【至急】障害対応", body="至急確認してください"))
    )

    assert is_ok(result)
    assert "要対応の可能性" in result.value.summary
    assert any("緊急語" in finding for finding in result.value.findings)
    assert "返信・起票" in result.value.recommended_action
    assert result.value.delivery is EventDeliveryKind.PROACTIVE


def test_non_urgent_email_stays_normal() -> None:
    result = analyze_event(
        validated(email_event(subject="<script>alert(1)</script>", body="本文"))
    )

    assert is_ok(result)
    assert "alerts@example.com からメールを受信" in result.value.summary
    assert "\\u003cscript\\u003e" in result.value.findings[0]
    assert "通常の受信箱" in result.value.recommended_action
    assert result.value.delivery is EventDeliveryKind.AMBIENT


def test_analysis_sanitizes_every_untrusted_field() -> None:
    sensor = analyze_event(
        validated(
            temperature_event(sensor_id="lab\n<html>\x00", celsius=31.0, threshold=30.0)
        )
    )
    email = analyze_event(
        validated(
            email_event(
                sender="a\n<b>\x00",
                subject="件名\n<script>",
                body="本文\x00</body>",
            )
        )
    )
    fault = analyze_event(validated(worker_fault_event(reason="boom\n<system>\x00")))

    assert is_ok(sensor)
    assert is_ok(email)
    assert "lab \\u003chtml\\u003e" in sensor.value.summary
    assert "a \\u003cb\\u003e" in email.value.summary
    assert "件名 \\u003cscript\\u003e" in email.value.findings[0]
    assert "本文\\u003c/body\\u003e" in email.value.findings[1]
    assert is_err(fault)
    assert "boom\n\\u003csystem\\u003e" in fault.error.message


def test_worker_fault_returns_typed_analysis_error() -> None:
    result = analyze_event(validated(worker_fault_event(reason="boom\nnow")))

    assert is_err(result)
    assert "Injected worker failure: boom\nnow" in result.error.message
    assert result.error.display_message == "イベント解析ワーカーが失敗しました。"
