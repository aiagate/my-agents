import fcntl
import os
import sqlite3
import tempfile
from collections.abc import Callable, Generator, Sequence
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from flow_res import Err, Ok, Result

from my_agents.core.events import (
    apply_policy,
    sanitize_single_line,
    sanitize_untrusted,
)
from my_agents.core.types import (
    AppError,
    AppErrorKind,
    ChatMessage,
    ConversationCounts,
    ConversationState,
    EventAction,
    EventDecision,
    EventRecord,
    EventStatus,
    MemoryBlocks,
    MessageOrigin,
    ProactivePolicy,
    Role,
)

type MessageEntry = tuple[Role, MessageOrigin, str, str | None]
CURRENT_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class EventCommit:
    action: EventAction
    reason: str
    note: str
    message: ChatMessage | None = None


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
        message_id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        surfaced_at TEXT,
        linked_message_id INTEGER
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
)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _required_date(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Expected ISO timestamp string, got {value!r}")
    return datetime.fromisoformat(value).astimezone(UTC)


def _optional_date(value: object) -> datetime | None:
    return None if value is None else _required_date(value)


def _role(value: object) -> Role:
    match value:
        case "user":
            return "user"
        case "assistant":
            return "assistant"
        case _:
            raise ValueError(f"Invalid message role: {value!r}")


def _storage(message: str) -> AppError:
    return AppError(AppErrorKind.STORAGE, message)


def _state(row: sqlite3.Row) -> ConversationState:
    return ConversationState(
        conversation_id=str(row["conversation_id"]),
        epoch=int(row["epoch"]),
        last_user_activity_at=_optional_date(row["last_user_activity_at"]),
        proactive_count_today=int(row["proactive_count_today"]),
        proactive_day=str(row["proactive_day"]),
    )


def _message(row: sqlite3.Row) -> ChatMessage:
    return ChatMessage(
        message_id=int(row["message_id"]),
        conversation_id=str(row["conversation_id"]),
        role=_role(row["role"]),
        origin=MessageOrigin(str(row["origin"])),
        content=str(row["content"]),
        source_event_id=row["source_event_id"],
        created_at=_required_date(row["created_at"]),
    )


def _event(row: sqlite3.Row) -> EventRecord:
    return EventRecord(
        event_id=str(row["event_id"]),
        conversation_id=str(row["conversation_id"]),
        idempotency_key=str(row["idempotency_key"]),
        name=str(row["name"]),
        source=str(row["source"]),
        occurred_at=_required_date(row["occurred_at"]),
        received_at=_required_date(row["received_at"]),
        status=EventStatus(str(row["status"])),
        action=EventAction(str(row["action"])),
        reason=str(row["reason"]),
        note=str(row["note"]),
        payload_json=str(row["payload_json"]),
        failure_reason=str(row["failure_reason"]),
        surfaced_at=_optional_date(row["surfaced_at"]),
        linked_message_id=row["linked_message_id"],
    )


def _require_supported_version(connection: sqlite3.Connection) -> None:
    # One statement keeps user_version and sqlite_master in one snapshot: a
    # concurrent first-time commit must not look like v0 with tables present.
    row = connection.execute(
        "SELECT (SELECT user_version FROM pragma_user_version), "
        "(SELECT COUNT(*) FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%')"
    ).fetchone()
    version = int(row[0])
    tables = int(row[1])
    if version == CURRENT_SCHEMA_VERSION:
        return
    if version == 0 and tables == 0:
        return
    raise ValueError(
        f"Incompatible database schema version {version} (expected "
        f"{CURRENT_SCHEMA_VERSION}). Remove the old database or choose a fresh DATA_DIR."
    )


class Store:
    def __init__(self, db_path: Path, memory_path: Path) -> None:
        self._db = db_path
        self._memory = memory_path
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
            connection = sqlite3.connect(self._db, isolation_level=None)
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
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == CURRENT_SCHEMA_VERSION:
                return
            tables = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if version != 0 or tables:
                raise ValueError(
                    f"Incompatible database schema version {version} (expected "
                    f"{CURRENT_SCHEMA_VERSION}). Remove the old database or choose "
                    "a fresh DATA_DIR."
                )
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
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
            connection = self._open(readonly=False)
        except (sqlite3.Error, OSError, ValueError) as exc:
            return Err(_storage(str(exc)))
        try:
            try:
                value = operation(connection)
            except BaseException:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
            return Ok(value)
        except (sqlite3.Error, OSError, ValueError) as exc:
            return Err(_storage(str(exc)))
        finally:
            connection.close()

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
            self._ensure_conversation(connection, conversation_id, now)
            return self._state_or_default(connection, conversation_id)

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
            self._ensure_conversation(connection, conversation_id, now)
            connection.execute(
                "UPDATE conversations SET epoch = epoch + 1, "
                "last_user_activity_at = ? WHERE conversation_id = ?",
                (_stamp(now), conversation_id),
            )
            return self._state_or_default(connection, conversation_id)

        return self._write(run)

    def _insert_message(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        entry: MessageEntry,
        now: datetime,
    ) -> ChatMessage:
        role, origin, content, source_event_id = entry
        cursor = connection.execute(
            "INSERT INTO messages "
            "(conversation_id, role, origin, content, source_event_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                role,
                origin.value,
                content,
                source_event_id,
                _stamp(now),
            ),
        )
        return ChatMessage(
            message_id=int(cursor.lastrowid or 0),
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
            connection.execute(
                "INSERT OR IGNORE INTO events VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    record.linked_message_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM events WHERE conversation_id = ? AND idempotency_key = ?",
                (record.conversation_id, record.idempotency_key),
            ).fetchall()[0]
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
            inserted: ChatMessage | None = None
            final_note = note
            surfaced_at: str | None = None
            linked_message_id: int | None = None
            if outcome.action is EventAction.SPEAK:
                spoken = sanitize_untrusted(decision.message).strip()
                final_note = note or sanitize_single_line(spoken)[:240]
                inserted = self._insert_message(
                    connection,
                    conversation_id,
                    ("assistant", MessageOrigin.PROACTIVE, spoken, event_id),
                    now,
                )
                surfaced_at = _stamp(now)
                linked_message_id = inserted.message_id
                today = now.date().isoformat()
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
            connection.execute(
                "UPDATE events SET status = ?, action = ?, reason = ?, note = ?, "
                "failure_reason = '', surfaced_at = ?, linked_message_id = ? "
                "WHERE event_id = ?",
                (
                    EventStatus.COMPLETED.value,
                    outcome.action.value,
                    reason,
                    final_note,
                    surfaced_at,
                    linked_message_id,
                    event_id,
                ),
            )
            return EventCommit(outcome.action, reason, final_note, inserted)

        return self._write(run)

    def fail_event(self, event_id: str, failure_reason: str) -> Result[None, AppError]:
        def run(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE events SET status = ?, action = ?, reason = 'worker_failed', "
                "failure_reason = ?, surfaced_at = NULL, linked_message_id = NULL "
                "WHERE event_id = ?",
                (
                    EventStatus.FAILED.value,
                    EventAction.IGNORE.value,
                    failure_reason,
                    event_id,
                ),
            )

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

    def list_events(
        self, conversation_id: str, *, limit: int = 20
    ) -> Result[tuple[EventRecord, ...], AppError]:
        if limit <= 0:
            return Ok(())

        def run(connection: sqlite3.Connection) -> tuple[EventRecord, ...]:
            rows = connection.execute(
                "SELECT * FROM events WHERE conversation_id = ? "
                "ORDER BY received_at DESC, rowid DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
            return tuple(_event(row) for row in rows)

        return self._read(run, empty=())

    def mark_surfaced(
        self, event_ids: Sequence[str], now: datetime
    ) -> Result[None, AppError]:
        if not event_ids:
            return Ok(None)

        def run(connection: sqlite3.Connection) -> None:
            markers = ", ".join("?" for _ in event_ids)
            connection.execute(
                f"UPDATE events SET surfaced_at = ? WHERE event_id IN ({markers})",
                (_stamp(now), *event_ids),
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

    def write_memory(self, blocks: MemoryBlocks) -> Result[None, AppError]:
        try:
            with self._memory_lock():
                self._write_memory_unlocked(blocks)
        except OSError as exc:
            return Err(_storage(str(exc)))
        return Ok(None)

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
        except OSError as exc:
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
            self._memory.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self._memory.parent, delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(_render_memory(blocks))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self._memory)
        except OSError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise

    def read_memory(self) -> Result[MemoryBlocks, AppError]:
        try:
            text = self._memory.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Ok(MemoryBlocks())
        except OSError as exc:
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


def _render_memory(blocks: MemoryBlocks) -> str:
    return (
        f"## user\n{blocks.user}\n\n"
        f"## relationship\n{blocks.relationship}\n\n"
        f"## arc\n{blocks.arc}\n"
    )
