from datetime import UTC, datetime, timedelta, timezone

from my_agents.core.memory import (
    CORE_MEMORY_MAX_CHARS,
    build_summary_prompt,
    edit_memory,
    format_prompt_date,
    format_ui_time,
    memory_char_count,
    merge_candidates,
    normalize_summary,
    should_rollover,
    should_summarize,
)
from my_agents.core.types import (
    AppErrorKind,
    DialogueTurn,
    Episode,
    MemoryBlock,
    MemoryBlocks,
    MemoryCandidate,
    TurnOrigin,
)
from tests.helpers import assert_err

_NOW = datetime(2026, 11, 22, 12, 30, tzinfo=UTC)


def _candidate(
    content: str, cid: str = "m", block: MemoryBlock = MemoryBlock.USER
) -> MemoryCandidate:
    return MemoryCandidate(cid, "c", block, content, "r", _NOW)


def _episode(
    *,
    turn_count: int = 2,
    updated_at: datetime = _NOW,
    closed_at: datetime | None = None,
) -> Episode:
    return Episode("e", "c", "t", "", "", _NOW, updated_at, turn_count, 0, closed_at)


def test_edit_memory_append_handles_empty_and_existing_blocks() -> None:
    first = edit_memory(MemoryBlocks(), "append", MemoryBlock.USER, " hello ")
    assert first.unwrap().user == "hello"
    second = edit_memory(first.unwrap(), "append", MemoryBlock.USER, "world")
    assert second.unwrap().user == "hello\n\nworld"


def test_edit_memory_append_rejects_empty_text() -> None:
    assert_err(
        edit_memory(MemoryBlocks(), "append", MemoryBlock.USER, "  "),
        kind=AppErrorKind.VALIDATION,
        message_contains="empty",
    )


def test_edit_memory_replace_requires_unique_target() -> None:
    base = MemoryBlocks(user="alpha beta alpha")
    assert_err(
        edit_memory(base, "replace", MemoryBlock.USER, "x"),
        kind=AppErrorKind.VALIDATION,
        message_contains="target",
    )
    assert_err(
        edit_memory(base, "replace", MemoryBlock.USER, "x", "missing"),
        kind=AppErrorKind.NOT_FOUND,
        message_contains="not found",
    )
    assert_err(
        edit_memory(base, "replace", MemoryBlock.USER, "x", "alpha"),
        kind=AppErrorKind.CONFLICT,
        message_contains="2 times",
    )
    assert edit_memory(
        base, "replace", MemoryBlock.USER, "x", "beta"
    ).unwrap().user == ("alpha x alpha")


def test_edit_memory_rewrite_and_limits() -> None:
    assert_err(
        edit_memory(MemoryBlocks(), "rewrite", MemoryBlock.ARC, " "),
        kind=AppErrorKind.VALIDATION,
        message_contains="empty",
    )
    assert (
        edit_memory(MemoryBlocks(), "rewrite", MemoryBlock.ARC, " arc ").unwrap().arc
        == "arc"
    )
    huge = "x" * (CORE_MEMORY_MAX_CHARS + 1)
    assert_err(
        edit_memory(MemoryBlocks(), "append", MemoryBlock.USER, huge),
        kind=AppErrorKind.VALIDATION,
        message_contains=str(CORE_MEMORY_MAX_CHARS),
    )


def test_memory_char_count_sums_blocks() -> None:
    assert memory_char_count(MemoryBlocks("a", "bb", "ccc")) == 6


def test_merge_candidates_skips_duplicates_and_limit_overflows() -> None:
    blocks = MemoryBlocks(user="- known")
    promoted, rejected = merge_candidates(
        blocks,
        (
            _candidate("- known"),
            _candidate(""),
            _candidate("new", "n"),
            _candidate("new", "dup"),
            _candidate("x" * (CORE_MEMORY_MAX_CHARS + 1), "big"),
        ),
    )[1:]
    assert promoted == ("n",)
    assert rejected == ("m", "m", "dup", "big")
    merged = merge_candidates(blocks, (_candidate("new", "n"),))[0]
    assert merged.user == "- known\n- new"


def test_should_rollover_checks_closed_turns_and_gap() -> None:
    assert should_rollover(None, _NOW, max_turns=3, gap_minutes=10)
    assert should_rollover(_episode(closed_at=_NOW), _NOW, max_turns=3, gap_minutes=10)
    assert should_rollover(_episode(turn_count=3), _NOW, max_turns=3, gap_minutes=10)
    assert should_rollover(
        _episode(updated_at=_NOW - timedelta(minutes=10)),
        _NOW,
        max_turns=5,
        gap_minutes=10,
    )
    assert not should_rollover(
        _episode(turn_count=1), _NOW, max_turns=5, gap_minutes=10
    )


def test_should_summarize_uses_turn_delta() -> None:
    assert should_summarize(6, 0, 6)
    assert not should_summarize(5, 0, 6)


def test_normalize_summary_truncates_and_falls_back() -> None:
    title, overview, summary = normalize_summary(
        "  ", " a  b ", "x" * 2100, fallback_title="fallback"
    )
    assert title == "fallback"
    assert overview == "a b"
    assert len(summary) == 2000
    assert normalize_summary("t", "o", "s", fallback_title="f") == ("t", "o", "s")


def test_format_ui_time_covers_same_minute_same_day_and_cross_day() -> None:
    assert format_ui_time(_NOW, _NOW, tz=UTC) == "11/22 12:30"
    assert format_ui_time(_NOW, _NOW + timedelta(minutes=5), tz=UTC) == (
        "11/22 12:30-12:35"
    )
    assert format_ui_time(_NOW, _NOW + timedelta(days=1), tz=UTC) == (
        "11/22 12:30-11/23 12:30"
    )
    tokyo = timezone(timedelta(hours=9))
    assert format_ui_time(_NOW, _NOW, tz=tokyo) == "11/22 21:30"


def test_format_prompt_date_uses_date_only() -> None:
    assert format_prompt_date(_NOW) == "2026-11-22"


def test_build_summary_prompt_includes_previous_and_new_turns() -> None:
    turns = (DialogueTurn("c", "e", 1, 1, "user", TurnOrigin.USER, "hi", None, _NOW),)
    prompt = build_summary_prompt(turns, "")
    assert "existing_summary:\n(none)" in prompt
    assert "user: hi" in prompt
    assert "previous" in build_summary_prompt((), "previous")
