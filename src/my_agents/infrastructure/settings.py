from collections.abc import Mapping
from dataclasses import dataclass
from os import environ
from pathlib import Path
from typing import Annotated, Literal

from dotenv import dotenv_values
from flow_res import Err, Ok, Result
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
)

from my_agents.contracts.secret import SecretStr
from my_agents.domain.chat_history import (
    DEFAULT_EPISODE_GAP_MINUTES,
    DEFAULT_EPISODE_MAX_MESSAGES,
    DEFAULT_RECENT_MESSAGES_LIMIT,
    DEFAULT_SUMMARY_EVERY_MESSAGES,
    DEFAULT_SUMMARY_INDEX_CHAR_BUDGET,
)

type ChatBackend = Literal["openai", "local"]
DEFAULT_MEMORY_DIR_NAME = "memory"
_INVALID_INT = "\x00invalid-int"
_REQUIRED_ENV = ("MAGPIE_MODEL", "MAGPIE_BASE_URL", "MAGPIE_API_KEY")
_ERROR_MESSAGES = {
    "SUMMARY_INDEX_CHAR_BUDGET": "SUMMARY_INDEX_CHAR_BUDGET must be a positive integer.",
    "SUMMARY_EVERY_MESSAGES": "SUMMARY_EVERY_MESSAGES must be a positive integer.",
    "RECENT_MESSAGES_LIMIT": "RECENT_MESSAGES_LIMIT must be a positive integer.",
    "EPISODE_MAX_MESSAGES": "EPISODE_MAX_MESSAGES must be a positive integer.",
    "EPISODE_GAP_MINUTES": "EPISODE_GAP_MINUTES must be a positive integer.",
    "CHAT_BACKEND": "CHAT_BACKEND must be 'openai' or 'local'.",
    "EVENT_WEBHOOK_PORT": "EVENT_WEBHOOK_PORT must be an integer from 0 to 65535.",
    "EVENT_ANALYSIS_DELAY_MS": (
        "EVENT_ANALYSIS_DELAY_MS must be a non-negative integer."
    ),
}


@dataclass(frozen=True, slots=True)
class SettingsError:
    message: str


@dataclass(frozen=True, slots=True)
class AppSettings:
    model_name: str
    base_url: str
    api_key: SecretStr
    memory_dir: Path
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


def _blank_to_none(value: str) -> str | None:
    return None if not value.strip() else value


def _python_int(value: str) -> int | str:
    try:
        return int(value)
    except ValueError:
        return _INVALID_INT


def _optional_python_int(value: str) -> int | str | None:
    return None if not value.strip() else _python_int(value)


def _non_negative_python_int(value: str) -> int | str:
    return 0 if not value.strip() else _python_int(value)


def _chat_backend(value: str) -> str:
    return value.strip() or "openai"


def _optional_path(value: str) -> Path | None:
    if not value.strip():
        return None
    return Path(value.strip()).expanduser()


def _memory_dir(value: str) -> Path:
    return _optional_path(value) or Path.cwd() / DEFAULT_MEMORY_DIR_NAME


class _EnvSettings(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        alias_generator=lambda name: name.upper(),
    )

    model_name: Annotated[str, Field(alias="MAGPIE_MODEL", min_length=1)]
    base_url: Annotated[str, Field(alias="MAGPIE_BASE_URL", min_length=1)]
    api_key: Annotated[SecretStr, Field(alias="MAGPIE_API_KEY", min_length=1)]
    memory_dir: Annotated[
        Path, Field(alias="MEMORY_PATH"), BeforeValidator(_memory_dir)
    ] = Field(default_factory=lambda: Path.cwd() / DEFAULT_MEMORY_DIR_NAME)
    chats_db_path: Annotated[Path | None, BeforeValidator(_optional_path)] = None
    summary_model_name: Annotated[
        str | None, Field(alias="SUMMARY_MODEL"), BeforeValidator(_blank_to_none)
    ] = None
    summary_base_url: Annotated[str | None, BeforeValidator(_blank_to_none)] = None
    summary_api_key: Annotated[SecretStr | None, BeforeValidator(_blank_to_none)] = None
    summary_index_char_budget: Annotated[
        int, Field(gt=0), BeforeValidator(_python_int)
    ] = DEFAULT_SUMMARY_INDEX_CHAR_BUDGET
    summary_every_messages: Annotated[
        int, Field(gt=0), BeforeValidator(_python_int)
    ] = DEFAULT_SUMMARY_EVERY_MESSAGES
    recent_messages_limit: Annotated[int, Field(gt=0), BeforeValidator(_python_int)] = (
        DEFAULT_RECENT_MESSAGES_LIMIT
    )
    episode_max_messages: Annotated[int, Field(gt=0), BeforeValidator(_python_int)] = (
        DEFAULT_EPISODE_MAX_MESSAGES
    )
    episode_gap_minutes: Annotated[int, Field(gt=0), BeforeValidator(_python_int)] = (
        DEFAULT_EPISODE_GAP_MINUTES
    )
    chat_backend: Annotated[ChatBackend, BeforeValidator(_chat_backend)] = "openai"
    event_webhook_port: Annotated[
        int | None, Field(ge=0, le=65535), BeforeValidator(_optional_python_int)
    ] = None
    event_analysis_delay_ms: Annotated[
        int, Field(ge=0), BeforeValidator(_non_negative_python_int)
    ] = 0


def _settings_error(exc: ValidationError) -> SettingsError:
    errors = exc.errors()
    failed = {str(error["loc"][0]) for error in errors}
    missing = [name for name in _REQUIRED_ENV if name in failed]
    if missing:
        return SettingsError(
            f"Missing required environment variables: {', '.join(missing)}"
        )
    return SettingsError(_ERROR_MESSAGES[str(errors[0]["loc"][0])])


def load_app_settings(
    *,
    env: Mapping[str, str] | None = None,
    env_path: str | Path | None = None,
) -> Result[AppSettings, SettingsError]:
    source: Mapping[str, str | None] = environ if env is None else env

    if env_path is not None:
        source = {**dotenv_values(env_path), **source}

    present = {name: value for name, value in source.items() if value is not None}
    try:
        parsed = _EnvSettings.model_validate(present)
    except ValidationError as exc:
        return Err(_settings_error(exc))

    return Ok(AppSettings(**parsed.model_dump()))
