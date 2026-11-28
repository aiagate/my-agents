import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values
from flow_res import Err, Ok, Result
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
)

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
DEFAULT_TIMEZONE = "UTC"


def _timezone(value: object) -> str:
    if value is None:
        return DEFAULT_TIMEZONE
    if not isinstance(value, str):
        raise ValueError("Timezone must be an IANA timezone name.")
    name = value.strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown IANA timezone: {name!r}") from exc
    return name


def _optional_path(value: object) -> Path | None:
    if value is None:
        return None
    if isinstance(value, Path):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return Path(text).expanduser()
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc


def _data_dir(value: object) -> Path:
    return _optional_path(value) or _default_data_dir()


def _default_data_dir() -> Path:
    return Path.cwd() / "data"


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
        default_factory=_default_data_dir
    )
    persona_path: Annotated[Path | None, BeforeValidator(_optional_path)] = None
    mcp_servers_json: Annotated[SecretStr | None, BeforeValidator(_blank_to_none)] = (
        None
    )
    server_host: str = "127.0.0.1"
    server_port: Annotated[int, Field(ge=0, le=65535)] = 8080
    proactive_daily_cap: Annotated[int, Field(ge=0)] = 5
    proactive_quiet_hours_start: Annotated[int, Field(ge=0, le=23)] = 22
    proactive_quiet_hours_end: Annotated[int, Field(ge=0, le=23)] = 7
    proactive_user_active_minutes: Annotated[int, Field(ge=0)] = 10
    timezone: Annotated[str, BeforeValidator(_timezone)] = DEFAULT_TIMEZONE
    event_max_concurrency: Annotated[int, Field(gt=0)] = 4
    recent_messages_limit: Annotated[int, Field(gt=0)] = 30
    log_path: Annotated[Path | None, BeforeValidator(_optional_path)] = None
    web_dist_dir: Annotated[Path | None, BeforeValidator(_optional_path)] = None
    vapid_private_key: Annotated[SecretStr | None, BeforeValidator(_blank_to_none)] = (
        None
    )
    vapid_subject: Annotated[str | None, BeforeValidator(_blank_to_none)] = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def core_memory_path(self) -> Path:
        return self.data_dir / "core.md"

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def proactive_policy(self) -> ProactivePolicy:
        return ProactivePolicy(
            daily_cap=self.proactive_daily_cap,
            quiet_hours_start=self.proactive_quiet_hours_start,
            quiet_hours_end=self.proactive_quiet_hours_end,
            user_active_minutes=self.proactive_user_active_minutes,
            tz=self.tzinfo,
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
    env: Mapping[str, str],
    env_path: Path | None = None,
) -> Result[Settings, AppError]:
    merged: dict[str, object] = {}
    if env_path is not None:
        try:
            merged.update(dotenv_values(env_path))
        except (OSError, UnicodeError) as exc:
            return Err(
                AppError(
                    AppErrorKind.VALIDATION,
                    f"Cannot read .env file: {exc}",
                )
            )
    merged.update(env)
    try:
        return Ok(Settings.model_validate(merged))
    except ValidationError as exc:
        messages = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "settings"
            messages.append(f"Invalid setting '{location}': {error['msg']}")
        return Err(AppError(AppErrorKind.VALIDATION, "; ".join(messages)))


class _PersonaInput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    name: StrictStr | None = None
    introduction: StrictStr | None = None
    dialogue_policy: StrictStr | None = None
    boundaries: StrictStr | None = None


def load_persona(path: Path | None) -> Result[CharacterPersona, AppError]:
    if path is None:
        return Ok(DEFAULT_PERSONA)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return Err(AppError(AppErrorKind.VALIDATION, f"Cannot read persona: {exc}"))
    if not isinstance(data, dict):
        return Err(AppError(AppErrorKind.VALIDATION, "Persona must be a JSON object."))
    try:
        parsed = _PersonaInput.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "persona"
        return Err(
            AppError(
                AppErrorKind.VALIDATION,
                f"Invalid persona field '{location}': {first['msg']}",
            )
        )
    return Ok(
        CharacterPersona(
            name=DEFAULT_PERSONA.name if parsed.name is None else parsed.name,
            introduction=(
                DEFAULT_PERSONA.introduction
                if parsed.introduction is None
                else parsed.introduction
            ),
            dialogue_policy=(
                DEFAULT_PERSONA.dialogue_policy
                if parsed.dialogue_policy is None
                else parsed.dialogue_policy
            ),
            boundaries=(
                DEFAULT_PERSONA.boundaries
                if parsed.boundaries is None
                else parsed.boundaries
            ),
        )
    )
