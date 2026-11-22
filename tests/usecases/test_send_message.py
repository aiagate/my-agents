import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, timedelta, timezone
from pathlib import Path

import pytest
from flow_res import Err, Ok, Result, is_ok

from my_agents.contracts.errors import AppError, DisplayableError, TypedError
from my_agents.contracts.messages import AgentReply, ChatMessage
from my_agents.contracts.ports.agent_runner import (
    AgentRunnerError,
    AgentRunnerErrorType,
    IAgentRunner,
)
from my_agents.contracts.ports.chat_history import NewDialogueTurn
from my_agents.contracts.ports.episode_summarizer import (
    EpisodeSummarizerError,
    EpisodeSummarizerErrorType,
    EpisodeSummaryRequest,
)
from my_agents.domain.context_assembler import assemble_instructions
from my_agents.domain.core_profile import CoreProfile
from my_agents.infrastructure.sqlite_chat_repository import SqliteChatRepository
from my_agents.usecases.clear_chat import ClearChatHandler
from my_agents.usecases.get_core_profile import (
    GetCoreProfileError,
    GetCoreProfileErrorType,
    GetCoreProfileResponse,
)
from my_agents.usecases.send_message import (
    SendMessageError,
    SendMessageErrorType,
    SendMessageHandler,
    SendMessageRequest,
    SendMessageResponse,
)
from my_agents.usecases.summarize_episodes import SummarizeEpisodesHandler
from tests.helpers import assert_err
from tests.usecases.test_clear_chat import NOW, RecordingSummarizer, add_turn


@dataclass(frozen=True, slots=True)
class RecordedRun:
    user_input: str
    history: tuple[ChatMessage, ...]
    instructions: str


class FakeAgentRunner(IAgentRunner):
    def __init__(self, result: Result[AgentReply, AgentRunnerError]) -> None:
        self.result = result
        self.runs: list[RecordedRun] = []

    async def run(
        self,
        user_input: str,
        *,
        history: Sequence[ChatMessage],
        instructions: str,
    ) -> Result[AgentReply, AgentRunnerError]:
        self.runs.append(
            RecordedRun(
                user_input=user_input,
                history=tuple(history),
                instructions=instructions,
            )
        )
        return self.result


class FakeGetCoreProfile:
    def __init__(
        self,
        result: Result[GetCoreProfileResponse, GetCoreProfileError],
    ) -> None:
        self.result = result
        self.calls = 0

    def execute(self) -> Result[GetCoreProfileResponse, GetCoreProfileError]:
        self.calls += 1
        return self.result


def _profile_response(profile: CoreProfile) -> GetCoreProfileResponse:
    return GetCoreProfileResponse(
        profile=profile,
        location="memory/core.md",
        max_chars=4096,
    )


@pytest.mark.anyio
async def test_execute_returns_reply_and_appends_history() -> None:
    profile = CoreProfile(content="## 価値観\n- 誠実さ")
    runner = FakeAgentRunner(
        Ok(
            AgentReply(
                content="こんにちは",
                core_memory_updates=("価値観を記録",),
            )
        )
    )
    get_core_profile = FakeGetCoreProfile(Ok(_profile_response(profile)))
    handler = SendMessageHandler(runner, get_core_profile)
    previous = ChatMessage(role="user", content="前回の質問")
    request = SendMessageRequest(
        user_input="  今日は？  ",
        history=(previous,),
    )

    result = await handler.execute(request)

    assert is_ok(result)
    assert result.unwrap() == SendMessageResponse(
        reply="こんにちは",
        history=(
            previous,
            ChatMessage(role="user", content="今日は？"),
            ChatMessage(role="assistant", content="こんにちは"),
        ),
        core_memory_updates=("価値観を記録",),
    )
    assert hash(request) == hash(
        SendMessageRequest(user_input="  今日は？  ", history=(previous,))
    )
    assert get_core_profile.calls == 1
    assert runner.runs == [
        RecordedRun(
            user_input="今日は？",
            history=(previous,),
            instructions=assemble_instructions(profile),
        )
    ]


