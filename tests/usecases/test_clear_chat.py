import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from flow_res import Err, Ok, Result

from my_agents.contracts.messages import ChatMessage
from my_agents.contracts.ports.chat_history import EpisodeRecord, NewDialogueTurn
from my_agents.contracts.ports.episode_summarizer import (
    EpisodeSummarizerError,
    EpisodeSummarizerErrorType,
    EpisodeSummaryOutcome,
    EpisodeSummaryRequest,
    IEpisodeSummarizer,
)
from my_agents.infrastructure.sqlite_chat_repository import SqliteChatRepository
from my_agents.usecases.clear_chat import ClearChatErrorType, ClearChatHandler
from tests.helpers import assert_err

NOW = datetime(2026, 11, 22, tzinfo=UTC)


_SUMMARY = Ok(EpisodeSummaryOutcome("new title", "overview", "summary"))


class RecordingSummarizer(IEpisodeSummarizer):
    def __init__(
        self,
        result: Result[EpisodeSummaryOutcome, EpisodeSummarizerError] = _SUMMARY,
    ) -> None:
        self._result = result
        self.requests: list[EpisodeSummaryRequest] = []

    async def summarize(
        self, request: EpisodeSummaryRequest
    ) -> Result[EpisodeSummaryOutcome, EpisodeSummarizerError]:
        self.requests.append(request)
        return self._result


def add_turn(repo: SqliteChatRepository) -> EpisodeRecord:
    return repo.record_turns(
        None, (NewDialogueTurn("user", "question", NOW),), title="title", updated_at=NOW
    ).unwrap()


@pytest.mark.anyio
async def test_clear_summarizes_and_closes_without_deleting_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    summarizer = RecordingSummarizer()
    clear = ClearChatHandler(repo, repo, summarizer, clock=lambda: NOW)
    assert (await clear.execute()).unwrap().warnings == ()
    assert not path.exists()
    add_turn(repo)
    assert (await clear.execute()).unwrap().warnings == ()
    assert (await clear.execute()).unwrap().warnings == ()
    assert summarizer.requests == [
        EpisodeSummaryRequest("title", "", (ChatMessage("user", "question"),))
    ]
    episode = repo.get_latest_episode().unwrap()
    assert episode is not None
    assert (
        episode.close_reason,
        episode.closed_at,
        episode.summary,
        episode.summarized_turn_count,
    ) == ("clear", NOW, "summary", 1)
    assert [turn.content for turn in repo.list_turns(episode.id).unwrap()] == [
        "question"
    ]


@pytest.mark.anyio
async def test_clear_keeps_summary_warning_and_reports_close_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    add_turn(repo)
    summarizer = RecordingSummarizer(
        Err(
            EpisodeSummarizerError(
                EpisodeSummarizerErrorType.API_ERROR, "model failed", "detail"
            )
        )
    )
    handler = ClearChatHandler(repo, repo, summarizer)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER prevent_close BEFORE UPDATE OF closed_at ON episodes BEGIN SELECT RAISE(ABORT, 'close denied'); END"
        )
    error = assert_err(
        await handler.execute(),
        expected_type=ClearChatErrorType.STORAGE_ERROR,
        message_contains="close denied",
        display_message_contains="閉じられませんでした",
    )
    assert [(w.event, w.error_type, w.message, w.detail) for w in error.warnings] == [
        ("episode_summary_failed", "API_ERROR", "model failed", "detail")
    ]
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER prevent_close")
    response = (await handler.execute()).unwrap()
    assert response.warnings == error.warnings
    episode = repo.get_latest_episode().unwrap()
    assert episode is not None
    assert episode.close_reason == "clear"


@pytest.mark.anyio
async def test_clear_reports_unreadable_history(tmp_path: Path) -> None:
    path = tmp_path / "chats.db"
    path.write_text("broken")
    repo = SqliteChatRepository(path)
    summarizer = RecordingSummarizer()
    assert_err(
        await ClearChatHandler(repo, repo, summarizer).execute(),
        expected_type=ClearChatErrorType.STORAGE_ERROR,
        message_contains="file is not a database",
        display_message_contains="読み込めませんでした",
    )
    assert summarizer.requests == []


@pytest.mark.anyio
@pytest.mark.parametrize("corrupt", [False, True])
async def test_clear_reports_failure_for_a_closed_rollover(
    tmp_path: Path, corrupt: bool
) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    episode = add_turn(repo)
    rolled_over = repo.close_episode(
        episode.id, closed_at=NOW, reason="rollover"
    ).unwrap()
    with closing(sqlite3.connect(path)) as connection, connection:
        if corrupt:
            connection.execute("UPDATE episodes SET updated_at = 'bad'")
        else:
            connection.execute(
                "CREATE TRIGGER reject_clear BEFORE UPDATE OF close_reason ON episodes BEGIN SELECT RAISE(ABORT, 'clear rejected'); END"
            )
    summarizer = RecordingSummarizer()
    awaitable = ClearChatHandler(repo, repo, summarizer).execute()
    if corrupt:
        assert_err(
            await awaitable,
            expected_type=ClearChatErrorType.STORAGE_ERROR,
            message_contains="Invalid isoformat string",
            display_message_contains="読み込めませんでした",
        )
    else:
        assert_err(
            await awaitable,
            expected_type=ClearChatErrorType.STORAGE_ERROR,
            message_contains="clear rejected",
            display_message_contains="閉じられませんでした",
        )
        assert repo.get_latest_episode().unwrap() == rolled_over
    assert summarizer.requests == []
