import subprocess
from pathlib import Path

import pytest

_DRIVER = (
    Path(__file__).resolve().parents[1]
    / ".agents/skills/verify-my-agents/helpers/drive.sh"
)


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