@pytest.mark.anyio
async def test_execute_uses_empty_profile_without_core_memory_updates() -> None:
    runner = FakeAgentRunner(Ok(AgentReply(content="ok")))
    get_core_profile = FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty())))
    handler = SendMessageHandler(runner, get_core_profile)

    result = await handler.execute(SendMessageRequest(user_input="hello"))

    assert is_ok(result)
    assert result.unwrap() == SendMessageResponse(
        reply="ok",
        history=(
            ChatMessage(role="user", content="hello"),
            ChatMessage(role="assistant", content="ok"),
        ),
        core_memory_updates=(),
    )
    instructions = runner.runs[0].instructions
    assert instructions == assemble_instructions(CoreProfile.empty())
    assert "まだ何も記録されていません。" in instructions


@pytest.mark.anyio
@pytest.mark.parametrize("user_input", ["", "   ", "\n\t "])
async def test_execute_rejects_empty_input(user_input: str) -> None:
    runner = FakeAgentRunner(Ok(AgentReply(content="ok")))
    get_core_profile = FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty())))
    handler = SendMessageHandler(runner, get_core_profile)

    result = await handler.execute(SendMessageRequest(user_input=user_input))

    error = assert_err(
        result,
        expected_type=SendMessageErrorType.EMPTY_INPUT,
        message_contains="empty",
        display_message_contains="メッセージを入力してください。",
    )
    assert isinstance(error, SendMessageError)
    assert isinstance(error, AppError)
    assert isinstance(error, TypedError)
    assert isinstance(error, DisplayableError)
    assert not isinstance(error, Exception)
    assert error.display_message == "メッセージを入力してください。"
    assert error.detail is None
    assert get_core_profile.calls == 0
    assert runner.runs == []


@pytest.mark.anyio
async def test_execute_maps_core_profile_error() -> None:
    profile_error = GetCoreProfileError(
        type=GetCoreProfileErrorType.INVALID_PROFILE,
        message="profile too large",
        display_message="Core Memory の内容が不正です。",
    )
    runner = FakeAgentRunner(Ok(AgentReply(content="ok")))
    get_core_profile = FakeGetCoreProfile(Err(profile_error))
    handler = SendMessageHandler(runner, get_core_profile)

    result = await handler.execute(SendMessageRequest(user_input="hello"))

    error = assert_err(
        result,
        expected_type=SendMessageErrorType.CORE_PROFILE_ERROR,
        message_contains="profile too large",
        display_message_contains="Core Memory の内容が不正です。",
    )
    assert isinstance(error, SendMessageError)
    assert isinstance(error, AppError)
    assert isinstance(error, TypedError)
    assert isinstance(error, DisplayableError)
    assert error.message == "profile too large"
    assert error.display_message == "Core Memory の内容が不正です。"
    assert error.detail is None
    assert get_core_profile.calls == 1
    assert runner.runs == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("runner_error_type", "expected_type", "expected_display_message"),
    [
        pytest.param(
            AgentRunnerErrorType.API_ERROR,
            SendMessageErrorType.API_ERROR,
            "APIとの通信でエラーが発生しました。しばらくしてから再度お試しください。",
            id="api-error",
        ),
        pytest.param(
            AgentRunnerErrorType.UNEXPECTED_BEHAVIOR,
            SendMessageErrorType.UNEXPECTED_BEHAVIOR,
            "モデルから予期しない応答が返されました。",
            id="unexpected-behavior",
        ),
        pytest.param(
            AgentRunnerErrorType.USAGE_LIMIT_EXCEEDED,
            SendMessageErrorType.USAGE_LIMIT_EXCEEDED,
            "利用上限に達しました。しばらくしてから再度お試しください。",
            id="usage-limit-exceeded",
        ),
        pytest.param(
            AgentRunnerErrorType.UNEXPECTED,
            SendMessageErrorType.UNEXPECTED,
            "予期しないエラーが発生しました。",
            id="unexpected",
        ),
    ],
)
async def test_maps_runner_errors(
    runner_error_type: AgentRunnerErrorType,
    expected_type: SendMessageErrorType,
    expected_display_message: str,
) -> None:
    runner_error = AgentRunnerError(
        type=runner_error_type,
        message="runner detail",
        detail="traceback detail",
    )
    runner = FakeAgentRunner(Err(runner_error))
    get_core_profile = FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty())))
    handler = SendMessageHandler(runner, get_core_profile)

    result = await handler.execute(SendMessageRequest(user_input="hello"))

    error = assert_err(
        result,
        expected_type=expected_type,
        message_contains="runner detail",
        display_message_contains=expected_display_message,
    )
    assert isinstance(error, SendMessageError)
    assert isinstance(error, AppError)
    assert isinstance(error, TypedError)
    assert isinstance(error, DisplayableError)
    assert not isinstance(error, Exception)
    assert error.message == "runner detail"
    assert error.type is expected_type
    assert error.display_message == expected_display_message
    assert error.detail == "traceback detail"


