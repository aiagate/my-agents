import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from my_agents.core.types import (
    AppErrorKind,
    ConversationCounts,
    EventBrief,
    EventDelivery,
    EventRecord,
    EventStatus,
    MemoryBlock,
    MemoryBlocks,
    MemoryCandidate,
    MemoryStatus,
    TurnOrigin,
)
from my_agents.shell.store import Store, TurnEntry
from tests.helpers import assert_err

_NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "data" / "chats.db", tmp_path / "memory" / "core.md")


def _brief() -> EventBrief:
    return EventBrief(
        headline="h",
        facts=("f",),
        impact="i",
        suggested_angle="s",
        urgency=8,
        relevance=8,
        confidence=0.9,
        requires_user_action=True,
        sensitive=False,
        dedupe_key="k",
    )


def _event(
    event_id: str = "evt_1",
    key: str = "key",
    status: EventStatus = EventStatus.RECEIVED,
    delivery: EventDelivery = EventDelivery.SILENT,
    conversation_id: str = "c",
) -> EventRecord:
    return EventRecord(
        event_id,
        conversation_id,
        key,
        "name",
        "source",
        _NOW,
        _NOW,
        status,
        delivery,
        "",
        "{}",
        None,
        "",
        None,
        None,
    )


def _turns(content: str = "hi") -> tuple[TurnEntry, ...]:
    return (
        ("user", TurnOrigin.USER, content, None),
        ("assistant", TurnOrigin.REPLY, "ok", None),
    )


