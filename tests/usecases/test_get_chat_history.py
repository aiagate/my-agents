import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from my_agents.contracts.ports.chat_history import NewDialogueTurn
from my_agents.infrastructure.sqlite_chat_repository import SqliteChatRepository
from my_agents.usecases.get_chat_history import (
    DialogueTurnView,
    GetChatHistoryErrorType,
    GetChatHistoryHandler,
)
from tests.helpers import assert_err

_NOW = datetime(2026, 11, 22, tzinfo=UTC)


def test_list_and_detail_expose_full_read_only_history(tmp_path: Path) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    handler = GetChatHistoryHandler(repo)
    assert handler.list_episodes().unwrap().episodes == ()
    first = repo.create_episode("id1", title="first", started_at=_NOW).unwrap()
    repo.append_turns(
        first.id,
        (
            NewDialogueTurn("user", "first question", _NOW),
            NewDialogueTurn("assistant", "first answer", _NOW),
        ),
        updated_at=_NOW,
    ).unwrap()
    repo.close_episode(first.id, closed_at=_NOW, reason="clear").unwrap()
    second = repo.create_episode(
        "id2", title="second", started_at=_NOW + timedelta(minutes=1)
    ).unwrap()
    listing = handler.list_episodes().unwrap()
    assert listing.location == str(path)
    assert [
        (e.index, e.episode_id, e.is_active, e.close_reason, e.turn_count)
        for e in listing.episodes
    ] == [(1, second.id, True, None, 0), (2, first.id, False, "clear", 2)]
    detail = handler.get_episode_detail(2).unwrap()
    assert detail.episode == listing.episodes[1]
    assert detail.turns == (
        DialogueTurnView(1, "user", "first question", _NOW),
        DialogueTurnView(2, "assistant", "first answer", _NOW),
    )
    for index in (0, 3):
        assert_err(
            handler.get_episode_detail(index),
            expected_type=GetChatHistoryErrorType.INVALID_INDEX,
            message_contains=f"Invalid history index: {index}",
            display_message_contains="会話の番号を指定",
        )
    repo.close_episode(second.id, closed_at=_NOW, reason="clear").unwrap()
    assert not handler.list_episodes().unwrap().episodes[0].is_active


def test_history_and_recall_handle_missing_storage_tables(tmp_path: Path) -> None:
    from my_agents.contracts.ports.chat_recall import ChatRecallErrorType
    from my_agents.usecases.recall_chat import RecallChatHandler

    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    repo.create_episode("id", title="title", started_at=_NOW).unwrap()
    history = GetChatHistoryHandler(repo)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE dialogue_turns")
    assert_err(
        history.get_episode_detail(1),
        expected_type=GetChatHistoryErrorType.STORAGE_ERROR,
        display_message_contains="会話履歴を読み込めませんでした",
        message_contains="no such table: dialogue_turns",
    )
    assert_err(
        RecallChatHandler(repo).recall_episode("id"),
        expected_type=ChatRecallErrorType.STORAGE_ERROR,
        message_contains="no such table: dialogue_turns",
    )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE episodes")
    assert_err(
        history.list_episodes(),
        expected_type=GetChatHistoryErrorType.STORAGE_ERROR,
        display_message_contains="会話履歴を読み込めませんでした",
        message_contains="no such table: episodes",
    )
    assert_err(
        history.get_episode_detail(1),
        expected_type=GetChatHistoryErrorType.STORAGE_ERROR,
        display_message_contains="会話履歴を読み込めませんでした",
        message_contains="no such table: episodes",
    )
