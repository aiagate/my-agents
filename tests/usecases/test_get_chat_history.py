import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flow_res import Err, Ok, Result

from my_agents.contracts.events import (
    EventDelivery,
    EventRecord,
    EventStatus,
)
from my_agents.contracts.ports.chat_history import NewDialogueTurn
from my_agents.contracts.ports.event_ledger import (
    EventLedgerError,
    EventLedgerErrorType,
    IEventLedger,
)
from my_agents.infrastructure.sqlite_chat_repository import SqliteChatRepository
from my_agents.usecases.get_chat_history import (
    DialogueTurnView,
    EventTimelineView,
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
    first = repo.record_turns(
        None,
        (
            NewDialogueTurn("user", "first question", _NOW),
            NewDialogueTurn("assistant", "first answer", _NOW),
        ),
        title="first",
        updated_at=_NOW,
    ).unwrap()
    repo.close_episode(first.id, closed_at=_NOW, reason="clear").unwrap()
    second = repo.record_turns(
        None, (), title="second", updated_at=_NOW + timedelta(minutes=1)
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
    episode = repo.record_turns(None, (), title="title", updated_at=_NOW).unwrap()
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
        RecallChatHandler(repo).recall_episode(episode.id),
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


class StubEventLedger(IEventLedger):
    def __init__(
        self, result: Result[tuple[EventRecord, ...], EventLedgerError]
    ) -> None:
        self.result = result

    def record(self, entry: EventRecord) -> Result[None, EventLedgerError]:
        raise AssertionError(entry)

    def list_recent(
        self, *, limit: int = 10, unsurfaced_only: bool = False
    ) -> Result[tuple[EventRecord, ...], EventLedgerError]:
        _ = (limit, unsurfaced_only)
        return self.result

    def get_event(self, event_id: str) -> Result[EventRecord | None, EventLedgerError]:
        raise AssertionError(event_id)

    def mark_surfaced(
        self, event_ids: Sequence[str], *, surfaced_at: datetime
    ) -> Result[None, EventLedgerError]:
        _ = (event_ids, surfaced_at)
        raise AssertionError("not used")


def _event(
    event_id: str,
    *,
    recorded_at: datetime,
    occurred_at: datetime | None = None,
    delivery: EventDelivery = EventDelivery.PROACTIVE,
    failure_reason: str = "",
) -> EventRecord:
    return EventRecord(
        event_id,
        "email.received",
        recorded_at if occurred_at is None else occurred_at,
        recorded_at,
        EventStatus.FAILED if failure_reason else EventStatus.COMPLETED,
        delivery,
        '{"subject": "定例"}',
        "メールを受信しました。",
        (),
        "確認する。",
        failure_reason,
        recorded_at,
    )


def test_detail_merges_correlated_event_ledger_records(tmp_path: Path) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    first = repo.record_turns(
        None,
        (
            NewDialogueTurn("user", "first question", _NOW),
            NewDialogueTurn("assistant", "first answer", _NOW),
        ),
        title="first",
        updated_at=_NOW,
    ).unwrap()
    repo.close_episode(
        first.id, closed_at=_NOW + timedelta(minutes=1), reason="clear"
    ).unwrap()
    repo.record_turns(
        None, (), title="second", updated_at=_NOW + timedelta(minutes=3)
    ).unwrap()
    later = _event(
        "evt_later",
        recorded_at=_NOW + timedelta(minutes=2),
        delivery=EventDelivery.AMBIENT,
    )
    inside = _event("evt_inside", recorded_at=_NOW, failure_reason="boom")
    after = _event(
        "evt_after",
        recorded_at=_NOW + timedelta(minutes=4),
        delivery=EventDelivery.AMBIENT,
    )
    ledger = StubEventLedger(Ok((after, later, inside)))

    detail = GetChatHistoryHandler(repo, event_ledger=ledger).get_episode_detail(2)

    assert detail.unwrap().events == (
        EventTimelineView(
            "evt_inside",
            EventStatus.FAILED,
            EventDelivery.PROACTIVE,
            _NOW,
            _NOW,
            "メールを受信しました。",
            "boom",
            _NOW,
        ),
    )
    active = GetChatHistoryHandler(repo, event_ledger=ledger).get_episode_detail(1)
    assert [event.event_id for event in active.unwrap().events] == ["evt_after"]
    assert GetChatHistoryHandler(repo).get_episode_detail(1).unwrap().events == ()


def test_detail_reports_event_ledger_failure(tmp_path: Path) -> None:
    repo = SqliteChatRepository(tmp_path / "chats.db")
    repo.record_turns(None, (), title="title", updated_at=_NOW).unwrap()
    ledger = StubEventLedger(
        Err(EventLedgerError(EventLedgerErrorType.READ_FAILED, "ledger offline"))
    )

    detail = GetChatHistoryHandler(repo, event_ledger=ledger).get_episode_detail(1)

    response = detail.unwrap()
    assert response.episode.episode_id
    assert response.events == ()
    (warning,) = response.warnings
    assert (
        warning.event,
        warning.error_type,
        warning.message,
        warning.display_message,
    ) == (
        "event_ledger_read_failed",
        "READ_FAILED",
        "ledger offline",
        "イベント台帳を読み込めませんでした。",
    )


def test_detail_correlates_delayed_events_by_occurrence_time(tmp_path: Path) -> None:
    path = tmp_path / "chats.db"
    repo = SqliteChatRepository(path)
    episode = repo.record_turns(
        None,
        (
            NewDialogueTurn("user", "first question", _NOW),
            NewDialogueTurn("assistant", "first answer", _NOW),
        ),
        title="first",
        updated_at=_NOW,
    ).unwrap()
    repo.close_episode(
        episode.id, closed_at=_NOW + timedelta(minutes=1), reason="clear"
    ).unwrap()
    delayed = _event(
        "evt_delayed",
        occurred_at=_NOW + timedelta(seconds=30),
        recorded_at=_NOW + timedelta(minutes=30),
    )
    too_early = _event(
        "evt_early",
        occurred_at=_NOW - timedelta(minutes=1),
        recorded_at=_NOW + timedelta(seconds=10),
    )
    too_late = _event(
        "evt_late",
        occurred_at=_NOW + timedelta(minutes=2),
        recorded_at=_NOW + timedelta(seconds=5),
    )
    ledger = StubEventLedger(Ok((too_late, delayed, too_early)))

    detail = GetChatHistoryHandler(repo, event_ledger=ledger).get_episode_detail(1)

    assert [event.event_id for event in detail.unwrap().events] == ["evt_delayed"]
