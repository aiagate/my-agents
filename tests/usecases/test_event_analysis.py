from flow_res import is_err, is_ok

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


def test_temperature_within_threshold_reports_no_action() -> None:
    result = analyze_event(validated(temperature_event(celsius=28.0, threshold=30.0)))

    assert is_ok(result)
    assert "閾値内" in result.value.summary
    assert "追加の対応は不要" in result.value.recommended_action


def test_urgent_email_is_flagged() -> None:
    result = analyze_event(
        validated(email_event(subject="【至急】障害対応", body="至急確認してください"))
    )

    assert is_ok(result)
    assert "要対応の可能性" in result.value.summary
    assert "緊急語" in result.value.findings[1]
    assert "返信・起票" in result.value.recommended_action


def test_non_urgent_email_stays_normal() -> None:
    result = analyze_event(
        validated(email_event(subject="<script>alert(1)</script>", body="本文"))
    )

    assert is_ok(result)
    assert "alerts@example.com からメールを受信" in result.value.summary
    assert "\\u003cscript\\u003e" in result.value.findings[0]
    assert "通常の受信箱" in result.value.recommended_action


def test_worker_fault_returns_typed_analysis_error() -> None:
    result = analyze_event(validated(worker_fault_event(reason="boom\nnow")))

    assert is_err(result)
    assert "Injected worker failure: boom\nnow" in result.error.message
    assert result.error.display_message == "イベント解析ワーカーが失敗しました。"
