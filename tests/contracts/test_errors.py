from dataclasses import dataclass
from enum import Enum, auto

import pytest
from flow_res import Err, Ok, Result

from my_agents.contracts.errors import AppError, DisplayableError, TypedError
from my_agents.contracts.ports.agent_runner import (
    AgentRunnerError,
    AgentRunnerErrorType,
)
from my_agents.infrastructure.settings import SettingsError
from my_agents.usecases.send_message import SendMessageError, SendMessageErrorType
from tests.helpers import assert_err


class SampleErrorType(Enum):
    SAMPLE = auto()


@dataclass(frozen=True, slots=True)
class DisplayableSampleError:
    type: SampleErrorType
    message: str
    display_message: str


def test_settings_error_conforms_to_app_error() -> None:
    error: AppError = SettingsError(message="missing MAGPIE_API_KEY")

    assert isinstance(error, AppError)
    assert not isinstance(error, Exception)
    assert error.message == "missing MAGPIE_API_KEY"


def test_agent_runner_error_conforms_to_typed_error() -> None:
    error: TypedError[AgentRunnerErrorType] = AgentRunnerError(
        type=AgentRunnerErrorType.API_ERROR,
        message="API is unavailable",
    )

    assert isinstance(error, AppError)
    assert isinstance(error, TypedError)
    assert not isinstance(error, Exception)
    assert error.type is AgentRunnerErrorType.API_ERROR
    assert error.message == "API is unavailable"


def test_displayable_sample_conforms_to_displayable_error() -> None:
    error: DisplayableError = DisplayableSampleError(
        type=SampleErrorType.SAMPLE,
        message="internal detail",
        display_message="Something went wrong.",
    )

    assert isinstance(error, AppError)
    assert isinstance(error, DisplayableError)
    assert not isinstance(error, Exception)
    assert error.message == "internal detail"
    assert error.display_message == "Something went wrong."


def test_send_message_error_conforms_to_all_error_protocols() -> None:
    expected = SendMessageError(
        type=SendMessageErrorType.API_ERROR,
        message="runner detail",
        display_message="APIとの通信でエラーが発生しました。",
    )
    result: Result[int, SendMessageError] = Err(expected)

    error = assert_err(
        result,
        expected_type=SendMessageErrorType.API_ERROR,
        message_contains="runner detail",
        display_message_contains="APIとの通信でエラーが発生しました。",
    )

    assert error is expected
    assert isinstance(error, AppError)
    assert isinstance(error, TypedError)
    assert isinstance(error, DisplayableError)
    assert not isinstance(error, Exception)
    assert error.type is SendMessageErrorType.API_ERROR
    assert error.display_message == "APIとの通信でエラーが発生しました。"


def test_assert_err_returns_typed_error() -> None:
    expected = AgentRunnerError(
        type=AgentRunnerErrorType.USAGE_LIMIT_EXCEEDED,
        message="usage limit exceeded",
    )
    result: Result[int, AgentRunnerError] = Err(expected)

    error = assert_err(
        result,
        expected_type=AgentRunnerErrorType.USAGE_LIMIT_EXCEEDED,
        message_contains="limit",
    )

    assert error is expected


def test_assert_err_accepts_untyped_app_error() -> None:
    expected = SettingsError(message="missing MAGPIE_MODEL")
    result: Result[int, SettingsError] = Err(expected)

    error = assert_err(result, message_contains="MAGPIE_MODEL")

    assert error is expected


def test_assert_err_rejects_ok() -> None:
    result: Result[int, SettingsError] = Ok(1)

    with pytest.raises(AssertionError):
        assert_err(result, message_contains="missing")


def test_assert_err_rejects_mismatched_type() -> None:
    result: Result[int, AgentRunnerError] = Err(
        AgentRunnerError(
            type=AgentRunnerErrorType.API_ERROR,
            message="API is unavailable",
        )
    )

    with pytest.raises(AssertionError):
        assert_err(
            result,
            expected_type=AgentRunnerErrorType.UNEXPECTED,
            message_contains="API is unavailable",
        )


def test_assert_err_rejects_missing_message() -> None:
    result: Result[int, AgentRunnerError] = Err(
        AgentRunnerError(
            type=AgentRunnerErrorType.UNEXPECTED,
            message="boom",
        )
    )

    with pytest.raises(AssertionError):
        assert_err(
            result,
            expected_type=AgentRunnerErrorType.UNEXPECTED,
            message_contains="not present",
        )


def test_assert_err_verifies_display_message() -> None:
    expected = DisplayableSampleError(
        type=SampleErrorType.SAMPLE,
        message="internal detail",
        display_message="Something went wrong.",
    )
    result: Result[int, DisplayableSampleError] = Err(expected)

    error = assert_err(
        result,
        expected_type=SampleErrorType.SAMPLE,
        message_contains="detail",
        display_message_contains="wrong",
    )

    assert error is expected


def test_assert_err_rejects_missing_display_message() -> None:
    result: Result[int, DisplayableSampleError] = Err(
        DisplayableSampleError(
            type=SampleErrorType.SAMPLE,
            message="internal detail",
            display_message="Something went wrong.",
        )
    )

    with pytest.raises(AssertionError):
        assert_err(
            result,
            expected_type=SampleErrorType.SAMPLE,
            message_contains="detail",
            display_message_contains="not present",
        )


def test_assert_err_rejects_non_displayable_error() -> None:
    result: Result[int, AgentRunnerError] = Err(
        AgentRunnerError(
            type=AgentRunnerErrorType.API_ERROR,
            message="API is unavailable",
        )
    )

    with pytest.raises(AssertionError):
        assert_err(
            result,
            expected_type=AgentRunnerErrorType.API_ERROR,
            message_contains="API",
            display_message_contains="API",
        )
