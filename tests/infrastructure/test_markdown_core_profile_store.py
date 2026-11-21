from pathlib import Path

from flow_res import is_ok

from my_agents.contracts.ports.core_profile_store import (
    CoreProfileStoreError,
    CoreProfileStoreErrorType,
)
from my_agents.infrastructure.markdown_core_profile_store import (
    MarkdownCoreProfileStore,
)
from tests.helpers import assert_err


def test_from_memory_dir_uses_core_md_and_location(tmp_path: Path) -> None:
    store = MarkdownCoreProfileStore.from_memory_dir(tmp_path / "memory")

    assert store.location == str(tmp_path / "memory" / "core.md")
    assert isinstance(store, MarkdownCoreProfileStore)


def test_load_returns_none_when_file_is_missing(tmp_path: Path) -> None:
    store = MarkdownCoreProfileStore(tmp_path / "missing" / "core.md")

    result = store.load()

    assert is_ok(result)
    assert result.unwrap() is None


def test_load_returns_file_content(tmp_path: Path) -> None:
    path = tmp_path / "core.md"
    path.write_text("## 価値観\n- 誠実さ\n", encoding="utf-8")
    store = MarkdownCoreProfileStore(path)

    result = store.load()

    assert is_ok(result)
    assert result.unwrap() == "## 価値観\n- 誠実さ\n"


def test_load_reports_os_error_when_path_is_directory(tmp_path: Path) -> None:
    path = tmp_path / "core.md"
    path.mkdir()
    store = MarkdownCoreProfileStore(path)

    result = store.load()

    error = assert_err(
        result,
        expected_type=CoreProfileStoreErrorType.READ_FAILED,
        message_contains="Failed to read core profile",
    )
    assert isinstance(error, CoreProfileStoreError)
    assert str(path) in error.message


def test_load_reports_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "core.md"
    path.write_bytes(b"\xff\xfe\x00")
    store = MarkdownCoreProfileStore(path)

    result = store.load()

    error = assert_err(
        result,
        expected_type=CoreProfileStoreErrorType.READ_FAILED,
        message_contains="Failed to read core profile",
    )
    assert isinstance(error, CoreProfileStoreError)
    assert str(path) in error.message


def test_save_creates_parent_directories_and_writes_content(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "memory" / "core.md"
    store = MarkdownCoreProfileStore(path)

    result = store.save("## Active Themes\n- 家計\n")

    assert is_ok(result)
    assert result.unwrap() is None
    assert path.read_text(encoding="utf-8") == "## Active Themes\n- 家計\n"
    assert list(path.parent.glob("*.tmp")) == []


def test_save_replaces_existing_content(tmp_path: Path) -> None:
    path = tmp_path / "core.md"
    path.write_text("old", encoding="utf-8")
    store = MarkdownCoreProfileStore(path)

    result = store.save("new")

    assert is_ok(result)
    assert path.read_text(encoding="utf-8") == "new"


def test_save_reports_error_when_parent_cannot_be_created(tmp_path: Path) -> None:
    parent = tmp_path / "memory"
    parent.write_text("not a directory", encoding="utf-8")
    path = parent / "core.md"
    store = MarkdownCoreProfileStore(path)

    result = store.save("content")

    error = assert_err(
        result,
        expected_type=CoreProfileStoreErrorType.WRITE_FAILED,
        message_contains="Failed to write core profile",
    )
    assert isinstance(error, CoreProfileStoreError)
    assert str(path) in error.message


def test_save_cleans_temp_file_when_replace_fails(tmp_path: Path) -> None:
    path = tmp_path / "core.md"
    path.mkdir()
    store = MarkdownCoreProfileStore(path)

    result = store.save("content")

    error = assert_err(
        result,
        expected_type=CoreProfileStoreErrorType.WRITE_FAILED,
        message_contains="Failed to write core profile",
    )
    assert isinstance(error, CoreProfileStoreError)
    assert str(path) in error.message
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []
