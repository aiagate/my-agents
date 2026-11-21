from flow_res import is_ok

from my_agents.domain.core_profile import (
    CORE_PROFILE_MAX_CHARS,
    CoreProfile,
    CoreProfileError,
    CoreProfileErrorType,
)
from tests.helpers import assert_err


def test_max_chars_and_empty_profile() -> None:
    profile = CoreProfile.empty()

    assert CORE_PROFILE_MAX_CHARS == 4096
    assert profile == CoreProfile(content="")
    assert profile.is_empty is True
    assert profile.char_count == 0
    assert profile.to_markdown() == ""


def test_from_markdown_normalizes_newlines_and_trims() -> None:
    result = CoreProfile.from_markdown("  one\r\ntwo\rthree  ")

    assert is_ok(result)
    assert result.unwrap() == CoreProfile(content="one\ntwo\nthree")
    assert result.unwrap().to_markdown() == "one\ntwo\nthree\n"


def test_from_markdown_accepts_empty_string() -> None:
    result = CoreProfile.from_markdown("  \r\n  ")

    assert is_ok(result)
    assert result.unwrap() == CoreProfile.empty()
    assert result.unwrap().is_empty is True


def test_from_markdown_accepts_exactly_max_chars() -> None:
    raw = "a" * CORE_PROFILE_MAX_CHARS

    result = CoreProfile.from_markdown(raw)

    assert is_ok(result)
    assert result.unwrap().char_count == 4096


def test_from_markdown_rejects_over_max_chars() -> None:
    result = CoreProfile.from_markdown("a" * (CORE_PROFILE_MAX_CHARS + 1))

    error = assert_err(
        result,
        expected_type=CoreProfileErrorType.MAX_LENGTH_EXCEEDED,
        message_contains="exceeds 4096 characters",
    )
    assert isinstance(error, CoreProfileError)
    assert error.message == "Core profile exceeds 4096 characters (got 4097)."


def test_append_to_empty_profile_uses_addition_as_content() -> None:
    result = CoreProfile.empty().append("  first\r\nsecond  ")

    assert is_ok(result)
    assert result.unwrap() == CoreProfile(content="first\nsecond")
    assert result.unwrap().char_count == 12


def test_append_to_existing_profile_separates_with_blank_line() -> None:
    result = CoreProfile(content="first").append("second")

    assert is_ok(result)
    assert result.unwrap() == CoreProfile(content="first\n\nsecond")
    assert result.unwrap().to_markdown() == "first\n\nsecond\n"


def test_append_rejects_empty_addition() -> None:
    result = CoreProfile(content="first").append("  \r\n  ")

    error = assert_err(
        result,
        expected_type=CoreProfileErrorType.EMPTY_CONTENT,
        message_contains="Cannot append empty content",
    )
    assert error.message == "Cannot append empty content to the core profile."


def test_append_rejects_content_over_limit() -> None:
    result = CoreProfile.empty().append("a" * (CORE_PROFILE_MAX_CHARS + 1))

    error = assert_err(
        result,
        expected_type=CoreProfileErrorType.MAX_LENGTH_EXCEEDED,
        message_contains="got 4097",
    )
    assert error.message == "Core profile exceeds 4096 characters (got 4097)."


def test_replace_requires_non_blank_target() -> None:
    result = CoreProfile(content="first").replace("  \r\n ", "new")

    error = assert_err(
        result,
        expected_type=CoreProfileErrorType.INVALID_OPERATION,
        message_contains="must not be empty or whitespace only",
    )
    assert error.message == ("Replacement target must not be empty or whitespace only.")


def test_replace_reports_missing_target() -> None:
    result = CoreProfile(content="first\nsecond").replace("third", "new")

    error = assert_err(
        result,
        expected_type=CoreProfileErrorType.TARGET_NOT_FOUND,
        message_contains="was not found: 'third'",
    )
    assert error.message == "Replacement target was not found: 'third'"


def test_replace_reports_ambiguous_target() -> None:
    result = CoreProfile(content="same\nsame").replace("same", "new")

    error = assert_err(
        result,
        expected_type=CoreProfileErrorType.AMBIGUOUS_TARGET,
        message_contains="occurs 2 times",
    )
    assert error.message == (
        "Replacement target occurs 2 times; provide a unique target."
    )


def test_replace_normalizes_newlines_in_target_and_replacement() -> None:
    profile = CoreProfile(content="first\nsecond\nthird")

    result = profile.replace("first\r\nsecond", "alpha\r\nbeta")

    assert is_ok(result)
    assert result.unwrap() == CoreProfile(content="alpha\nbeta\nthird")


def test_replace_allows_deletion_with_empty_replacement() -> None:
    profile = CoreProfile(content="first\nsecond\nthird")

    result = profile.replace("second\n", "")

    assert is_ok(result)
    assert result.unwrap() == CoreProfile(content="first\nthird")


def test_replace_rejects_result_over_limit() -> None:
    profile = CoreProfile(content="short")

    result = profile.replace("short", "a" * (CORE_PROFILE_MAX_CHARS + 1))

    error = assert_err(
        result,
        expected_type=CoreProfileErrorType.MAX_LENGTH_EXCEEDED,
        message_contains="got 4097",
    )
    assert error.message == "Core profile exceeds 4096 characters (got 4097)."


def test_rewrite_normalizes_and_replaces_content() -> None:
    profile = CoreProfile(content="old")

    result = profile.rewrite("  new\r\ncontent  ")

    assert is_ok(result)
    assert result.unwrap() == CoreProfile(content="new\ncontent")
    assert result.unwrap().char_count == 11


def test_rewrite_rejects_empty_content() -> None:
    result = CoreProfile(content="old").rewrite("  \r\n  ")

    error = assert_err(
        result,
        expected_type=CoreProfileErrorType.EMPTY_CONTENT,
        message_contains="Cannot rewrite",
    )
    assert error.message == "Cannot rewrite the core profile with empty content."


def test_rewrite_rejects_content_over_limit() -> None:
    result = CoreProfile(content="old").rewrite("a" * (CORE_PROFILE_MAX_CHARS + 1))

    error = assert_err(
        result,
        expected_type=CoreProfileErrorType.MAX_LENGTH_EXCEEDED,
        message_contains="got 4097",
    )
    assert error.message == "Core profile exceeds 4096 characters (got 4097)."
