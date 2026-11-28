import fcntl
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable, Generator, Sequence
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid7

from flow_res import Err, Ok, Result
from pydantic import TypeAdapter

from my_agents.core.context import MAX_CONTEXT_EVENTS
from my_agents.core.events import (
    apply_policy,
    sanitize_single_line,
    sanitize_untrusted,
)
from my_agents.core.memory import format_prompt_date
from my_agents.core.types import (
    AppError,
    AppErrorKind,
    ChatMessage,
    ConversationCounts,
    ConversationState,
    ConversationSummary,
    EventAction,
    EventDecision,
    EventEnvelope,
    EventRecord,
    EventStatus,
    MemoryBlock,
    MemoryBlocks,
    MessageOrigin,
    ProactivePolicy,
    Role,
    TimelineItem,
)
from my_agents.shell.push import PushSubscription

type MessageEntry = tuple[Role, MessageOrigin, str, str | None]
CURRENT_SCHEMA_VERSION = 5


@dataclass(frozen=True, slots=True)
class EventCommit:
    action: EventAction
    reason: str
    note: str
    record: EventRecord
    message: ChatMessage | None = None


@dataclass(frozen=True, slots=True)
class PendingEvent:
    envelope: EventEnvelope
    epoch: int


_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS conversations (
        conversation_id TEXT PRIMARY KEY,
        epoch INTEGER NOT NULL DEFAULT 0,
        last_user_activity_at TEXT,
        proactive_count_today INTEGER NOT NULL DEFAULT 0,
        proactive_day TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        message_id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        origin TEXT NOT NULL,
        content TEXT NOT NULL,
        source_event_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS messages_recency
    ON messages(conversation_id, message_id DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
        idempotency_key TEXT NOT NULL,
        name TEXT NOT NULL,
        source TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        received_at TEXT NOT NULL,
        status TEXT NOT NULL,
        action TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL,
        failure_reason TEXT NOT NULL DEFAULT '',
        surfaced_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS events_idempotency
    ON events(conversation_id, idempotency_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS events_recency
    ON events(conversation_id, received_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS events_pending_notes
    ON events(conversation_id, action, surfaced_at, occurred_at DESC, event_id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS events_received
    ON events(received_at ASC, event_id ASC)
    WHERE status = 'received'
    """,
    """
    CREATE TABLE IF NOT EXISTS push_subscriptions (
        endpoint TEXT PRIMARY KEY,
        p256dh TEXT NOT NULL,
        auth TEXT NOT NULL,
        conversation_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _storage(message: str) -> AppError:
    return AppError(AppErrorKind.STORAGE, message)


_STATE = TypeAdapter(ConversationState)
_MESSAGE = TypeAdapter(ChatMessage)
_EVENT = TypeAdapter(EventRecord)
_PENDING = TypeAdapter(PendingEvent)
_SUMMARY = TypeAdapter(ConversationSummary)
_PUSH = TypeAdapter(PushSubscription)
_TIMELINE = TypeAdapter(TimelineItem)


# PL019 bans exception subclasses in src, so the internal not-found signal is an alias.
_NotFoundError = LookupError


def _state(row: sqlite3.Row) -> ConversationState:
    return _STATE.validate_python(dict(row))


def _message(row: sqlite3.Row) -> ChatMessage:
    return _MESSAGE.validate_python(dict(row))


def _event(row: sqlite3.Row) -> EventRecord:
    return _EVENT.validate_python(dict(row))


def _pending_event(row: sqlite3.Row) -> PendingEvent:
    data = dict(row)
    epoch = data.pop("epoch")
    return _PENDING.validate_python({"envelope": data, "epoch": epoch})


def _conversation_summary(row: sqlite3.Row) -> ConversationSummary:
    summary = _SUMMARY.validate_python(dict(row))
    return replace(
        summary,
        last_message_preview=sanitize_single_line(summary.last_message_preview)[:120],
    )


def _push_subscription(row: sqlite3.Row) -> PushSubscription:
    return _PUSH.validate_python(dict(row))


def _timeline_item(row: sqlite3.Row) -> TimelineItem:
    return _TIMELINE.validate_python(dict(row))


def _check_version(version: int, tables: int) -> None:
    if version == CURRENT_SCHEMA_VERSION:
        return
    if version == 0 and tables == 0:
        return
    raise ValueError(
        f"Incompatible database schema version {version} (expected "
        f"{CURRENT_SCHEMA_VERSION}). Remove the old database or choose a fresh DATA_DIR."
    )


def _require_supported_version(connection: sqlite3.Connection) -> None:
    # One statement keeps user_version and sqlite_master in one snapshot: a
    # concurrent first-time commit must not look like v0 with tables present.
    row = connection.execute(
        "SELECT (SELECT user_version FROM pragma_user_version), "
        "(SELECT COUNT(*) FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%')"
    ).fetchone()
    _check_version(int(row[0]), int(row[1]))


class Store:
    def __init__(
        self,
        db_path: Path,
        memory_path: Path,
        *,
        schema_statements: Sequence[str] = _SCHEMA_STATEMENTS,
    ) -> None:
        self._db = db_path
        self._memory = memory_path
        self._schema_statements = tuple(schema_statements)
        self._init_error: AppError | None = None
        if db_path.exists():
            self._init_error = self._prepare()

    def _prepare(self) -> AppError | None:
        checked = self._read(lambda _connection: None, empty=None)
        return checked.error if isinstance(checked, Err) else None

    def _open(self, *, readonly: bool) -> sqlite3.Connection:
        if readonly:
            connection = sqlite3.connect(
                f"{self._db.absolute().as_uri()}?mode=ro", uri=True
            )
        else:
            self._db.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._db)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            if readonly:
                _require_supported_version(connection)
            else:
                with suppress(sqlite3.OperationalError):
                    connection.execute("PRAGMA journal_mode = WAL")
                self._initialize(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def _initialize(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT (SELECT user_version FROM pragma_user_version), "
                "(SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%')"
            ).fetchone()
            version = int(row[0])
            _check_version(version, int(row[1]))
            if version != CURRENT_SCHEMA_VERSION:
                for statement in self._schema_statements:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
            connection.commit()
        except BaseException:
            with suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise

    def _read[T](
        self, operation: Callable[[sqlite3.Connection], T], *, empty: T
    ) -> Result[T, AppError]:
        if self._init_error is not None:
            return Err(self._init_error)
        if not self._db.exists():
            return Ok(empty)
        try:
            with closing(self._open(readonly=True)) as connection:
                return Ok(operation(connection))
        except (sqlite3.Error, OSError, ValueError) as exc:
            return Err(_storage(str(exc)))

    def _write[T](
        self, operation: Callable[[sqlite3.Connection], T]
    ) -> Result[T, AppError]:
        if self._init_error is not None:
            return Err(self._init_error)
        try:
            with closing(self._open(readonly=False)) as connection, connection:
                return Ok(operation(connection))
        except _NotFoundError as exc:
            return Err(AppError(AppErrorKind.NOT_FOUND, str(exc)))
        except (sqlite3.Error, OSError, ValueError) as exc:
            return Err(_storage(str(exc)))

    def _ensure_conversation(
        self, connection: sqlite3.Connection, conversation_id: str, now: datetime
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO conversations (conversation_id, created_at) "
            "VALUES (?, ?)",
            (conversation_id, _stamp(now)),
        )

    def _state_or_default(
        self, connection: sqlite3.Connection, conversation_id: str
    ) -> ConversationState:
        row = connection.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return ConversationState(conversation_id) if row is None else _state(row)

    def ensure_conversation(
        self, conversation_id: str, now: datetime
    ) -> Result[ConversationState, AppError]:
        def run(connection: sqlite3.Connection) -> ConversationState:
            row = connection.execute(
                "INSERT INTO conversations (conversation_id, created_at) "
                "VALUES (?, ?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET "
                "conversation_id = conversations.conversation_id "
                "RETURNING *",
                (conversation_id, _stamp(now)),
            ).fetchone()
            if row is None:
                raise _NotFoundError(f"Conversation not found: {conversation_id}")
            return _state(row)

        return self._write(run)

    def get_state(self, conversation_id: str) -> Result[ConversationState, AppError]:
        return self._read(
            lambda connection: self._state_or_default(connection, conversation_id),
            empty=ConversationState(conversation_id),
        )

    def bump_epoch(
        self, conversation_id: str, now: datetime
    ) -> Result[ConversationState, AppError]:
        def run(connection: sqlite3.Connection) -> ConversationState:
            row = connection.execute(
                "INSERT INTO conversations "
                "(conversation_id, epoch, last_user_activity_at, created_at) "
                "VALUES (?, 1, ?, ?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET "
                "epoch = conversations.epoch + 1, "
                "last_user_activity_at = excluded.last_user_activity_at "
                "RETURNING *",
                (conversation_id, _stamp(now), _stamp(now)),
            ).fetchone()
            if row is None:
                raise _NotFoundError(f"Conversation not found: {conversation_id}")
            return _state(row)

        return self._write(run)

    def _insert_message(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        entry: MessageEntry,
        now: datetime,
    ) -> ChatMessage:
        role, origin, content, source_event_id = entry
        message_id = str(uuid7())
        connection.execute(
            "INSERT INTO messages "
            "(message_id, conversation_id, role, origin, content, source_event_id, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                conversation_id,
                role,
                origin.value,
                content,
                source_event_id,
                _stamp(now),
            ),
        )
        return ChatMessage(
            message_id=message_id,
            conversation_id=conversation_id,
            role=role,
            origin=origin,
            content=content,
            source_event_id=source_event_id,
            created_at=now.astimezone(UTC),
        )

    def append_messages(
        self,
        conversation_id: str,
        entries: Sequence[MessageEntry],
        now: datetime,
    ) -> Result[tuple[ChatMessage, ...], AppError]:
        def run(connection: sqlite3.Connection) -> tuple[ChatMessage, ...]:
            self._ensure_conversation(connection, conversation_id, now)
            return tuple(
                self._insert_message(connection, conversation_id, entry, now)
                for entry in entries
            )

        return self._write(run)

    def recent_messages(
        self, conversation_id: str, *, limit: int = 30
    ) -> Result[tuple[ChatMessage, ...], AppError]:
        if limit <= 0:
            return Ok(())

        def run(connection: sqlite3.Connection) -> tuple[ChatMessage, ...]:
            rows = connection.execute(
                "SELECT * FROM messages WHERE conversation_id = ? "
                "ORDER BY message_id DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
            return tuple(_message(row) for row in reversed(rows))

        return self._read(run, empty=())

    def insert_event(self, record: EventRecord) -> Result[EventRecord, AppError]:
        def run(connection: sqlite3.Connection) -> EventRecord:
            self._ensure_conversation(
                connection, record.conversation_id, record.received_at
            )
            row = connection.execute(
                "INSERT INTO events "
                "(event_id, conversation_id, idempotency_key, name, source, "
                "occurred_at, received_at, status, action, reason, note, "
                "payload_json, failure_reason, surfaced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(conversation_id, idempotency_key) DO NOTHING "
                "RETURNING *",
                (
                    record.event_id,
                    record.conversation_id,
                    record.idempotency_key,
                    record.name,
                    record.source,
                    _stamp(record.occurred_at),
                    _stamp(record.received_at),
                    record.status.value,
                    record.action.value,
                    record.reason,
                    record.note,
                    record.payload_json,
                    record.failure_reason,
                    None if record.surfaced_at is None else _stamp(record.surfaced_at),
                ),
            ).fetchone()
            if row is not None:
                return _event(row)
            row = connection.execute(
                "SELECT * FROM events WHERE conversation_id = ? AND idempotency_key = ?",
                (record.conversation_id, record.idempotency_key),
            ).fetchone()
            if row is None:
                raise _NotFoundError(
                    f"Event not found after insert: {record.idempotency_key}"
                )
            return _event(row)

        return self._write(run)

    def finalize_event(
        self,
        conversation_id: str,
        event_id: str,
        decision: EventDecision,
        *,
        received_epoch: int,
        now: datetime,
        policy: ProactivePolicy,
    ) -> Result[EventCommit, AppError]:
        def run(connection: sqlite3.Connection) -> EventCommit:
            state = self._state_or_default(connection, conversation_id)
            outcome = apply_policy(
                decision,
                state,
                now,
                policy,
                epoch_changed=(state.epoch != received_epoch),
            )
            reason = sanitize_single_line(outcome.reason)[:160] or "evaluated"
            note = sanitize_single_line(decision.note)[:240]
            final_note = note
            surfaced_at: str | None = None
            spoken = ""
            if outcome.action is EventAction.SPEAK:
                spoken = sanitize_untrusted(decision.message).strip()
                final_note = note or sanitize_single_line(spoken)[:240]
                surfaced_at = _stamp(now)
            row = connection.execute(
                "UPDATE events SET status = ?, action = ?, reason = ?, note = ?, "
                "failure_reason = '', surfaced_at = ? "
                "WHERE event_id = ? AND conversation_id = ? RETURNING *",
                (
                    EventStatus.COMPLETED.value,
                    outcome.action.value,
                    reason,
                    final_note,
                    surfaced_at,
                    event_id,
                    conversation_id,
                ),
            ).fetchone()
            if row is None:
                raise _NotFoundError(f"Event not found: {event_id}")
            inserted: ChatMessage | None = None
            if outcome.action is EventAction.SPEAK:
                inserted = self._insert_message(
                    connection,
                    conversation_id,
                    ("assistant", MessageOrigin.PROACTIVE, spoken, event_id),
                    now,
                )
                today = format_prompt_date(now, policy.tz)
                count = (
                    1
                    if state.proactive_day != today
                    else state.proactive_count_today + 1
                )
                connection.execute(
                    "UPDATE conversations SET proactive_count_today = ?, "
                    "proactive_day = ? WHERE conversation_id = ?",
                    (count, today, conversation_id),
                )
            return EventCommit(
                outcome.action, reason, final_note, _event(row), inserted
            )

        return self._write(run)

    def fail_event(
        self, conversation_id: str, event_id: str, failure_reason: str
    ) -> Result[EventRecord, AppError]:
        def run(connection: sqlite3.Connection) -> EventRecord:
            row = connection.execute(
                "UPDATE events SET status = ?, action = ?, reason = 'worker_failed', "
                "failure_reason = ?, surfaced_at = NULL "
                "WHERE event_id = ? AND conversation_id = ? RETURNING *",
                (
                    EventStatus.FAILED.value,
                    EventAction.IGNORE.value,
                    failure_reason,
                    event_id,
                    conversation_id,
                ),
            ).fetchone()
            if row is None:
                raise _NotFoundError(f"Event not found: {event_id}")
            return _event(row)

        return self._write(run)

    def _event_or_none(
        self,
        connection: sqlite3.Connection,
        where: str,
        parameters: tuple[object, ...],
    ) -> EventRecord | None:
        row = connection.execute(
            f"SELECT * FROM events WHERE {where}", parameters
        ).fetchone()
        return None if row is None else _event(row)

    def get_event(self, event_id: str) -> Result[EventRecord | None, AppError]:
        return self._read(
            lambda connection: self._event_or_none(
                connection, "event_id = ?", (event_id,)
            ),
            empty=None,
        )

    def get_event_in(
        self, conversation_id: str, event_id: str
    ) -> Result[EventRecord | None, AppError]:
        return self._read(
            lambda connection: self._event_or_none(
                connection,
                "conversation_id = ? AND event_id = ?",
                (conversation_id, event_id),
            ),
            empty=None,
        )

    def list_received_events(self) -> Result[tuple[PendingEvent, ...], AppError]:
        def run(connection: sqlite3.Connection) -> tuple[PendingEvent, ...]:
            rows = connection.execute(
                "SELECT e.event_id, e.conversation_id, e.name, e.source, "
                "e.occurred_at, e.idempotency_key, e.payload_json, c.epoch "
                "FROM events e "
                "JOIN conversations c ON c.conversation_id = e.conversation_id "
                "WHERE e.status = ? "
                "ORDER BY e.received_at ASC, e.event_id ASC",
                (EventStatus.RECEIVED.value,),
            ).fetchall()
            return tuple(_pending_event(row) for row in rows)

        return self._read(run, empty=())

    def list_pending_notes(
        self, conversation_id: str, *, limit: int = MAX_CONTEXT_EVENTS
    ) -> Result[tuple[tuple[EventRecord, ...], int], AppError]:
        def run(
            connection: sqlite3.Connection,
        ) -> tuple[tuple[EventRecord, ...], int]:
            total = connection.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE conversation_id = ? AND action = 'note' "
                "AND surfaced_at IS NULL",
                (conversation_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT * FROM events "
                "WHERE conversation_id = ? AND action = 'note' "
                "AND surfaced_at IS NULL "
                "ORDER BY occurred_at DESC, event_id DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
            return tuple(_event(row) for row in rows), int(total[0])

        return self._read(run, empty=((), 0))

    def list_timeline(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
        after_id: str | None = None,
    ) -> Result[tuple[TimelineItem, ...], AppError]:
        if limit <= 0:
            return Ok(())

        timeline = (
            "SELECT 'turn' AS kind, conversation_id, content, created_at, "
            "message_id AS item_id, source_event_id AS event_id, origin "
            "FROM messages WHERE conversation_id = ? "
            "UNION ALL "
            "SELECT CASE WHEN status = 'failed' THEN 'event_failed' "
            "ELSE 'event' END AS kind, conversation_id, "
            "CASE WHEN status = 'failed' THEN failure_reason "
            "WHEN note != '' THEN note ELSE name END AS content, "
            "received_at AS created_at, event_id AS item_id, event_id, "
            "NULL AS origin FROM events WHERE conversation_id = ? "
            "AND NOT (status = 'completed' AND action = 'speak')"
        )

        def run(connection: sqlite3.Connection) -> tuple[TimelineItem, ...]:
            if after_id is None:
                rows = connection.execute(
                    f"SELECT * FROM ({timeline}) ORDER BY item_id DESC LIMIT ?",
                    (conversation_id, conversation_id, limit),
                ).fetchall()
                return tuple(_timeline_item(row) for row in reversed(rows))
            rows = connection.execute(
                f"SELECT * FROM ({timeline}) WHERE item_id > ? "
                "ORDER BY item_id ASC LIMIT ?",
                (conversation_id, conversation_id, after_id, limit),
            ).fetchall()
            return tuple(_timeline_item(row) for row in rows)

        return self._read(run, empty=())

    def mark_surfaced(
        self, conversation_id: str, event_ids: Sequence[str], now: datetime
    ) -> Result[None, AppError]:
        if not event_ids:
            return Ok(None)

        def run(connection: sqlite3.Connection) -> None:
            markers = ", ".join("?" for _ in event_ids)
            connection.execute(
                f"UPDATE events SET surfaced_at = ? "
                f"WHERE conversation_id = ? AND event_id IN ({markers})",
                (_stamp(now), conversation_id, *event_ids),
            )

        return self._write(run)

    def counts(self, conversation_id: str) -> Result[ConversationCounts, AppError]:
        def run(connection: sqlite3.Connection) -> ConversationCounts:
            row = connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM messages WHERE conversation_id = ?), "
                "(SELECT COUNT(*) FROM events WHERE conversation_id = ?)",
                (conversation_id, conversation_id),
            ).fetchone()
            return ConversationCounts(int(row[0]), int(row[1]))

        return self._read(run, empty=ConversationCounts())

    def list_conversations(
        self, *, limit: int = 100
    ) -> Result[tuple[ConversationSummary, ...], AppError]:
        if limit <= 0:
            return Ok(())

        def run(connection: sqlite3.Connection) -> tuple[ConversationSummary, ...]:
            rows = connection.execute(
                "SELECT c.conversation_id, c.epoch, c.created_at, "
                "MAX(c.created_at, "
                "COALESCE((SELECT MAX(m.created_at) FROM messages m "
                "WHERE m.conversation_id = c.conversation_id), c.created_at), "
                "COALESCE((SELECT MAX(e.received_at) FROM events e "
                "WHERE e.conversation_id = c.conversation_id), c.created_at)) "
                "AS updated_at, "
                "(SELECT COUNT(*) FROM messages m "
                "WHERE m.conversation_id = c.conversation_id) AS message_count, "
                "(SELECT COUNT(*) FROM events e "
                "WHERE e.conversation_id = c.conversation_id) AS event_count, "
                "COALESCE(lm.content, '') AS last_message_preview, "
                "lm.origin AS last_message_origin "
                "FROM conversations c "
                "LEFT JOIN messages lm ON lm.message_id = "
                "(SELECT MAX(m.message_id) FROM messages m "
                "WHERE m.conversation_id = c.conversation_id) "
                "ORDER BY updated_at DESC, c.conversation_id ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return tuple(_conversation_summary(row) for row in rows)

        return self._read(run, empty=())

    def upsert_push_subscription(
        self, subscription: PushSubscription
    ) -> Result[None, AppError]:
        def run(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO push_subscriptions "
                "(endpoint, p256dh, auth, conversation_id, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, "
                "auth = excluded.auth, conversation_id = excluded.conversation_id, "
                "created_at = excluded.created_at",
                (
                    subscription.endpoint,
                    subscription.p256dh,
                    subscription.auth.get_secret_value(),
                    subscription.conversation_id,
                    _stamp(subscription.created_at),
                ),
            )

        return self._write(run)

    def delete_push_subscription(self, endpoint: str) -> Result[bool, AppError]:
        def run(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
            )
            return cursor.rowcount > 0

        return self._write(run)

    def list_push_subscriptions(
        self, *, conversation_id: str | None = None
    ) -> Result[tuple[PushSubscription, ...], AppError]:
        def run(connection: sqlite3.Connection) -> tuple[PushSubscription, ...]:
            if conversation_id is None:
                rows = connection.execute(
                    "SELECT * FROM push_subscriptions "
                    "ORDER BY created_at ASC, endpoint ASC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM push_subscriptions "
                    "WHERE conversation_id IS NULL OR conversation_id = ? "
                    "ORDER BY created_at ASC, endpoint ASC",
                    (conversation_id,),
                ).fetchall()
            return tuple(_push_subscription(row) for row in rows)

        return self._read(run, empty=())

    def reset_conversation(self, conversation_id: str) -> Result[int, AppError]:
        def run(connection: sqlite3.Connection) -> int:
            deleted = connection.execute(
                "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,)
            ).rowcount
            connection.execute(
                "DELETE FROM events WHERE conversation_id = ?", (conversation_id,)
            )
            connection.execute(
                "UPDATE conversations SET epoch = epoch + 1, "
                "last_user_activity_at = NULL, proactive_count_today = 0, "
                "proactive_day = '' WHERE conversation_id = ?",
                (conversation_id,),
            )
            return deleted

        return self._write(run)

    def update_memory(
        self, transform: Callable[[MemoryBlocks], Result[MemoryBlocks, AppError]]
    ) -> Result[None, AppError]:
        try:
            with self._memory_lock():
                memory = self.read_memory()
                if isinstance(memory, Err):
                    return memory
                updated = transform(memory.value)
                if isinstance(updated, Err):
                    return updated
                self._write_memory_unlocked(updated.value)
        except (OSError, UnicodeError) as exc:
            return Err(_storage(str(exc)))
        return Ok(None)

    @contextmanager
    def _memory_lock(self) -> Generator[None]:
        self._memory.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._memory.with_name(f"{self._memory.name}.lock")
        handle = os.open(lock_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
        except BaseException:
            os.close(handle)
            raise
        try:
            yield
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                os.close(handle)

    def _write_memory_unlocked(self, blocks: MemoryBlocks) -> None:
        temporary: Path | None = None
        try:
            try:
                existing = self._memory.read_text(encoding="utf-8")
            except FileNotFoundError:
                existing = ""
            self._memory.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self._memory.parent, delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(_render_memory(blocks, existing))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self._memory)
        except OSError, UnicodeError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise

    def read_memory(self) -> Result[MemoryBlocks, AppError]:
        try:
            text = self._memory.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Ok(MemoryBlocks())
        except (OSError, UnicodeError) as exc:
            return Err(_storage(str(exc)))
        return Ok(_parse_memory(text))


def _parse_memory(text: str) -> MemoryBlocks:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return MemoryBlocks(
        "\n".join(sections.get("user", [])).strip(),
        "\n".join(sections.get("relationship", [])).strip(),
        "\n".join(sections.get("arc", [])).strip(),
    )


_SECTION = re.compile(r"(?m)^## ")


def _split_memory(text: str) -> tuple[str, list[tuple[str, str]]]:
    matches = list(_SECTION.finditer(text))
    if not matches:
        return text, []
    preamble = text[: matches[0].start()]
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        raw = text[match.start() : end]
        heading = raw.split("\n", 1)[0]
        sections.append((heading[3:].strip(), raw))
    return preamble, sections


def _render_memory(blocks: MemoryBlocks, existing: str) -> str:
    preamble, sections = _split_memory(existing)
    known = {block.value: block for block in MemoryBlock}
    rendered: list[str] = []
    seen: set[str] = set()
    for key, raw in sections:
        block = known.get(key)
        if block is None:
            rendered.append(raw.rstrip("\n"))
            continue
        seen.add(key)
        rendered.append(f"## {key}\n{blocks.value(block)}")
    for block in MemoryBlock:
        if block.value not in seen:
            rendered.append(f"## {block.value}\n{blocks.value(block)}")
    pieces = [piece for piece in (preamble.rstrip("\n"), *rendered) if piece]
    return "\n\n".join(pieces) + "\n"
