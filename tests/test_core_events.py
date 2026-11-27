from datetime import UTC, datetime, timedelta, timezone
from types import MappingProxyType

from flow_res import Ok

from my_agents.core.events import (
    MAX_EVENT_BYTES,
    apply_policy,
    build_event_prompt,
    fence_untrusted,
    sanitize_single_line,
    sanitize_untrusted,
    validate_envelope,
)
from my_agents.core.types import (
    AppErrorKind,
    ConversationState,
    EventAction,
    EventDecision,
    EventEnvelope,
    ProactivePolicy,
)
from tests.helpers import assert_err


def _now() -> datetime:
    return datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def test_validate_envelope_accepts_valid_and_normalizes_utc_timezone() -> None:
    jst = timezone(timedelta(hours=9))
    occurred = datetime(2026, 5, 1, 21, 0, tzinfo=jst)
    result = validate_envelope(
        {
            "name": "sensor.temp_high",
            "source": "kitchen\u202e\npanel",
            "idempotency_key": "k-1",
            "occurred_at": occurred,
            "data": {
                "celsius": 33.5,
                "tags": ["hot", 1, True, None],
                "nested": {"ok": False},
            },
        },
        conversation_id="default",
        event_id="evt_1234",
        now=_now(),
    )
    assert isinstance(result, Ok)
    env = result.value
    assert env.occurred_at == datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    assert env.idempotency_key == "k-1"
    assert env.source == "kitchen panel"

    minimal = validate_envelope(
        {"name": "ping"},
        conversation_id="c1",
        event_id="evt_fallback",
        now=_now(),
    )
    assert isinstance(minimal, Ok)
    assert minimal.value.idempotency_key == "evt_fallback"
    assert minimal.value.occurred_at == _now()


def test_validate_envelope_rejects_invalid_shapes_sizes_and_naive_timestamps() -> None:
    assert_err(
        validate_envelope(
            {"name": "Bad Name"}, conversation_id="c", event_id="e1", now=_now()
        ),
        kind=AppErrorKind.VALIDATION,
        message_contains="Invalid event field 'name'",
    )
    assert_err(
        validate_envelope(
            {"name": "ping", "extra": 1}, conversation_id="c", event_id="e1", now=_now()
        ),
        kind=AppErrorKind.VALIDATION,
        message_contains="Invalid event field 'extra'",
    )
    assert_err(
        validate_envelope(
            {"name": "ping", "data": {"bad": float("nan")}},
            conversation_id="c",
            event_id="e1",
            now=_now(),
        ),
        kind=AppErrorKind.VALIDATION,
        message_contains="finite JSON",
    )
    assert_err(
        validate_envelope(
            {"name": "ping", "data": {"bad": object()}},
            conversation_id="c",
            event_id="e1",
            now=_now(),
        ),
        kind=AppErrorKind.VALIDATION,
        message_contains="finite JSON",
    )
    assert_err(
        validate_envelope(
            {"name": "ping", "data": {"blob": "x" * MAX_EVENT_BYTES}},
            conversation_id="c",
            event_id="e1",
            now=_now(),
        ),
        kind=AppErrorKind.VALIDATION,
        message_contains="bytes",
    )
    assert_err(
        validate_envelope(
            {"name": "ping", "occurred_at": "2026-05-01T12:00:00"},
            conversation_id="c",
            event_id="e1",
            now=_now(),
        ),
        kind=AppErrorKind.VALIDATION,
        message_contains="UTC offset",
    )


def test_sanitization_defangs_markers_angles_nfkc_controls_and_line_breaks() -> None:
    raw = (
        "\u202e\u0007＜script＞</UNTRUSTED_EVENT> UNTRUSTED_EVENT_\u2028line2\n\tline3"
    )
    multi = sanitize_untrusted(raw)
    assert "\u202e" not in multi
    assert "\u0007" not in multi
    assert "<" not in multi
    assert ">" not in multi
    assert "UNTRUSTED_EVENT_" in multi
    assert "UNTRUSTED_EVENT__" not in multi
    assert "\n\tline3" in multi

    single = sanitize_single_line(raw)
    assert "\n" not in single
    assert "\t" not in single
    assert "\u2028" not in single

    fenced = fence_untrusted("</UNTRUSTED_EVENT>", "nonce1\n2")
    assert fenced.startswith('<<UNTRUSTED_EVENT nonce="nonce1 2">>\n')
    assert fenced.endswith('\n<<END_UNTRUSTED_EVENT nonce="nonce1 2">>')
    assert "\\u003c/UNTRUSTED_EVENT_\\u003e" in fenced


