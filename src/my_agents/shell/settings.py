import json
from collections.abc import Mapping
from os import environ
from pathlib import Path
from typing import Annotated

from dotenv import dotenv_values
from flow_res import Err, Ok, Result
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from my_agents.core.types import (
    DEFAULT_PERSONA,
    AppError,
    AppErrorKind,
    CharacterPersona,
    ProactivePolicy,
    SecretStr,
)
from my_agents.shell.mcp import McpServerSpec, parse_mcp_servers

DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"


def _optional_path(value: object) -> Path | None:
    if value is None:
        return None
    if isinstance(value, Path):
        return value
    text = str(value).strip()
    return Path(text).expanduser() if text else None


def _data_dir(value: object) -> Path:
    return _optional_path(value) or Path.cwd() / "data"


def _blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _llm_base_url(value: object) -> str:
    return str(value or "").strip() or DEFAULT_LLM_BASE_URL


class Settings(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        populate_by_name=True,
        extra="ignore",
        alias_generator=str.upper,
    )

    llm_model: Annotated[str | None, BeforeValidator(_blank_to_none)] = None
    llm_base_url: Annotated[str, BeforeValidator(_llm_base_url)] = DEFAULT_LLM_BASE_URL
    llm_api_key: Annotated[SecretStr | None, BeforeValidator(_blank_to_none)] = None
    data_dir: Annotated[Path, BeforeValidator(_data_dir)] = Field(
        default_factory=lambda: Path.cwd() / "data"
    )
    db_path_override: Annotated[
        Path | None, Field(alias="DB_PATH"), BeforeValidator(_optional_path)
    ] = None
    persona_path: Annotated[Path | None, BeforeValidator(_optional_path)] = None
    mcp_servers_json: Annotated[SecretStr | None, BeforeValidator(_blank_to_none)] = (
        None
    )
    server_host: str = "127.0.0.1"
    server_port: Annotated[int, Field(ge=0, le=65535)] = 8080
    event_eval_delay_ms: Annotated[int, Field(ge=0)] = 0
    proactive_daily_cap: Annotated[int, Field(ge=0)] = 5
    proactive_quiet_hours_start_utc: Annotated[int, Field(ge=0, le=23)] = 22
    proactive_quiet_hours_end_utc: Annotated[int, Field(ge=0, le=23)] = 7
    proactive_user_active_minutes: Annotated[int, Field(ge=0)] = 10
    recent_messages_limit: Annotated[int, Field(gt=0)] = 30
    log_path: Annotated[Path | None, BeforeValidator(_optional_path)] = None

    @property
    def db_path(self) -> Path:
        return self.db_path_override or self.data_dir / "state.db"

    @property
    def core_memory_path(self) -> Path:
        return self.data_dir / "core.md"

    @property
    def proactive_policy(self) -> ProactivePolicy:
        return ProactivePolicy(
            daily_cap=self.proactive_daily_cap,
            quiet_hours_start_utc=self.proactive_quiet_hours_start_utc,
            quiet_hours_end_utc=self.proactive_quiet_hours_end_utc,
            user_active_minutes=self.proactive_user_active_minutes,
        )

    @property
    def mcp_servers(self) -> Result[tuple[McpServerSpec, ...], AppError]:
        raw = (
            None
            if self.mcp_servers_json is None
            else self.mcp_servers_json.get_secret_value()
        )
        return parse_mcp_servers(raw)


def load_settings(
    *,
    env: Mapping[str, str] | None = None,
    env_path: Path | None = None,
) -> Result[Settings, AppError]:
    source: Mapping[str, str | None] = environ if env is None else env
    merged: dict[str, object] = {}
    if env_path is not None:
        merged.update(dotenv_values(env_path))
    merged.update({key: value for key, value in source.items() if value is not None})
    try:
        return Ok(Settings.model_validate(merged))
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "settings"
        return Err(
            AppError(
                AppErrorKind.VALIDATION,
                f"Invalid setting '{field}': {first['msg']}",
            )
        )


def load_persona(path: Path | None) -> Result[CharacterPersona, AppError]:
    if path is None:
        return Ok(DEFAULT_PERSONA)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Err(AppError(AppErrorKind.VALIDATION, f"Cannot read persona: {exc}"))
    if not isinstance(data, dict):
        return Err(AppError(AppErrorKind.VALIDATION, "Persona must be a JSON object."))
    values = {
        "name": str(data.get("name", DEFAULT_PERSONA.name)),
        "introduction": str(data.get("introduction", DEFAULT_PERSONA.introduction)),
        "dialogue_policy": str(
            data.get("dialogue_policy", DEFAULT_PERSONA.dialogue_policy)
        ),
        "boundaries": str(data.get("boundaries", DEFAULT_PERSONA.boundaries)),
    }
    return Ok(CharacterPersona(**values))