@pytest.mark.anyio
async def test_persistent_chat_resumes_and_summarizes_only_new_turns(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    summarizer = RecordingSummarizer()
    runner = FakeAgentRunner(Ok(AgentReply("reply")))
    profile = FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty())))
    handler = SendMessageHandler(
        runner,
        profile,
        chat_store=repo,
        chat_query=repo,
        summary_every_messages=4,
        recent_messages_limit=2,
        clock=lambda: NOW,
    )
    legacy = (ChatMessage("user", "legacy"),)
    first = (await handler.execute(SendMessageRequest("first", legacy))).unwrap()
    assert first.warnings == first.pending_summary_episode_ids == ()
    assert runner.runs[0].history == legacy
    assert summarizer.requests == []
    second = (await handler.execute(SendMessageRequest("second"))).unwrap()
    assert second.warnings == ()
    (episode_id,) = second.pending_summary_episode_ids
    assert summarizer.requests == []
    summary_handler = SummarizeEpisodesHandler(repo, repo, summarizer)
    assert (await summary_handler.execute((episode_id,))).warnings == ()
    episode = repo.get_episode(episode_id).unwrap()
    assert episode is not None
    assert (
        episode.title,
        episode.summary,
        episode.turn_count,
        episode.summarized_turn_count,
    ) == ("new title", "summary", 4, 4)
    reopened = SqliteChatRepository(path)
    restarted = SendMessageHandler(
        runner,
        profile,
        chat_store=reopened,
        chat_query=reopened,
        summary_every_messages=2,
        recent_messages_limit=2,
        clock=lambda: NOW,
    )
    third = (await restarted.execute(SendMessageRequest("third"))).unwrap()
    assert third.warnings == ()
    assert third.pending_summary_episode_ids == (episode_id,)
    assert (
        await summary_handler.execute(third.pending_summary_episode_ids)
    ).warnings == ()
    assert runner.runs[-1].history == (
        ChatMessage("user", "second"),
        ChatMessage("assistant", "reply"),
    )
    assert "## 直近の話題のサマリー" in runner.runs[-1].instructions
    assert "## 過去の会話サマリー一覧" not in runner.runs[-1].instructions
    assert summarizer.requests == [
        EpisodeSummaryRequest(
            "first",
            "",
            (
                ChatMessage("user", "first"),
                ChatMessage("assistant", "reply"),
                ChatMessage("user", "second"),
                ChatMessage("assistant", "reply"),
            ),
        ),
        EpisodeSummaryRequest(
            "new title",
            "summary",
            (ChatMessage("user", "third"), ChatMessage("assistant", "reply")),
        ),
    ]
    assert [item.id for item in reopened.list_episodes().unwrap()] == [episode.id]


