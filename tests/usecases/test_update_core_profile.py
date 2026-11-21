from flow_res import Err, Ok, Result, is_ok

from my_agents.contracts.ports.core_profile_store import (
    CoreProfileStoreError,
    CoreProfileStoreErrorType,
    ICoreProfileStore,
)
from my_agents.contracts.ports.core_profile_updater import (
    CoreMemoryUpdateCommand,
    CoreMemoryUpdateError,
    CoreMemoryUpdateErrorType,
    CoreMemoryUpdateOutcome,
)
from my_agents.usecases.update_core_profile import UpdateCoreProfileHandler
from tests.helpers import assert_err

_DEFAULT_SAVE_RESULT: Result[None, CoreProfileStoreError] = Ok(None)


class FakeStore(ICoreProfileStore):
    def __init__(
        self,
        load_result: Result[str | None, CoreProfileStoreError],
        save_result: Result[None, CoreProfileStoreError] = _DEFAULT_SAVE_RESULT,
    ) -> None:
        self._load_result = load_result
        self._save_result = save_result
        self.load_calls = 0
        self.saved: list[str] = []

    @property
    def location(self) -> str:
        return "memory/core.md"

    def load(self) -> Result[str | None, CoreProfileStoreError]:
        self.load_calls += 1
        return self._load_result

    def save(self, content: str) -> Result[None, CoreProfileStoreError]:
        self.saved.append(content)
        return self._save_result


def test_rejects_blank_reason_before_loading() -> None:
    store = FakeStore(Ok(None))
    handler = UpdateCoreProfileHandler(store)
    command = CoreMemoryUpdateCommand(
        action="append",
        reason="   ",
        content="new",
    )

    result = handler.update(command)

    error = assert_err(
        result,
        expected_type=CoreMemoryUpdateErrorType.INVALID_INPUT,
        message_contains="reason must not be empty",
    )
    assert isinstance(error, CoreMemoryUpdateError)
    assert error.message == "Update reason must not be empty."
    assert store.load_calls == 0
    assert store.saved == []


def test_append_creates_content_when_file_is_missing() -> None:
    store = FakeStore(Ok(None))
    handler = UpdateCoreProfileHandler(store)
    command = CoreMemoryUpdateCommand(
        action="append",
        reason="  価値観を記録  ",
        content="  ## 価値観\r\n- 誠実さ  ",
    )

    result = handler.update(command)

    assert is_ok(result)
    assert result.unwrap() == CoreMemoryUpdateOutcome(
        char_count=12,
        max_chars=4096,
        reason="価値観を記録",
    )
    assert store.load_calls == 1
    assert store.saved == ["## 価値観\n- 誠実さ\n"]


def test_append_combines_with_existing_content() -> None:
    store = FakeStore(Ok("existing"))
    handler = UpdateCoreProfileHandler(store)
    command = CoreMemoryUpdateCommand(
        action="append",
        reason="add",
        content="addition",
    )

    result = handler.update(command)

    assert is_ok(result)
    assert result.unwrap() == CoreMemoryUpdateOutcome(
        char_count=18,
        max_chars=4096,
        reason="add",
    )
    assert store.saved == ["existing\n\naddition\n"]


def test_append_rejects_empty_content() -> None:
    store = FakeStore(Ok(None))
    handler = UpdateCoreProfileHandler(store)
    command = CoreMemoryUpdateCommand(
        action="append",
        reason="add",
        content="   ",
    )

    result = handler.update(command)

    error = assert_err(
        result,
        expected_type=CoreMemoryUpdateErrorType.INVALID_INPUT,
        message_contains="Cannot append empty content",
    )
    assert error.message == "Cannot append empty content to the core profile."
    assert store.saved == []


def test_append_rejects_content_over_limit() -> None:
    store = FakeStore(Ok(None))
    handler = UpdateCoreProfileHandler(store)
    command = CoreMemoryUpdateCommand(
        action="append",
        reason="add",
        content="a" * 4097,
    )

    result = handler.update(command)

    error = assert_err(
        result,
        expected_type=CoreMemoryUpdateErrorType.MAX_LENGTH_EXCEEDED,
        message_contains="got 4097",
    )
    assert error.message == "Core profile exceeds 4096 characters (got 4097)."
    assert store.saved == []


def test_replace_updates_matching_target() -> None:
    store = FakeStore(Ok("first\nsecond"))
    handler = UpdateCoreProfileHandler(store)
    command = CoreMemoryUpdateCommand(
        action="replace",
        reason="update",
        content="changed",
        old_text="second",
    )

    result = handler.update(command)

    assert is_ok(result)
    assert result.unwrap() == CoreMemoryUpdateOutcome(
        char_count=13,
        max_chars=4096,
        reason="update",
    )
    assert store.saved == ["first\nchanged\n"]


def test_replace_reports_missing_target() -> None:
    store = FakeStore(Ok("first\nsecond"))
    handler = UpdateCoreProfileHandler(store)
    command = CoreMemoryUpdateCommand(
        action="replace",
        reason="update",
        content="changed",
        old_text="third",
    )

    result = handler.update(command)

    error = assert_err(
        result,
        expected_type=CoreMemoryUpdateErrorType.TARGET_NOT_FOUND,
        message_contains="was not found: 'third'",
    )
    assert error.message == "Replacement target was not found: 'third'"
    assert store.saved == []


