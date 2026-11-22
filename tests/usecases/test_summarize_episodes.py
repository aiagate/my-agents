import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from flow_res import Ok

from my_agents.contracts.messages import ChatMessage
from my_agents.contracts.ports.chat_history import NewDialogueTurn
from my_agents.contracts.ports.episode_summarizer import (
    EpisodeSummaryOutcome,
    EpisodeSummaryRequest,
)
from my_agents.infrastructure.sqlite_chat_repository import SqliteChatRepository
from my_agents.usecases.summarize_episodes import SummarizeEpisodesHandler
from tests.usecases.test_clear_chat import NOW, RecordingSummarizer, add_turn


@pytest.mark.anyio
async def test_summarization_continues_after_missing_and_corrupt_episodes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    corrupt = repo.record_turns(None, (), title="old", updated_at=NOW).unwrap()
    repo.close_episode(corrupt.id, closed_at=NOW, reason="rollover").unwrap()
    current = add_turn(repo)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE episodes SET started_at = 'bad' WHERE id = ?", (corrupt.id,)
        )
    summarizer = RecordingSummarizer()
    handler = SummarizeEpisodesHandler(repo, repo, summarizer)
    response = await handler.execute(("missing", corrupt.id, current.id))
    (warning,) = response.warnings
    assert (warning.event, warning.error_type) == (
        "summary_episode_read_failed",
        "CORRUPT_DATA",
    )
    assert "Invalid isoformat string" in warning.message
    assert summarizer.requests == [
        EpisodeSummaryRequest("title", "", (ChatMessage("user", "question"),))
    ]
    episode = repo.get_episode(current.id).unwrap()
    assert episode is not None
    assert (episode.summary, episode.summarized_turn_count) == ("summary", 1)


@pytest.mark.anyio
async def test_blank_summary_preserves_previous_summary_and_checkpoint(
    tmp_path: Path,
) -> None:
    repo = SqliteChatRepository(tmp_path / "chats.db")
    current = add_turn(repo)
    previous = repo.update_episode_summary(
        current.id,
        title="previous title",
        overview="previous overview",
        summary="previous summary",
        summarized_turn_count=1,
    ).unwrap()
    unsummarized = repo.record_turns(
        current.id,
        (NewDialogueTurn("assistant", "reply", NOW),),
        title=current.title,
        updated_at=NOW,
    ).unwrap()
    summarizer = RecordingSummarizer(Ok(EpisodeSummaryOutcome("new", "new", " \n\t")))
    response = await SummarizeEpisodesHandler(repo, repo, summarizer).execute(
        (current.id,)
    )
    (warning,) = response.warnings
    assert (warning.event, warning.error_type, warning.message) == (
        "episode_summary_empty",
        "EMPTY_SUMMARY",
        "Episode summarizer returned an empty summary.",
    )
    assert summarizer.requests == [
        EpisodeSummaryRequest(
            previous.title, previous.summary, (ChatMessage("assistant", "reply"),)
        )
    ]
    assert repo.get_episode(current.id).unwrap() == unsummarized
