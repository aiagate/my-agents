from datetime import UTC, datetime, timedelta, timezone

from my_agents.domain.chat_history import (
    format_episode_time_range,
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


def test_format_time_ranges() -> None:
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
    local_time = _NOW.astimezone()
    assert format_episode_time_range(_NOW, _NOW) == f"{local_time:%m/%d %H:%M}"