def test_replace_reports_ambiguous_target() -> None:
    store = FakeStore(Ok("same\nsame"))
    handler = UpdateCoreProfileHandler(store)
    command = CoreMemoryUpdateCommand(
        action="replace",
        reason="update",
        content="changed",
        old_text="same",
    )

    result = handler.update(command)

    error = assert_err(
        result,
        expected_type=CoreMemoryUpdateErrorType.AMBIGUOUS_TARGET,
        message_contains="occurs 2 times",
    )
    assert error.message == (
        "Replacement target occurs 2 times; provide a unique target."
    )
    assert store.saved == []


def test_replace_rejects_blank_target() -> None:
    store = FakeStore(Ok("first"))
    handler = UpdateCoreProfileHandler(store)
    command = CoreMemoryUpdateCommand(
        action="replace",
        reason="update",
        content="changed",
        old_text="   ",
    )

    result = handler.update(command)

    error = assert_err(
        result,
        expected_type=CoreMemoryUpdateErrorType.INVALID_INPUT,
        message_contains="must not be empty or whitespace only",
    )
    assert error.message == ("Replacement target must not be empty or whitespace only.")
    assert store.saved == []


def test_rewrite_does_not_load_existing_content() -> None:
    store = FakeStore(Ok("a" * 5000))
    handler = UpdateCoreProfileHandler(store)
    command = CoreMemoryUpdateCommand(
        action="rewrite",
        reason="reorganize",
        content="  ## 新しい構成\r\n- 項目  ",
    )

    result = handler.update(command)

    assert is_ok(result)
    assert result.unwrap() == CoreMemoryUpdateOutcome(
        char_count=13,
        max_chars=4096,
        reason="reorganize",
    )
    assert store.load_calls == 0
    assert store.saved == ["## 新しい構成\n- 項目\n"]


def test_rewrite_rejects_empty_content() -> None:
    store = FakeStore(Ok(None))
    handler = UpdateCoreProfileHandler(store)
    command = CoreMemoryUpdateCommand(
        action="rewrite",
        reason="reorganize",
        content="  \r\n  ",
    )

    result = handler.update(command)

    error = assert_err(
        result,
        expected_type=CoreMemoryUpdateErrorType.INVALID_INPUT,
        message_contains="Cannot rewrite",
    )
    assert error.message == "Cannot rewrite the core profile with empty content."
    assert store.saved == []


def test_rewrite_rejects_content_over_limit() -> None:
    store = FakeStore(Ok(None))
    handler = UpdateCoreProfileHandler(store)
    command = CoreMemoryUpdateCommand(
        action="rewrite",
        reason="reorganize",
        content="a" * 4097,
    )

    result = handler.update(command)

    error = assert_err(
        result,
        expected_type=CoreMemoryUpdateErrorType.MAX_LENGTH_EXCEEDED,
        message_contains="got 4097",
    )
    assert error.message == "Core profile exceeds 4096 characters (got 4097)."
    assert store.saved == []


def test_load_failure_maps_to_storage_error() -> None:
    store = FakeStore(
        Err(
            CoreProfileStoreError(
                type=CoreProfileStoreErrorType.READ_FAILED,
                message="permission denied",
            )
        )
    )
    handler = UpdateCoreProfileHandler(store)
    command = CoreMemoryUpdateCommand(
        action="append",
        reason="add",
        content="new",
    )

    result = handler.update(command)

    error = assert_err(
        result,
        expected_type=CoreMemoryUpdateErrorType.STORAGE_ERROR,
        message_contains="permission denied",
    )
    assert error.message == "permission denied"
    assert store.saved == []


def test_existing_profile_over_limit_maps_to_max_length_error() -> None:
    store = FakeStore(Ok("a" * 4097))
    handler = UpdateCoreProfileHandler(store)
    command = CoreMemoryUpdateCommand(
        action="append",
        reason="add",
        content="new",
    )

    result = handler.update(command)

    error = assert_err(
        result,
        expected_type=CoreMemoryUpdateErrorType.MAX_LENGTH_EXCEEDED,
        message_contains="got 4097",
    )
    assert error.message == "Core profile exceeds 4096 characters (got 4097)."
    assert store.saved == []


def test_save_failure_maps_to_storage_error() -> None:
    store = FakeStore(
        Ok(None),
        Err(
            CoreProfileStoreError(
                type=CoreProfileStoreErrorType.WRITE_FAILED,
                message="disk full",
            )
        ),
    )
    handler = UpdateCoreProfileHandler(store)
    command = CoreMemoryUpdateCommand(
        action="append",
        reason="add",
        content="new",
    )

    result = handler.update(command)

    error = assert_err(
        result,
        expected_type=CoreMemoryUpdateErrorType.STORAGE_ERROR,
        message_contains="disk full",
    )
    assert error.message == "disk full"
    assert store.saved == ["new\n"]