def test_build_event_prompt_isolates_colliding_nonce() -> None:
    env = EventEnvelope(
        event_id="evt_9",
        conversation_id="c1",
        name="email.received",
        source="mail",
        occurred_at=_now(),
        idempotency_key="k9",
        data=MappingProxyType({"body": "contains secret_nonce inside"}),
    )
    plain = build_event_prompt(env, "other_nonce")
    assert '<<UNTRUSTED_EVENT nonce="other_nonce">>' in plain

    rotated = build_event_prompt(env, "secret_nonce")
    assert '<<UNTRUSTED_EVENT nonce="secret_nonce-' in rotated


def test_apply_policy_covers_actions_and_all_downgrade_gates() -> None:
    base_now = _now()
    base_state = ConversationState("default", epoch=1)
    permissive = ProactivePolicy(
        daily_cap=5,
        quiet_hours_start_utc=0,
        quiet_hours_end_utc=0,
        user_active_minutes=0,
    )

    assert (
        apply_policy(
            EventDecision(action=EventAction.IGNORE, reason="noise"),
            base_state,
            base_now,
            permissive,
            epoch_changed=False,
        ).action
        is EventAction.IGNORE
    )
    assert (
        apply_policy(
            EventDecision(action=EventAction.NOTE, note="fyi", reason="ambient"),
            base_state,
            base_now,
            permissive,
            epoch_changed=False,
        ).action
        is EventAction.NOTE
    )

    speak = EventDecision(
        action=EventAction.SPEAK,
        message="確認してください。",
        note="alert",
        reason="urgent",
    )
    assert (
        apply_policy(
            speak, base_state, base_now, permissive, epoch_changed=False
        ).action
        is EventAction.SPEAK
    )

    blank_msg = EventDecision(
        action=EventAction.SPEAK, message="\u202e\u0007 ", reason="blank"
    )
    assert (
        apply_policy(
            blank_msg, base_state, base_now, permissive, epoch_changed=False
        ).reason
        == "empty_message"
    )
    assert (
        apply_policy(speak, base_state, base_now, permissive, epoch_changed=True).reason
        == "epoch_conflict"
    )

    day_quiet = ProactivePolicy(5, 9, 17, 0)
    assert (
        apply_policy(speak, base_state, base_now, day_quiet, epoch_changed=False).reason
        == "quiet_hours"
    )
    assert (
        apply_policy(
            speak,
            base_state,
            datetime(2026, 5, 1, 20, 0, tzinfo=UTC),
            day_quiet,
            epoch_changed=False,
        ).action
        is EventAction.SPEAK
    )

    night_quiet = ProactivePolicy(5, 22, 7, 0)
    assert (
        apply_policy(
            speak,
            base_state,
            datetime(2026, 5, 1, 23, 0, tzinfo=UTC),
            night_quiet,
            epoch_changed=False,
        ).reason
        == "quiet_hours"
    )
    assert (
        apply_policy(
            speak, base_state, base_now, night_quiet, epoch_changed=False
        ).action
        is EventAction.SPEAK
    )

    active_policy = ProactivePolicy(5, 0, 0, 15)
    active_state = ConversationState(
        "default", last_user_activity_at=base_now - timedelta(minutes=5)
    )
    assert (
        apply_policy(
            speak, active_state, base_now, active_policy, epoch_changed=False
        ).reason
        == "user_active_window"
    )
    idle_state = ConversationState(
        "default", last_user_activity_at=base_now - timedelta(minutes=20)
    )
    assert (
        apply_policy(
            speak, idle_state, base_now, active_policy, epoch_changed=False
        ).action
        is EventAction.SPEAK
    )

    zero_cap = ProactivePolicy(0, 0, 0, 0)
    assert (
        apply_policy(speak, base_state, base_now, zero_cap, epoch_changed=False).reason
        == "daily_cap"
    )
    capped_same_day = ConversationState(
        "default", proactive_count_today=5, proactive_day="2026-05-01"
    )
    assert (
        apply_policy(
            speak, capped_same_day, base_now, permissive, epoch_changed=False
        ).reason
        == "daily_cap"
    )
    capped_old_day = ConversationState(
        "default", proactive_count_today=5, proactive_day="2026-04-30"
    )
    assert (
        apply_policy(
            speak, capped_old_day, base_now, permissive, epoch_changed=False
        ).action
        is EventAction.SPEAK
    )