@pytest.mark.anyio
@pytest.mark.parametrize(("minutes", "max_messages"), [(60, 40), (-60, 2)])
async def test_rollover_carries_previous_summary_and_clear_breaks_continuity(
    tmp_path: Path, minutes: int, max_messages: int
) -> None:
    repo = SqliteChatRepository(tmp_path / "chats.db")
    summarizer = RecordingSummarizer()
    runner = FakeAgentRunner(Ok(AgentReply("reply")))
    profile = FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty())))
    handler = SendMessageHandler(
        runner,
        profile,
        chat_store=repo,
        chat_query=repo,
        clock=lambda: NOW,
    )
    (await handler.execute(SendMessageRequest("first"))).unwrap()
    (first,) = repo.list_episodes().unwrap()
    later = NOW + timedelta(minutes=minutes)
    next_handler = SendMessageHandler(
        runner,
        profile,
        chat_store=repo,
        chat_query=repo,
        episode_max_messages=max_messages,
        clock=lambda: later,
    )
    runner.result = Err(AgentRunnerError(AgentRunnerErrorType.API_ERROR, "offline"))
    assert_err(
        await next_handler.execute(SendMessageRequest("failed rollover")),
        expected_type=SendMessageErrorType.API_ERROR,
        message_contains="offline",
        display_message_contains="APIとの通信でエラーが発生しました",
    )
    assert repo.get_open_episode().unwrap() == first
    assert repo.list_episodes().unwrap() == (first,)
    runner.result = Ok(AgentReply("reply"))
    second = (await next_handler.execute(SendMessageRequest("second"))).unwrap()
    assert second.warnings == ()
    assert second.pending_summary_episode_ids == (first.id,)
    assert runner.runs[-1].history == ()
    assert "## 直近の話題のサマリー" not in runner.runs[-1].instructions
    assert f"id:{first.id}" in runner.runs[-1].instructions
    assert summarizer.requests == []
    assert (
        await SummarizeEpisodesHandler(repo, repo, summarizer).execute(
            second.pending_summary_episode_ids
        )
    ).warnings == ()
    current = repo.get_open_episode().unwrap()
    previous = repo.get_episode(first.id).unwrap()
    assert current is not None
    assert previous is not None
    assert (previous.id, previous.close_reason, previous.summarized_turn_count) == (
        first.id,
        "rollover",
        2,
    )
    continuation = SendMessageHandler(
        runner, profile, chat_store=repo, chat_query=repo, clock=lambda: later
    )
    (await continuation.execute(SendMessageRequest("third"))).unwrap()
    assert runner.runs[-1].history == (
        ChatMessage("user", "second"),
        ChatMessage("assistant", "reply"),
    )
    assert "## 直近の話題のサマリー" in runner.runs[-1].instructions
    assert f"id:{current.id}" not in runner.runs[-1].instructions
    (
        await ClearChatHandler(repo, repo, summarizer, clock=lambda: later).execute()
    ).unwrap()
    result = (
        await continuation.execute(
            SendMessageRequest("fresh", (ChatMessage("user", "stale"),))
        )
    ).unwrap()
    assert result.history == (
        ChatMessage("user", "fresh"),
        ChatMessage("assistant", "reply"),
    )
    assert runner.runs[-1].history == ()
    assert "## 直近の話題のサマリー" not in runner.runs[-1].instructions
    assert f"id:{current.id}" in runner.runs[-1].instructions
    (await continuation.execute(SendMessageRequest("follow fresh"))).unwrap()
    assert "## 直近の話題のサマリー" not in runner.runs[-1].instructions


