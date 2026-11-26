from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SKILL = _ROOT / ".agents/skills/verify-my-agents"


def test_skill_and_readme_cover_the_two_layer_server_surface() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    skill = (_SKILL / "SKILL.md").read_text(encoding="utf-8")
    features = (_SKILL / "features/README.md").read_text(encoding="utf-8")
    web = (_SKILL / "features/web-server.md").read_text(encoding="utf-8")
    webhook = (_SKILL / "features/event-webhook.md").read_text(encoding="utf-8")
    server_flags = (_SKILL / "features/server-flags.md").read_text(encoding="utf-8")
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
