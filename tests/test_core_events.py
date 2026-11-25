from datetime import UTC, datetime, timedelta

import pytest

from my_agents.core.events import (
    MAX_EVENT_BYTES,
    _json_safe,
    build_distill_prompt,
    decide_merge,
    fence_untrusted,
    sanitize_single_line,
    sanitize_untrusted,
    validate_envelope,
    validate_proactive_message,
)
from my_agents.core.types import (
    AppErrorKind,
    ConversationState,
    EventBrief,
    EventDelivery,
    EventEnvelope,
    ProactivePolicy,
)
from tests.helpers import assert_err

_NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)
_POLICY = ProactivePolicy(60, 3, 22, 7, 10)


def _brief(**overrides: object) -> EventBrief:
    values: dict[str, object] = {
        "headline": "h",
        "facts": ("f",),
        "impact": "i",
        "suggested_angle": "s",
        "urgency": 8,
        "relevance": 8,
        "confidence": 0.9,
        "requires_user_action": True,
        "sensitive": False,
        "dedupe_key": "k",
    }
    values.update(overrides)
    return EventBrief.model_validate(values)


def _envelope(data: dict[str, object] | None = None) -> EventEnvelope:
    return EventEnvelope(
        "evt_123",
        "default",
        "temperature.threshold_exceeded",
        "external",
        _NOW,
        "key",
        data or {"celsius": 31.0},
    )


def test_validate_envelope_applies_defaults_and_utc() -> None:
    result = validate_envelope(
        {
            "name": "email.received",
            "data": {"subject": "hello"},
            "occurred_at": "2026-11-22T21:00:00+09:00",
        },
        conversation_id="c",
        event_id="evt_1",
        now=_NOW,
    )
    assert result.unwrap().occurred_at == datetime(2026, 11, 22, 12, tzinfo=UTC)
    assert result.unwrap().idempotency_key == "evt_1"
    assert result.unwrap().source == "external"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"name": "Bad Name"}, "Invalid event field 'name'"),
        ({"name": "ok", "extra": 1}, "Invalid event field 'extra'"),
        ({"name": "ok", "data": []}, "Invalid event field 'data'"),
        (
            {"name": "ok", "idempotency_key": "x" * 200},
            "Invalid event field 'idempotency_key'",
        ),
        ({"name": "ok", "occurred_at": "not-a-date"}, "Invalid event field"),
        ({"name": "ok", "occurred_at": "2026-11-22T12:00:00"}, "UTC offset"),
        ({"name": "ok", "data": {"x": float("nan")}}, "finite JSON"),
        ({"name": "ok", "data": {"x": {1, 2}}}, "finite JSON"),
        (
            {"name": "ok", "data": {"x": "a" * (MAX_EVENT_BYTES + 1)}},
            "at most",
        ),
    ],
)
def test_validate_envelope_rejects_invalid_input(
    raw: dict[str, object], message: str
) -> None:
    error = assert_err(
        validate_envelope(raw, conversation_id="c", event_id="evt_1", now=_NOW),
        kind=AppErrorKind.VALIDATION,
        message_contains=message,
    )
    assert error.kind is AppErrorKind.VALIDATION


def test_json_safe_accepts_scalars_lists_and_dicts() -> None:
    assert _json_safe([True, 1, "x", None, 1.5, {"nested": [2]}])


@pytest.mark.parametrize(
    "value", [float("inf"), float("nan"), {1: "x"}, object(), {object()}]
)
def test_json_safe_rejects_non_json_values(value: object) -> None:
    assert not _json_safe(value)


def test_sanitizers_remove_control_and_defang_markers() -> None:
    raw = "a\x00b\u200bc<d>UNTRUSTED_EVENT\u2028e\n f"
    assert sanitize_untrusted(raw) == ("abc\\u003cd\\u003eUNTRUSTED_EVENT_ e\n f")
    assert sanitize_single_line(raw) == ("abc\\u003cd\\u003eUNTRUSTED_EVENT_ e f")


