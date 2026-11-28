import errno
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID
from zoneinfo import ZoneInfo

from flow_res import Err, Ok
from pydantic import SecretStr

from my_agents.core.types import (
    AppError,
    AppErrorKind,
    ConversationCounts,
    EventAction,
    EventDecision,
    EventRecord,
    EventStatus,
    MemoryBlocks,
    MessageOrigin,
    ProactivePolicy,
    TimelineKind,
)
from my_agents.shell.push import PushSubscription
from my_agents.shell.store import CURRENT_SCHEMA_VERSION, PendingEvent, Store
from tests.helpers import assert_err


def _t(minutes: int = 0, *, days: int = 0) -> datetime:
    return datetime(2026, 5, 1, 10, 0, tzinfo=UTC) + timedelta(
        days=days, minutes=minutes
    )


def _record(
    event_id: str,
    conversation_id: str = "default",
    key: str | None = None,
    minutes: int = 0,
) -> EventRecord:
    when = _t(minutes)
    return EventRecord(
        event_id=event_id,
        conversation_id=conversation_id,
        idempotency_key=key or event_id,
        name="email.received",
        source="mail",
        occurred_at=when,
        received_at=when,
        status=EventStatus.RECEIVED,
        action=EventAction.IGNORE,
        reason="",
        note="",
        payload_json='{"subject":"hello"}',
        failure_reason="",
        surfaced_at=None,
    )


_CONCURRENT_INIT_SCRIPT = """
import sys
from datetime import UTC, datetime
from pathlib import Path

from flow_res import Ok

from my_agents.shell.store import Store

store = Store(Path(sys.argv[1]), Path(sys.argv[2]))
result = store.ensure_conversation("default", datetime(2026, 5, 1, tzinfo=UTC))
if not isinstance(result, Ok):
    print(result.error.message, file=sys.stderr)
    raise SystemExit(1)
"""


