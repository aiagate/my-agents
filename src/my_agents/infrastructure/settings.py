from collections.abc import Mapping
from dataclasses import dataclass
from os import environ
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.secret import SecretStr
from my_agents.domain.chat_history import (
    DEFAULT_EPISODE_GAP_MINUTES,
    DEFAULT_EPISODE_MAX_MESSAGES,
    DEFAULT_RECENT_MESSAGES_LIMIT,
    DEFAULT_SUMMARY_EVERY_MESSAGES,
    DEFAULT_SUMMARY_INDEX_CHAR_BUDGET,
)

_SETTING_NAMES = ("MAGPIE_MODEL", "MAGPIE_BASE_URL", "MAGPIE_API_KEY")
type ChatBackend = Literal["openai", "local"]
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
    chat_backend: ChatBackend = "openai"
    event_webhook_port: int | None = None
    event_analysis_delay_ms: int = 0

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


def _resolve_chat_backend(
    source: Mapping[str, str | None],
) -> Result[ChatBackend, SettingsError]:
    value = _optional_text(source, "CHAT_BACKEND")
    if value is None:
        return Ok("openai")
    match value:
        case "openai" | "local":
            return Ok(value)
        case _:
            return Err(SettingsError("CHAT_BACKEND must be 'openai' or 'local'."))


def _resolve_event_webhook_port(
    source: Mapping[str, str | None],
) -> Result[int | None, SettingsError]:
    value = _optional_text(source, "EVENT_WEBHOOK_PORT")
    if value is None:
        return Ok(None)
    try:
        port = int(value)
    except ValueError:
        return Err(
            SettingsError("EVENT_WEBHOOK_PORT must be an integer from 0 to 65535.")
        )
    if not 0 <= port <= 65535:
        return Err(
            SettingsError("EVENT_WEBHOOK_PORT must be an integer from 0 to 65535.")
        )
    return Ok(port)


def _resolve_event_analysis_delay(
    source: Mapping[str, str | None],
) -> Result[int, SettingsError]:
    value = _optional_text(source, "EVENT_ANALYSIS_DELAY_MS")
    if value is None:
        return Ok(0)
    try:
        delay = int(value)
    except ValueError:
        return Err(
            SettingsError("EVENT_ANALYSIS_DELAY_MS must be a non-negative integer.")
        )
    if delay < 0:
        return Err(
            SettingsError("EVENT_ANALYSIS_DELAY_MS must be a non-negative integer.")
        )
    return Ok(delay)


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

    chat_backend = _resolve_chat_backend(source)
    if is_err(chat_backend):
        return Err(chat_backend.error)
    event_webhook_port = _resolve_event_webhook_port(source)
    if is_err(event_webhook_port):
        return Err(event_webhook_port.error)
    event_analysis_delay = _resolve_event_analysis_delay(source)
    if is_err(event_analysis_delay):
        return Err(event_analysis_delay.error)

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
            chat_backend=chat_backend.value,
            event_webhook_port=event_webhook_port.value,
            event_analysis_delay_ms=event_analysis_delay.value,
        )
    )
