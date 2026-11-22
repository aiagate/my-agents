from datetime import UTC, datetime
from pathlib import Path

import pytest

from my_agents.contracts.ports.chat_history import NewDialogueTurn
from my_agents.contracts.ports.chat_recall import (
    ChatRecallErrorType,
    ChatSearchHit,
    RecalledTurn,
)
from my_agents.infrastructure.sqlite_chat_repository import SqliteChatRepository
from my_agents.usecases.recall_chat import RecallChatHandler
from tests.helpers import assert_err

_NOW = datetime(2026, 11, 22, tzinfo=UTC)


def test_recall_and_search_persisted_conversation(tmp_path: Path) -> None:
    repo = SqliteChatRepository(tmp_path / "chats.db")
    handler = RecallChatHandler(repo)
    assert_err(
        handler.recall_episode("missing"),
        expected_type=ChatRecallErrorType.NOT_FOUND,
        message_contains="Episode not found: missing",
    )
    first = repo.create_episode("id", title="title", started_at=_NOW).unwrap()
    repo.append_turns(
        first.id,
        (
            NewDialogueTurn("user", "searchable question", _NOW),
            NewDialogueTurn("assistant", "reply", _NOW),
        ),
        updated_at=_NOW,
    ).unwrap()
    repo.update_episode_summary(
        first.id,
        title="title",
        overview="overview",
        summary="summary",
        summarized_turn_count=2,
    ).unwrap()
    result = handler.recall_episode(" id ", limit=1).unwrap()
    assert (
        result.episode_id,
        result.title,
        result.overview,
        result.summary,
        result.started_at,
        result.updated_at,
    ) == ("id", "title", "overview", "summary", _NOW, _NOW)
    assert result.turns == (RecalledTurn(2, "assistant", "reply", _NOW),)
    assert result.total_turn_count == 2
    assert result.truncated
    assert not handler.recall_episode(first.id).unwrap().truncated
    assert handler.search_episodes(" searchable ").unwrap() == (
        ChatSearchHit("id", "title", "overview", _NOW, _NOW, 2),
    )


@pytest.mark.parametrize(("text", "limit"), [(" ", 1), ("valid", 0)])
def test_recall_rejects_invalid_inputs(tmp_path: Path, text: str, limit: int) -> None:
    handler = RecallChatHandler(SqliteChatRepository(tmp_path / "chats.db"))
    assert_err(
        handler.recall_episode(text, limit=limit),
        expected_type=ChatRecallErrorType.INVALID_INPUT,
        message_contains="Episode ID and a positive limit",
    )
    assert_err(
        handler.search_episodes(text, limit=limit),
        expected_type=ChatRecallErrorType.INVALID_INPUT,
        message_contains="Search text and a positive limit",
    )


def test_recall_propagates_storage_errors(tmp_path: Path) -> None:
    path = tmp_path / "chats.db"
    path.write_text("broken")
    handler = RecallChatHandler(SqliteChatRepository(path))
    assert_err(
        handler.recall_episode("valid"),
        expected_type=ChatRecallErrorType.STORAGE_ERROR,
        message_contains="file is not a database",
    )
    assert_err(
        handler.search_episodes("valid"),
        expected_type=ChatRecallErrorType.STORAGE_ERROR,
        message_contains="file is not a database",
    )