def test_fence_and_distill_prompt_include_nonce_and_escape_payload() -> None:
    fenced = fence_untrusted("payload", "n")
    assert fenced.startswith('<<UNTRUSTED_EVENT nonce="n">>')
    assert "payload" in fenced
    prompt = build_distill_prompt(_envelope({"celsius": 30}), "nonce-1")
    assert "event_name: temperature.threshold_exceeded" in prompt
    assert 'nonce="nonce-1"' in prompt


def test_distill_prompt_regenerates_colliding_nonce() -> None:
    prompt = build_distill_prompt(_envelope({"note": "abc"}), "abc")
    assert 'nonce="abc-' in prompt


def test_decide_merge_sensitive_and_low_signal_are_silent() -> None:
    state = ConversationState("c")
    cases = (
        (_brief(sensitive=True), "sensitive"),
        (_brief(urgency=1), "low_urgency"),
        (_brief(relevance=1), "low_relevance"),
        (_brief(confidence=0.1), "low_confidence"),
    )
    for brief, reason in cases:
        decision = decide_merge(brief, state, _NOW, _POLICY)
        assert (decision.delivery, decision.reason) == (
            EventDelivery.SILENT,
            reason,
        )


@pytest.mark.parametrize(
    ("policy", "hour", "quiet"),
    [
        (ProactivePolicy(0, 0, 9, 17, 0), 12, True),
        (ProactivePolicy(0, 0, 9, 17, 0), 18, False),
        (ProactivePolicy(0, 0, 22, 7, 0), 23, True),
        (ProactivePolicy(0, 0, 22, 7, 0), 6, True),
        (ProactivePolicy(0, 0, 22, 7, 0), 12, False),
        (ProactivePolicy(0, 0, 0, 0, 0), 3, False),
    ],
)
def test_decide_merge_quiet_hours(
    policy: ProactivePolicy, hour: int, quiet: bool
) -> None:
    now = _NOW.replace(hour=hour)
    decision = decide_merge(_brief(), ConversationState("c"), now, policy)
    assert (decision.delivery is EventDelivery.SILENT) is quiet


def test_decide_merge_demotes_for_user_activity_cooldown_and_cap() -> None:
    active = ConversationState("c", last_user_activity_at=_NOW - timedelta(minutes=1))
    assert decide_merge(_brief(), active, _NOW, _POLICY).reason == (
        "user_active_window"
    )
    assert (
        decide_merge(
            _brief(),
            ConversationState("c", last_user_activity_at=_NOW - timedelta(minutes=30)),
            _NOW,
            _POLICY,
        ).delivery
        is EventDelivery.PROACTIVE
    )
    assert (
        decide_merge(
            _brief(),
            ConversationState("c", last_user_activity_at=_NOW),
            _NOW,
            ProactivePolicy(60, 3, 22, 7, 0),
        ).delivery
        is EventDelivery.PROACTIVE
    )
    cooldown = ConversationState("c", last_proactive_at=_NOW - timedelta(minutes=5))
    assert decide_merge(_brief(), cooldown, _NOW, _POLICY).reason == "cooldown"
    capped = ConversationState(
        "c", proactive_count_today=3, proactive_day=_NOW.date().isoformat()
    )
    assert decide_merge(_brief(), capped, _NOW, _POLICY).reason == "daily_cap"
    assert (
        decide_merge(
            _brief(),
            ConversationState(
                "c", proactive_count_today=2, proactive_day=_NOW.date().isoformat()
            ),
            _NOW,
            _POLICY,
        ).delivery
        is EventDelivery.PROACTIVE
    )
    assert (
        decide_merge(
            _brief(),
            ConversationState("c", proactive_count_today=9, proactive_day="2026-01-01"),
            _NOW,
            _POLICY,
        ).reason
        == "policy_allows"
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "x" * 281,
        "a\nb\nc\nd",
        "evt_123",
        "[event:x]",
        "UNTRUSTED data",
        "name=value",
        "```json",
        "{json}",
    ],
)
def test_validate_proactive_message_rejects_machine_output(text: str) -> None:
    assert not validate_proactive_message(text)


def test_validate_proactive_message_accepts_natural_text() -> None:
    assert validate_proactive_message("少し暑くなってきましたね。窓を開けますか？")
