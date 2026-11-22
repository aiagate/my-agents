import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from my_agents.contracts.ports.chat_history import ChatStorageErrorType, NewDialogueTurn
from my_agents.infrastructure.sqlite_chat_repository import SqliteChatRepository
from tests.helpers import assert_err

_NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)


def _execute(path: Path, sql: str) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(sql)


def test_lazy_creation_and_sqlite_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "new-parent" / "chats.db"
    repo = SqliteChatRepository(path)
    assert repo.location == str(path)
    assert repo.get_latest_episode().unwrap() is None
    assert repo.get_episode("missing").unwrap() is None
    assert repo.list_episodes().unwrap() == ()
    assert repo.list_turns("missing").unwrap() == ()
    assert repo.search_episodes("missing").unwrap() == ()
    assert not path.parent.exists()
    assert_err(
        repo.close_episode("missing", closed_at=_NOW, reason="clear"),
        expected_type=ChatStorageErrorType.NOT_FOUND,
        message_contains="Episode not found: missing",
    )
    assert repo.get_latest_episode().unwrap() is None
    assert repo.get_episode("missing").unwrap() is None
    first = repo.create_episode(
        "z-first", title="find in title", started_at=_NOW
    ).unwrap()
    assert first.turn_count == first.summarized_turn_count == 0
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA foreign_key_list(dialogue_turns)").fetchone()[
            2:5
        ] == ("episodes", "episode_id", "id")
    turns = (
        NewDialogueTurn("user", "find in content %_\\", _NOW),
        NewDialogueTurn("assistant", "reply", _NOW),
    )
    updated = repo.append_turns(first.id, turns, updated_at=_NOW).unwrap()
    assert updated.turn_count == 2
    assert (
        repo.append_turns(first.id, (), updated_at=_NOW - timedelta(days=1)).unwrap()
        == updated
    )
    assert [
        (turn.turn_index, turn.role, turn.content, turn.created_at)
        for turn in repo.list_turns(first.id).unwrap()
    ] == [(1, "user", "find in content %_\\", _NOW), (2, "assistant", "reply", _NOW)]
    assert [turn.content for turn in repo.list_turns(first.id, limit=1).unwrap()] == [
        "reply"
    ]
    assert repo.list_turns(first.id, limit=0).unwrap() == ()
    assert repo.list_episodes(limit=0).unwrap() == ()
    assert repo.search_episodes("find", limit=0).unwrap() == ()
    for query in ("%", "_", "\\", "title", "content"):
        assert [episode.id for episode in repo.search_episodes(query).unwrap()] == [
            first.id
        ]
    summarized = repo.update_episode_summary(
        first.id,
        title=first.title,
        overview="find in overview",
        summary="find in summary",
        summarized_turn_count=2,
    ).unwrap()
    assert (
        summarized.overview,
        summarized.summary,
        summarized.summarized_turn_count,
    ) == ("find in overview", "find in summary", 2)
    assert (
        repo.update_episode_summary(
            first.id, title="stale", overview="", summary="", summarized_turn_count=1
        ).unwrap()
        == summarized
    )
    assert_err(
        repo.update_episode_summary(
            first.id, title="x", overview="", summary="", summarized_turn_count=3
        ),
        expected_type=ChatStorageErrorType.WRITE_FAILED,
        message_contains="Invalid summarized turn count: 3",
    )
    for query in ("overview", "summary"):
        assert repo.search_episodes(query).unwrap() == (summarized,)
    closed = repo.close_episode(first.id, closed_at=_NOW, reason="rollover").unwrap()
    assert (closed.closed_at, closed.close_reason) == (_NOW, "rollover")
    cleared = repo.close_episode(
        first.id, closed_at=_NOW + timedelta(hours=1), reason="clear"
    ).unwrap()
    assert (cleared.closed_at, cleared.close_reason) == (_NOW, "clear")
    assert_err(
        repo.append_turns(first.id, turns, updated_at=_NOW),
        expected_type=ChatStorageErrorType.WRITE_FAILED,
        message_contains="Episode is closed",
    )
    second = repo.create_episode(
        "a-second", title="find second", started_at=_NOW
    ).unwrap()
    assert repo.get_latest_episode().unwrap() == second
    assert repo.list_episodes(limit=1).unwrap() == (second,)
    assert repo.list_episodes().unwrap() == (second, cleared)
    assert repo.search_episodes("find").unwrap() == (second, cleared)
    assert repo.search_episodes("find", limit=1).unwrap() == (second,)
    assert repo.get_episode(first.id).unwrap() == cleared
    assert_err(
        repo.create_episode("third", title="collision", started_at=_NOW),
        expected_type=ChatStorageErrorType.WRITE_FAILED,
        message_contains="one_open_episode",
    )


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("UPDATE episodes SET title = X'00'", "Invalid episode row"),
        ("UPDATE episodes SET close_reason = X'00'", "Invalid episode close reason"),
        (
            "UPDATE episodes SET close_reason = 'unknown'",
            "Invalid episode close reason",
        ),
        ("UPDATE episodes SET closed_at = X'00'", "Invalid episode close timestamp"),
        ("UPDATE episodes SET summarized_turn_count = 5", "Inconsistent episode state"),
        ("UPDATE episodes SET close_reason = 'clear'", "Inconsistent episode state"),
        ("UPDATE episodes SET updated_at = 'bad'", "Invalid isoformat string"),
        (
            "UPDATE episodes SET started_at = '2026-11-22'",
            "Timestamp must use UTC",
        ),
        (
            "UPDATE episodes SET started_at = '0001-01-01T00:00:00+01:00'",
            "Timestamp must use UTC",
        ),
        (
            "UPDATE episodes SET started_at = '2026-11-22T12:00:00+09:00'",
            "Timestamp must use UTC",
        ),
    ],
)
def test_reports_corrupt_episode_reads_and_writes(
    tmp_path: Path, sql: str, message: str
) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    created = repo.create_episode("episode", title="title", started_at=_NOW).unwrap()
    _execute(path, sql)
    assert_err(
        repo.get_episode(created.id),
        expected_type=ChatStorageErrorType.CORRUPT_DATA,
        message_contains=message,
    )
    assert_err(
        repo.close_episode(created.id, closed_at=_NOW, reason="clear"),
        expected_type=ChatStorageErrorType.CORRUPT_DATA,
        message_contains=message,
    )


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("UPDATE dialogue_turns SET content = X'00'", "Invalid dialogue turn row"),
        ("UPDATE dialogue_turns SET role = 'system'", "Invalid dialogue turn role"),
        ("UPDATE dialogue_turns SET role = X'00'", "Invalid dialogue turn role"),
        ("UPDATE dialogue_turns SET created_at = 'no'", "Invalid isoformat string"),
    ],
)
def test_reports_corrupt_dialogue(tmp_path: Path, sql: str, message: str) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    episode = repo.create_episode("episode", title="title", started_at=_NOW).unwrap()
    repo.append_turns(
        episode.id, (NewDialogueTurn("user", "question", _NOW),), updated_at=_NOW
    ).unwrap()
    _execute(path, sql)
    assert_err(
        repo.list_turns(episode.id),
        expected_type=ChatStorageErrorType.CORRUPT_DATA,
        message_contains=message,
    )