@pytest.mark.anyio
@pytest.mark.parametrize("clear_rollover", [False, True])
async def test_already_rolled_over_episode_only_supplies_topic_without_clear(
    tmp_path: Path,
    clear_rollover: bool,
) -> None:
    repo = SqliteChatRepository(tmp_path / "chats.db")
    episode = add_turn(repo)
    repo.update_episode_summary(
        episode.id,
        title="title",
        overview="overview",
        summary="carried forward",
        summarized_turn_count=1,
    ).unwrap()
    repo.close_episode(episode.id, closed_at=NOW, reason="rollover").unwrap()
    if clear_rollover:
        cleared_at = NOW + timedelta(minutes=1)
        summarizer = RecordingSummarizer()
        assert (
            await ClearChatHandler(
                repo, repo, summarizer, clock=lambda: cleared_at
            ).execute()
        ).unwrap().warnings == ()
        assert summarizer.requests == []
        cleared = repo.get_episode(episode.id).unwrap()
        assert cleared is not None
        assert (cleared.closed_at, cleared.close_reason) == (NOW, "clear")
    runner = FakeAgentRunner(Ok(AgentReply("reply")))
    profile = FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty())))
    handler = SendMessageHandler(
        runner, profile, chat_store=repo, chat_query=repo, clock=lambda: NOW
    )
    assert (
        await handler.execute(SendMessageRequest("new episode"))
    ).unwrap().warnings == ()
    assert runner.runs[-1].history == ()
    assert ("carried forward" in runner.runs[-1].instructions) is not clear_rollover


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("sql", "limits", "events"),
    [
        (
            "DROP TABLE episodes",
            40,
            ("latest_episode_read_failed", "episode_index_read_failed"),
        ),
        ("DROP TABLE dialogue_turns", 40, ("recent_turns_read_failed",)),
        (
            "CREATE TRIGGER reject_append BEFORE INSERT ON dialogue_turns BEGIN SELECT RAISE(ABORT, 'append refused'); END",
            40,
            ("dialogue_turns_write_failed",),
        ),
        (
            "CREATE TRIGGER reject_close BEFORE UPDATE OF closed_at ON episodes BEGIN SELECT RAISE(ABORT, 'close refused'); END",
            1,
            ("dialogue_turns_write_failed",),
        ),
    ],
)
async def test_storage_failures_are_warnings_without_losing_reply(
    tmp_path: Path, sql: str, limits: int, events: tuple[str, ...]
) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    add_turn(repo)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(sql)
    runner = FakeAgentRunner(Ok(AgentReply("reply", ("update",))))
    profile = FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty())))
    handler = SendMessageHandler(
        runner,
        profile,
        chat_store=repo,
        chat_query=repo,
        episode_max_messages=limits,
        summary_every_messages=2,
        clock=lambda: NOW,
    )
    response = (await handler.execute(SendMessageRequest("next"))).unwrap()
    assert response.reply == "reply"
    assert response.core_memory_updates == ("update",)
    assert tuple(warning.event for warning in response.warnings) == events
    assert all(
        warning.message and warning.error_type in {"READ_FAILED", "WRITE_FAILED"}
        for warning in response.warnings
    )


@pytest.mark.anyio
async def test_store_without_query_uses_legacy_path(tmp_path: Path) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    runner = FakeAgentRunner(Ok(AgentReply("reply")))
    profile = FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty())))
    handler = SendMessageHandler(runner, profile, chat_store=repo)
    assert (
        await handler.execute(SendMessageRequest("legacy"))
    ).unwrap().reply == "reply"
    assert not path.exists()
    assert "recall_conversation" not in runner.runs[-1].instructions


@pytest.mark.anyio
async def test_rollover_without_a_summarizer_keeps_only_the_index(
    tmp_path: Path,
) -> None:
    repo = SqliteChatRepository(tmp_path / "chats.db")
    episode = add_turn(repo)
    runner = FakeAgentRunner(Ok(AgentReply("reply")))
    profile = FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty())))
    handler = SendMessageHandler(
        runner,
        profile,
        chat_store=repo,
        chat_query=repo,
        episode_max_messages=1,
        clock=lambda: NOW,
    )
    response = (await handler.execute(SendMessageRequest("after rollover"))).unwrap()
    assert response.warnings == ()
    assert response.pending_summary_episode_ids == (episode.id,)
    assert runner.runs[-1].history == ()
    assert f"(id:{episode.id}) title" in runner.runs[-1].instructions
    assert "## 直近の話題のサマリー" not in runner.runs[-1].instructions


