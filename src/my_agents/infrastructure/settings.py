from collections.abc import Mapping
from dataclasses import dataclass
from os import environ
from pathlib import Path

from dotenv import dotenv_values
from flow_res import Err, Ok, Result

from my_agents.contracts.secret import SecretStr

_SETTING_NAMES = ("MAGPIE_MODEL", "MAGPIE_BASE_URL", "MAGPIE_API_KEY")
DEFAULT_MEMORY_DIR_NAME = "memory"
DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"


@dataclass(frozen=True, slots=True)
class SettingsError:
    message: str


@dataclass(frozen=True, slots=True)
class AppSettings:
    model_name: str
    base_url: str
    api_key: SecretStr
    memory_dir: Path
    gemini_api_key: SecretStr | None = None
    embedding_model: str = DEFAULT_EMBEDDING_MODEL


def _resolve_optional_secret(
    source: Mapping[str, str | None],
    name: str,
) -> SecretStr | None:
    value = source.get(name)
    if value is None or not value.strip():
        return None
    return SecretStr(value.strip())


def _resolve_embedding_model(source: Mapping[str, str | None]) -> str:
    value = source.get("GEMINI_EMBEDDING_MODEL")
    if value is None or not value.strip():
        return DEFAULT_EMBEDDING_MODEL
    return value.strip()


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

    return Ok(
        AppSettings(
            model_name=collected["MAGPIE_MODEL"],
            base_url=collected["MAGPIE_BASE_URL"],
            api_key=SecretStr(collected["MAGPIE_API_KEY"]),
            memory_dir=_resolve_memory_dir(source),
            gemini_api_key=_resolve_optional_secret(source, "GEMINI_API_KEY"),
            embedding_model=_resolve_embedding_model(source),
        )
    )
