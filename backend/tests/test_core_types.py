from datetime import UTC, datetime

import pytest
from pydantic import SecretStr, ValidationError

from my_agents.core.types import (
    DEFAULT_PERSONA,
    AssembledContext,
    ChatMessage,
    ContextSnapshot,
    ConversationCounts,
    ConversationState,
    ConversationSummary,
    EventAction,
    EventDecision,
    EventEnvelope,
    EventReceipt,
    EventRecord,
    EventStatus,
    MemoryBlock,
    MemoryBlocks,
    MemorySnapshot,
    MessageOrigin,
    PolicyOutcome,
    ProactivePolicy,
    PushPayload,
    PushSubscription,
    TimelineItem,
    TimelineKind,
    TurnOutcome,
    redact_secrets,
)
from my_agents.core.types import (
    __all__ as exported,
)

_MESSAGE_ID = "01960000-0000-7000-8000-000000000001"
_REPLY_ID = "01960000-0000-7000-8000-000000000002"
_EVENT_ID = "01960000-0000-7000-8000-000000000003"


def test_memory_blocks_value_and_with_value_for_all_blocks() -> None:
    blocks = MemoryBlocks(user="tea lover", relationship="calm", arc="writing")
    assert blocks.value(MemoryBlock.USER) == "tea lover"
    assert blocks.value(MemoryBlock.RELATIONSHIP) == "calm"
    assert blocks.value(MemoryBlock.ARC) == "writing"

    updated = blocks.with_value(MemoryBlock.USER, "coffee")
    assert updated == MemoryBlocks(user="coffee", relationship="calm", arc="writing")
    updated = updated.with_value(MemoryBlock.RELATIONSHIP, "friendly")
    assert updated.relationship == "friendly"
    updated = updated.with_value(MemoryBlock.ARC, "shipping")
    assert updated.arc == "shipping"


def test_redact_secrets_masks_tokens_and_url_credentials() -> None:
    raw = "key=sk-12345 url=https://alice:topsecret@example.com/v1 empty="
    redacted = redact_secrets(
        raw,
        (SecretStr(""), SecretStr("sk-12345"), SecretStr("topsecret")),
    )
    assert "sk-12345" not in redacted
    assert "topsecret" not in redacted
    assert "https://***@example.com/v1" in redacted
    assert "key=***" in redacted


def test_redact_secrets_deduplicates_sorts_and_handles_any_scheme() -> None:
    raw = (
        "long=supersecretvalue short=secret "
        "one=postgres://u:p@db/x two=https://user:p@ss@host/x "
        "three=https://host/path"
    )
    redacted = redact_secrets(
        raw,
        (
            SecretStr("secret"),
            SecretStr("supersecretvalue"),
            SecretStr("secret"),
            SecretStr(""),
        ),
    )
    assert "supersecretvalue" not in redacted
    assert "secretvalue" not in redacted
    assert redacted.count("***") == 4
    assert "postgres://***@db/x" in redacted
    assert "https://***@host/x" in redacted
    assert "https://host/path" in redacted


def test_event_decision_validation_and_immutability() -> None:
    decision = EventDecision(
        action=EventAction.SPEAK,
        message="雨が降りそうです。",
        note="rain expected",
        reason="weather_alert",
    )
    assert decision.action is EventAction.SPEAK

    with pytest.raises(
        ValidationError, match="String should have at least 1 character"
    ):
        EventDecision(action=EventAction.IGNORE, reason="")

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EventDecision.model_validate(
            {"action": "ignore", "reason": "ok", "unexpected": True}
        )

    field_name = "reason"
    with pytest.raises(ValidationError, match="Instance is frozen"):
        setattr(decision, field_name, "mutated")


