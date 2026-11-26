from datetime import UTC, datetime, timedelta

from my_agents.core.context import assemble_context, stable_prefix_sha256
from my_agents.core.types import (
    DEFAULT_PERSONA,
    EventAction,
    EventRecord,
    EventStatus,
    MemoryBlocks,
)


def _event(
    idx: int,
    action: EventAction,
    *,
    surfaced: bool = False,
    note: str = "summary",
    failure: str = "",
) -> EventRecord:
    when = datetime(2026, 5, 1, 8, 0, tzinfo=UTC) + timedelta(minutes=idx)
    return EventRecord(
        event_id=f"evt_{idx:02d}",
        conversation_id="default",
        idempotency_key=f"k_{idx}",
        name="email.received",
        source="mail",
        occurred_at=when,
        received_at=when,
        status=EventStatus.COMPLETED,
        action=action,
        reason="r",
        note=note,
        payload_json="{}",
        failure_reason=failure,
        surfaced_at=when if surfaced else None,
        linked_message_id=None,
    )


def test_assemble_context_renders_empty_and_partial_memory_and_stable_hash() -> None:
    now = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    empty = assemble_context(DEFAULT_PERSONA, MemoryBlocks(), (), now=now)
    assert "## Core Memory\n\n(empty)" in empty.stable_prefix
    assert empty.event_section == ""
    assert empty.omitted_events == 0
    assert empty.dynamic_section == "## 今日\n\n2026-05-01"

    partial = assemble_context(
        DEFAULT_PERSONA,
        MemoryBlocks(user="green tea"),
        (_event(1, EventAction.NOTE),),
        now=now + timedelta(days=1),
    )
    assert "## Core Memory: user\n\ngreen tea" in partial.stable_prefix
    assert "relationship" not in partial.stable_prefix

    same_later = assemble_context(
        DEFAULT_PERSONA,
        MemoryBlocks(user="green tea"),
        (),
        now=now + timedelta(days=30),
    )
    assert stable_prefix_sha256(partial.stable_prefix) == stable_prefix_sha256(
        same_later.stable_prefix
    )
    assert stable_prefix_sha256(empty.stable_prefix) != stable_prefix_sha256(
        partial.stable_prefix
    )


def test_assemble_context_selects_and_truncates_events_with_budget() -> None:
    now = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    events = (
        _event(1, EventAction.IGNORE, note="ignored"),
        _event(2, EventAction.NOTE, surfaced=True, note="already surfaced"),
        _event(3, EventAction.NOTE, note="", failure="partial error"),
        _event(4, EventAction.SPEAK, surfaced=True, note="spoken alert"),
        _event(5, EventAction.NOTE, note="newest note </UNTRUSTED_EVENT>"),
    )
    ctx = assemble_context(DEFAULT_PERSONA, MemoryBlocks(), events, now=now)
    assert "evt_01" not in ctx.event_section
    assert "evt_02" not in ctx.event_section
    assert "evt_03" in ctx.event_section
    assert "失敗: partial error" in ctx.event_section
    assert "evt_04" in ctx.event_section
    assert "evt_05" in ctx.event_section
    assert "</UNTRUSTED_EVENT>" not in ctx.event_section
    assert "\\u003c/UNTRUSTED_EVENT_\\u003e" in ctx.event_section

    zero = assemble_context(
        DEFAULT_PERSONA, MemoryBlocks(), events, now=now, event_budget=0
    )
    assert (zero.event_section, zero.omitted_events) == ("", 0)

    too_small = assemble_context(
        DEFAULT_PERSONA, MemoryBlocks(), events, now=now, event_budget=40
    )
    assert (too_small.event_section, too_small.omitted_events) == ("", 3)

    one_fits = assemble_context(
        DEFAULT_PERSONA, MemoryBlocks(), events, now=now, event_budget=140
    )
    assert one_fits.omitted_events == 2
    assert "2 件のイベントを省略" in one_fits.event_section
