import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from my_agents.infrastructure.sqlite_chat_repository import SqliteChatRepository
from my_agents.usecases.finalize_chat import FinalizeChatHandler
from tests.usecases.test_clear_chat import RecordingSummarizer, add_turn


@pytest.mark.anyio
async def test_finalize_keeps_episode_open_after_summary_and_skips_finished_work(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    summarizer = RecordingSummarizer()
    finalize = FinalizeChatHandler(repo, repo, summarizer)
    assert (await finalize.execute()).warnings == ()
    assert not path.exists()
    add_turn(repo)
    assert (await finalize.execute()).warnings == ()
    assert (await finalize.execute()).warnings == ()
    episode = repo.get_latest_episode().unwrap()
    assert episode is not None
    assert (episode.closed_at, episode.summarized_turn_count, episode.summary) == (
        None,
        1,
        "summary",
    )
    repo.close_episode(
        episode.id, closed_at=episode.updated_at, reason="clear"
    ).unwrap()
    assert (await finalize.execute()).warnings == ()
    assert len(summarizer.requests) == 1


@pytest.mark.anyio
async def test_finalize_reports_failed_reads_and_summary_writes(tmp_path: Path) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    summarizer = RecordingSummarizer()
    finalize = FinalizeChatHandler(repo, repo, summarizer)
    add_turn(repo)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER reject_summary BEFORE UPDATE OF summary ON episodes BEGIN SELECT RAISE(ABORT, 'write denied'); END"
        )
    (warning,) = (await finalize.execute()).warnings
    assert (warning.event, warning.error_type, warning.message) == (
        "episode_summary_write_failed",
        "WRITE_FAILED",
        "write denied",
    )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE dialogue_turns")
    (warning,) = (await finalize.execute()).warnings
    assert (warning.event, warning.error_type, warning.message) == (
        "summary_turns_read_failed",
        "READ_FAILED",
        "no such table: dialogue_turns",
    )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE episodes")
    (warning,) = (await finalize.execute()).warnings
    assert (warning.event, warning.error_type, warning.message) == (
        "finalize_chat_read_failed",
        "READ_FAILED",
        "no such table: episodes",
    )