def test_reports_io_errors(tmp_path: Path) -> None:
    path = tmp_path / "chats.db"
    path.write_text("not a database")
    assert_err(
        SqliteChatRepository(path).get_latest_episode(),
        expected_type=ChatStorageErrorType.READ_FAILED,
        message_contains="file is not a database",
    )
    assert_err(
        SqliteChatRepository(path / "child").create_episode(
            "episode", title="title", started_at=_NOW
        ),
        expected_type=ChatStorageErrorType.WRITE_FAILED,
        message_contains="chats.db",
    )


def test_failed_append_rolls_back_the_whole_turn_pair(tmp_path: Path) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    episode = repo.create_episode("episode", title="title", started_at=_NOW).unwrap()
    _execute(
        path,
        "CREATE TRIGGER reject_assistant BEFORE INSERT ON dialogue_turns WHEN NEW.role = 'assistant' BEGIN SELECT RAISE(ABORT, 'rejected'); END;",
    )
    turns = (
        NewDialogueTurn("user", "question", _NOW),
        NewDialogueTurn("assistant", "reply", _NOW),
    )
    assert_err(
        repo.append_turns(episode.id, turns, updated_at=_NOW),
        expected_type=ChatStorageErrorType.WRITE_FAILED,
        message_contains="rejected",
    )
    assert repo.get_episode(episode.id).unwrap() == episode
    assert repo.list_turns(episode.id).unwrap() == ()


