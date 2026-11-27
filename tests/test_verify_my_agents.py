from pathlib import Path

from tools.e2e_event_chat import server_env

_ROOT = Path(__file__).resolve().parents[1]
_SKILL = _ROOT / ".agents/skills/verify-my-agents"
_FEATURES = _SKILL / "features"


def _feature(name: str) -> str:
    return (_FEATURES / name).read_text(encoding="utf-8")


def _all_skill_docs() -> list[Path]:
    return sorted(_SKILL.rglob("*.md"))


def test_skill_and_readme_cover_the_two_layer_server_surface() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    skill = (_SKILL / "SKILL.md").read_text(encoding="utf-8")
    features = _feature("README.md")
    web = _feature("web-server.md")
    webhook = _feature("event-webhook.md")
    server_flags = _feature("server-flags.md")
    for endpoint in (
        "| GET | `/` |",
        "| GET | `/health` |",
        "| GET | `/context?conversation_id=` |",
        "| GET | `/stream?conversation_id=` |",
        "| POST | `/events` |",
        "| POST | `/chat` |",
        "| GET | `/timeline?conversation_id=` |",
        "| GET | `/events/{event_id}` |",
        "| GET | `/memory?conversation_id=` |",
        "| POST | `/reset?conversation_id=` |",
    ):
        assert endpoint in readme, endpoint
    for token in (
        "MCP_SERVERS_JSON",
        "tools/mcp_divergence_server.py",
        "tools/e2e_event_chat.py",
        "stable_prefix_sha256",
        "include_instructions=False",
        "uv run my-agents",
    ):
        assert token in readme, token
    assert "scenario_1" in web
    assert "scenario_11" in web
    assert "scenario_11" in webhook
    assert "web-server.md" in features
    assert "event-webhook.md" in features
    assert "server-flags.md" in features
    assert "tools/e2e_event_chat.py" in skill
    assert "EventSource(" in skill
    assert "--log-file" in server_flags
    assert "SIGTERM" in server_flags
    for stale in (
        "shell" + ".cli",
        "shell" + "/cli",
        "cli" + ".py",
        "my-agents " + "serve",
        "drive" + ".sh",
        "repl" + "-commands",
        "cli" + "-flags",
        "REPL",
    ):
        assert stale not in readme, stale
        assert stale not in skill, stale
        assert stale not in features, stale
    assert not (_SKILL / ("helpers/" + "drive" + ".sh")).exists()
    assert not (_SKILL / ("features/repl" + "-commands.md")).exists()
    assert not (_SKILL / ("features/cli" + "-flags.md")).exists()


def test_skill_documents_per_run_port_and_environment_isolation() -> None:
    skill = (_SKILL / "SKILL.md").read_text(encoding="utf-8")
    for token in (
        "SERVER_HOST=127.0.0.1",
        "SERVER_PORT",
        "DATA_DIR=",
        "DB_PATH=",
        'kill -0 "$pid"',
        'wait "$pid"',
        "kill -INT",
        "http://127.0.0.1:$SERVER_PORT/health",
        '"service": "my-agents"',
    ):
        assert token in skill, token
    for name in ("chat.md", "core-memory.md", "chat-history.md"):
        feature = _feature(name)
        for token in ("DATA_DIR=", "DB_PATH=", "$SERVER_PORT", "kill -INT"):
            assert token in feature, (name, token)


def test_skill_docs_have_no_stale_port_or_log_claims() -> None:
    docs = _all_skill_docs()
    assert len(docs) == 9, [path.name for path in docs]
    for path in docs:
        text = path.read_text(encoding="utf-8")
        for stale in (
            "localhost:8080",
            "エラー時のみ",
            "`-2`",
            "状態ディレクトリ由来のログ",
            "（環境により `-2` / `-15`）を許す",
            "`SIGTERM` は exit 0",
            "`SIGTERM` で exit 0",
            "`SIGTERM` → `0`",
        ):
            assert stale not in text, (path.name, stale)


def test_docs_state_exact_signal_exit_codes() -> None:
    skill = (_SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "`SIGINT` は graceful shutdown して exit 0" in skill
    assert "`SIGTERM` も\n  graceful shutdown したうえで信号を再 raise" in skill
    assert "143" in skill
    assert "`-15`" in skill

    features = _feature("README.md")
    assert "`SIGINT`（exit 0）" in features
    assert "`SIGTERM`（shell は 143、`Popen.returncode` は `-15`）" in features

    flags = _feature("server-flags.md")
    assert "`SIGINT` は graceful shutdown で exit 0" in flags
    assert "`Popen.returncode` は `-15`" in flags
    assert "finally: logger.close()` と stdout の flush が走らない" in flags

    webhook = _feature("event-webhook.md")
    assert "`SIGINT` 後の graceful shutdown で exit `0`" in webhook
    assert "`SIGTERM` で `-15`（shell では `143`" in webhook


def test_server_flags_document_log_file_writes_on_successful_calls() -> None:
    flags = _feature("server-flags.md")
    for token in (
        "0600",
        "settings_load_failed",
        "chat_completed",
        "event_evaluated",
        "server_start_failed",
        "no-op logger",
    ):
        assert token in flags, token
    assert "1 呼び出し 1 行" in flags


def test_error_evidence_distinguishes_settings_and_engine_failures() -> None:
    evidence = _feature("error-evidence.md")
    for token in (
        "settings_load_failed",
        "LLM_MODEL",
        "LLM_API_KEY",
        "--debug",
        "engine 初期化失敗",
        "追記なし（空のまま）",
        "プロセス環境変数が `.env` より優先される",
    ):
        assert token in evidence, token


def test_e2e_server_env_ignores_ambient_and_pins_test_paths() -> None:
    root = Path("/tmp/verify-my-agents-test")
    env = server_env(
        root,
        18123,
        18234,
        {"MCP_SERVERS_JSON": '{"mock":{}}'},
        ambient={
            "PATH": "/usr/bin",
            "DB_PATH": "/ambient/leak.db",
            "DATA_DIR": "/ambient",
            "LOG_PATH": "/ambient/leak.jsonl",
            "PERSONA_PATH": "/ambient/persona.json",
            "SERVER_HOST": "0.0.0.0",
            "SERVER_PORT": "8080",
            "LLM_MODEL": "ambient-model",
            "MCP_SERVERS_JSON": '{"ambient":{}}',
            "KEEP_ME": "1",
        },
    )
    assert env["DB_PATH"] == str(root / "state.db")
    assert env["DATA_DIR"] == str(root)
    assert env["LOG_PATH"] == ""
    assert env["PERSONA_PATH"] == ""
    assert env["SERVER_HOST"] == "127.0.0.1"
    assert env["SERVER_PORT"] == "18123"
    assert env["LLM_MODEL"] == "mock-model"
    assert env["LLM_BASE_URL"] == "http://127.0.0.1:18234/v1"
    assert env["MCP_SERVERS_JSON"] == '{"mock":{}}'
    assert env["KEEP_ME"] == "1"


def test_e2e_script_checks_liveness_and_exact_signal_exit_codes() -> None:
    script = (_ROOT / "tools/e2e_event_chat.py").read_text(encoding="utf-8")
    for token in (
        "process.poll() is not None",
        "my-agents listening on http://127.0.0.1:{port}",
        "process.send_signal(signal.SIGINT)",
        "assert exit_code == 0",
        "process.send_signal(signal.SIGTERM)",
        "assert exit_code == -signal.SIGTERM",
        "sigterm_exit",
    ):
        assert token in script, token
    assert "in (0, -2, -15)" not in script