@pytest.mark.anyio
@pytest.mark.parametrize("already_summarized", [False, True])
async def test_first_rollover_reply_uses_current_summary_without_queuing_it(
    tmp_path: Path, already_summarized: bool
) -> None:
    repo = SqliteChatRepository(tmp_path / "chats.db")
    now = NOW.replace(hour=13, minute=48)
    episode = repo.record_turns(
        None, (NewDialogueTurn("user", "question", now),), title="title", updated_at=now
    ).unwrap()
    if already_summarized:
        repo.update_episode_summary(
            episode.id,
            title="new title",
            overview="overview",
            summary="summary",
            summarized_turn_count=1,
        ).unwrap()
    summarizer = RecordingSummarizer()
    runner = FakeAgentRunner(Ok(AgentReply("reply")))
    handler = SendMessageHandler(
        runner,
        FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty()))),
        chat_store=repo,
        chat_query=repo,
        episode_summarizer=summarizer,
        episode_max_messages=1,
        clock=lambda: now,
        display_tz=timezone(timedelta(hours=9)),
    )
    response = (await handler.execute(SendMessageRequest("next topic"))).unwrap()
    assert response.warnings == response.pending_summary_episode_ids == ()
    (run,) = runner.runs
    assert run.history == ()
    assert (
        "## 直近の話題のサマリー\n\n### [11/22 22:48] new title\n\nsummary"
        in run.instructions
    )
    assert "## 過去の会話サマリー一覧" not in run.instructions
    assert summarizer.requests == (
        []
        if already_summarized
        else [EpisodeSummaryRequest("title", "", (ChatMessage("user", "question"),))]
    )
    closed = repo.get_episode(episode.id).unwrap()
    assert closed is not None
    assert (closed.close_reason, closed.summary, closed.summarized_turn_count) == (
        "rollover",
        "summary",
        1,
    )
    opened = repo.get_open_episode().unwrap()
    assert opened is not None
    assert (opened.title, opened.turn_count) == ("next topic", 2)


@pytest.mark.anyio
async def test_failed_rollover_reply_leaves_caught_up_episode_open_for_clear(
    tmp_path: Path,
) -> None:
    repo = SqliteChatRepository(tmp_path / "chats.db")
    episode = add_turn(repo)
    summarizer = RecordingSummarizer()
    runner = FakeAgentRunner(
        Err(AgentRunnerError(AgentRunnerErrorType.API_ERROR, "offline"))
    )
    later = NOW + timedelta(hours=1)
    handler = SendMessageHandler(
        runner,
        FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty()))),
        chat_store=repo,
        chat_query=repo,
        episode_summarizer=summarizer,
        clock=lambda: later,
    )
    assert_err(
        await handler.execute(SendMessageRequest("failed rollover")),
        expected_type=SendMessageErrorType.API_ERROR,
        message_contains="offline",
        display_message_contains="APIとの通信でエラーが発生しました",
    )
    (run,) = runner.runs
    assert "## 直近の話題のサマリー" in run.instructions
    opened = repo.get_open_episode().unwrap()
    assert opened is not None
    assert (
        opened.id,
        opened.summary,
        opened.turn_count,
        opened.summarized_turn_count,
    ) == (episode.id, "summary", 1, 1)
    clear = ClearChatHandler(repo, repo, summarizer, clock=lambda: later)
    assert (await clear.execute()).unwrap().warnings == ()
    assert repo.get_open_episode().unwrap() is None
    (cleared,) = repo.list_episodes().unwrap()
    assert (cleared.id, cleared.closed_at, cleared.close_reason) == (
        episode.id,
        later,
        "clear",
    )
    assert len(summarizer.requests) == 1


@pytest.mark.anyio
async def test_failed_rollover_summary_warns_and_queues_retry_after_reply(
    tmp_path: Path,
) -> None:
    repo = SqliteChatRepository(tmp_path / "chats.db")
    episode = add_turn(repo)
    summarizer = RecordingSummarizer(
        Err(
            EpisodeSummarizerError(
                EpisodeSummarizerErrorType.API_ERROR, "summary offline"
            )
        )
    )
    runner = FakeAgentRunner(Ok(AgentReply("reply")))
    handler = SendMessageHandler(
        runner,
        FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty()))),
        chat_store=repo,
        chat_query=repo,
        episode_summarizer=summarizer,
        episode_max_messages=1,
        clock=lambda: NOW,
    )
    response = (await handler.execute(SendMessageRequest("next topic"))).unwrap()
    assert response.reply == "reply"
    assert response.pending_summary_episode_ids == (episode.id,)
    (warning,) = response.warnings
    assert (warning.event, warning.error_type, warning.message) == (
        "episode_summary_failed",
        "API_ERROR",
        "summary offline",
    )
    (run,) = runner.runs
    assert "## 直近の話題のサマリー" not in run.instructions
    assert f"(id:{episode.id}) title" in run.instructions
    closed = repo.get_episode(episode.id).unwrap()
    assert closed is not None
    assert (closed.close_reason, closed.summary, closed.summarized_turn_count) == (
        "rollover",
        "",
        0,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["empty", "open", "clear", "clear_empty", "rollover"])
