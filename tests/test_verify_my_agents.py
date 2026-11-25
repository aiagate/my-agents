import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_DRIVER = _ROOT / ".agents/skills/verify-my-agents/helpers/drive.sh"
_SKILL = _ROOT / ".agents/skills/verify-my-agents"


@pytest.mark.parametrize("shared_db", [False, True])
def test_verify_driver_overrides_the_inherited_chat_database(
    tmp_path: Path, shared_db: bool
) -> None:
    production = tmp_path / "production.db"
    production.write_text("existing chats\n")
    commands = tmp_path / "commands.txt"
    commands.write_text("exit\n")
    launcher = tmp_path / "launcher.sh"
    launcher.write_text('printf "verification chat\\n" > "$CHATS_DB_PATH"\n')
    scratch = tmp_path / "artifacts/verify/run/tmp"
    env = {"CHATS_DB_PATH": str(production), "MY_AGENTS_BIN": f"sh {launcher.name}"}
    expected = scratch / "chat-memory/chats.db"
    if shared_db:
        expected = scratch / "shared.db"
        env["VERIFY_CHATS_DB_PATH"] = str(expected)

    subprocess.run(
        ("sh", str(_DRIVER), "run", "chat", str(commands)),
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        timeout=5,
    )

    assert expected.read_text() == "verification chat\n"
    assert production.read_text() == "existing chats\n"
    assert "exit=0" in (scratch.parent / "chat.txt").read_text()


def test_skill_and_readme_cover_the_two_layer_server_surface() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    skill = (_SKILL / "SKILL.md").read_text(encoding="utf-8")
    features = (_SKILL / "features/README.md").read_text(encoding="utf-8")
    web = (_SKILL / "features/web-server.md").read_text(encoding="utf-8")
    webhook = (_SKILL / "features/event-webhook.md").read_text(encoding="utf-8")
    for endpoint in (
        "| GET | `/` |",
        "| GET | `/health` |",
        "| GET | `/context?conversation_id=` |",
        "| GET | `/stream?conversation_id=` |",
        "| POST | `/events` |",
        "| POST | `/chat` |",
        "| GET | `/memory?conversation_id=` |",
        "| POST | `/curate` |",
        "| POST | `/reset?conversation_id=` |",
    ):
        assert endpoint in readme, endpoint
    for token in (
        "MCP_SERVERS_JSON",
        "tools/mcp_divergence_server.py",
        "tools/e2e_event_chat.py",
        "stable_prefix_sha256",
        "include_instructions=False",
    ):
        assert token in readme, token
    assert "scenario_1" in web
    assert "scenario_11" in web
    assert "scenario_11" in webhook
    assert "web-server.md" in features
    assert "event-webhook.md" in features
    assert "tools/e2e_event_chat.py" in skill
    assert "EventSource(" in skill


def test_verify_helpers_preserve_isolated_databases_and_logs() -> None:
    driver = _DRIVER.read_text(encoding="utf-8")
    assert 'MEMORY_PATH="$memory_dir"' in driver
    assert 'CHATS_DB_PATH="$chats_db"' in driver
    assert '--log-file "$log_file"' in driver
    assert "VERIFY_CHATS_DB_PATH" in driver
    assert "artifacts/verify/$run_id" in driver
