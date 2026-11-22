from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

EPISODE_TITLE_MAX_CHARS = 40
EPISODE_OVERVIEW_MAX_CHARS = 250
EPISODE_SUMMARY_MAX_CHARS = 2000
DEFAULT_SUMMARY_INDEX_CHAR_BUDGET = 16000
DEFAULT_SUMMARY_EVERY_MESSAGES = 6
DEFAULT_RECENT_MESSAGES_LIMIT = 20
DEFAULT_EPISODE_MAX_MESSAGES = 40
DEFAULT_EPISODE_GAP_MINUTES = 60


@dataclass(frozen=True, slots=True)
class EpisodeIndexEntry:
    episode_id: str
    title: str
    overview: str
    started_at: datetime
    updated_at: datetime
    turn_count: int


@dataclass(frozen=True, slots=True)
class LatestTopicSummary:
    episode_id: str
    title: str
    summary: str
    started_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ConversationContext:
    latest_topic_summary: LatestTopicSummary | None = None
    summary_index_entries: tuple[EpisodeIndexEntry, ...] = ()
    summary_index_char_budget: int = DEFAULT_SUMMARY_INDEX_CHAR_BUDGET


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    return f"{text[: max_chars - 1]}…" if len(text) > max_chars else text


def make_fallback_title(
    first_user_message: str, *, max_chars: int = EPISODE_TITLE_MAX_CHARS
) -> str:
    return _truncate(" ".join(first_user_message.split()) or "無題の会話", max_chars)


def normalize_summary_fields(
    title: str, overview: str, summary: str, *, fallback_title: str
) -> tuple[str, str, str]:
    return (
        make_fallback_title(title.strip() or fallback_title),
        _truncate(" ".join(overview.split()), EPISODE_OVERVIEW_MAX_CHARS),
        _truncate(summary.strip(), EPISODE_SUMMARY_MAX_CHARS),
    )


def should_rollover_episode(
    *,
    closed_at: datetime | None,
    updated_at: datetime,
    turn_count: int,
    now: datetime,
    max_messages: int = DEFAULT_EPISODE_MAX_MESSAGES,
    gap_minutes: int = DEFAULT_EPISODE_GAP_MINUTES,
) -> bool:
    return (
        closed_at is not None
        or turn_count >= max_messages
        or (now - updated_at).total_seconds() >= gap_minutes * 60
    )


def should_summarize_episode(
    *,
    turn_count: int,
    summarized_turn_count: int,
    summary_every_messages: int = DEFAULT_SUMMARY_EVERY_MESSAGES,
) -> bool:
    return turn_count - summarized_turn_count >= summary_every_messages


def format_episode_time_range(
    started_at: datetime, updated_at: datetime, *, tz: tzinfo = UTC
) -> str:
    start = started_at.astimezone(tz)
    end = updated_at.astimezone(tz)
    start_label = start.strftime("%m/%d %H:%M")
    if start.date() != end.date():
        return f"{start_label}-{end:%m/%d %H:%M}"
    if (start.hour, start.minute) == (end.hour, end.minute):
        return start_label
    return f"{start_label}-{end:%H:%M}"


def format_summary_index(
    entries: tuple[EpisodeIndexEntry, ...],
    *,
    char_budget: int = DEFAULT_SUMMARY_INDEX_CHAR_BUDGET,
    tz: tzinfo = UTC,
) -> tuple[str, int]:
    lines: list[str] = []
    used = 0
    for entry in sorted(
        entries, key=lambda item: (item.updated_at, item.episode_id), reverse=True
    ):
        period = format_episode_time_range(entry.started_at, entry.updated_at, tz=tz)
        line = f"- [{period}] (id:{entry.episode_id}) {entry.title}"
        if entry.overview:
            line += f": {entry.overview}"
        cost = len(line) + bool(lines)
        if used + cost > char_budget:
            break
        lines.append(line)
        used += cost
    return "\n".join(lines), len(entries) - len(lines)