def test_update_that_loses_episode_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    episode = repo.create_episode("episode", title="title", started_at=_NOW).unwrap()
    _execute(
        path,
        "CREATE TRIGGER delete_episode AFTER UPDATE ON episodes BEGIN DELETE FROM episodes; END;",
    )
    assert_err(
        repo.close_episode(episode.id, closed_at=_NOW, reason="clear"),
        expected_type=ChatStorageErrorType.CORRUPT_DATA,
        message_contains="Episode disappeared during update: episode",
    )
    assert repo.get_episode(episode.id).unwrap() == episode


def test_failed_first_record_leaves_no_empty_episode(tmp_path: Path) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    assert_err(
        repo.record_turns("missing", (), title="unused", updated_at=_NOW),
        expected_type=ChatStorageErrorType.NOT_FOUND,
        message_contains="Episode not found: missing",
    )
    _execute(
        path,
        "CREATE TRIGGER reject_assistant BEFORE INSERT ON dialogue_turns WHEN NEW.role = 'assistant' BEGIN SELECT RAISE(ABORT, 'rejected'); END;",
    )
    assert_err(
        repo.record_turns(
            None,
            (
                NewDialogueTurn("user", "question", _NOW),
                NewDialogueTurn("assistant", "reply", _NOW),
            ),
            title="question",
            updated_at=_NOW,
        ),
        expected_type=ChatStorageErrorType.WRITE_FAILED,
        message_contains="rejected",
    )
    assert repo.list_episodes().unwrap() == ()
    assert repo.get_open_episode().unwrap() is None


def test_concurrent_first_records_attach_to_the_same_open_episode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    ready = Barrier(2)

    def write_exchange(question: str) -> str:
        connection = SqliteChatRepository(path)
        assert connection.get_open_episode().unwrap() is None
        ready.wait(timeout=5)
        return (
            connection.record_turns(
                None,
                (
                    NewDialogueTurn("user", question, _NOW),
                    NewDialogueTurn("assistant", f"reply to {question}", _NOW),
                ),
                title=question,
                updated_at=_NOW,
            )
            .unwrap()
            .id
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(write_exchange, ("first", "second"))
    (episode,) = repo.list_episodes().unwrap()
    assert episode.id == first == second
    assert repo.get_open_episode().unwrap() == episode
    assert episode.turn_count == 4
    turns = repo.list_turns(episode.id).unwrap()
    assert {
        tuple(turn.content for turn in turns[:2]),
        tuple(turn.content for turn in turns[2:]),
    } == {("first", "reply to first"), ("second", "reply to second")}


def test_record_rejects_closed_episode_and_rolls_back_when_update_loses_episode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    repo.create_episode("closed", title="old", started_at=_NOW).unwrap()
    repo.close_episode("closed", closed_at=_NOW, reason="clear").unwrap()
    turns = (NewDialogueTurn("user", "question", _NOW),)
    assert_err(
        repo.record_turns("closed", turns, title="question", updated_at=_NOW),
        expected_type=ChatStorageErrorType.WRITE_FAILED,
        message_contains="Episode is closed: closed",
    )
    opened = repo.create_episode("open", title="open", started_at=_NOW).unwrap()
    _execute(
        path,
        """
        CREATE TRIGGER lose_episode AFTER UPDATE OF turn_count ON episodes
        BEGIN
            DELETE FROM dialogue_turns WHERE episode_id = NEW.id;
            DELETE FROM episodes WHERE id = NEW.id;
        END;
    """,
    )
    assert_err(
        repo.record_turns(opened.id, turns, title="question", updated_at=_NOW),
        expected_type=ChatStorageErrorType.CORRUPT_DATA,
        message_contains="Episode disappeared: open",
    )
    assert repo.get_open_episode().unwrap() == opened
    assert repo.list_turns(opened.id).unwrap() == ()


@pytest.mark.parametrize("naive_turn", [False, True])
def test_record_rejects_naive_timestamp_and_rolls_back(
    tmp_path: Path, naive_turn: bool
) -> None:
    repo = SqliteChatRepository(tmp_path / "chats.db")
    naive = _NOW.replace(tzinfo=None)
    assert_err(
        repo.record_turns(
            None,
            (
                NewDialogueTurn("user", "question", _NOW),
                NewDialogueTurn("assistant", "reply", naive if naive_turn else _NOW),
            ),
            title="question",
            updated_at=_NOW if naive_turn else naive,
        ),
        expected_type=ChatStorageErrorType.CORRUPT_DATA,
        message_contains="Timestamp must use UTC.",
    )
    assert repo.list_episodes().unwrap() == ()
