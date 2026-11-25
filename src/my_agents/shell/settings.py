import json
from collections.abc import Mapping
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
    SecretStr,
    ValidationError,
)

from my_agents.core.types import (
    DEFAULT_PERSONA,
    AppError,
    AppErrorKind,
    CharacterPersona,
    ProactivePolicy,
)
from my_agents.shell.mcp import McpServerSpec, parse_mcp_servers


def _optional_path(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, Path):
        return value
    text = str(value).strip()
    return Path(text).expanduser() if text else None


def _memory_dir(value: object) -> Path:
    path = _optional_path(value)
    return Path.cwd() / "memory" if path is None else Path(str(path))


def _blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class Settings(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        populate_by_name=True,
        extra="ignore",
    )

    magpie_model: Annotated[str, Field(alias="MAGPIE_MODEL")] = "local"
    magpie_base_url: Annotated[str, Field(alias="MAGPIE_BASE_URL")] = (
        "http://127.0.0.1:9/v1"
    )
    magpie_api_key: Annotated[
        SecretStr | None,
        Field(alias="MAGPIE_API_KEY"),
        BeforeValidator(_blank_to_none),
    ] = None
    memory_dir: Annotated[
        Path, Field(alias="MEMORY_PATH"), BeforeValidator(_memory_dir)
    ] = Field(default_factory=lambda: Path.cwd() / "memory")
    chats_db_path: Annotated[
        Path | None, Field(alias="CHATS_DB_PATH"), BeforeValidator(_optional_path)
    ] = None
    persona_path: Annotated[
        Path | None, Field(alias="PERSONA_PATH"), BeforeValidator(_optional_path)
    ] = None
    mcp_servers_json: Annotated[
        SecretStr | None,
        Field(alias="MCP_SERVERS_JSON"),
        BeforeValidator(_blank_to_none),
    ] = None
    chat_backend: Annotated[Literal["openai", "local"], Field(alias="CHAT_BACKEND")] = (
        "local"
    )
    server_host: Annotated[str, Field(alias="SERVER_HOST")] = "127.0.0.1"
    server_port: Annotated[int, Field(alias="SERVER_PORT", ge=0, le=65535)] = 8080
    event_analysis_delay_ms: Annotated[
        int, Field(alias="EVENT_ANALYSIS_DELAY_MS", ge=0)
    ] = 0
    proactive_cooldown_minutes: Annotated[
        int, Field(alias="PROACTIVE_COOLDOWN_MINUTES", ge=0)
    ] = 180
    proactive_daily_cap: Annotated[int, Field(alias="PROACTIVE_DAILY_CAP", ge=0)] = 3
    quiet_hours_start: Annotated[int, Field(alias="QUIET_HOURS_START", ge=0, le=23)] = (
        22
    )
    quiet_hours_end: Annotated[int, Field(alias="QUIET_HOURS_END", ge=0, le=23)] = 7
    proactive_user_active_minutes: Annotated[
        int, Field(alias="PROACTIVE_USER_ACTIVE_MINUTES", ge=0)
    ] = 10
    episode_max_turns: Annotated[int, Field(alias="EPISODE_MAX_TURNS", gt=0)] = 40
    episode_gap_minutes: Annotated[int, Field(alias="EPISODE_GAP_MINUTES", gt=0)] = 60
    summary_every_turns: Annotated[int, Field(alias="SUMMARY_EVERY_TURNS", gt=0)] = 6
    recent_turns_limit: Annotated[int, Field(alias="RECENT_TURNS_LIMIT", gt=0)] = 20
    log_file: Annotated[
        Path | None, Field(alias="LOG_FILE"), BeforeValidator(_optional_path)
    ] = None

    @property
    def db_path(self) -> Path:
        return self.chats_db_path or self.memory_dir / "chats.db"

    @property
    def core_memory_path(self) -> Path:
        return self.memory_dir / "core.md"

    @property
    def proactive_policy(self) -> ProactivePolicy:
        return ProactivePolicy(
            cooldown_minutes=self.proactive_cooldown_minutes,
            daily_cap=self.proactive_daily_cap,
            quiet_hours_start=self.quiet_hours_start,
            quiet_hours_end=self.quiet_hours_end,
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