async def test_recovery_retries_failed_turns_only_in_the_same_episode(
    tmp_path: Path, mode: str
) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    runner = FakeAgentRunner(Ok(AgentReply("reply")))
    profile = FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty())))
    now = NOW
    handler = SendMessageHandler(
        runner,
        profile,
        chat_store=repo,
        chat_query=repo,
        recent_messages_limit=4,
        clock=lambda: now,
    )
    stored_history = ()
    if mode not in {"empty", "clear_empty"}:
        stored = (await handler.execute(SendMessageRequest("saved"))).unwrap()
        stored_history = stored.history
    else:
        repo.record_turns(None, (), title="setup", updated_at=NOW).unwrap()
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("DELETE FROM episodes")

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER reject_assistant BEFORE INSERT ON dialogue_turns WHEN NEW.role = 'assistant' BEGIN SELECT RAISE(ABORT, 'disk full'); END"
        )
    episodes_before = repo.list_episodes().unwrap()
    failed = (
        await handler.execute(SendMessageRequest("unsaved", stored_history))
    ).unwrap()
    assert failed.reply == "reply"
    assert [(warning.event, warning.message) for warning in failed.warnings] == [
        ("dialogue_turns_write_failed", "disk full")
    ]
    assert failed.pending_summary_episode_ids == ()
    assert repo.list_episodes().unwrap() == episodes_before
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER reject_assistant")

    local_history = failed.history
    expected_history = (
        *stored_history,
        ChatMessage("user", "unsaved"),
        ChatMessage("assistant", "reply"),
    )
    if mode in {"clear", "clear_empty"}:
        (
            await ClearChatHandler(
                repo, repo, RecordingSummarizer(), clock=lambda: now
            ).execute()
        ).unwrap()
        local_history = ()
        expected_history = ()
    if mode == "rollover":
        now += timedelta(hours=1)
        expected_history = ()
    recovered = (
        await handler.execute(SendMessageRequest("recovered", local_history))
    ).unwrap()
    assert recovered.warnings == ()
    assert runner.runs[-1].history == expected_history
    assert (
        recovered.history
        == (
            *expected_history,
            ChatMessage("user", "recovered"),
            ChatMessage("assistant", "reply"),
        )[-4:]
    )
    episode = repo.get_open_episode().unwrap()
    assert episode is not None
    assert tuple(
        ChatMessage(turn.role, turn.content)
        for turn in repo.list_turns(episode.id).unwrap()
    ) == (
        *expected_history,
        ChatMessage("user", "recovered"),
        ChatMessage("assistant", "reply"),
    )
    (await handler.execute(SendMessageRequest("next", recovered.history))).unwrap()
    assert runner.runs[-1].history == recovered.history
    assert [turn.content for turn in repo.list_turns(episode.id).unwrap()] == [
        *(message.content for message in expected_history),
        "recovered",
        "reply",
        "next",
        "reply",
    ]


@pytest.mark.anyio
async def test_repeated_failures_only_retain_the_recent_turns(tmp_path: Path) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    episode = add_turn(repo)
    runner = FakeAgentRunner(Ok(AgentReply("reply")))
    profile = FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty())))
    handler = SendMessageHandler(
        runner,
        profile,
        chat_store=repo,
        chat_query=repo,
        recent_messages_limit=2,
        clock=lambda: NOW,
    )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER reject_turn BEFORE INSERT ON dialogue_turns BEGIN SELECT RAISE(ABORT, 'disk full'); END"
        )
    first = (await handler.execute(SendMessageRequest("first"))).unwrap()
    second = (
        await handler.execute(SendMessageRequest("second", first.history))
    ).unwrap()
    assert runner.runs[-1].history == first.history
    assert second.history == (
        ChatMessage("user", "second"),
        ChatMessage("assistant", "reply"),
    )
    assert [turn.content for turn in repo.list_turns(episode.id).unwrap()] == [
        "question"
    ]
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER reject_turn")
    third = (
        await handler.execute(SendMessageRequest("third", second.history))
    ).unwrap()
    assert third.warnings == ()
    assert runner.runs[-1].history == second.history
    assert [turn.content for turn in repo.list_turns(episode.id).unwrap()] == [
        "question",
        "second",
        "reply",
        "third",
        "reply",
    ]


