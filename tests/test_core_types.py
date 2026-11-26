from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from my_agents.core.types import (
    DEFAULT_PERSONA,
    AssembledContext,
    ChatMessage,
    ContextSnapshot,
    ConversationCounts,
    ConversationState,
    DeliveryMode,
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
    TimelineItem,
    TimelineKind,
    TurnOutcome,
    redact_secrets,
)


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
    redacted = redact_secrets(raw, ("", "sk-12345", "topsecret"))
    assert "sk-12345" not in redacted
    assert "topsecret" not in redacted
    assert "https://***@example.com/v1" in redacted
    assert "key=***" in redacted


def test_event_decision_validation_and_immutability() -> None:
    decision = EventDecision(
        action=EventAction.SPEAK,
        message="雨が降りそうです。",
        note="rain expected",
        reason="weather_alert",
    )
    assert decision.action is EventAction.SPEAK

    with pytest.raises(ValidationError):
        EventDecision(action=EventAction.IGNORE, reason="")

    with pytest.raises(ValidationError):
        EventDecision.model_validate(
            {"action": "ignore", "reason": "ok", "unexpected": True}
        )

    field_name = "reason"
    with pytest.raises(ValidationError):
        setattr(decision, field_name, "mutated")


def test_snapshot_and_record_models_construct_cleanly() -> None:
    now = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    assert DEFAULT_PERSONA.name == "Sora"
    state = ConversationState(
        conversation_id="default", epoch=2, proactive_count_today=1
    )
    msg = ChatMessage(
        1, "default", "assistant", MessageOrigin.PROACTIVE, "hi", "evt_1", now
    )
    env = EventEnvelope("evt_1", "default", "alarm", "sensor", now, "k1", {"temp": 33})
    rec = EventRecord(
        "evt_1",
        "default",
        "k1",
        "alarm",
        "sensor",
        now,
        now,
        EventStatus.COMPLETED,
        EventAction.SPEAK,
        "hot",
        "temp 33",
        "{}",
        "",
        None,
        msg.message_id,
    )
    outcome = PolicyOutcome(EventAction.NOTE, "quiet_hours")
    policy = ProactivePolicy(5, 22, 7, 15)
    timeline = TimelineItem(
        TimelineKind.TURN,
        "default",
        "hi",
        now,
        "evt_1",
        "evt_1",
        MessageOrigin.PROACTIVE,
        EventStatus.COMPLETED,
        EventAction.SPEAK,
        "hot",
    )
    reply_item = TimelineItem(
        TimelineKind.TURN,
        "default",
        "hi",
        now,
        origin=MessageOrigin.REPLY,
    )
    event_item = TimelineItem(TimelineKind.EVENT, "default", "x", now)
    turn = TurnOutcome("default", "ok", 2, ("evt_1",), ("append user",))
    receipt = EventReceipt("evt_1", False, EventStatus.RECEIVED)
    assembled = AssembledContext("p", "d", "e", 0)
    sparse = AssembledContext("p", "", "e", 0)
    only_prefix = AssembledContext("p", "", "", 0)
    counts = ConversationCounts(messages=2, events=1)
    ctx = ContextSnapshot("default", 2, "p", "a" * 64, "d", "e", 0, counts, 10)
    mem = MemorySnapshot("default", MemoryBlocks(user="u"), counts)

    assert state.epoch == 2
    assert env.data["temp"] == 33
    assert rec.linked_message_id == 1
    assert (outcome.action, policy.daily_cap) == (EventAction.NOTE, 5)
    assert (timeline.delivery, turn.message_id, receipt.duplicate) == (
        DeliveryMode.PUSH,
        2,
        False,
    )
    assert reply_item.delivery is DeliveryMode.REPLY
    assert event_item.delivery is DeliveryMode.REPLY
    assert assembled.instructions == "p\n\nd\n\ne"
    assert sparse.instructions == "p\n\ne"
    assert only_prefix.instructions == "p"
    assert (assembled.omitted_events, ctx.memory_chars, mem.blocks.user) == (0, 10, "u")
