from datetime import UTC, datetime

import pytest
from pydantic import SecretStr, ValidationError

from my_agents.core.types import (
    DEFAULT_PERSONA,
    AppError,
    AppErrorKind,
    CharacterPersona,
    ConversationState,
    DeliveryMode,
    DialogueTurn,
    Episode,
    EventBrief,
    EventDelivery,
    EventEnvelope,
    EventRecord,
    EventStatus,
    MemoryBlock,
    MemoryBlocks,
    MemoryCandidate,
    MemoryStatus,
    MergeDecision,
    ProactivePolicy,
    TimelineItem,
    TimelineKind,
    TurnOrigin,
)

_NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)


def test_app_error_carries_kind_message_and_detail() -> None:
    error = AppError(AppErrorKind.VALIDATION, "bad", "context")
    assert error.kind is AppErrorKind.VALIDATION
    assert (error.message, error.detail) == ("bad", "context")


def test_secret_str_is_reexported() -> None:
    assert SecretStr("secret").get_secret_value() == "secret"


@pytest.mark.parametrize(
    ("block", "value"),
    [
        (MemoryBlock.USER, "u"),
        (MemoryBlock.RELATIONSHIP, "r"),
        (MemoryBlock.ARC, "a"),
    ],
)
def test_memory_blocks_read_and_replace_each_block(
    block: MemoryBlock, value: str
) -> None:
    blocks = MemoryBlocks(user="u", relationship="r", arc="a")
    assert blocks.value(block) == value
    assert blocks.with_value(block, "new").value(block) == "new"


def test_persona_defaults_are_available() -> None:
    assert DEFAULT_PERSONA.name == "Sora"
    assert "UNTRUSTED" in DEFAULT_PERSONA.boundaries or "未信頼" in (
        DEFAULT_PERSONA.boundaries
    )


def test_event_brief_is_frozen_and_forbids_extra_fields() -> None:
    brief = EventBrief(
        headline="h",
        facts=("f",),
        impact="i",
        suggested_angle="s",
        urgency=8,
        relevance=8,
        confidence=0.9,
        requires_user_action=True,
        sensitive=False,
        dedupe_key="k",
    )
    assert brief.headline == "h"
    with pytest.raises(ValidationError):
        EventBrief.model_validate({**brief.model_dump(), "unexpected": 1})
    with pytest.raises(ValidationError):
        EventBrief.model_validate({**brief.model_dump(), "urgency": 11})


def test_value_objects_keep_their_fields() -> None:
    state = ConversationState("c", 2, _NOW, None, 1, "2026-11-22")
    episode = Episode("e", "c", "t", "o", "s", _NOW, _NOW, 2, 1, None)
    turn = DialogueTurn("c", "e", 1, 1, "user", TurnOrigin.USER, "hi", None, _NOW)
    envelope = EventEnvelope("evt_1", "c", "name", "src", _NOW, "key", {"x": 1})
    record = EventRecord(
        "evt_1",
        "c",
        "key",
        "name",
        "src",
        _NOW,
        _NOW,
        EventStatus.COMPLETED,
        EventDelivery.AMBIENT,
        "reason",
        "{}",
        None,
        "",
        None,
        None,
    )
    candidate = MemoryCandidate("m", "c", MemoryBlock.USER, "x", "r", _NOW)
    item = TimelineItem(TimelineKind.TURN, "c", "hi", _NOW)
    assert (state.epoch, episode.turn_count, turn.turn_index) == (2, 2, 1)
    assert (envelope.data["x"], record.status, candidate.status) == (
        1,
        EventStatus.COMPLETED,
        MemoryStatus.PENDING,
    )
    assert (item.delivery, MergeDecision(EventDelivery.SILENT, "r").reason) == (
        DeliveryMode.REPLY,
        "r",
    )
    assert ProactivePolicy(1, 2, 3, 4, 5).min_urgency == 6
    assert isinstance(DEFAULT_PERSONA, CharacterPersona)
