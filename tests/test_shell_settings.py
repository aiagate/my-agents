import json
from pathlib import Path

from flow_res import Ok

from my_agents.core.types import DEFAULT_PERSONA, AppErrorKind
from my_agents.shell.settings import (
    DEFAULT_LLM_BASE_URL,
    Settings,
    load_persona,
    load_settings,
)
from tests.helpers import assert_err


def test_load_settings_defaults_env_file_and_precedence(tmp_path: Path) -> None:
    defaults = load_settings(env={})
    assert isinstance(defaults, Ok)
    cfg = defaults.value
    assert cfg.llm_model is None
    assert cfg.llm_base_url == DEFAULT_LLM_BASE_URL
    assert cfg.llm_api_key is None
    assert cfg.data_dir == Path.cwd() / "data"
    assert cfg.db_path == cfg.data_dir / "state.db"
    assert cfg.core_memory_path == cfg.data_dir / "core.md"
    assert isinstance(cfg.mcp_servers, Ok)
    assert cfg.mcp_servers.value == ()

    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_MODEL=from-dotenv\nLLM_API_KEY=secret-from-file\nPROACTIVE_DAILY_CAP=9\n",
        encoding="utf-8",
    )
    loaded = load_settings(
        env={
            "LLM_MODEL": "from-env",
            "LLM_BASE_URL": "   ",
            "DB_PATH": " ",
            "DATA_DIR": str(tmp_path / "app"),
            "MCP_SERVERS_JSON": '{"s":{"command":"python"}}',
        },
        env_path=env_file,
    )
    assert isinstance(loaded, Ok)
    resolved = loaded.value
    assert resolved.llm_model == "from-env"
    assert resolved.llm_base_url == DEFAULT_LLM_BASE_URL
    assert resolved.llm_api_key is not None
    assert resolved.llm_api_key.get_secret_value() == "secret-from-file"
    assert resolved.db_path == tmp_path / "app" / "state.db"
    assert resolved.proactive_daily_cap == 9
    assert isinstance(resolved.mcp_servers, Ok)
    assert resolved.mcp_servers.value[0].name == "s"


def test_settings_validators_overrides_and_errors(tmp_path: Path) -> None:
    custom_db = tmp_path / "custom.sqlite"
    validated = Settings.model_validate(
        {
            "DATA_DIR": tmp_path,
            "DB_PATH": custom_db,
            "PERSONA_PATH": None,
            "LLM_BASE_URL": None,
            "LLM_API_KEY": "   ",
            "PROACTIVE_QUIET_HOURS_START_UTC": 21,
            "PROACTIVE_QUIET_HOURS_END_UTC": 6,
            "PROACTIVE_USER_ACTIVE_MINUTES": 12,
        }
    )
    assert validated.data_dir == tmp_path
    assert validated.db_path == custom_db
    assert validated.llm_base_url == DEFAULT_LLM_BASE_URL
    assert validated.llm_api_key is None
    assert (
        validated.proactive_policy.daily_cap,
        validated.proactive_policy.quiet_hours_start_utc,
        validated.proactive_policy.quiet_hours_end_utc,
        validated.proactive_policy.user_active_minutes,
    ) == (5, 21, 6, 12)

    assert_err(
        load_settings(env={"SERVER_PORT": "not-an-int"}),
        kind=AppErrorKind.VALIDATION,
        message_contains="SERVER_PORT",
    )


def test_load_settings_rejects_unreadable_env_and_home_paths(tmp_path: Path) -> None:
    bad_env = tmp_path / ".env"
    bad_env.write_bytes(b"\xff")
    assert_err(
        load_settings(env={}, env_path=bad_env),
        kind=AppErrorKind.VALIDATION,
        message_contains="Cannot read .env file",
    )

    assert_err(
        load_settings(env={"LOG_PATH": "~nonexistent_user_zzz/log.jsonl"}),
        kind=AppErrorKind.VALIDATION,
        message_contains="Could not determine home directory",
    )


def test_load_persona_handles_default_valid_and_invalid_files(tmp_path: Path) -> None:
    default = load_persona(None)
    assert isinstance(default, Ok)
    assert default.value == DEFAULT_PERSONA

    persona_path = tmp_path / "persona.json"
    persona_path.write_text(
        json.dumps({"name": "Yuki", "introduction": "Calm guide."}),
        encoding="utf-8",
    )
    loaded = load_persona(persona_path)
    assert isinstance(loaded, Ok)
    assert loaded.value.name == "Yuki"
    assert loaded.value.boundaries == DEFAULT_PERSONA.boundaries

    assert_err(
        load_persona(tmp_path / "missing.json"),
        kind=AppErrorKind.VALIDATION,
        message_contains="Cannot read persona",
    )

    array_path = tmp_path / "array.json"
    array_path.write_text("[]", encoding="utf-8")
    assert_err(
        load_persona(array_path),
        kind=AppErrorKind.VALIDATION,
        message_contains="JSON object",
    )

    bad_utf8_path = tmp_path / "bad-utf8.json"
    bad_utf8_path.write_bytes(b"\xca")
    assert_err(
        load_persona(bad_utf8_path),
        kind=AppErrorKind.VALIDATION,
        message_contains="Cannot read persona",
    )
