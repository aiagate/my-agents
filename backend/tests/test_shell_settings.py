import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from flow_res import Ok
from pydantic import ValidationError

from my_agents.core.types import DEFAULT_PERSONA, AppErrorKind
from my_agents.shell.settings import (
    DEFAULT_LLM_BASE_URL,
    DEFAULT_TIMEZONE,
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
    assert cfg.timezone == DEFAULT_TIMEZONE
    assert cfg.tzinfo == ZoneInfo("UTC")
    assert cfg.proactive_policy.tz == ZoneInfo("UTC")
    assert cfg.event_max_concurrency == 4
    assert isinstance(cfg.mcp_servers, Ok)
    assert cfg.mcp_servers.value == ()
    assert cfg.web_dist_dir is None
    assert cfg.vapid_private_key is None
    assert cfg.vapid_subject is None
    assert cfg.llm_timeout_seconds == 60.0
    assert cfg.llm_max_retries == 2

    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_MODEL=from-dotenv\nLLM_API_KEY=secret-from-file\nPROACTIVE_DAILY_CAP=9\n",
        encoding="utf-8",
    )
    loaded = load_settings(
        env={
            "LLM_MODEL": "from-env",
            "LLM_BASE_URL": "   ",
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
    validated = Settings.model_validate(
        {
            "DATA_DIR": tmp_path,
            "PERSONA_PATH": None,
            "LLM_BASE_URL": None,
            "LLM_API_KEY": "   ",
            "PROACTIVE_QUIET_HOURS_START": 21,
            "PROACTIVE_QUIET_HOURS_END": 6,
            "PROACTIVE_USER_ACTIVE_MINUTES": 12,
            "TIMEZONE": "Asia/Tokyo",
            "EVENT_MAX_CONCURRENCY": 2,
        }
    )
    assert validated.data_dir == tmp_path
    assert validated.db_path == tmp_path / "state.db"
    assert validated.llm_base_url == DEFAULT_LLM_BASE_URL
    assert validated.llm_api_key is None
    assert validated.event_max_concurrency == 2
    assert (
        validated.proactive_policy.daily_cap,
        validated.proactive_policy.quiet_hours_start,
        validated.proactive_policy.quiet_hours_end,
        validated.proactive_policy.user_active_minutes,
        validated.proactive_policy.tz,
    ) == (5, 21, 6, 12, ZoneInfo("Asia/Tokyo"))

    assert_err(
        load_settings(env={"SERVER_PORT": "not-an-int"}),
        kind=AppErrorKind.VALIDATION,
        message_contains="SERVER_PORT",
    )


def test_settings_validates_timezone_and_event_max_concurrency() -> None:
    blank_tz = load_settings(env={"TIMEZONE": "   "})
    assert isinstance(blank_tz, Ok)
    assert blank_tz.value.timezone == "UTC"
    assert blank_tz.value.tzinfo == ZoneInfo("UTC")

    none_tz = Settings.model_validate({"TIMEZONE": None})
    assert none_tz.timezone == "UTC"

    jst = load_settings(env={"TIMEZONE": "Asia/Tokyo"})
    assert isinstance(jst, Ok)
    assert jst.value.timezone == "Asia/Tokyo"
    assert jst.value.tzinfo == ZoneInfo("Asia/Tokyo")

    assert_err(
        load_settings(env={"TIMEZONE": "Invalid/Zone"}),
        kind=AppErrorKind.VALIDATION,
        message_contains="Unknown IANA timezone",
    )
    with pytest.raises(ValidationError, match="Timezone must be an IANA timezone name"):
        Settings.model_validate({"TIMEZONE": 123})

    concurrency = load_settings(env={"EVENT_MAX_CONCURRENCY": "8"})
    assert isinstance(concurrency, Ok)
    assert concurrency.value.event_max_concurrency == 8

    assert_err(
        load_settings(env={"EVENT_MAX_CONCURRENCY": "0"}),
        kind=AppErrorKind.VALIDATION,
        message_contains="EVENT_MAX_CONCURRENCY",
    )
    assert_err(
        load_settings(env={"EVENT_MAX_CONCURRENCY": "-1"}),
        kind=AppErrorKind.VALIDATION,
        message_contains="EVENT_MAX_CONCURRENCY",
    )


def test_settings_parse_web_dist_and_vapid_fields(tmp_path: Path) -> None:
    loaded = load_settings(
        env={
            "WEB_DIST_DIR": str(tmp_path / "dist"),
            "VAPID_PRIVATE_KEY": "private-material",
            "VAPID_SUBJECT": "mailto:ops@example.com",
        }
    )
    assert isinstance(loaded, Ok)
    cfg = loaded.value
    assert cfg.web_dist_dir == tmp_path / "dist"
    assert cfg.vapid_private_key is not None
    assert cfg.vapid_private_key.get_secret_value() == "private-material"
    assert "private-material" not in repr(cfg.vapid_private_key)
    assert cfg.vapid_subject == "mailto:ops@example.com"

    blank = Settings.model_validate(
        {
            "WEB_DIST_DIR": " ",
            "VAPID_PRIVATE_KEY": " ",
            "VAPID_SUBJECT": " ",
        }
    )
    assert blank.web_dist_dir is None
    assert blank.vapid_private_key is None
    assert blank.vapid_subject is None


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


def test_load_persona_validates_field_types_and_ignores_extra(tmp_path: Path) -> None:
    persona_path = tmp_path / "persona.json"
    persona_path.write_text(
        json.dumps(
            {
                "name": "Yuki",
                "introduction": None,
                "dialogue_policy": "short",
                "extra": "ignored",
            }
        ),
        encoding="utf-8",
    )
    loaded = load_persona(persona_path)
    assert isinstance(loaded, Ok)
    assert loaded.value.name == "Yuki"
    assert loaded.value.introduction == DEFAULT_PERSONA.introduction
    assert loaded.value.dialogue_policy == "short"
    assert loaded.value.boundaries == DEFAULT_PERSONA.boundaries

    persona_path.write_text(json.dumps({"name": 123}), encoding="utf-8")
    assert_err(
        load_persona(persona_path),
        kind=AppErrorKind.VALIDATION,
        message_contains="Invalid persona field 'name'",
    )


def test_load_settings_reports_every_invalid_field() -> None:
    error = assert_err(
        load_settings(
            env={
                "SERVER_PORT": "not-an-int",
                "RECENT_MESSAGES_LIMIT": "0",
            }
        ),
        kind=AppErrorKind.VALIDATION,
        message_contains="Invalid setting 'SERVER_PORT'",
    )
    assert "Invalid setting 'RECENT_MESSAGES_LIMIT'" in error.message
    assert "; " in error.message


def test_settings_llm_timeout_and_retries_validation() -> None:
    custom = load_settings(env={"LLM_TIMEOUT_SECONDS": "12.5", "LLM_MAX_RETRIES": "0"})
    assert isinstance(custom, Ok)
    assert (custom.value.llm_timeout_seconds, custom.value.llm_max_retries) == (12.5, 0)

    higher = load_settings(env={"LLM_TIMEOUT_SECONDS": "5", "LLM_MAX_RETRIES": "4"})
    assert isinstance(higher, Ok)
    assert (higher.value.llm_timeout_seconds, higher.value.llm_max_retries) == (5.0, 4)

    for bad_timeout in ("0", "-1.5", "nan", "inf", "bad"):
        assert_err(
            load_settings(env={"LLM_TIMEOUT_SECONDS": bad_timeout}),
            kind=AppErrorKind.VALIDATION,
            message_contains="LLM_TIMEOUT_SECONDS",
        )

    for bad_retries in ("-1", "bad"):
        assert_err(
            load_settings(env={"LLM_MAX_RETRIES": bad_retries}),
            kind=AppErrorKind.VALIDATION,
            message_contains="LLM_MAX_RETRIES",
        )
