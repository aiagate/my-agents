from flow_res import Result, is_err

from my_agents.core.types import AppError, AppErrorKind


def assert_err[T, E: AppError](
    result: Result[T, E],
    *,
    kind: AppErrorKind,
    message_contains: str,
) -> E:
    assert is_err(result), f"Expected an Err result, got {result!r}"
    error = result.error
    assert error.kind is kind, f"Expected kind {kind!r}, got {error.kind!r}"
    assert message_contains in error.message, (
        f"Expected {message_contains!r} in error message {error.message!r}"
    )
    return error
