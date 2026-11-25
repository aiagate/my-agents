from collections.abc import Sequence
from datetime import UTC, datetime, tzinfo
from typing import Literal, assert_never

from flow_res import Err, Ok, Result

from my_agents.core.types import (
    AppError,
    AppErrorKind,
    DialogueTurn,
    Episode,
    MemoryBlock,
    MemoryBlocks,
    MemoryCandidate,
)

CORE_MEMORY_MAX_CHARS = 4096
_TITLE_MAX = 40
_OVERVIEW_MAX = 250
_SUMMARY_MAX = 2000


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def memory_char_count(blocks: MemoryBlocks) -> int:
    return len(blocks.user) + len(blocks.relationship) + len(blocks.arc)


def _checked(
    blocks: MemoryBlocks, block: MemoryBlock, value: str
) -> Result[MemoryBlocks, AppError]:
    updated = blocks.with_value(block, value)
    if memory_char_count(updated) > CORE_MEMORY_MAX_CHARS:
        return Err(
            AppError(
                AppErrorKind.VALIDATION,
                f"Core memory exceeds {CORE_MEMORY_MAX_CHARS} characters.",
            )
        )
    return Ok(updated)


def edit_memory(
    blocks: MemoryBlocks,
    action: Literal["append", "replace", "rewrite"],
    block: MemoryBlock,
    content: str,
    old_text: str = "",
) -> Result[MemoryBlocks, AppError]:
    normalized = _normalize(content)
    current = blocks.value(block)
    match action:
        case "append":
            if not normalized:
                return Err(
                    AppError(AppErrorKind.VALIDATION, "Cannot append empty text.")
                )
            combined = normalized if not current else f"{current}\n\n{normalized}"
            return _checked(blocks, block, combined)
        case "replace":
            target = _normalize(old_text)
            if not target:
                return Err(
                    AppError(AppErrorKind.VALIDATION, "Replace needs a target snippet.")
                )
            count = current.count(target)
            if count == 0:
                return Err(
                    AppError(AppErrorKind.NOT_FOUND, f"Target not found: {target!r}")
                )
            if count > 1:
                return Err(
                    AppError(
                        AppErrorKind.CONFLICT,
                        f"Target occurs {count} times; provide a unique snippet.",
                    )
                )
            return _checked(blocks, block, current.replace(target, normalized, 1))
        case "rewrite":
            if not normalized:
                return Err(
                    AppError(AppErrorKind.VALIDATION, "Cannot rewrite with empty text.")
                )
            return _checked(blocks, block, normalized)
        case _:
            assert_never(action)


def merge_candidates(
    blocks: MemoryBlocks,
    candidates: Sequence[MemoryCandidate],
) -> tuple[MemoryBlocks, tuple[str, ...], tuple[str, ...]]:
    promoted: list[str] = []
    rejected: list[str] = []
    current = blocks
    for candidate in candidates:
        text = _normalize(candidate.content)
        block_value = current.value(candidate.block)
        if not text or text in block_value:
            rejected.append(candidate.candidate_id)
            continue
        line = f"- {text}"
        combined = line if not block_value else f"{block_value}\n{line}"
        updated = _checked(current, candidate.block, combined)
        if isinstance(updated, Err):
            rejected.append(candidate.candidate_id)
            continue
        current = updated.value
        promoted.append(candidate.candidate_id)
    return current, tuple(promoted), tuple(rejected)


def should_rollover(
    episode: Episode | None,
    now: datetime,
    *,
    max_turns: int,
    gap_minutes: int,
) -> bool:
    if episode is None or episode.closed_at is not None:
        return True
    if episode.turn_count >= max_turns:
        return True
    return (now - episode.updated_at).total_seconds() >= gap_minutes * 60


def should_summarize(turn_count: int, summarized_turn_count: int, every: int) -> bool:
    return turn_count - summarized_turn_count >= every


def _truncate(text: str, limit: int) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return f"{stripped[: limit - 1]}…"


def normalize_summary(
    title: str, overview: str, summary: str, *, fallback_title: str
) -> tuple[str, str, str]:
    normalized_title = _truncate(" ".join(title.split()) or fallback_title, _TITLE_MAX)
    return (
        normalized_title,
        _truncate(" ".join(overview.split()), _OVERVIEW_MAX),
        _truncate(summary, _SUMMARY_MAX),
    )


def format_ui_time(
    started_at: datetime, updated_at: datetime, *, tz: tzinfo | None = None
) -> str:
    start = started_at.astimezone(tz)
    end = updated_at.astimezone(tz)
    label = start.strftime("%m/%d %H:%M")
    if start.date() != end.date():
        return f"{label}-{end:%m/%d %H:%M}"
    if (start.hour, start.minute) == (end.hour, end.minute):
        return label
    return f"{label}-{end:%H:%M}"


def format_prompt_date(value: datetime, *, tz: tzinfo | None = None) -> str:
    return value.astimezone(tz or UTC).strftime("%Y-%m-%d")


def build_summary_prompt(turns: Sequence[DialogueTurn], previous_summary: str) -> str:
    body = "\n".join(f"{turn.role}: {turn.content}" for turn in turns)
    return (
        "Summarize this conversation for future recall. Return title, overview, "
        "and summary.\n"
        f"existing_summary:\n{previous_summary or '(none)'}\n"
        f"new_turns:\n{body}"
    )
