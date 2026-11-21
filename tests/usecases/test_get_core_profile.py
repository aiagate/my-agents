from flow_res import Err, Ok, Result, is_ok

from my_agents.contracts.ports.core_profile_store import (
    CoreProfileStoreError,
    CoreProfileStoreErrorType,
    ICoreProfileStore,
)
from my_agents.domain.core_profile import CoreProfile
from my_agents.usecases.get_core_profile import (
    GetCoreProfileError,
    GetCoreProfileErrorType,
    GetCoreProfileHandler,
    GetCoreProfileResponse,
)
from tests.helpers import assert_err


class FakeStore(ICoreProfileStore):
    def __init__(
        self,
        location: str,
        load_result: Result[str | None, CoreProfileStoreError],
    ) -> None:
        self._location = location
        self._load_result = load_result

    @property
    def location(self) -> str:
        return self._location

    def load(self) -> Result[str | None, CoreProfileStoreError]:
        return self._load_result

    def save(self, content: str) -> Result[None, CoreProfileStoreError]:
        raise AssertionError(f"save must not be called: {content}")


def test_execute_returns_empty_profile_when_file_is_missing() -> None:
    store = FakeStore("/tmp/memory/core.md", Ok(None))
    handler = GetCoreProfileHandler(store)

    result = handler.execute()

    assert is_ok(result)
    assert result.unwrap() == GetCoreProfileResponse(
        profile=CoreProfile.empty(),
        location="/tmp/memory/core.md",
        max_chars=4096,
    )
    assert result.unwrap().profile.is_empty is True


def test_execute_normalizes_existing_markdown() -> None:
    store = FakeStore("memory/core.md", Ok("  ## 価値観\r\n- 誠実さ  "))
    handler = GetCoreProfileHandler(store)

    result = handler.execute()

    assert is_ok(result)
    response = result.unwrap()
    assert response.profile == CoreProfile(content="## 価値観\n- 誠実さ")
    assert response.profile.char_count == 12
    assert response.location == "memory/core.md"
    assert response.max_chars == 4096


def test_execute_maps_load_error_to_storage_error() -> None:
    store = FakeStore(
        "/tmp/memory/core.md",
        Err(
            CoreProfileStoreError(
                type=CoreProfileStoreErrorType.READ_FAILED,
                message="permission denied",
            )
        ),
    )
    handler = GetCoreProfileHandler(store)

    result = handler.execute()

    error = assert_err(
        result,
        expected_type=GetCoreProfileErrorType.STORAGE_ERROR,
        message_contains="permission denied",
        display_message_contains="/tmp/memory/core.md",
    )
    assert isinstance(error, GetCoreProfileError)
    assert error.message == "permission denied"
    assert error.display_message == (
        "Core Memory の読み込みに失敗しました。"
        "ファイルを確認してください: /tmp/memory/core.md"
    )


def test_execute_maps_over_limit_profile_to_invalid_profile() -> None:
    store = FakeStore("/tmp/memory/core.md", Ok("a" * 4097))
    handler = GetCoreProfileHandler(store)

    result = handler.execute()

    error = assert_err(
        result,
        expected_type=GetCoreProfileErrorType.INVALID_PROFILE,
        message_contains="got 4097",
        display_message_contains="/tmp/memory/core.md",
    )
    assert isinstance(error, GetCoreProfileError)
    assert "4096 文字以内" in error.display_message
    assert "4096" in error.display_message
