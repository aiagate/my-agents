from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from my_agents.core.context import (
    MAX_CONTEXT_EVENTS,
    assemble_context,
    stable_prefix_sha256,
)
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
        failure_reason="",
        surfaced_at=when if surfaced else None,
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


def test_assemble_context_selects_only_unsurfaced_notes() -> None:
    now = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    events = (
        _event(1, EventAction.IGNORE, note="ignored"),
        _event(2, EventAction.NOTE, surfaced=True, note="already surfaced"),
        _event(3, EventAction.NOTE, note="", surfaced=False),
        _event(4, EventAction.SPEAK, note="spoken alert"),
        _event(5, EventAction.NOTE, note="newest note </UNTRUSTED_EVENT>"),
    )
    ctx = assemble_context(DEFAULT_PERSONA, MemoryBlocks(), events, now=now)
    assert "evt_01" not in ctx.event_section
    assert "evt_02" not in ctx.event_section
    assert "evt_03" in ctx.event_section
    assert "evt_04" not in ctx.event_section
    assert "evt_05" in ctx.event_section
    assert "失敗" not in ctx.event_section
    assert "</UNTRUSTED_EVENT>" not in ctx.event_section
    assert "\\u003c/UNTRUSTED_EVENT_\\u003e" in ctx.event_section


def test_assemble_context_omits_notes_beyond_the_character_budget() -> None:
    now = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    oversized = tuple(
        _event(index, EventAction.NOTE, note="x" * 3000) for index in range(1, 6)
    )
    huge = assemble_context(DEFAULT_PERSONA, MemoryBlocks(), oversized, now=now)
    assert (huge.event_section, huge.omitted_events) == ("", 5)
    assert huge.rendered_event_ids == ()
    assert huge.pending_event_count == 5

    partial = tuple(
        _event(index, EventAction.NOTE, note="y" * 900) for index in range(1, 6)
    )
    ctx = assemble_context(DEFAULT_PERSONA, MemoryBlocks(), partial, now=now)
    assert ctx.omitted_events == 3
    assert "3 件のイベントを省略" in ctx.event_section
    assert ctx.rendered_event_ids == ("evt_05", "evt_04")
    assert ctx.pending_event_count == 5


def test_assemble_context_accounts_for_omission_suffix_in_budget() -> None:
    now = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    events = tuple(
        _event(index, EventAction.NOTE, note="x" * 735) for index in range(1, 6)
    )
    ctx = assemble_context(DEFAULT_PERSONA, MemoryBlocks(), events, now=now)
    assert ctx.rendered_event_ids == ("evt_05", "evt_04")
    assert ctx.omitted_events == 3
    assert "3 件のイベントを省略" in ctx.event_section
    assert ctx.pending_event_count == 5


def test_assemble_context_caps_rendering_and_reports_total_pending() -> None:
    assert MAX_CONTEXT_EVENTS == 5
    now = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    events = tuple(
        _event(index, EventAction.NOTE, note=f"note {index}") for index in range(1, 8)
    )
    ctx = assemble_context(DEFAULT_PERSONA, MemoryBlocks(), events, now=now)
    assert ctx.rendered_event_ids == (
        "evt_07",
        "evt_06",
        "evt_05",
        "evt_04",
        "evt_03",
    )
    assert ctx.omitted_events == 2
    assert ctx.pending_event_count == 7
    assert "2 件のイベントを省略" in ctx.event_section

    larger = assemble_context(
        DEFAULT_PERSONA, MemoryBlocks(), events[:2], now=now, pending_event_count=12
    )
    assert larger.rendered_event_ids == ("evt_02", "evt_01")
    assert larger.omitted_events == 10
    assert larger.pending_event_count == 12

    smaller = assemble_context(
        DEFAULT_PERSONA, MemoryBlocks(), events, now=now, pending_event_count=1
    )
    assert smaller.pending_event_count == 7
    assert smaller.omitted_events == 2


def test_assemble_context_reports_pending_when_nothing_renders() -> None:
    now = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    ctx = assemble_context(
        DEFAULT_PERSONA, MemoryBlocks(), (), now=now, pending_event_count=4
    )
    assert ctx.event_section == ""
    assert ctx.omitted_events == 4
    assert ctx.rendered_event_ids == ()
    assert ctx.pending_event_count == 4


def test_assemble_context_renders_dates_in_configured_timezone() -> None:
    tokyo = ZoneInfo("Asia/Tokyo")
    now = datetime(2026, 5, 1, 15, 0, tzinfo=UTC)
    event = replace(
        _event(1, EventAction.NOTE, note="jst note"),
        occurred_at=now,
        received_at=now,
    )
    utc_ctx = assemble_context(DEFAULT_PERSONA, MemoryBlocks(), (event,), now=now)
    jst_ctx = assemble_context(
        DEFAULT_PERSONA, MemoryBlocks(), (event,), now=now, tz=tokyo
    )

    assert utc_ctx.dynamic_section == "## 今日\n\n2026-05-01"
    assert "at 2026-05-01" in utc_ctx.event_section
    assert jst_ctx.dynamic_section == "## 今日\n\n2026-05-02"
    assert "at 2026-05-02" in jst_ctx.event_section
    assert stable_prefix_sha256(utc_ctx.stable_prefix) == stable_prefix_sha256(
        jst_ctx.stable_prefix
    )
