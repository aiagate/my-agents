from enum import Enum
from typing import cast, overload

from flow_res import Result, is_err

from my_agents.contracts.errors import AppError, DisplayableError, TypedError


@overload
def assert_err[T, E: TypedError[Enum]](
    result: Result[T, E],
    *,
    expected_type: Enum,
    message_contains: str,
    display_message_contains: str | None = None,
) -> E: ...


@overload
def assert_err[T, E: AppError](
    result: Result[T, E],
    *,
    message_contains: str,
    display_message_contains: str | None = None,
) -> E: ...


def _assert_expected_type(error: AppError, expected_type: Enum) -> None:
    assert isinstance(error, TypedError), (
        f"Expected a TypedError with '.type', got {error!r}"
    )
    typed = cast(TypedError[Enum], error)
    assert typed.type is expected_type, (
        f"Expected error type {expected_type!r}, got {typed.type!r}"
    )


def _assert_display_message(
    error: AppError,
    display_message_contains: str,
) -> None:
    assert isinstance(error, DisplayableError), (
        f"Expected a DisplayableError with '.display_message', got {error!r}"
    )
    assert display_message_contains in error.display_message, (
        f"Expected {display_message_contains!r} in display_message "
        f"{error.display_message!r}"
    )


def assert_err[T, E: AppError](
    result: Result[T, E],
    *,
    expected_type: Enum | None = None,
    message_contains: str,
    display_message_contains: str | None = None,
) -> E:
    assert is_err(result), f"Expected an Err result, got {result!r}"
    error = result.error
    if expected_type is not None:
        _assert_expected_type(error, expected_type)
    assert message_contains in error.message, (
        f"Expected {message_contains!r} in error message {error.message!r}"
    )
    if display_message_contains is not None:
        _assert_display_message(error, display_message_contains)
    return error
