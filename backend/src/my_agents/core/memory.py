from datetime import UTC, datetime, tzinfo
from typing import Literal, assert_never

from flow_res import Err, Ok, Result

from my_agents.core.types import AppError, AppErrorKind, MemoryBlock, MemoryBlocks

CORE_MEMORY_MAX_CHARS = 4096


def _error(message: str, *, kind: AppErrorKind = AppErrorKind.VALIDATION) -> AppError:
    return AppError(kind=kind, message=message)


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
                return Err(_error("Cannot append empty text."))
            combined = normalized if not current else f"{current}\n\n{normalized}"
            return _checked(blocks, block, combined)
        case "replace":
            target = _normalize(old_text)
            if not target:
                return Err(_error("Replace needs a target snippet."))
            count = current.count(target)
            if count == 0:
                return Err(
                    _error(f"Target not found: {target!r}", kind=AppErrorKind.NOT_FOUND)
                )
            if count > 1:
                return Err(
                    _error(
                        f"Target occurs {count} times; provide a unique snippet.",
                        kind=AppErrorKind.CONFLICT,
                    )
                )
            return _checked(blocks, block, current.replace(target, normalized, 1))
        case "rewrite":
            if not normalized:
                return Err(_error("Cannot rewrite with empty text."))
            return _checked(blocks, block, normalized)
        case _:
            assert_never(action)


def format_prompt_date(value: datetime, tz: tzinfo = UTC) -> str:
    return value.astimezone(tz).strftime("%Y-%m-%d")