def test_reads_are_empty_before_any_database_exists(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get_state("c").unwrap().epoch == 0
    assert store.get_open_episode("c").unwrap() is None
    assert store.get_episode("e").unwrap() is None
    assert store.list_episodes("c").unwrap() == ()
    assert store.list_turns("e").unwrap() == ()
    assert store.recent_turns("c").unwrap() == ()
    assert store.get_event("evt").unwrap() is None
    assert store.list_events("c").unwrap() == ()
    assert store.list_candidates("c", MemoryStatus.PENDING).unwrap() == ()
    assert not (tmp_path / "data").exists()


def test_conversation_state_epoch_and_proactive_accounting(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.ensure_conversation("c", _NOW).unwrap().epoch == 0
    assert store.ensure_conversation("c", _NOW).unwrap().epoch == 0
    assert store.bump_epoch("c", _NOW).unwrap().epoch == 1
    first = store.record_proactive("c", _NOW).unwrap()
    second = store.record_proactive("c", _NOW).unwrap()
    next_day = store.record_proactive("c", _NOW + timedelta(days=1)).unwrap()
    assert (first.proactive_count_today, second.proactive_count_today) == (1, 2)
    assert next_day.proactive_count_today == 1
    assert next_day.proactive_day == "2026-11-23"
    assert store.get_state("c").unwrap().last_proactive_at == (_NOW + timedelta(days=1))


def test_record_turns_creates_rolls_and_closes_episodes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first, turns = store.record_turns(
        "c",
        None,
        _turns(),
        now=_NOW,
        title="first",
        max_turns=1,
        gap_minutes=60,
    ).unwrap()
    assert (first.turn_count, turns[0].turn_index, turns[1].seq) == (2, 1, 2)
    same, _ = store.record_turns(
        "c",
        first.episode_id,
        (),
        now=_NOW,
        title="ignored",
        max_turns=10,
        gap_minutes=60,
    ).unwrap()
    assert same.episode_id == first.episode_id
    closed, _ = store.record_turns(
        "c",
        first.episode_id,
        _turns("second"),
        now=_NOW + timedelta(minutes=1),
        title="second",
        max_turns=2,
        gap_minutes=60,
    ).unwrap()
    assert closed.episode_id != first.episode_id
    closed_episode = store.get_episode(first.episode_id).unwrap()
    assert closed_episode is not None
    assert closed_episode.closed_at is not None
    third, _ = store.record_turns(
        "c",
        first.episode_id,
        _turns("third"),
        now=_NOW + timedelta(minutes=2),
        title="third",
        max_turns=10,
        gap_minutes=1,
    ).unwrap()
    assert third.episode_id == closed.episode_id
    assert [episode.title for episode in store.list_episodes("c").unwrap()] == [
        "second",
        "first",
    ]


def test_turn_reads_limits_and_summary_updates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    episode, _ = store.record_turns(
        "c",
        None,
        _turns("hello"),
        now=_NOW,
        title="topic",
        max_turns=10,
        gap_minutes=60,
    ).unwrap()
    assert [turn.content for turn in store.list_turns(episode.episode_id).unwrap()] == [
        "hello",
        "ok",
    ]
    assert store.list_turns(episode.episode_id, limit=1).unwrap()[0].content == "ok"
    assert store.list_turns(episode.episode_id, limit=0).unwrap() == ()
    assert store.recent_turns("c", limit=1).unwrap()[0].content == "ok"
    assert store.recent_turns("c", limit=0).unwrap() == ()
    assert store.list_episodes("c", limit=0).unwrap() == ()
    updated = store.update_summary(
        episode.episode_id,
        title="new",
        overview="overview",
        summary="summary",
        summarized_turn_count=2,
    ).unwrap()
    assert updated is not None
    assert (updated.title, updated.summarized_turn_count) == ("new", 2)
    assert (
        store.update_summary(
            "missing",
            title="x",
            overview="",
            summary="",
            summarized_turn_count=0,
        ).unwrap()
        is None
    )


def test_event_insert_duplicate_update_and_surfacing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _event()
    assert store.insert_event(record).unwrap() == record
    duplicate = _event("evt_2", "key")
    assert store.insert_event(duplicate).unwrap().event_id == "evt_1"
    assert store.get_event("evt_1").unwrap() == record
    assert store.get_event("missing").unwrap() is None
    assert (
        store.update_event(
            "evt_1",
            status=EventStatus.COMPLETED,
            delivery=EventDelivery.AMBIENT,
            merge_reason="reason",
            brief=_brief(),
            failure_reason="",
            surfaced_at=None,
            linked_turn_id=None,
        ).unwrap()
        is None
    )
    completed = store.get_event("evt_1").unwrap()
    assert completed is not None
    assert completed.brief is not None
    assert (completed.status, completed.delivery) == (
        EventStatus.COMPLETED,
        EventDelivery.AMBIENT,
    )
    assert store.list_events("c", unsurfaced_only=True).unwrap() == (completed,)
    assert store.list_events("c", limit=0).unwrap() == ()
    assert store.mark_surfaced((), _NOW).unwrap() is None
    assert store.mark_surfaced(("evt_1",), _NOW).unwrap() is None
    assert store.list_events("c", unsurfaced_only=True).unwrap() == ()
    surfaced_event = store.get_event("evt_1").unwrap()
    assert surfaced_event is not None
    assert surfaced_event.surfaced_at == _NOW


def test_memory_candidates_and_curation_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    candidate = MemoryCandidate("m1", "c", MemoryBlock.USER, "content", "reason", _NOW)
    assert store.save_candidate(candidate).unwrap() is None
    assert store.list_candidates("c", MemoryStatus.PENDING).unwrap() == (candidate,)
    assert store.set_candidate_status((), MemoryStatus.PROMOTED).unwrap() is None
    assert store.set_candidate_status(("m1",), MemoryStatus.PROMOTED).unwrap() is None
    assert store.list_candidates("c", MemoryStatus.PENDING).unwrap() == ()
    assert store.list_candidates("c", MemoryStatus.PROMOTED).unwrap()[
        0
    ].candidate_id == ("m1")


def test_memory_file_roundtrip_parse_and_errors(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.read_memory().unwrap() == MemoryBlocks()
    blocks = MemoryBlocks("u", "r", "a")
    assert store.write_memory(blocks).unwrap() is None
    assert store.read_memory().unwrap() == blocks
    (tmp_path / "memory" / "core.md").write_text(
        "preamble\n## user\nU\n\n## relationship\n\n## arc\nA\n", encoding="utf-8"
    )
    parsed = store.read_memory().unwrap()
    assert (parsed.user, parsed.relationship, parsed.arc) == ("U", "", "A")
    (tmp_path / "memory" / "core.md").unlink()
    (tmp_path / "memory" / "core.md").mkdir()
    assert_err(
        store.read_memory(),
        kind=AppErrorKind.STORAGE,
        message_contains="core.md",
    )
    assert_err(
        store.write_memory(blocks),
        kind=AppErrorKind.STORAGE,
        message_contains="core.md",
    )
    parent_file = tmp_path / "parent-file"
    parent_file.write_text("x", encoding="utf-8")
    assert_err(
        Store(tmp_path / "other.db", parent_file / "core.md").write_memory(blocks),
        kind=AppErrorKind.STORAGE,
        message_contains="parent-file",
    )


def test_reset_removes_conversation_data_and_bumps_epoch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure_conversation("c", _NOW).unwrap()
    store.bump_epoch("c", _NOW).unwrap()
    store.record_turns(
        "c",
        None,
        _turns(),
        now=_NOW,
        title="topic",
        max_turns=10,
        gap_minutes=60,
    ).unwrap()
    store.insert_event(_event())
    store.save_candidate(
        MemoryCandidate("m", "c", MemoryBlock.USER, "x", "r", _NOW)
    ).unwrap()
    assert store.reset_conversation("c").unwrap() == 2
    assert store.list_episodes("c").unwrap() == ()
    assert store.list_events("c").unwrap() == ()
    assert store.get_state("c").unwrap().epoch == 2


def test_storage_errors_are_reported(tmp_path: Path) -> None:
    broken = Store(tmp_path / "broken.db", tmp_path / "core.md")
    broken_path = tmp_path / "broken.db"
    broken_path.write_text("not a database", encoding="utf-8")
    assert_err(
        broken.get_state("c"),
        kind=AppErrorKind.STORAGE,
        message_contains="database",
    )
    directory_db = tmp_path / "directory.db"
    directory_db.mkdir()
    assert_err(
        Store(directory_db, tmp_path / "core.md").ensure_conversation("c", _NOW),
        kind=AppErrorKind.STORAGE,
        message_contains="unable to open database",
    )


def test_corrupt_rows_are_reported_as_storage_errors(tmp_path: Path) -> None:
    store = _store(tmp_path)
    episode, _ = store.record_turns(
        "c",
        None,
        _turns(),
        now=_NOW,
        title="topic",
        max_turns=10,
        gap_minutes=60,
    ).unwrap()
    with closing(sqlite3.connect(tmp_path / "data" / "chats.db")) as connection:
        connection.execute("UPDATE dialogue_turns SET created_at = 'bad'")
        connection.commit()
    assert_err(
        store.list_turns(episode.episode_id),
        kind=AppErrorKind.STORAGE,
        message_contains="Invalid isoformat",
    )


def test_search_episodes_spans_conversations_and_honours_limit(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.record_turns(
        "c",
        None,
        _turns(),
        now=_NOW,
        title="alpha topic",
        max_turns=10,
        gap_minutes=60,
    ).unwrap()
    store.record_turns(
        "other",
        None,
        _turns(),
        now=_NOW,
        title="beta topic",
        max_turns=10,
        gap_minutes=60,
    ).unwrap()
    assert [episode.title for episode in store.search_episodes("topic").unwrap()] == [
        "beta topic",
        "alpha topic",
    ]
    assert store.search_episodes("topic", limit=1).unwrap()[0].title == "beta topic"
    assert store.search_episodes("topic", limit=0).unwrap() == ()
    assert store.search_episodes("missing").unwrap() == ()


def test_idempotency_is_scoped_to_one_conversation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.insert_event(_event("evt_a", "shared")).unwrap().event_id == "evt_a"
    assert store.insert_event(_event("evt_b", "shared")).unwrap().event_id == "evt_a"
    assert (
        store.insert_event(_event("evt_c", "shared", conversation_id="other"))
        .unwrap()
        .event_id
        == "evt_c"
    )


def test_counts_cover_turns_episodes_events_and_candidates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.counts("c").unwrap() == ConversationCounts()
    store.record_turns(
        "c",
        None,
        _turns(),
        now=_NOW,
        title="topic",
        max_turns=10,
        gap_minutes=60,
    ).unwrap()
    store.insert_event(_event())
    for candidate_id, status in (
        ("p", MemoryStatus.PENDING),
        ("q", MemoryStatus.PROMOTED),
        ("r", MemoryStatus.REJECTED),
    ):
        store.save_candidate(
            MemoryCandidate(
                candidate_id,
                "c",
                MemoryBlock.USER,
                "x",
                "r",
                _NOW,
                status,
            )
        ).unwrap()
    counts = store.counts("c").unwrap()
    assert (counts.turns, counts.episodes, counts.events) == (2, 1, 1)
    assert (
        counts.pending_candidates,
        counts.promoted_candidates,
        counts.rejected_candidates,
    ) == (1, 1, 1)
