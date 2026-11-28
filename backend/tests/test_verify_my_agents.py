from pathlib import Path

from tools.e2e_event_chat import server_env


def test_e2e_server_env_ignores_ambient_and_pins_test_paths() -> None:
    root = Path("/tmp/verify-my-agents-test")
    env = server_env(
        root,
        18123,
        18234,
        {"MCP_SERVERS_JSON": '{"mock":{}}'},
        ambient={
            "PATH": "/usr/bin",
            "DATA_DIR": "/ambient",
            "LOG_PATH": "/ambient/leak.jsonl",
            "PERSONA_PATH": "/ambient/persona.json",
            "SERVER_HOST": "0.0.0.0",
            "SERVER_PORT": "8080",
            "LLM_MODEL": "ambient-model",
            "LLM_BASE_URL": "http://ambient.example/v1",
            "MCP_SERVERS_JSON": '{"ambient":{}}',
            "WEB_DIST_DIR": "/ambient/dist",
            "KEEP_ME": "1",
        },
    )
    assert env["DATA_DIR"] == str(root)
    assert env["LOG_PATH"] == str(root / "app.jsonl")
    assert env["PERSONA_PATH"] == ""
    assert env["WEB_DIST_DIR"] == str(root / "no-web-dist")
    assert env["SERVER_HOST"] == "127.0.0.1"
    assert env["SERVER_PORT"] == "18123"
    assert env["LLM_MODEL"] == "mock-model"
    assert env["LLM_BASE_URL"] == "http://127.0.0.1:18234/v1"
    assert env["MCP_SERVERS_JSON"] == '{"mock":{}}'
    assert env["KEEP_ME"] == "1"
    for retired in (
        "DB_PATH",
        "CORS_ALLOW_ORIGINS",
        "VAPID_PUBLIC_KEY",
        "EVENT_EVAL_DELAY_MS",
    ):
        assert retired not in env, retired
