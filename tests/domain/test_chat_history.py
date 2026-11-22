from datetime import UTC, datetime, timedelta, timezone

from my_agents.domain.chat_history import (
    EpisodeIndexEntry,
    format_episode_time_range,
    format_summary_index,
    make_fallback_title,
    normalize_summary_fields,
    should_rollover_episode,
    should_summarize_episode,
)

_NOW = datetime(2026, 11, 22, 18, tzinfo=UTC)


def test_normalize_titles_and_summaries() -> None:
    assert make_fallback_title("  \n\t") == "無題の会話"
    assert make_fallback_title(" hello\nworld ", max_chars=8) == "hello w…"
    assert make_fallback_title("hi", max_chars=0) == ""
    assert normalize_summary_fields(
        "", "概要\n  内容", "\n- 一\n- 二\n", fallback_title=" 題名 "
    ) == ("題名", "概要 内容", "- 一\n- 二")
    assert normalize_summary_fields(
        "a" * 41, "b" * 251, "c" * 2001, fallback_title="ignored"
    ) == ("a" * 39 + "…", "b" * 249 + "…", "c" * 1999 + "…")


def test_rollover_and_summary_thresholds() -> None:
    assert should_rollover_episode(
        closed_at=_NOW, updated_at=_NOW, now=_NOW, turn_count=0
    )
    assert should_rollover_episode(
        closed_at=None, updated_at=_NOW, now=_NOW, turn_count=40
    )
    assert should_rollover_episode(
        closed_at=None, updated_at=_NOW, now=_NOW + timedelta(hours=1), turn_count=39
    )
    assert not should_rollover_episode(
        closed_at=None, updated_at=_NOW, now=_NOW + timedelta(minutes=59), turn_count=39
    )
    assert should_summarize_episode(turn_count=8, summarized_turn_count=2)
    assert not should_summarize_episode(turn_count=7, summarized_turn_count=2)


def test_format_time_ranges_and_budgeted_index() -> None:
    assert (
        format_episode_time_range(_NOW, _NOW + timedelta(seconds=30), tz=UTC)
        == "11/22 18:00"
    )
    assert (
        format_episode_time_range(_NOW, _NOW + timedelta(minutes=5), tz=UTC)
        == "11/22 18:00-18:05"
    )
    assert (
        format_episode_time_range(_NOW, _NOW + timedelta(hours=7), tz=UTC)
        == "11/22 18:00-11/23 01:00"
    )
    assert (
        format_episode_time_range(_NOW, _NOW, tz=timezone(timedelta(hours=9)))
        == "11/23 03:00"
    )
    entries = (
        EpisodeIndexEntry(
            "older", "Old", "", _NOW - timedelta(days=1), _NOW - timedelta(days=1), 2
        ),
        EpisodeIndexEntry("newer", "New", "Research", _NOW, _NOW, 4),
    )
    newer = "- [11/22 18:00] (id:newer) New: Research"
    older = "- [11/21 18:00] (id:older) Old"
    all_lines = f"{newer}\n{older}"
    assert format_summary_index(entries, char_budget=len(all_lines), tz=UTC) == (
        all_lines,
        0,
    )
    assert format_summary_index(entries, char_budget=len(all_lines) - 1, tz=UTC) == (
        newer,
        1,
    )
    assert format_summary_index(entries, char_budget=1) == ("", 2)
    assert format_summary_index(()) == ("", 0)


def test_formats_time_range_and_index_in_system_timezone_by_default() -> None:
    local_time = _NOW.astimezone()
    entry = EpisodeIndexEntry("local", "Local time", "", _NOW, _NOW, 2)
    assert format_episode_time_range(_NOW, _NOW) == f"{local_time:%m/%d %H:%M}"
    assert format_summary_index((entry,)) == (
        f"- [{local_time:%m/%d %H:%M}] (id:local) Local time",
        0,
    )


def test_summary_index_keeps_recency_order_for_equal_timestamps() -> None:
    entries = (
        EpisodeIndexEntry("a-second", "Second", "", _NOW, _NOW, 2),
        EpisodeIndexEntry("z-first", "First", "", _NOW, _NOW, 2),
    )
    assert format_summary_index(entries, tz=UTC) == (
        "- [11/22 18:00] (id:a-second) Second\n- [11/22 18:00] (id:z-first) First",
        0,
    )
