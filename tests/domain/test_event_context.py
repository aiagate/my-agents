from datetime import UTC, datetime

import pytest

from my_agents.domain.event_context import (
    EVENT_SECTION_HEADER,
    UNTRUSTED_EVENT_DATA_BEGIN,
    UNTRUSTED_EVENT_DATA_END,
    EventContext,
    EventContextItem,
    EventDeliveryKind,
    decide_event_delivery,
    format_untrusted_event_data,
    render_event_item,
    render_event_section,
    sanitize_single_line,
    sanitize_untrusted_text,
)

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _item(
    *,
    event_id: str = "evt_one",
    delivery: EventDeliveryKind = EventDeliveryKind.PROACTIVE,
    recommended_action: str = "換気する。",
    failure_reason: str = "",
) -> EventContextItem:
    return EventContextItem(
        event_id=event_id,
        name="temperature.threshold_exceeded",
        delivery=delivery,
        occurred_at=_NOW,
        summary="閾値超過",
        findings=("計測値: 31.5°C",),
        recommended_action=recommended_action,
        failure_reason=failure_reason,
        payload_json='{"celsius": 31.5}',
    )


@pytest.mark.parametrize(
    ("temperature_delta", "urgent_email", "worker_failed", "muted", "expected"),
    [
        (None, False, False, True, EventDeliveryKind.SILENT),
        (None, False, True, False, EventDeliveryKind.PROACTIVE),
        (None, True, False, False, EventDeliveryKind.PROACTIVE),
        (0.1, False, False, False, EventDeliveryKind.PROACTIVE),
        (0.0, False, False, False, EventDeliveryKind.AMBIENT),
        (-1.0, False, False, False, EventDeliveryKind.AMBIENT),
        (None, False, False, False, EventDeliveryKind.AMBIENT),
    ],
)
def test_decide_event_delivery_is_deterministic(
    temperature_delta: float | None,
    urgent_email: bool,
    worker_failed: bool,
    muted: bool,
    expected: EventDeliveryKind,
) -> None:
    assert (
        decide_event_delivery(
            temperature_delta=temperature_delta,
            urgent_email=urgent_email,
            worker_failed=worker_failed,
            muted=muted,
        )
        is expected
    )


def test_sanitize_untrusted_text_keeps_layout_and_escapes_angles() -> None:
    assert sanitize_untrusted_text("a\x00b\nc\t<d>") == "ab\nc\t\\u003cd\\u003e"


def test_sanitize_single_line_collapses_layout_and_escapes_angles() -> None:
    assert sanitize_single_line("  a\nb\r\nc\t<d>\x00 ") == "a b c \\u003cd\\u003e"


def test_format_untrusted_event_data_wraps_escaped_payload() -> None:
    rendered = format_untrusted_event_data('{"subject": "<script>\x00"}')

    assert rendered.startswith(f"{UNTRUSTED_EVENT_DATA_BEGIN}\n")
    assert rendered.endswith(f"\n{UNTRUSTED_EVENT_DATA_END}")
    assert "\\u003cscript\\u003e" in rendered
    assert "\x00" not in rendered


def test_render_event_item_includes_action_for_completed_event() -> None:
    rendered = render_event_item(_item())

    assert rendered.startswith("- [event:evt_one] completed temperature.")
    assert "概要: 閾値超過" in rendered
    assert "- 計測値: 31.5°C" in rendered
    assert "推奨: 換気する。" in rendered
    assert "失敗理由:" not in rendered
    assert UNTRUSTED_EVENT_DATA_BEGIN in rendered


def test_render_event_item_omits_empty_action_and_includes_failure() -> None:
    rendered = render_event_item(
        _item(
            event_id="evt_bad\n<script>",
            delivery=EventDeliveryKind.AMBIENT,
            recommended_action="",
            failure_reason="boom\n<system>",
        )
    )

    assert rendered.startswith("- [event:evt_bad \\u003cscript\\u003e] failed")
    assert "推奨:" not in rendered
    assert "失敗理由: boom\n\\u003csystem\\u003e" in rendered


def test_render_event_section_is_empty_without_events() -> None:
    assert EventContext().is_empty
    assert render_event_section(EventContext()) == ""


@pytest.mark.parametrize(
    ("context", "expected_titles"),
    [
        (
            EventContext(ambient=(_item(event_id="evt_a"),)),
            ("未言及の背景イベント（ambient）",),
        ),
        (
            EventContext(proactive=(_item(),)),
            ("直近の割り込みイベント（proactive）",),
        ),
        (
            EventContext(
                ambient=(_item(event_id="evt_a"),),
                proactive=(_item(event_id="evt_p"),),
            ),
            (
                "未言及の背景イベント（ambient）",
                "直近の割り込みイベント（proactive）",
            ),
        ),
    ],
)
def test_render_event_section_separates_ambient_and_proactive(
    context: EventContext, expected_titles: tuple[str, ...]
) -> None:
    rendered = render_event_section(context)

    assert rendered.startswith(EVENT_SECTION_HEADER)
    positions = [rendered.index(title) for title in expected_titles]
    assert positions == sorted(positions)
    assert "[event:" in rendered