@pytest.mark.anyio
async def test_episode_index_applies_character_budget_to_more_than_200_episodes(
    tmp_path: Path,
) -> None:
    repo = SqliteChatRepository(tmp_path / "chats.db")
    episode_ids: list[str] = []
    for index in range(201):
        episode = repo.record_turns(
            None, (), title=f"Topic {index}", updated_at=NOW
        ).unwrap()
        episode_ids.append(episode.id)
        repo.close_episode(episode.id, closed_at=NOW, reason="clear").unwrap()
    runner = FakeAgentRunner(Ok(AgentReply("reply")))
    profile = FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty())))
    handler = SendMessageHandler(
        runner,
        profile,
        chat_store=repo,
        chat_query=repo,
        summary_index_char_budget=100_000,
        clock=lambda: NOW,
    )
    (await handler.execute(SendMessageRequest("next"))).unwrap()
    instructions = runner.runs[-1].instructions
    assert instructions.index(f"(id:{episode_ids[-1]}) Topic 200") < instructions.index(
        f"(id:{episode_ids[0]}) Topic 0"
    )


@pytest.mark.anyio
async def test_prompt_index_preserves_sqlite_recency_for_equal_timestamps(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    for episode_id, title in (("z-first", "First"), ("a-second", "Second")):
        episode = repo.record_turns(None, (), title=title, updated_at=NOW).unwrap()
        repo.close_episode(episode.id, closed_at=NOW, reason="clear").unwrap()
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "UPDATE episodes SET id = ? WHERE id = ?", (episode_id, episode.id)
            )
    runner = FakeAgentRunner(Ok(AgentReply("reply")))
    handler = SendMessageHandler(
        runner,
        FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty()))),
        chat_store=repo,
        chat_query=repo,
        clock=lambda: NOW,
        display_tz=UTC,
    )
    (await handler.execute(SendMessageRequest("next"))).unwrap()
    (run,) = runner.runs
    assert run.instructions.endswith(
        "- [11/22 00:00] (id:a-second) Second\n- [11/22 00:00] (id:z-first) First"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("has_open", [False, True])
async def test_corrupt_previous_episode_warns_without_losing_reply(
    tmp_path: Path, has_open: bool
) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    previous = repo.record_turns(None, (), title="previous", updated_at=NOW).unwrap()
    repo.close_episode(previous.id, closed_at=NOW, reason="rollover").unwrap()
    if has_open:
        add_turn(repo)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE episodes SET started_at = 'bad' WHERE id = ?", (previous.id,)
        )
    runner = FakeAgentRunner(Ok(AgentReply("reply")))
    profile = FakeGetCoreProfile(Ok(_profile_response(CoreProfile.empty())))
    handler = SendMessageHandler(
        runner, profile, chat_store=repo, chat_query=repo, clock=lambda: NOW
    )
    response = (await handler.execute(SendMessageRequest("next"))).unwrap()
    assert response.reply == "reply"
    expected_event = (
        "previous_episode_read_failed" if has_open else "latest_episode_read_failed"
    )
    assert [(warning.event, warning.error_type) for warning in response.warnings] == [
        (expected_event, "CORRUPT_DATA"),
        ("episode_index_read_failed", "CORRUPT_DATA"),
    ]
    assert runner.runs[-1].history == (
        (ChatMessage("user", "question"),) if has_open else ()
    )
    assert "## 直近の話題のサマリー" not in runner.runs[-1].instructions
