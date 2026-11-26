import errno
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from flow_res import Err, Ok

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
)
from my_agents.shell.store import CURRENT_SCHEMA_VERSION, Store
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
        linked_message_id=None,
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
    events = store.list_events("default", limit=10)
    event = store.get_event("evt_1")
    event_in = store.get_event_in("default", "evt_1")
    counts = store.counts("default")
    memory = store.read_memory()

    assert isinstance(state, Ok)
    assert state.value.epoch == 0
    assert isinstance(msgs, Ok)
    assert msgs.value == ()
    assert isinstance(events, Ok)
    assert events.value == ()
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

    assert isinstance(store.mark_surfaced((), _t(9)), Ok)
    assert isinstance(store.mark_surfaced(("evt_note",), _t(9)), Ok)
    note_row = store.get_event("evt_note")
    assert isinstance(note_row, Ok)
    assert note_row.value is not None
    assert note_row.value.surfaced_at == _t(9)

    assert isinstance(store.insert_event(_record("evt_fail", minutes=10)), Ok)
    assert isinstance(store.fail_event("evt_fail", "upstream timeout"), Ok)
    failed = store.get_event("evt_fail")
    assert isinstance(failed, Ok)
    assert failed.value is not None
    assert (failed.value.status, failed.value.failure_reason) == (
        EventStatus.FAILED,
        "upstream timeout",
    )

    zero_events = store.list_events("default", limit=0)
    listed = store.list_events("default", limit=10)
    assert isinstance(zero_events, Ok)
    assert zero_events.value == ()
    assert isinstance(listed, Ok)
    assert listed.value[0].event_id == "evt_fail"


def test_store_memory_lock_atomic_writes_and_error_handling(tmp_path: Path) -> None:
    mem_path = tmp_path / "core.md"
    store = Store(tmp_path / "state.db", mem_path)

    assert isinstance(
        store.write_memory(MemoryBlocks(user="tea", relationship="warm", arc="active")),
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
            store.write_memory(MemoryBlocks(user="x")),
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
            store.write_memory(MemoryBlocks(user="x")),
            kind=AppErrorKind.STORAGE,
            message_contains="disk full",
        )
        assert_err(
            store.update_memory(lambda b: Ok(b)),
            kind=AppErrorKind.STORAGE,
            message_contains="disk full",
        )
    assert not any(path.name.startswith("tmp") for path in tmp_path.iterdir())

    with patch("fcntl.flock", side_effect=OSError(errno.ENOLCK, "no locks")):
        assert_err(
            store.write_memory(MemoryBlocks(user="x")),
            kind=AppErrorKind.STORAGE,
            message_contains="no locks",
        )


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
            "INSERT INTO messages (conversation_id, role, origin, content, created_at) "
            "VALUES ('default', 'system', 'reply', 'x', '2026-05-01T00:00:00+00:00')"
        )
        conn.commit()
    assert_err(
        good_store.recent_messages("default", limit=5),
        kind=AppErrorKind.STORAGE,
        message_contains="Invalid message role",
    )

    with closing(sqlite3.connect(corrupt_db)) as conn:
        conn.execute("DELETE FROM messages")
        conn.execute(
            "INSERT INTO messages (conversation_id, role, origin, content, created_at) "
            "VALUES ('default', 'user', 'user', 'x', X'0102')"
        )
        conn.commit()
    assert_err(
        good_store.recent_messages("default", limit=5),
        kind=AppErrorKind.STORAGE,
        message_contains="Expected ISO timestamp",
    )

    with patch(
        "my_agents.shell.store._storage",
        wraps=lambda msg: AppError(AppErrorKind.STORAGE, msg),
    ):
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


def test_mid_ddl_failure_rolls_back_atomically_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    store = Store(db_path, tmp_path / "core.md")
    with monkeypatch.context() as injected:
        injected.setattr(
            "my_agents.shell.store._SCHEMA_STATEMENTS",
            ("CREATE TABLE probe (id INTEGER)", "NOT_A_STATEMENT"),
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

    recovered = store.ensure_conversation("default", _t(1))
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
    assert names == {"conversations", "messages", "events"}
    assert version == CURRENT_SCHEMA_VERSION


def test_v2_database_missing_table_returns_storage_error_without_recreating(
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
    assert tables == {"conversations", "messages"}


def test_initialized_v2_database_skips_ddl_on_normal_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    store = Store(db_path, tmp_path / "core.md")
    assert isinstance(store.ensure_conversation("default", _t()), Ok)
    with monkeypatch.context() as injected:
        injected.setattr(
            "my_agents.shell.store._SCHEMA_STATEMENTS", ("NOT_A_STATEMENT",)
        )
        appended = store.append_messages(
            "default",
            (("user", MessageOrigin.USER, "hello", None),),
            _t(1),
        )
    assert isinstance(appended, Ok)
    assert [message.content for message in appended.value] == ["hello"]
    persisted = store.recent_messages("default", limit=1)
    assert isinstance(persisted, Ok)
    assert persisted.value[0].content == "hello"


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
    assert tables == {"conversations", "messages", "events"}
