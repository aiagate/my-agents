from pathlib import Path

from flow_res import is_ok

from my_agents.contracts.secret import SecretStr
from my_agents.infrastructure.settings import (
    AppSettings,
    SettingsError,
    load_app_settings,
)
from tests.helpers import assert_err


def test_loads_valid_settings_with_default_memory_dir() -> None:
    result = load_app_settings(
        env={
            "MAGPIE_MODEL": "test-model",
            "MAGPIE_BASE_URL": "https://example.com/v1",
            "MAGPIE_API_KEY": "secret",
        }
    )

    assert is_ok(result)
    assert result.unwrap() == AppSettings(
        model_name="test-model",
        base_url="https://example.com/v1",
        api_key=SecretStr("secret"),
        memory_dir=Path.cwd() / "memory",
    )


def test_reports_missing_variable() -> None:
    result = load_app_settings(
        env={
            "MAGPIE_MODEL": "test-model",
            "MAGPIE_BASE_URL": "https://example.com/v1",
        }
    )

    error = assert_err(result, message_contains="MAGPIE_API_KEY")
    assert isinstance(error, SettingsError)
    assert not isinstance(error, Exception)
    assert "MAGPIE_API_KEY" in error.message


def test_rejects_whitespace_only_value() -> None:
    result = load_app_settings(
        env={
            "MAGPIE_MODEL": "test-model",
            "MAGPIE_BASE_URL": "   ",
            "MAGPIE_API_KEY": "secret",
        }
    )

    error = assert_err(result, message_contains="MAGPIE_BASE_URL")
    assert not isinstance(error, Exception)


def test_whitespace_memory_path_uses_default_memory_dir() -> None:
    result = load_app_settings(
        env={
            "MAGPIE_MODEL": "test-model",
            "MAGPIE_BASE_URL": "https://example.com/v1",
            "MAGPIE_API_KEY": "secret",
            "MEMORY_PATH": "   ",
        }
    )

    assert is_ok(result)
    assert result.unwrap().memory_dir == Path.cwd() / "memory"


def test_memory_path_is_expanded() -> None:
    result = load_app_settings(
        env={
            "MAGPIE_MODEL": "test-model",
            "MAGPIE_BASE_URL": "https://example.com/v1",
            "MAGPIE_API_KEY": "secret",
            "MEMORY_PATH": "  ~/agent-memory  ",
        }
    )

    assert is_ok(result)
    assert result.unwrap().memory_dir == Path("~/agent-memory").expanduser()


def test_loads_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MAGPIE_MODEL=env-model\n"
        "MAGPIE_BASE_URL=https://env.example.com/v1\n"
        "MAGPIE_API_KEY=env-secret\n"
        "MEMORY_PATH=/env/memory\n"
    )

    result = load_app_settings(env={}, env_path=env_file)

    assert is_ok(result)
    assert result.unwrap() == AppSettings(
        model_name="env-model",
        base_url="https://env.example.com/v1",
        api_key=SecretStr("env-secret"),
        memory_dir=Path("/env/memory"),
    )


def test_injected_env_overrides_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MAGPIE_MODEL=env-model\n"
        "MAGPIE_BASE_URL=https://env.example.com/v1\n"
        "MAGPIE_API_KEY=env-secret\n"
        "MEMORY_PATH=/env/memory\n"
    )

    result = load_app_settings(
        env={
            "MAGPIE_MODEL": "process-model",
            "MEMORY_PATH": "/process/memory",
        },
        env_path=env_file,
    )

    assert is_ok(result)
    assert result.unwrap().model_name == "process-model"
    assert result.unwrap().memory_dir == Path("/process/memory")
