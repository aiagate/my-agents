from datetime import UTC, datetime

from flow_res import Ok

from my_agents.core.memory import (
    CORE_MEMORY_MAX_CHARS,
    edit_memory,
    format_prompt_date,
    memory_char_count,
)
from my_agents.core.types import AppErrorKind, MemoryBlock, MemoryBlocks
from tests.helpers import assert_err


def test_edit_memory_append_replace_rewrite_and_char_count() -> None:
    initial = MemoryBlocks()
    assert memory_char_count(initial) == 0

    first = edit_memory(initial, "append", MemoryBlock.USER, "likes green tea\r\n")
    assert isinstance(first, Ok)
    assert first.value.user == "likes green tea"

    second = edit_memory(first.value, "append", MemoryBlock.USER, "lives in Tokyo")
    assert isinstance(second, Ok)
    assert second.value.user == "likes green tea\n\nlives in Tokyo"

    replaced = edit_memory(
        second.value,
        "replace",
        MemoryBlock.USER,
        "likes roasted tea",
        old_text="likes green tea",
    )
    assert isinstance(replaced, Ok)
    assert replaced.value.user == "likes roasted tea\n\nlives in Tokyo"

    rewritten = edit_memory(
        replaced.value, "rewrite", MemoryBlock.RELATIONSHIP, "respectful partner"
    )
    assert isinstance(rewritten, Ok)
    assert memory_char_count(rewritten.value) == (
        len("likes roasted tea\n\nlives in Tokyo") + len("respectful partner")
    )


def test_edit_memory_rejects_empty_missing_ambiguous_and_oversized_inputs() -> None:
    blocks = MemoryBlocks(user="repeat item\n\nrepeat item")

    assert_err(
        edit_memory(blocks, "append", MemoryBlock.ARC, "   \r\n "),
        kind=AppErrorKind.VALIDATION,
        message_contains="empty text",
    )
    assert_err(
        edit_memory(blocks, "replace", MemoryBlock.USER, "new", old_text="  "),
        kind=AppErrorKind.VALIDATION,
        message_contains="target snippet",
    )
    assert_err(
        edit_memory(blocks, "replace", MemoryBlock.USER, "new", old_text="missing"),
        kind=AppErrorKind.NOT_FOUND,
        message_contains="Target not found",
    )
    assert_err(
        edit_memory(blocks, "replace", MemoryBlock.USER, "new", old_text="repeat item"),
        kind=AppErrorKind.CONFLICT,
        message_contains="2 times",
    )
    assert_err(
        edit_memory(blocks, "rewrite", MemoryBlock.USER, "  "),
        kind=AppErrorKind.VALIDATION,
        message_contains="empty text",
    )
    assert_err(
        edit_memory(
            MemoryBlocks(),
            "rewrite",
            MemoryBlock.ARC,
            "x" * (CORE_MEMORY_MAX_CHARS + 1),
        ),
        kind=AppErrorKind.VALIDATION,
        message_contains=str(CORE_MEMORY_MAX_CHARS),
    )


def test_format_prompt_date_uses_utc() -> None:
    value = datetime(2026, 5, 1, 20, 0, tzinfo=UTC)
    assert format_prompt_date(value) == "2026-05-01"