def test_uninitialized_store_reads_empty_without_creating_files(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"
    mem_path = tmp_path / "core.md"
    store = Store(db_path, mem_path)

    state = store.get_state("default")
    msgs = store.recent_messages("default", limit=10)
    pending = store.list_pending_notes("default")
    received = store.list_received_events()
    timeline = store.list_timeline("default")
    event = store.get_event("evt_1")
    event_in = store.get_event_in("default", "evt_1")
    counts = store.counts("default")
    memory = store.read_memory()

    assert isinstance(state, Ok)
    assert state.value.epoch == 0
    assert isinstance(msgs, Ok)
    assert msgs.value == ()
    assert pending == Ok(((), 0))
    assert received == Ok(())
    assert timeline == Ok(())
    assert isinstance(event, Ok)
    assert event.value is None
    assert isinstance(event_in, Ok)
    assert event_in.value is None
    assert isinstance(counts, Ok)
    assert counts.value == ConversationCounts(0, 0)
    assert isinstance(memory, Ok)
    assert memory.value == MemoryBlocks()
    assert not db_path.exists()
    assert not mem_path.exists()


def test_read_only_open_does_not_initialize_existing_empty_database(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "empty.db"
    db_path.touch()
    store = Store(db_path, tmp_path / "core.md")

    assert_err(
        store.get_state("default"),
        kind=AppErrorKind.STORAGE,
        message_contains="no such table: conversations",
    )
    assert db_path.stat().st_size == 0

    written = store.ensure_conversation("default", _t())
    assert isinstance(written, Ok)
    assert written.value.conversation_id == "default"


def test_store_messages_epochs_and_reset_isolation(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db", tmp_path / "core.md")
    ensured = store.ensure_conversation("default", _t(0))
    assert isinstance(ensured, Ok)
    assert ensured.value.epoch == 0

    bumped = store.bump_epoch("default", _t(1))
    assert isinstance(bumped, Ok)
    assert (bumped.value.epoch, bumped.value.last_user_activity_at) == (1, _t(1))

    appended = store.append_messages(
        "default",
        (
            ("user", MessageOrigin.USER, "first", None),
            ("assistant", MessageOrigin.REPLY, "second", None),
            ("assistant", MessageOrigin.PROACTIVE, "third", "evt_1"),
        ),
        _t(2),
    )
    assert isinstance(appended, Ok)
    assert len(appended.value) == 3
    assert all(UUID(message.message_id).version == 7 for message in appended.value)
    assert len({message.message_id for message in appended.value}) == 3

    zero = store.recent_messages("default", limit=0)
    assert isinstance(zero, Ok)
    assert zero.value == ()

    recent = store.recent_messages("default", limit=2)
    assert isinstance(recent, Ok)
    assert [m.content for m in recent.value] == ["second", "third"]

    assert isinstance(
        store.append_messages(
            "other", (("user", MessageOrigin.USER, "keep me", None),), _t(3)
        ),
        Ok,
    )
    assert isinstance(store.insert_event(_record("evt_d", "default")), Ok)
    assert isinstance(store.insert_event(_record("evt_o", "other")), Ok)

    counts = store.counts("default")
    assert isinstance(counts, Ok)
    assert counts.value == ConversationCounts(messages=3, events=1)

    deleted = store.reset_conversation("default")
    assert isinstance(deleted, Ok)
    assert deleted.value == 3

    after_reset = store.get_state("default")
    other_msgs = store.recent_messages("other", limit=10)
    assert isinstance(after_reset, Ok)
    assert (after_reset.value.epoch, after_reset.value.proactive_count_today) == (2, 0)
    assert isinstance(other_msgs, Ok)
    assert [m.content for m in other_msgs.value] == ["keep me"]


def test_store_events_idempotency_finalize_fail_and_surfacing(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db", tmp_path / "core.md")
    policy = ProactivePolicy(5, 0, 0, 0)
    rec = _record("evt_1", key="same-key")
    first = store.insert_event(rec)
    duplicate = store.insert_event(_record("evt_2", key="same-key"))
    assert isinstance(first, Ok)
    assert isinstance(duplicate, Ok)
    assert duplicate.value.event_id == "evt_1"

    speak_no_note = EventDecision(
        action=EventAction.SPEAK,
        message="換気をしてください。",
        note="",
        reason="temp_high",
    )
    committed = store.finalize_event(
        "default",
        "evt_1",
        speak_no_note,
        received_epoch=0,
        now=_t(5),
        policy=policy,
    )
    assert isinstance(committed, Ok)
    assert committed.value.action is EventAction.SPEAK
    assert committed.value.note == "換気をしてください。"
    assert committed.value.message is not None

    loaded = store.get_event_in("default", "evt_1")
    wrong_conv = store.get_event_in("other", "evt_1")
    assert isinstance(loaded, Ok)
    assert loaded.value is not None
    assert loaded.value.surfaced_at == _t(5)
    assert isinstance(wrong_conv, Ok)
    assert wrong_conv.value is None

    assert isinstance(store.insert_event(_record("evt_3", minutes=6)), Ok)
    same_day_speak = store.finalize_event(
        "default",
        "evt_3",
        EventDecision(
            action=EventAction.SPEAK, message="2nd alert", note="n", reason="r"
        ),
        received_epoch=0,
        now=_t(6),
        policy=policy,
    )
    assert isinstance(same_day_speak, Ok)
    state_after = store.get_state("default")
    assert isinstance(state_after, Ok)
    assert state_after.value.proactive_count_today == 2

    assert isinstance(store.insert_event(_record("evt_4", minutes=7)), Ok)
    next_day_speak = store.finalize_event(
        "default",
        "evt_4",
        EventDecision(
            action=EventAction.SPEAK, message="next day", note="n", reason="  "
        ),
        received_epoch=0,
        now=_t(7, days=1),
        policy=policy,
    )
    assert isinstance(next_day_speak, Ok)
    assert next_day_speak.value.reason == "evaluated"

    assert isinstance(store.insert_event(_record("evt_note", minutes=8)), Ok)
    downgraded = store.finalize_event(
        "default",
        "evt_note",
        speak_no_note,
        received_epoch=99,
        now=_t(8, days=1),
        policy=policy,
    )
    assert isinstance(downgraded, Ok)
    assert (downgraded.value.action, downgraded.value.reason) == (
        EventAction.NOTE,
        "epoch_conflict",
    )

    assert isinstance(store.mark_surfaced("default", (), _t(9)), Ok)
    assert isinstance(store.mark_surfaced("default", ("evt_note",), _t(9)), Ok)
    note_row = store.get_event("evt_note")
    assert isinstance(note_row, Ok)
    assert note_row.value is not None
    assert note_row.value.surfaced_at == _t(9)

    assert isinstance(store.insert_event(_record("evt_fail", minutes=10)), Ok)
    assert isinstance(store.fail_event("default", "evt_fail", "upstream timeout"), Ok)
    failed = store.get_event("evt_fail")
    assert isinstance(failed, Ok)
    assert failed.value is not None
    assert (failed.value.status, failed.value.failure_reason) == (
        EventStatus.FAILED,
        "upstream timeout",
    )

    pending = store.list_pending_notes("default")
    timeline = store.list_timeline("default")
    assert pending == Ok(((), 0))
    assert isinstance(timeline, Ok)
    failed_items = [
        item for item in timeline.value if item.kind is TimelineKind.EVENT_FAILED
    ]
    assert [item.item_id for item in failed_items] == ["evt_fail"]


def test_store_memory_lock_atomic_writes_and_error_handling(tmp_path: Path) -> None:
    mem_path = tmp_path / "core.md"
    store = Store(tmp_path / "state.db", mem_path)

    assert isinstance(
        store.update_memory(
            lambda _blocks: Ok(
                MemoryBlocks(user="tea", relationship="warm", arc="active")
            )
        ),
        Ok,
    )
    mem_path.write_text(
        "preamble ignored\n## user\ncoffee\n## relationship\ncalm\n",
        encoding="utf-8",
    )
    parsed = store.read_memory()
    assert isinstance(parsed, Ok)
    assert parsed.value == MemoryBlocks(user="coffee", relationship="calm", arc="")

    rejected = store.update_memory(
        lambda _b: Err(AppError(AppErrorKind.VALIDATION, "bad edit"))
    )
    assert_err(rejected, kind=AppErrorKind.VALIDATION, message_contains="bad edit")

    assert isinstance(
        store.update_memory(
            lambda blocks: Ok(
                MemoryBlocks(
                    user=blocks.user + "!",
                    relationship=blocks.relationship,
                    arc=blocks.arc,
                )
            )
        ),
        Ok,
    )
    assert store.read_memory() == Ok(
        MemoryBlocks(user="coffee!", relationship="calm", arc="")
    )

    with patch(
        "tempfile.NamedTemporaryFile",
        side_effect=OSError(errno.EACCES, "no tmp"),
    ):
        assert_err(
            store.update_memory(lambda _blocks: Ok(MemoryBlocks(user="x"))),
            kind=AppErrorKind.STORAGE,
            message_contains="no tmp",
        )

    with patch.object(Path, "read_text", side_effect=OSError(errno.EACCES, "denied")):
        assert_err(
            store.read_memory(), kind=AppErrorKind.STORAGE, message_contains="denied"
        )
        assert_err(
            store.update_memory(lambda b: Ok(b)),
            kind=AppErrorKind.STORAGE,
            message_contains="denied",
        )

    with patch("os.fsync", side_effect=OSError(errno.EIO, "disk full")):
        assert_err(
            store.update_memory(lambda _blocks: Ok(MemoryBlocks(user="x"))),
            kind=AppErrorKind.STORAGE,
            message_contains="disk full",
        )
    assert not any(path.name.startswith("tmp") for path in tmp_path.iterdir())

    with patch("fcntl.flock", side_effect=OSError(errno.ENOLCK, "no locks")):
        assert_err(
            store.update_memory(lambda _blocks: Ok(MemoryBlocks(user="x"))),
            kind=AppErrorKind.STORAGE,
            message_contains="no locks",
        )


def test_store_memory_reports_unicode_failures_and_cleans_temporary_files(
    tmp_path: Path,
) -> None:
    bad_memory = tmp_path / "bad.md"
    bad_memory.write_bytes(b"\xca")
    bad_store = Store(tmp_path / "bad.db", bad_memory)
    assert_err(
        bad_store.read_memory(),
        kind=AppErrorKind.STORAGE,
        message_contains="can't decode",
    )

    store = Store(tmp_path / "state.db", tmp_path / "core.md")
    assert isinstance(
        store.update_memory(lambda _blocks: Ok(MemoryBlocks(user="ok"))),
        Ok,
    )
    assert_err(
        store.update_memory(lambda _blocks: Ok(MemoryBlocks(user="\ud800"))),
        kind=AppErrorKind.STORAGE,
        message_contains="surrogates not allowed",
    )
    assert not any(path.name.startswith("tmp") for path in tmp_path.iterdir())


def test_store_rejects_incompatible_schema_and_corrupt_rows(tmp_path: Path) -> None:
    bad_ver_db = tmp_path / "old.db"
    with closing(sqlite3.connect(bad_ver_db)) as conn:
        conn.execute("CREATE TABLE legacy (id TEXT)")
    bad_store = Store(bad_ver_db, tmp_path / "core1.md")
    assert_err(
        bad_store.get_state("default"),
        kind=AppErrorKind.STORAGE,
        message_contains=f"expected {CURRENT_SCHEMA_VERSION}",
    )
    assert_err(
        bad_store.ensure_conversation("default", _t()),
        kind=AppErrorKind.STORAGE,
        message_contains="Incompatible database schema",
    )

    corrupt_db = tmp_path / "corrupt.db"
    good_store = Store(corrupt_db, tmp_path / "core2.md")
    assert isinstance(good_store.ensure_conversation("default", _t()), Ok)
    reopened = Store(corrupt_db, tmp_path / "core2.md")
    assert isinstance(reopened.get_state("default"), Ok)
    with closing(sqlite3.connect(corrupt_db)) as conn:
        conn.execute(
            "INSERT INTO messages "
            "(message_id, conversation_id, role, origin, content, created_at) "
            "VALUES ('01960000-0000-7000-8000-000000000001', 'default', 'system', "
            "'reply', 'x', '2026-05-01T00:00:00+00:00')"
        )
        conn.commit()
    assert_err(
        good_store.recent_messages("default", limit=5),
        kind=AppErrorKind.STORAGE,
        message_contains="Input should be 'user' or 'assistant'",
    )

    with closing(sqlite3.connect(corrupt_db)) as conn:
        conn.execute("DELETE FROM messages")
        conn.execute(
            "INSERT INTO messages "
            "(message_id, conversation_id, role, origin, content, created_at) "
            "VALUES ('01960000-0000-7000-8000-000000000002', 'default', 'user', "
            "'user', 'x', X'0102')"
        )
        conn.commit()
    assert_err(
        good_store.recent_messages("default", limit=5),
        kind=AppErrorKind.STORAGE,
        message_contains="created_at",
    )

    readonly_missing = Store(tmp_path / "unwritable" / "x.db", tmp_path / "c.md")
    with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("locked")):
        assert_err(
            readonly_missing.ensure_conversation("default", _t()),
            kind=AppErrorKind.STORAGE,
            message_contains="locked",
        )


def test_late_incompatible_schema_returns_storage_error(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = Store(db_path, tmp_path / "core.md")
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE legacy (id TEXT)")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()

    assert_err(
        store.ensure_conversation("default", _t()),
        kind=AppErrorKind.STORAGE,
        message_contains=(
            f"Incompatible database schema version 1 "
            f"(expected {CURRENT_SCHEMA_VERSION})"
        ),
    )
    with closing(sqlite3.connect(db_path)) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
    assert tables == ["legacy"]


def test_mid_ddl_failure_rolls_back_atomically_and_recovers(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = Store(
        db_path,
        tmp_path / "core.md",
        schema_statements=("CREATE TABLE probe (id INTEGER)", "NOT_A_STATEMENT"),
    )
    failure = store.ensure_conversation("default", _t())
    assert_err(failure, kind=AppErrorKind.STORAGE, message_contains="syntax error")

    with closing(sqlite3.connect(db_path)) as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert tables == []
    assert version == 0

    recovered = Store(db_path, tmp_path / "core.md").ensure_conversation(
        "default", _t(1)
    )
    assert isinstance(recovered, Ok)
    assert recovered.value.conversation_id == "default"
    with closing(sqlite3.connect(db_path)) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert names == {"conversations", "messages", "events", "push_subscriptions"}
    assert version == CURRENT_SCHEMA_VERSION


def test_v5_database_missing_table_returns_storage_error_without_recreating(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    store = Store(db_path, tmp_path / "core.md")
    assert isinstance(store.ensure_conversation("default", _t()), Ok)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("DROP TABLE events")
        conn.commit()

    corrupted = Store(db_path, tmp_path / "other.md")
    assert_err(
        corrupted.insert_event(_record("evt_1")),
        kind=AppErrorKind.STORAGE,
        message_contains="no such table: events",
    )
    with closing(sqlite3.connect(db_path)) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert tables == {"conversations", "messages", "push_subscriptions"}


def test_initialized_v5_database_skips_ddl_on_normal_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = Store(db_path, tmp_path / "core.md")
    assert isinstance(store.ensure_conversation("default", _t()), Ok)
    reopened = Store(
        db_path,
        tmp_path / "core.md",
        schema_statements=("NOT_A_STATEMENT",),
    )
    appended = reopened.append_messages(
        "default",
        (("user", MessageOrigin.USER, "hello", None),),
        _t(1),
    )
    assert isinstance(appended, Ok)
    assert [message.content for message in appended.value] == ["hello"]
    persisted = store.recent_messages("default", limit=1)
    assert isinstance(persisted, Ok)
    assert persisted.value[0].content == "hello"


def test_store_lists_conversations_with_counts_preview_and_order(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db", tmp_path / "core.md")

    empty = store.list_conversations()
    zero = store.list_conversations(limit=0)
    negative = store.list_conversations(limit=-1)
    assert isinstance(empty, Ok)
    assert empty.value == ()
    assert isinstance(zero, Ok)
    assert zero.value == ()
    assert isinstance(negative, Ok)
    assert negative.value == ()

    assert isinstance(store.ensure_conversation("alpha", _t(0)), Ok)
    assert isinstance(store.ensure_conversation("beta", _t(0)), Ok)
    assert isinstance(store.ensure_conversation("aardvark", _t(0)), Ok)
    assert isinstance(store.ensure_conversation("gamma", _t(0)), Ok)
    assert isinstance(
        store.append_messages(
            "alpha",
            (("user", MessageOrigin.USER, "  hello\n\tworld  ", None),),
            _t(1),
        ),
        Ok,
    )
    assert isinstance(store.insert_event(_record("evt_alpha", "alpha", minutes=2)), Ok)
    assert isinstance(
        store.append_messages(
            "gamma",
            (("assistant", MessageOrigin.PROACTIVE, "later", "evt_g"),),
            _t(3),
        ),
        Ok,
    )

    listed = store.list_conversations(limit=10)
    assert isinstance(listed, Ok)
    assert [summary.conversation_id for summary in listed.value] == [
        "gamma",
        "alpha",
        "aardvark",
        "beta",
    ]
    gamma, alpha, _aardvark, beta = listed.value
    assert (gamma.message_count, gamma.event_count) == (1, 0)
    assert gamma.last_message_preview == "later"
    assert gamma.last_message_origin is MessageOrigin.PROACTIVE
    assert (alpha.message_count, alpha.event_count) == (1, 1)
    assert alpha.last_message_preview == "hello world"
    assert alpha.last_message_origin is MessageOrigin.USER
    assert alpha.updated_at == _t(2)
    assert beta.last_message_preview == ""
    assert beta.last_message_origin is None
    assert beta.updated_at == _t(0)

    limited = store.list_conversations(limit=2)
    assert isinstance(limited, Ok)
    assert [summary.conversation_id for summary in limited.value] == [
        "gamma",
        "alpha",
    ]


def test_store_push_subscriptions_upsert_list_and_delete(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db", tmp_path / "core.md")

    uninitialized = store.list_push_subscriptions()
    assert isinstance(uninitialized, Ok)
    assert uninitialized.value == ()

    global_sub = PushSubscription(
        endpoint="https://push.example.com/a",
        p256dh="p256dh-a",
        auth=SecretStr("auth-a"),
        created_at=_t(0),
    )
    default_sub = PushSubscription(
        endpoint="https://push.example.com/b",
        p256dh="p256dh-b",
        auth=SecretStr("auth-b"),
        created_at=_t(1),
        conversation_id="default",
    )
    other_sub = PushSubscription(
        endpoint="https://push.example.com/c",
        p256dh="p256dh-c",
        auth=SecretStr("auth-c"),
        created_at=_t(2),
        conversation_id="other",
    )
    for subscription in (global_sub, default_sub, other_sub):
        assert isinstance(store.upsert_push_subscription(subscription), Ok)

    all_subs = store.list_push_subscriptions()
    assert isinstance(all_subs, Ok)
    assert [sub.endpoint for sub in all_subs.value] == [
        "https://push.example.com/a",
        "https://push.example.com/b",
        "https://push.example.com/c",
    ]
    assert all_subs.value[0].conversation_id is None
    assert all_subs.value[0].auth.get_secret_value() == "auth-a"

    for_default = store.list_push_subscriptions(conversation_id="default")
    assert isinstance(for_default, Ok)
    assert [sub.endpoint for sub in for_default.value] == [
        "https://push.example.com/a",
        "https://push.example.com/b",
    ]

    updated = PushSubscription(
        endpoint="https://push.example.com/a",
        p256dh="p256dh-new",
        auth=SecretStr("auth-new"),
        created_at=_t(3),
        conversation_id="default",
    )
    assert isinstance(store.upsert_push_subscription(updated), Ok)
    reloaded = store.list_push_subscriptions(conversation_id="default")
    assert isinstance(reloaded, Ok)
    assert reloaded.value[1].endpoint == "https://push.example.com/a"
    assert reloaded.value[1].p256dh == "p256dh-new"
    assert reloaded.value[1].auth.get_secret_value() == "auth-new"
    assert reloaded.value[1].conversation_id == "default"
    assert reloaded.value[1].created_at == _t(3)

    assert isinstance(store.delete_push_subscription("https://push.example.com/a"), Ok)
    deleted = store.delete_push_subscription("https://push.example.com/a")
    assert isinstance(deleted, Ok)
    assert deleted.value is False
    remaining = store.list_push_subscriptions()
    assert isinstance(remaining, Ok)
    assert [sub.endpoint for sub in remaining.value] == [
        "https://push.example.com/b",
        "https://push.example.com/c",
    ]
    assert store.delete_push_subscription("https://push.example.com/b") == Ok(True)


def test_concurrent_first_time_initialization_from_two_processes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CONCURRENT_INIT_SCRIPT,
                str(db_path),
                str(tmp_path / f"core-{index}.md"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=tmp_path,
        )
        for index in range(2)
    ]
    outputs = [process.communicate(timeout=30) for process in processes]
    assert [process.returncode for process in processes] == [0, 0]
    assert [stderr for _stdout, stderr in outputs] == ["", ""]

    with closing(sqlite3.connect(db_path)) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conversations = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert integrity == "ok"
    assert version == CURRENT_SCHEMA_VERSION
    assert conversations == 1
    assert tables == {"conversations", "messages", "events", "push_subscriptions"}


def test_store_lists_pending_notes_with_total_count(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db", tmp_path / "core.md")
    empty = store.list_pending_notes("default")
    assert empty == Ok(((), 0))

    for index in range(3):
        assert isinstance(
            store.insert_event(
                replace(
                    _record(f"note_{index}", minutes=index),
                    action=EventAction.NOTE,
                    note=f"note {index}",
                )
            ),
            Ok,
        )
    assert isinstance(
        store.insert_event(
            replace(
                _record("note_surfaced", minutes=5),
                action=EventAction.NOTE,
                surfaced_at=_t(6),
            )
        ),
        Ok,
    )
    assert isinstance(store.insert_event(_record("speak_event", minutes=6)), Ok)
    assert isinstance(
        store.insert_event(
            replace(
                _record("note_other", conversation_id="other", minutes=7),
                action=EventAction.NOTE,
            )
        ),
        Ok,
    )

    listed = store.list_pending_notes("default")
    assert isinstance(listed, Ok)
    events, total = listed.value
    assert total == 3
    assert [event.event_id for event in events] == ["note_2", "note_1", "note_0"]

    limited = store.list_pending_notes("default", limit=2)
    assert isinstance(limited, Ok)
    assert [event.event_id for event in limited.value[0]] == ["note_2", "note_1"]
    assert limited.value[1] == 3

    assert isinstance(store.mark_surfaced("default", ("note_2",), _t(8)), Ok)
    after_surface = store.list_pending_notes("default")
    assert isinstance(after_surface, Ok)
    assert after_surface.value[1] == 2


def test_store_timeline_union_orders_filters_and_caps(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db", tmp_path / "core.md")
    assert store.list_timeline("default") == Ok(())
    assert store.list_timeline("default", limit=0) == Ok(())

    assert isinstance(store.ensure_conversation("default", _t(0)), Ok)
    appended = store.append_messages(
        "default",
        (
            ("user", MessageOrigin.USER, "first", None),
            ("assistant", MessageOrigin.REPLY, "second", None),
        ),
        _t(0),
    )
    assert isinstance(appended, Ok)
    note = replace(
        _record("01960000-0000-7000-8000-000000000010", minutes=1),
        status=EventStatus.COMPLETED,
        action=EventAction.NOTE,
        note="ambient note",
    )
    no_note = replace(
        _record("01960000-0000-7000-8000-000000000011", minutes=2),
        status=EventStatus.COMPLETED,
        action=EventAction.NOTE,
        note="",
    )
    spoken = replace(
        _record("01960000-0000-7000-8000-000000000012", minutes=3),
        status=EventStatus.COMPLETED,
        action=EventAction.SPEAK,
    )
    failed = replace(
        _record("01960000-0000-7000-8000-000000000013", minutes=4),
        status=EventStatus.FAILED,
        failure_reason="worker exploded",
    )
    for record in (note, no_note, spoken, failed):
        assert isinstance(store.insert_event(record), Ok)

    timeline = store.list_timeline("default", limit=50)
    assert isinstance(timeline, Ok)
    items = timeline.value
    assert len(items) == 5
    assert [item.item_id for item in items] == sorted(item.item_id for item in items)
    assert spoken.event_id not in {item.item_id for item in items}
    by_id = {item.item_id: item for item in items}
    assert by_id[note.event_id].content == "ambient note"
    assert by_id[no_note.event_id].content == "email.received"
    assert by_id[failed.event_id].kind is TimelineKind.EVENT_FAILED
    assert by_id[failed.event_id].content == "worker exploded"
    assert by_id[failed.event_id].origin is None
    assert by_id[appended.value[0].message_id].origin is MessageOrigin.USER
    assert by_id[appended.value[0].message_id].kind is TimelineKind.TURN

    limited = store.list_timeline("default", limit=2)
    assert limited == Ok(tuple(items[-2:]))
    after = store.list_timeline("default", after_id=items[1].item_id)
    assert after == Ok(tuple(items[2:]))
    after_missing = store.list_timeline(
        "default", after_id="ffffffff-ffff-7fff-8fff-ffffffffffff"
    )
    assert after_missing == Ok(())
    assert store.list_timeline("other") == Ok(())


def test_store_upsert_conversations_and_event_collisions(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db", tmp_path / "core.md")
    ensured = store.ensure_conversation("default", _t(0))
    again = store.ensure_conversation("default", _t(1))
    assert isinstance(ensured, Ok)
    assert ensured.value.epoch == 0
    assert again == ensured

    bumped = store.bump_epoch("default", _t(2))
    bumped_again = store.bump_epoch("default", _t(3))
    assert isinstance(bumped, Ok)
    assert isinstance(bumped_again, Ok)
    assert bumped.value.epoch == 1
    assert bumped_again.value.epoch == 2
    assert bumped_again.value.last_user_activity_at == _t(3)

    assert isinstance(store.insert_event(_record("evt_1", key="k1")), Ok)
    collision = store.insert_event(_record("evt_1", key="k2"))
    assert_err(
        collision,
        kind=AppErrorKind.STORAGE,
        message_contains="UNIQUE constraint failed: events.event_id",
    )


def test_store_insert_event_not_found_and_missing_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = Store(db_path, tmp_path / "core.md")
    assert isinstance(store.ensure_conversation("default", _t(0)), Ok)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "CREATE TRIGGER skip_events BEFORE INSERT ON events "
            "BEGIN SELECT RAISE(IGNORE); END"
        )
        conn.commit()
    assert_err(
        store.insert_event(_record("evt_ignored")),
        kind=AppErrorKind.NOT_FOUND,
        message_contains="Event not found after insert: evt_ignored",
    )

    missing = store.finalize_event(
        "default",
        "evt_missing",
        EventDecision(action=EventAction.SPEAK, message="hello", reason="r"),
        received_epoch=0,
        now=_t(1),
        policy=ProactivePolicy(5, 0, 0, 0),
    )
    assert_err(missing, kind=AppErrorKind.NOT_FOUND, message_contains="evt_missing")
    assert store.counts("default") == Ok(ConversationCounts(messages=0, events=0))
    state = store.get_state("default")
    assert isinstance(state, Ok)
    assert state.value.proactive_count_today == 0

    assert_err(
        store.fail_event("default", "evt_missing", "boom"),
        kind=AppErrorKind.NOT_FOUND,
        message_contains="evt_missing",
    )


def test_store_conversation_upserts_report_missing_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = Store(db_path, tmp_path / "core.md")
    assert isinstance(store.ensure_conversation("seed", _t(0)), Ok)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("DELETE FROM conversations")
        conn.execute(
            "CREATE TRIGGER skip_conversations BEFORE INSERT ON conversations "
            "BEGIN SELECT RAISE(IGNORE); END"
        )
        conn.commit()
    assert_err(
        store.ensure_conversation("default", _t(0)),
        kind=AppErrorKind.NOT_FOUND,
        message_contains="Conversation not found: default",
    )
    assert_err(
        store.bump_epoch("default", _t(0)),
        kind=AppErrorKind.NOT_FOUND,
        message_contains="Conversation not found: default",
    )


def test_store_finalize_and_fail_event_guard_conversation(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db", tmp_path / "core.md")
    assert isinstance(store.insert_event(_record("evt_1")), Ok)
    missing = store.finalize_event(
        "other",
        "evt_1",
        EventDecision(action=EventAction.NOTE, note="n", reason="r"),
        received_epoch=0,
        now=_t(1),
        policy=ProactivePolicy(5, 0, 0, 0),
    )
    assert_err(missing, kind=AppErrorKind.NOT_FOUND, message_contains="evt_1")
    assert_err(
        store.fail_event("other", "evt_1", "boom"),
        kind=AppErrorKind.NOT_FOUND,
        message_contains="evt_1",
    )


def test_store_memory_preserves_custom_sections_and_preamble(tmp_path: Path) -> None:
    mem_path = tmp_path / "core.md"
    store = Store(tmp_path / "state.db", mem_path)
    mem_path.write_text(
        "intro text\n"
        "## user\nold user\n"
        "## custom\ncustom body\nline 2\n"
        "## relationship\nold rel\n"
        "## arc\nold arc\n"
        "## trailing\ntail\n",
        encoding="utf-8",
    )
    assert isinstance(
        store.update_memory(lambda blocks: Ok(replace(blocks, user="new user"))), Ok
    )
    rendered = mem_path.read_text(encoding="utf-8")
    assert "intro text" in rendered
    assert "## user\nnew user" in rendered
    assert "## custom\ncustom body\nline 2" in rendered
    assert "## relationship\nold rel" in rendered
    assert "## arc\nold arc" in rendered
    assert "## trailing\ntail" in rendered
    assert rendered.index("## custom") < rendered.index("## relationship")
    assert store.read_memory() == Ok(
        MemoryBlocks(user="new user", relationship="old rel", arc="old arc")
    )

    mem_path.write_text("# preamble only\n## custom\nkeep\n", encoding="utf-8")
    assert isinstance(
        store.update_memory(lambda blocks: Ok(replace(blocks, arc="new arc"))), Ok
    )
    rendered = mem_path.read_text(encoding="utf-8")
    assert "# preamble only" in rendered
    assert "## custom\nkeep" in rendered
    assert "## arc\nnew arc" in rendered
    assert "## user\n" in rendered
    assert "## relationship\n" in rendered


def test_store_creates_pending_notes_index_and_uses_it_in_query_plan(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    store = Store(db_path, tmp_path / "core.md")
    assert isinstance(store.ensure_conversation("default", _t(0)), Ok)
    with closing(sqlite3.connect(db_path)) as conn:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT * FROM events "
            "WHERE conversation_id = ? AND action = 'note' AND surfaced_at IS NULL "
            "ORDER BY occurred_at DESC, event_id DESC LIMIT ?",
            ("default", 5),
        ).fetchall()
    assert {"events_pending_notes", "events_received"} <= indexes
    assert any("events_pending_notes" in str(row[-1]) for row in plan), plan


def test_store_mark_surfaced_scopes_updates_to_conversation(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db", tmp_path / "core.md")
    assert isinstance(
        store.insert_event(
            replace(
                _record("evt_a", conversation_id="conv_a", minutes=1),
                status=EventStatus.COMPLETED,
                action=EventAction.NOTE,
                note="note a",
            )
        ),
        Ok,
    )
    assert isinstance(
        store.insert_event(
            replace(
                _record("evt_b", conversation_id="conv_b", minutes=2),
                status=EventStatus.COMPLETED,
                action=EventAction.NOTE,
                note="note b",
            )
        ),
        Ok,
    )

    assert isinstance(store.mark_surfaced("conv_a", ("evt_a", "evt_b"), _t(9)), Ok)

    marked = store.get_event_in("conv_a", "evt_a")
    untouched = store.get_event_in("conv_b", "evt_b")
    pending_b = store.list_pending_notes("conv_b")
    assert isinstance(marked, Ok)
    assert marked.value is not None
    assert marked.value.surfaced_at == _t(9)
    assert isinstance(untouched, Ok)
    assert untouched.value is not None
    assert untouched.value.surfaced_at is None
    assert isinstance(pending_b, Ok)
    assert ([event.event_id for event in pending_b.value[0]], pending_b.value[1]) == (
        ["evt_b"],
        1,
    )


def test_store_list_received_events_returns_pending_envelopes_with_epochs(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db", tmp_path / "core.md")
    assert isinstance(store.bump_epoch("conv_a", _t(0)), Ok)
    assert isinstance(store.bump_epoch("conv_a", _t(0)), Ok)
    assert isinstance(store.ensure_conversation("conv_b", _t(0)), Ok)

    assert isinstance(
        store.insert_event(_record("evt_2", conversation_id="conv_b", minutes=5)), Ok
    )
    assert isinstance(
        store.insert_event(_record("evt_1", conversation_id="conv_a", minutes=1)), Ok
    )
    assert isinstance(
        store.insert_event(
            replace(
                _record("evt_done", conversation_id="conv_a", minutes=2),
                status=EventStatus.COMPLETED,
                action=EventAction.NOTE,
            )
        ),
        Ok,
    )
    assert isinstance(
        store.insert_event(
            replace(
                _record("evt_failed", conversation_id="conv_b", minutes=3),
                status=EventStatus.FAILED,
                failure_reason="boom",
            )
        ),
        Ok,
    )

    listed = store.list_received_events()
    assert isinstance(listed, Ok)
    assert len(listed.value) == 2
    first, second = listed.value
    assert isinstance(first, PendingEvent)
    assert (first.envelope.event_id, first.envelope.conversation_id, first.epoch) == (
        "evt_1",
        "conv_a",
        2,
    )
    assert first.envelope.occurred_at == _t(1)
    assert first.envelope.payload_json == '{"subject":"hello"}'
    assert (
        second.envelope.event_id,
        second.envelope.conversation_id,
        second.epoch,
    ) == ("evt_2", "conv_b", 0)


def test_store_finalize_event_daily_cap_resets_at_timezone_midnight(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.db", tmp_path / "core.md")
    tokyo = ZoneInfo("Asia/Tokyo")
    policy = ProactivePolicy(1, 0, 0, 0, tz=tokyo)
    speak = EventDecision(
        action=EventAction.SPEAK, message="通知です。", note="n", reason="alert"
    )
    before_midnight = datetime(2026, 5, 1, 14, 59, tzinfo=UTC)
    same_jst_day = datetime(2026, 5, 1, 14, 59, 30, tzinfo=UTC)
    after_midnight = datetime(2026, 5, 1, 15, 0, tzinfo=UTC)

    for event_id in ("evt_1", "evt_2", "evt_3"):
        assert isinstance(store.insert_event(_record(event_id)), Ok)

    first = store.finalize_event(
        "default", "evt_1", speak, received_epoch=0, now=before_midnight, policy=policy
    )
    assert isinstance(first, Ok)
    assert first.value.action is EventAction.SPEAK
    state_before = store.get_state("default")
    assert isinstance(state_before, Ok)
    assert (
        state_before.value.proactive_day,
        state_before.value.proactive_count_today,
    ) == ("2026-05-01", 1)

    capped = store.finalize_event(
        "default", "evt_2", speak, received_epoch=0, now=same_jst_day, policy=policy
    )
    assert isinstance(capped, Ok)
    assert (capped.value.action, capped.value.reason) == (
        EventAction.NOTE,
        "daily_cap",
    )

    reset = store.finalize_event(
        "default", "evt_3", speak, received_epoch=0, now=after_midnight, policy=policy
    )
    assert isinstance(reset, Ok)
    assert reset.value.action is EventAction.SPEAK
    state_after = store.get_state("default")
    assert isinstance(state_after, Ok)
    assert (
        state_after.value.proactive_day,
        state_after.value.proactive_count_today,
    ) == ("2026-05-02", 1)
