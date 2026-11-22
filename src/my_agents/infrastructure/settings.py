from collections.abc import Mapping
from dataclasses import dataclass
from os import environ
from pathlib import Path

from dotenv import dotenv_values
from flow_res import Err, Ok, Result

from my_agents.contracts.secret import SecretStr
from my_agents.domain.chat_history import (
    DEFAULT_EPISODE_GAP_MINUTES,
    DEFAULT_EPISODE_MAX_MESSAGES,
    DEFAULT_RECENT_MESSAGES_LIMIT,
    DEFAULT_SUMMARY_EVERY_MESSAGES,
    DEFAULT_SUMMARY_INDEX_CHAR_BUDGET,
)

_SETTING_NAMES = ("MAGPIE_MODEL", "MAGPIE_BASE_URL", "MAGPIE_API_KEY")
DEFAULT_MEMORY_DIR_NAME = "memory"
DEFAULT_MLX_LORA_MODEL = "mlx-community/Qwen2.5-3B-Instruct-4bit"


@dataclass(frozen=True, slots=True)
class SettingsError:
    message: str


@dataclass(frozen=True, slots=True)
class AppSettings:
    model_name: str
    base_url: str
    api_key: SecretStr
    memory_dir: Path
    mlx_lora_model: str = DEFAULT_MLX_LORA_MODEL
    mlx_lora_adapter_path: Path | None = None
    chats_db_path: Path | None = None
    summary_model_name: str | None = None
    summary_base_url: str | None = None
    summary_api_key: SecretStr | None = None
    summary_index_char_budget: int = DEFAULT_SUMMARY_INDEX_CHAR_BUDGET
    summary_every_messages: int = DEFAULT_SUMMARY_EVERY_MESSAGES
    recent_messages_limit: int = DEFAULT_RECENT_MESSAGES_LIMIT
    episode_max_messages: int = DEFAULT_EPISODE_MAX_MESSAGES
    episode_gap_minutes: int = DEFAULT_EPISODE_GAP_MINUTES

    @property
    def resolved_chats_db_path(self) -> Path:
        return self.chats_db_path or self.memory_dir / "chats.db"

    @property
    def resolved_summary_model_name(self) -> str:
        return self.summary_model_name or self.model_name

    @property
    def resolved_summary_base_url(self) -> str:
        return self.summary_base_url or self.base_url

    @property
    def resolved_summary_api_key(self) -> SecretStr:
        return self.api_key if self.summary_api_key is None else self.summary_api_key


def _optional_text(source: Mapping[str, str | None], name: str) -> str | None:
    return (source.get(name) or "").strip() or None


def _optional_secret(source: Mapping[str, str | None], name: str) -> SecretStr | None:
    value = _optional_text(source, name)
    return None if value is None else SecretStr(value)


def _resolve_mlx_lora_model(source: Mapping[str, str | None]) -> str:
    value = source.get("MLX_LORA_MODEL")
    if value is None or not value.strip():
        return DEFAULT_MLX_LORA_MODEL
    return value.strip()


def _resolve_optional_path(
    source: Mapping[str, str | None],
    name: str,
) -> Path | None:
    value = source.get(name)
    if value is None or not value.strip():
        return None
    return Path(value.strip()).expanduser()


def _resolve_memory_dir(source: Mapping[str, str | None]) -> Path:
    value = source.get("MEMORY_PATH")
    if value is None or not value.strip():
        return Path.cwd() / DEFAULT_MEMORY_DIR_NAME
    return Path(value.strip()).expanduser()


def load_app_settings(
    *,
    env: Mapping[str, str] | None = None,
    env_path: str | Path | None = None,
) -> Result[AppSettings, SettingsError]:
    source: Mapping[str, str | None] = environ if env is None else env

    if env_path is not None:
        file_values: dict[str, str | None] = dotenv_values(env_path)
        source = {**file_values, **source}

    missing: list[str] = []
    collected: dict[str, str] = {}
    for name in _SETTING_NAMES:
        value = source.get(name)
        if value is None or not value.strip():
            missing.append(name)
        else:
            collected[name] = value.strip()

    if missing:
        return Err(
            SettingsError(
                f"Missing required environment variables: {', '.join(missing)}"
            )
        )

    integers = {
        "SUMMARY_INDEX_CHAR_BUDGET": DEFAULT_SUMMARY_INDEX_CHAR_BUDGET,
        "SUMMARY_EVERY_MESSAGES": DEFAULT_SUMMARY_EVERY_MESSAGES,
        "RECENT_MESSAGES_LIMIT": DEFAULT_RECENT_MESSAGES_LIMIT,
        "EPISODE_MAX_MESSAGES": DEFAULT_EPISODE_MAX_MESSAGES,
        "EPISODE_GAP_MINUTES": DEFAULT_EPISODE_GAP_MINUTES,
    }
    for name in integers:
        raw = source.get(name)
        if raw is not None:
            try:
                integers[name] = int(raw)
            except ValueError:
                integers[name] = 0
            if integers[name] <= 0:
                return Err(SettingsError(f"{name} must be a positive integer."))

    return Ok(
        AppSettings(
            model_name=collected["MAGPIE_MODEL"],
            base_url=collected["MAGPIE_BASE_URL"],
            api_key=SecretStr(collected["MAGPIE_API_KEY"]),
            memory_dir=_resolve_memory_dir(source),
            mlx_lora_model=_resolve_mlx_lora_model(source),
            mlx_lora_adapter_path=_resolve_optional_path(
                source, "MLX_LORA_ADAPTER_PATH"
            ),
            chats_db_path=_resolve_optional_path(source, "CHATS_DB_PATH"),
            summary_model_name=_optional_text(source, "SUMMARY_MODEL"),
            summary_base_url=_optional_text(source, "SUMMARY_BASE_URL"),
            summary_api_key=_optional_secret(source, "SUMMARY_API_KEY"),
            summary_index_char_budget=integers["SUMMARY_INDEX_CHAR_BUDGET"],
            summary_every_messages=integers["SUMMARY_EVERY_MESSAGES"],
            recent_messages_limit=integers["RECENT_MESSAGES_LIMIT"],
            episode_max_messages=integers["EPISODE_MAX_MESSAGES"],
            episode_gap_minutes=integers["EPISODE_GAP_MINUTES"],
        )
    )
