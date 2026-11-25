import json
from pathlib import Path

from pydantic import SecretStr

from my_agents.core.types import DEFAULT_PERSONA, AppErrorKind
from my_agents.shell.settings import Settings, load_persona, load_settings
from tests.helpers import assert_err


def test_load_settings_defaults(tmp_path: Path) -> None:
    settings = load_settings(env={"MEMORY_PATH": str(tmp_path)}).unwrap()
    assert settings.chat_backend == "local"
    assert settings.db_path == tmp_path / "chats.db"
    assert settings.core_memory_path == tmp_path / "core.md"
    assert settings.server_port == 8080
    assert settings.proactive_policy.cooldown_minutes == 180


def test_load_settings_parses_aliases_and_optional_paths(tmp_path: Path) -> None:
    settings = load_settings(
        env={
            "MAGPIE_MODEL": "model",
            "MAGPIE_BASE_URL": "https://example.test/v1",
            "MAGPIE_API_KEY": "secret",
            "MEMORY_PATH": f" {tmp_path} ",
            "CHATS_DB_PATH": str(tmp_path / "custom.db"),
            "PERSONA_PATH": str(tmp_path / "persona.json"),
            "MCP_SERVERS_JSON": '{"mcp": true}',
            "CHAT_BACKEND": "openai",
            "SERVER_HOST": "0.0.0.0",
            "SERVER_PORT": "9000",
            "EVENT_ANALYSIS_DELAY_MS": "1500",
            "PROACTIVE_COOLDOWN_MINUTES": "5",
            "PROACTIVE_DAILY_CAP": "2",
            "QUIET_HOURS_START": "21",
            "QUIET_HOURS_END": "8",
            "PROACTIVE_USER_ACTIVE_MINUTES": "0",
            "EPISODE_MAX_TURNS": "10",
            "EPISODE_GAP_MINUTES": "20",
            "SUMMARY_EVERY_TURNS": "3",
            "RECENT_TURNS_LIMIT": "4",
            "LOG_FILE": str(tmp_path / "log.jsonl"),
        }
    ).unwrap()
    assert settings.magpie_api_key == SecretStr("secret")
    assert settings.chats_db_path == tmp_path / "custom.db"
    assert settings.persona_path == tmp_path / "persona.json"
    assert settings.mcp_servers_json == SecretStr('{"mcp": true}')
    assert (settings.server_host, settings.server_port) == ("0.0.0.0", 9000)
    assert settings.event_analysis_delay_ms == 1500
    assert settings.proactive_policy.user_active_minutes == 0
    assert settings.log_file == tmp_path / "log.jsonl"


def test_load_settings_blank_paths_use_defaults() -> None:
    settings = load_settings(
        env={
            "MEMORY_PATH": "   ",
            "CHATS_DB_PATH": " ",
            "PERSONA_PATH": " ",
            "LOG_FILE": " ",
        }
    ).unwrap()
    assert settings.memory_dir == Path.cwd() / "memory"
    assert settings.chats_db_path is None
    assert settings.persona_path is None
    assert settings.log_file is None


def test_load_settings_merges_dotenv_with_process_env(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MEMORY_PATH=/env/memory\nSERVER_PORT=7000\nMAGPIE_MODEL=env-model\n",
        encoding="utf-8",
    )
    settings = load_settings(
        env={"MEMORY_PATH": str(tmp_path), "MAGPIE_MODEL": "process-model"},
        env_path=env_file,
    ).unwrap()
    assert settings.memory_dir == tmp_path
    assert settings.magpie_model == "process-model"
    assert settings.server_port == 7000


def test_load_settings_reports_validation_errors() -> None:
    assert_err(
        load_settings(env={"CHAT_BACKEND": "unknown"}),
        kind=AppErrorKind.VALIDATION,
        message_contains="CHAT_BACKEND",
    )
    assert_err(
        load_settings(env={"SERVER_PORT": "70000"}),
        kind=AppErrorKind.VALIDATION,
        message_contains="SERVER_PORT",
    )


def test_settings_accepts_direct_construction() -> None:
    settings = Settings(MEMORY_PATH=Path("/tmp/memory"))
    assert settings.memory_dir == Path("/tmp/memory")
    blank = Settings(CHATS_DB_PATH=None, MAGPIE_API_KEY=" ")
    assert blank.chats_db_path is None
    assert blank.magpie_api_key is None


def test_load_persona_defaults_and_json(tmp_path: Path) -> None:
    assert load_persona(None).unwrap() == DEFAULT_PERSONA
    path = tmp_path / "persona.json"
    path.write_text(json.dumps({"name": "A", "introduction": "I"}), encoding="utf-8")
    persona = load_persona(path).unwrap()
    assert (persona.name, persona.introduction) == ("A", "I")
    assert persona.dialogue_policy == DEFAULT_PERSONA.dialogue_policy


def test_load_persona_reports_bad_files(tmp_path: Path) -> None:
    assert_err(
        load_persona(tmp_path / "missing.json"),
        kind=AppErrorKind.VALIDATION,
        message_contains="Cannot read persona",
    )
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert_err(
        load_persona(bad),
        kind=AppErrorKind.VALIDATION,
        message_contains="Cannot read persona",
    )
    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    assert_err(
        load_persona(array),
        kind=AppErrorKind.VALIDATION,
        message_contains="JSON object",
    )