def test_snapshot_and_record_models_construct_cleanly() -> None:
    now = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    assert DEFAULT_PERSONA.name == "Sora"
    state = ConversationState(
        conversation_id="default", epoch=2, proactive_count_today=1
    )
    msg = ChatMessage(
        _MESSAGE_ID, "default", "assistant", MessageOrigin.PROACTIVE, "hi", "evt_1", now
    )
    env = EventEnvelope(
        "evt_1",
        "default",
        "alarm",
        "sensor",
        now,
        "k1",
        '{"temp":33}',
    )
    rec = EventRecord(
        event_id="evt_1",
        conversation_id="default",
        idempotency_key="k1",
        name="alarm",
        source="sensor",
        occurred_at=now,
        received_at=now,
        status=EventStatus.COMPLETED,
        action=EventAction.SPEAK,
        reason="hot",
        note="temp 33",
        payload_json="{}",
        failure_reason="",
        surfaced_at=None,
    )
    outcome = PolicyOutcome(EventAction.NOTE, "quiet_hours")
    policy = ProactivePolicy(5, 22, 7, 15)
    timeline = TimelineItem(
        TimelineKind.TURN,
        "default",
        "hi",
        now,
        _MESSAGE_ID,
        event_id="evt_1",
        origin=MessageOrigin.PROACTIVE,
    )
    reply_item = TimelineItem(
        TimelineKind.TURN,
        "default",
        "hi",
        now,
        _REPLY_ID,
        origin=MessageOrigin.REPLY,
    )
    event_item = TimelineItem(TimelineKind.EVENT, "default", "x", now, _EVENT_ID)
    turn = TurnOutcome("default", "ok", _REPLY_ID, ("evt_1",), ("append user",))
    receipt = EventReceipt("evt_1", False, EventStatus.RECEIVED)
    assembled = AssembledContext("p", "d", "e", 0)
    sparse = AssembledContext("p", "", "e", 0)
    only_prefix = AssembledContext("p", "", "", 0)
    rendered = AssembledContext(
        "p",
        "d",
        "e",
        2,
        rendered_event_ids=("evt_1", "evt_2"),
        pending_event_count=4,
    )
    counts = ConversationCounts(messages=2, events=1)
    ctx = ContextSnapshot("default", 2, "p", "a" * 64, "d", "e", 0, counts, 10)
    mem = MemorySnapshot("default", MemoryBlocks(user="u"), counts)

    assert (state.epoch, msg.message_id, rec.status) == (
        2,
        _MESSAGE_ID,
        EventStatus.COMPLETED,
    )
    assert env.payload_json == '{"temp":33}'
    assert (outcome.action, policy.daily_cap) == (EventAction.NOTE, 5)
    assert (timeline.item_id, timeline.event_id, timeline.origin) == (
        _MESSAGE_ID,
        "evt_1",
        MessageOrigin.PROACTIVE,
    )
    assert (reply_item.origin, event_item.item_id) == (
        MessageOrigin.REPLY,
        _EVENT_ID,
    )
    assert (turn.message_id, receipt.duplicate) == (_REPLY_ID, False)
    assert assembled.instructions == "p\n\nd\n\ne"
    assert sparse.instructions == "p\n\ne"
    assert only_prefix.instructions == "p"
    assert (assembled.omitted_events, ctx.memory_chars, mem.blocks.user) == (0, 10, "u")
    assert rendered.rendered_event_ids == ("evt_1", "evt_2")
    assert rendered.pending_event_count == 4


def test_conversation_summary_push_models_and_item_ids_are_immutable() -> None:
    now = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    summary = ConversationSummary(
        conversation_id="default",
        epoch=2,
        created_at=now,
        updated_at=now,
        message_count=3,
        event_count=1,
        last_message_preview="latest",
        last_message_origin=MessageOrigin.PROACTIVE,
    )
    subscription = PushSubscription(
        endpoint="https://push.example.com/1",
        p256dh="p256dh-value",
        auth=SecretStr("auth-secret"),
        created_at=now,
        conversation_id="default",
    )
    payload = PushPayload(
        conversation_id="default",
        title="Sora",
        body="hello",
        url="/?c=default",
        tag="proactive:evt_1",
        event_id="evt_1",
    )
    item = TimelineItem(TimelineKind.TURN, "default", "hello", now, item_id=_MESSAGE_ID)

    assert summary.last_message_origin is MessageOrigin.PROACTIVE
    assert subscription.conversation_id == "default"
    assert subscription.auth.get_secret_value() == "auth-secret"
    assert "auth-secret" not in repr(subscription)
    assert (payload.tag, payload.event_id) == ("proactive:evt_1", "evt_1")
    assert item.item_id == _MESSAGE_ID
    assert TimelineItem(TimelineKind.EVENT, "default", "x", now, _EVENT_ID).item_id == (
        _EVENT_ID
    )
    mutation: str = "mutated"
    field_name = "conversation_id"
    for frozen in (summary, subscription, payload, item):
        with pytest.raises(
            (AttributeError, TypeError), match="cannot assign|read-only"
        ):
            setattr(frozen, field_name, mutation)


def test_all_exports_only_public_symbols() -> None:
    module = __import__("my_agents.core.types", fromlist=["__all__"])
    assert exported == sorted(exported)
    assert "SecretStr" in exported
    assert all(not name.startswith("_") for name in exported)
    for name in exported:
        assert hasattr(module, name), name
