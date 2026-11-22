from pathlib import Path

import pytest
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


def test_optional_mlx_lora_settings_default_when_unset() -> None:
    result = load_app_settings(
        env={
            "MAGPIE_MODEL": "test-model",
            "MAGPIE_BASE_URL": "https://example.com/v1",
            "MAGPIE_API_KEY": "secret",
        }
    )

    assert is_ok(result)
    assert result.unwrap().mlx_lora_model == "mlx-community/Qwen2.5-3B-Instruct-4bit"
    assert result.unwrap().mlx_lora_adapter_path is None


def test_loads_custom_mlx_lora_model_and_expands_adapter_path() -> None:
    result = load_app_settings(
        env={
            "MAGPIE_MODEL": "test-model",
            "MAGPIE_BASE_URL": "https://example.com/v1",
            "MAGPIE_API_KEY": "secret",
            "MLX_LORA_MODEL": "  /models/custom-3b  ",
            "MLX_LORA_ADAPTER_PATH": "  ~/my-adapters  ",
        }
    )

    assert is_ok(result)
    assert result.unwrap().mlx_lora_model == "/models/custom-3b"
    assert result.unwrap().mlx_lora_adapter_path == Path("~/my-adapters").expanduser()


def test_whitespace_mlx_lora_settings_fall_back_to_defaults() -> None:
    result = load_app_settings(
        env={
            "MAGPIE_MODEL": "test-model",
            "MAGPIE_BASE_URL": "https://example.com/v1",
            "MAGPIE_API_KEY": "secret",
            "MLX_LORA_MODEL": "   ",
            "MLX_LORA_ADAPTER_PATH": "   ",
        }
    )

    assert is_ok(result)
    assert result.unwrap().mlx_lora_model == "mlx-community/Qwen2.5-3B-Instruct-4bit"
    assert result.unwrap().mlx_lora_adapter_path is None


def test_chat_settings_defaults_and_summary_inheritance(tmp_path: Path) -> None:
    settings = load_app_settings(
        env={
            "MAGPIE_MODEL": "chat",
            "MAGPIE_BASE_URL": "https://chat.example",
            "MAGPIE_API_KEY": "chat-credential",
            "MEMORY_PATH": str(tmp_path),
        }
    ).unwrap()
    assert settings.resolved_chats_db_path == tmp_path / "chats.db"
    assert settings.resolved_summary_model_name == "chat"
    assert settings.resolved_summary_base_url == "https://chat.example"
    assert settings.resolved_summary_api_key == SecretStr("chat-credential")
    assert (
        settings.summary_index_char_budget,
        settings.summary_every_messages,
        settings.recent_messages_limit,
        settings.episode_max_messages,
        settings.episode_gap_minutes,
    ) == (16000, 6, 20, 40, 60)


def test_chat_settings_overrides(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite"
    settings = load_app_settings(
        env={
            "MAGPIE_MODEL": "chat",
            "MAGPIE_BASE_URL": "https://chat.example",
            "MAGPIE_API_KEY": "chat-credential",
            "CHATS_DB_PATH": f" {path} ",
            "SUMMARY_MODEL": " summary ",
            "SUMMARY_BASE_URL": " https://summary.example ",
            "SUMMARY_API_KEY": " summary-credential ",
            "SUMMARY_INDEX_CHAR_BUDGET": "100",
            "SUMMARY_EVERY_MESSAGES": "2",
            "RECENT_MESSAGES_LIMIT": "4",
            "EPISODE_MAX_MESSAGES": "8",
            "EPISODE_GAP_MINUTES": "15",
        }
    ).unwrap()
    assert settings.resolved_chats_db_path == path
    assert settings.resolved_summary_model_name == "summary"
    assert settings.resolved_summary_base_url == "https://summary.example"
    assert settings.resolved_summary_api_key == SecretStr("summary-credential")
    assert (
        settings.summary_index_char_budget,
        settings.summary_every_messages,
        settings.recent_messages_limit,
        settings.episode_max_messages,
        settings.episode_gap_minutes,
    ) == (100, 2, 4, 8, 15)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SUMMARY_EVERY_MESSAGES", "no"),
        ("SUMMARY_INDEX_CHAR_BUDGET", "0"),
        ("RECENT_MESSAGES_LIMIT", "-1"),
        ("EPISODE_MAX_MESSAGES", ""),
        ("EPISODE_GAP_MINUTES", "  "),
    ],
)
def test_chat_settings_reject_invalid_integer(name: str, value: str) -> None:
    env = {
        "MAGPIE_MODEL": "chat",
        "MAGPIE_BASE_URL": "https://chat.example",
        "MAGPIE_API_KEY": "chat-credential",
        name: value,
    }
    assert_err(
        load_app_settings(env=env),
        message_contains=f"{name} must be a positive integer.",
    )
