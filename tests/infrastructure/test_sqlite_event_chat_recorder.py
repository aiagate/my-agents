from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from flow_res import Err, Ok, Result

from my_agents.contracts.ports.chat_history import (
    ChatStorageError,
    ChatStorageErrorType,
    EpisodeCloseReason,
    EpisodeRecord,
    IChatStore,
    NewDialogueTurn,
)
from my_agents.infrastructure.sqlite_chat_repository import SqliteChatRepository
from my_agents.infrastructure.sqlite_event_chat_recorder import (
    SqliteEventChatRecorder,
)
from tests.helpers import assert_err

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


class ErrorStore(IChatStore):
    @property
    def location(self) -> str:
        return "/tmp/error.db"

    def record_turns(
        self,
        episode_id: str | None,
        turns: Sequence[NewDialogueTurn],
        *,
        title: str,
        updated_at: datetime,
        close_episode_id: str | None = None,
    ) -> Result[EpisodeRecord, ChatStorageError]:
        _ = (episode_id, turns, title, updated_at, close_episode_id)
        return Err(
            ChatStorageError(ChatStorageErrorType.WRITE_FAILED, "db is read-only")
        )

    def update_episode_summary(
        self,
        episode_id: str,
        *,
        title: str,
        overview: str,
        summary: str,
        summarized_turn_count: int,
    ) -> Result[EpisodeRecord, ChatStorageError]:
        _ = (title, overview, summary, summarized_turn_count)
        raise AssertionError(episode_id)

    def close_episode(
        self, episode_id: str, *, closed_at: datetime, reason: EpisodeCloseReason
    ) -> Result[EpisodeRecord, ChatStorageError]:
        _ = (closed_at, reason)
        raise AssertionError(episode_id)


def test_records_event_message_as_assistant_turn(tmp_path: Path) -> None:
    repository = SqliteChatRepository(tmp_path / "chats.db")
    recorder = SqliteEventChatRecorder(repository)

    result = recorder.record("evt_test", "[event:evt_test] completed", occurred_at=_NOW)

    assert result == Ok(None)
    episode = repository.get_latest_episode().unwrap()
    assert episode is not None
    assert episode.title == "イベント evt_test"
    assert episode.turn_count == 1
    turns = repository.list_turns(episode.id).unwrap()
    assert len(turns) == 1
    assert turns[0].role == "assistant"
    assert turns[0].content == "[event:evt_test] completed"


def test_maps_storage_failure_to_event_record_error() -> None:
    recorder = SqliteEventChatRecorder(ErrorStore())

    result = recorder.record("evt_test", "[event:evt_test] completed", occurred_at=_NOW)

    assert_err(result, message_contains="db is read-only")
