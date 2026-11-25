import fcntl
import os
import sqlite3
import tempfile
from collections.abc import Callable, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import assert_never
from uuid import uuid4

from flow_res import Err, Ok, Result

from my_agents.core.events import decide_merge
from my_agents.core.memory import should_rollover
from my_agents.core.types import (
    AppError,
    AppErrorKind,
    ConversationCounts,
    ConversationState,
    DialogueTurn,
    Episode,
    EventBrief,
    EventDelivery,
    EventRecord,
    EventStatus,
    MemoryBlock,
    MemoryBlocks,
    MemoryCandidate,
    MemoryStatus,
    ProactivePolicy,
    Role,
    TurnOrigin,
)

type TurnEntry = tuple[Role, TurnOrigin, str, str | None]
CURRENT_SCHEMA_VERSION = 1
_LEGACY_TABLES = (
    "dialogue_turns",
    "conversation_state",
    "memory_candidates",
    "events",
    "episodes",
)


@dataclass(frozen=True, slots=True)
class ProactiveCommit:
    delivery: EventDelivery
    reason: str
    episode_id: str | None = None
    turn: DialogueTurn | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (conversation_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversation_state (conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id) ON DELETE CASCADE, epoch INTEGER NOT NULL DEFAULT 0, last_user_activity_at TEXT, last_proactive_at TEXT, proactive_count_today INTEGER NOT NULL DEFAULT 0, proactive_day TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS episodes (episode_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE, title TEXT NOT NULL, overview TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, updated_at TEXT NOT NULL, turn_count INTEGER NOT NULL DEFAULT 0, summarized_turn_count INTEGER NOT NULL DEFAULT 0, closed_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_episode_per_conversation ON episodes(conversation_id) WHERE closed_at IS NULL;
CREATE TABLE IF NOT EXISTS dialogue_turns (episode_id TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE, conversation_id TEXT NOT NULL, seq INTEGER NOT NULL, turn_index INTEGER NOT NULL, role TEXT NOT NULL, origin TEXT NOT NULL, content TEXT NOT NULL, source_event_id TEXT, created_at TEXT NOT NULL, PRIMARY KEY (episode_id, turn_index));
CREATE INDEX IF NOT EXISTS dialogue_turns_seq ON dialogue_turns(conversation_id, seq);
CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, name TEXT NOT NULL, source TEXT NOT NULL, occurred_at TEXT NOT NULL, received_at TEXT NOT NULL, status TEXT NOT NULL, delivery TEXT NOT NULL, merge_reason TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL, brief_json TEXT, failure_reason TEXT NOT NULL DEFAULT '', surfaced_at TEXT, linked_turn_id TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS events_idempotency ON events(conversation_id, idempotency_key);
CREATE INDEX IF NOT EXISTS events_recency ON events(conversation_id, received_at DESC);
CREATE TABLE IF NOT EXISTS memory_candidates (candidate_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, block TEXT NOT NULL, content TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS candidates_status ON memory_candidates(conversation_id, status);
"""


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _date(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value).astimezone(UTC)


def _storage(message: str) -> AppError:
    return AppError(AppErrorKind.STORAGE, message)


def _state(row: sqlite3.Row) -> ConversationState:
    data = dict(row)
    data["last_user_activity_at"] = _date(data["last_user_activity_at"])
    data["last_proactive_at"] = _date(data["last_proactive_at"])
    return ConversationState(**data)


def _episode(row: sqlite3.Row) -> Episode:
    data = dict(row)
    data["started_at"] = _date(data["started_at"])
    data["updated_at"] = _date(data["updated_at"])
    data["closed_at"] = _date(data["closed_at"])
    return Episode(**data)


def _turn(row: sqlite3.Row) -> DialogueTurn:
    data = dict(row)
    data["origin"] = TurnOrigin(data["origin"])
    data["created_at"] = _date(data["created_at"])
    return DialogueTurn(**data)


def _event(row: sqlite3.Row) -> EventRecord:
    data = dict(row)
    data["status"] = EventStatus(data["status"])
    data["delivery"] = EventDelivery(data["delivery"])
    data["occurred_at"] = _date(data["occurred_at"])
    data["received_at"] = _date(data["received_at"])
    data["surfaced_at"] = _date(data["surfaced_at"])
    brief = data.pop("brief_json")
    data["brief"] = None if brief is None else EventBrief.model_validate_json(brief)
    return EventRecord(**data)


def _candidate(row: sqlite3.Row) -> MemoryCandidate:
    data = dict(row)
    data["block"] = MemoryBlock(data["block"])
    data["status"] = MemoryStatus(data["status"])
    data["created_at"] = _date(data["created_at"])
    return MemoryCandidate(**data)


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _unique_single_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    found: list[str] = []
    for index in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
        if not index[2]:
            continue
        columns = [
            str(row[2])
            for row in connection.execute(f'PRAGMA index_info("{index[1]}")').fetchall()
        ]
        if len(columns) == 1:
            found.extend(columns)
    return found


def _pick(columns: set[str], *names: str, default: str = "NULL") -> str:
    for name in names:
        if name in columns:
            return f'"{name}"'
    return default


def _needs_migration(connection: sqlite3.Connection) -> bool:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    for table in ("episodes", "dialogue_turns", "events", "memory_candidates"):
        if table not in tables:
            continue
        columns = _table_columns(connection, table)
        if "conversation_id" not in columns:
            return True
        if table == "episodes" and "episode_id" not in columns:
            return True
        if table == "events" and (
            "event_id" not in columns or "received_at" not in columns
        ):
            return True
        if table == "events" and "idempotency_key" in _unique_single_columns(
            connection, table
        ):
            return True
    return False


class Store:
    def __init__(self, db_path: Path, memory_path: Path) -> None:
        self._db = db_path
        self._memory = memory_path
        self._init_error: AppError | None = None
        if db_path.exists():
            self._init_error = self._prepare()

    def _prepare(self) -> AppError | None:
        try:
            connection = sqlite3.connect(self._db)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout = 5000")
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > CURRENT_SCHEMA_VERSION:
                    return _storage(
                        f"Database schema version {version} is newer than "
                        f"{CURRENT_SCHEMA_VERSION}."
                    )
                if _needs_migration(connection):
                    self._migrate(connection)
                connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
                connection.commit()
            finally:
                connection.close()
        except (sqlite3.Error, OSError, ValueError) as exc:
            return _storage(str(exc))
        return None

    def _migrate(self, connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            present = [table for table in _LEGACY_TABLES if table in tables]
            for table in present:
                connection.execute(f'ALTER TABLE "{table}" RENAME TO "{table}__legacy"')
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            now = _stamp(datetime.now(UTC))
            now_literal = f"'{now}'"
            connection.execute(
                "INSERT OR IGNORE INTO conversations (conversation_id, created_at) "
                "VALUES ('default', ?)",
                (now,),
            )
            if "conversation_state" in present:
                columns = _table_columns(connection, "conversation_state__legacy")
                connection.execute(
                    "INSERT INTO conversation_state "
                    "(conversation_id, epoch, last_user_activity_at, "
                    "last_proactive_at, proactive_count_today, proactive_day) "
                    f"SELECT 'default', {_pick(columns, 'epoch', default='0')}, "
                    f"{_pick(columns, 'last_user_activity_at')}, "
                    f"{_pick(columns, 'last_proactive_at')}, "
                    f"{_pick(columns, 'proactive_count_today', default='0')}, "
                    f"{_pick(columns, 'proactive_day', default="''")} "
                    "FROM conversation_state__legacy LIMIT 1"
                )
            if "episodes" in present:
                columns = _table_columns(connection, "episodes__legacy")
                connection.execute(
                    "INSERT INTO episodes (episode_id, conversation_id, "
                    "title, overview, summary, started_at, updated_at, turn_count, "
                    "summarized_turn_count, closed_at) "
                    f"SELECT {_pick(columns, 'episode_id', 'id')}, 'default', "
                    f"{_pick(columns, 'title', default="''")}, "
                    f"{_pick(columns, 'overview', default="''")}, "
                    f"{_pick(columns, 'summary', default="''")}, "
                    f"{_pick(columns, 'started_at', default=now_literal)}, "
                    f"{_pick(columns, 'updated_at', default=now_literal)}, "
                    f"{_pick(columns, 'turn_count', default='0')}, "
                    f"{_pick(columns, 'summarized_turn_count', default='0')}, "
                    f"{_pick(columns, 'closed_at')} FROM episodes__legacy"
                )
            if "dialogue_turns" in present:
                columns = _table_columns(connection, "dialogue_turns__legacy")
                connection.execute(
                    "INSERT INTO dialogue_turns (episode_id, "
                    "conversation_id, seq, turn_index, role, origin, content, "
                    "source_event_id, created_at) "
                    f"SELECT {_pick(columns, 'episode_id', default="''")}, 'default', "
                    f"{_pick(columns, 'seq', 'turn_index', 'rowid', default='0')}, "
                    f"{_pick(columns, 'turn_index', 'seq', default='0')}, "
                    f"{_pick(columns, 'role', default="'assistant'")}, "
                    f"{_pick(columns, 'origin', default="'reply'")}, "
                    f"{_pick(columns, 'content', default="''")}, "
                    f"{_pick(columns, 'source_event_id')}, "
                    f"{_pick(columns, 'created_at', default=now_literal)} "
                    "FROM dialogue_turns__legacy"
                )
            if "events" in present:
                columns = _table_columns(connection, "events__legacy")
                connection.execute(
                    "INSERT INTO events (event_id, conversation_id, "
                    "idempotency_key, name, source, occurred_at, received_at, "
                    "status, delivery, merge_reason, payload_json, brief_json, "
                    "failure_reason, surfaced_at, linked_turn_id) "
                    f"SELECT {_pick(columns, 'event_id', 'id')}, 'default', "
                    f"{_pick(columns, 'idempotency_key', default='event_id')}, "
                    f"{_pick(columns, 'name', default="''")}, "
                    f"{_pick(columns, 'source', default="''")}, "
                    f"{_pick(columns, 'occurred_at', default=now_literal)}, "
                    f"{_pick(columns, 'received_at', 'recorded_at', default=now_literal)}, "
                    f"{_pick(columns, 'status', default="'received'")}, "
                    f"{_pick(columns, 'delivery', default="'silent'")}, "
                    f"{_pick(columns, 'merge_reason', default="''")}, "
                    f"{_pick(columns, 'payload_json', default="'{}'")}, "
                    f"{_pick(columns, 'brief_json')}, "
                    f"{_pick(columns, 'failure_reason', default="''")}, "
                    f"{_pick(columns, 'surfaced_at')}, "
                    f"{_pick(columns, 'linked_turn_id')} FROM events__legacy"
                )
            if "memory_candidates" in present:
                columns = _table_columns(connection, "memory_candidates__legacy")
                connection.execute(
                    "INSERT INTO memory_candidates (candidate_id, "
                    "conversation_id, block, content, reason, status, created_at) "
                    f"SELECT {_pick(columns, 'candidate_id', 'id')}, 'default', "
                    f"{_pick(columns, 'block', default="'user'")}, "
                    f"{_pick(columns, 'content', default="''")}, "
                    f"{_pick(columns, 'reason', default="''")}, "
                    f"{_pick(columns, 'status', default="'pending'")}, "
                    f"{_pick(columns, 'created_at', default=now_literal)} "
                    "FROM memory_candidates__legacy"
                )
            for table in present:
                connection.execute(f'DROP TABLE IF EXISTS "{table}__legacy"')
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
            connection.commit()
        except Exception:
            connection.rollback()
            connection.execute("PRAGMA foreign_keys = ON")
            raise

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
            if not readonly:
                with suppress(sqlite3.OperationalError):
                    connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(_SCHEMA)
                connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        except Exception:
            connection.close()
            raise
        return connection

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
            with closing(connection), connection:
                connection.execute("BEGIN IMMEDIATE")
                return Ok(operation(connection))
        except (sqlite3.Error, OSError, ValueError) as exc:
            return Err(_storage(str(exc)))

    def _ensure_conversation(
        self, connection: sqlite3.Connection, conversation_id: str, now: datetime
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO conversations VALUES (?, ?)",
            (conversation_id, _stamp(now)),
        )
        connection.execute(
            "INSERT OR IGNORE INTO conversation_state (conversation_id) VALUES (?)",
            (conversation_id,),
        )

    def ensure_conversation(
        self, conversation_id: str, now: datetime
    ) -> Result[ConversationState, AppError]:
        def run(connection: sqlite3.Connection) -> ConversationState:
            self._ensure_conversation(connection, conversation_id, now)
            return self._state_or_default(connection, conversation_id)

        return self._write(run)

    def _state_or_default(
        self, connection: sqlite3.Connection, conversation_id: str
    ) -> ConversationState:
        row = connection.execute(
            "SELECT * FROM conversation_state WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return ConversationState(conversation_id) if row is None else _state(row)

    def get_state(self, conversation_id: str) -> Result[ConversationState, AppError]:
        return self._read(
            lambda connection: self._state_or_default(connection, conversation_id),
            empty=ConversationState(conversation_id),
        )

    def bump_epoch(
        self, conversation_id: str, now: datetime
    ) -> Result[ConversationState, AppError]:
        def run(connection: sqlite3.Connection) -> ConversationState:
            connection.execute(
                "UPDATE conversation_state SET epoch = epoch + 1, last_user_activity_at = ? WHERE conversation_id = ?",
                (_stamp(now), conversation_id),
            )
            return self._state_or_default(connection, conversation_id)

        return self._write(run)

    def record_proactive(
        self, conversation_id: str, now: datetime
    ) -> Result[ConversationState, AppError]:
        def run(connection: sqlite3.Connection) -> ConversationState:
            self._record_proactive(connection, conversation_id, now)
            return self._state_or_default(connection, conversation_id)

        return self._write(run)

    def get_open_episode(
        self, conversation_id: str
    ) -> Result[Episode | None, AppError]:
        return self._read(
            lambda connection: self._episode_or_none(
                connection,
                "SELECT * FROM episodes WHERE conversation_id = ? AND closed_at IS NULL",
                (conversation_id,),
            ),
            empty=None,
        )

    def get_episode(self, episode_id: str) -> Result[Episode | None, AppError]:
        return self._read(
            lambda connection: self._episode_or_none(
                connection, "SELECT * FROM episodes WHERE episode_id = ?", (episode_id,)
            ),
            empty=None,
        )

    def get_episode_in(
        self, conversation_id: str, episode_id: str
    ) -> Result[Episode | None, AppError]:
        return self._read(
            lambda connection: self._episode_or_none(
                connection,
                "SELECT * FROM episodes WHERE conversation_id = ? AND episode_id = ?",
                (conversation_id, episode_id),
            ),
            empty=None,
        )

    def _episode_or_none(
        self,
        connection: sqlite3.Connection,
        sql: str,
        parameters: tuple[object, ...],
    ) -> Episode | None:
        row = connection.execute(sql, parameters).fetchone()
        return None if row is None else _episode(row)

    def list_episodes(
        self, conversation_id: str, *, limit: int = 20
    ) -> Result[tuple[Episode, ...], AppError]:
        if limit <= 0:
            return Ok(())

        def run(connection: sqlite3.Connection) -> tuple[Episode, ...]:
            rows = connection.execute(
                "SELECT * FROM episodes WHERE conversation_id = ? ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
            return tuple(_episode(row) for row in rows)

        return self._read(run, empty=())

    def record_turns(
        self,
        conversation_id: str,
        episode_id: str | None,
        entries: Sequence[TurnEntry],
        *,
        now: datetime,
        title: str,
        max_turns: int,
        gap_minutes: int,
    ) -> Result[tuple[Episode, tuple[DialogueTurn, ...]], AppError]:
        def run(
            connection: sqlite3.Connection,
        ) -> tuple[Episode, tuple[DialogueTurn, ...]]:
            return self._record_turns(
                connection,
                conversation_id,
                episode_id,
                entries,
                now=now,
                title=title,
                max_turns=max_turns,
                gap_minutes=gap_minutes,
            )

        return self._write(run)

    def _record_turns(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        episode_id: str | None,
        entries: Sequence[TurnEntry],
        *,
        now: datetime,
        title: str,
        max_turns: int,
        gap_minutes: int,
    ) -> tuple[Episode, tuple[DialogueTurn, ...]]:
        self._ensure_conversation(connection, conversation_id, now)
        episode = (
            None
            if episode_id is None
            else self._episode_or_none(
                connection,
                "SELECT * FROM episodes WHERE episode_id = ?",
                (episode_id,),
            )
        )
        if episode is None or should_rollover(
            episode, now, max_turns=max_turns, gap_minutes=gap_minutes
        ):
            if episode is not None and episode.closed_at is None:
                connection.execute(
                    "UPDATE episodes SET closed_at = ? WHERE episode_id = ?",
                    (_stamp(now), episode.episode_id),
                )
            open_row = connection.execute(
                "SELECT * FROM episodes WHERE conversation_id = ? AND closed_at IS NULL",
                (conversation_id,),
            ).fetchone()
            episode = (
                _episode(open_row)
                if open_row is not None
                else self._insert_episode(connection, conversation_id, title, now)
            )
        seq = self._next_seq(connection, conversation_id)
        turns = tuple(
            DialogueTurn(
                conversation_id,
                episode.episode_id,
                seq + offset,
                episode.turn_count + offset + 1,
                role,
                origin,
                content,
                source_event_id,
                now,
            )
            for offset, (role, origin, content, source_event_id) in enumerate(entries)
        )
        connection.executemany(
            "INSERT INTO dialogue_turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    turn.episode_id,
                    turn.conversation_id,
                    turn.seq,
                    turn.turn_index,
                    turn.role,
                    turn.origin.value,
                    turn.content,
                    turn.source_event_id,
                    _stamp(turn.created_at),
                )
                for turn in turns
            ],
        )
        connection.execute(
            "UPDATE episodes SET turn_count = ?, updated_at = ? WHERE episode_id = ?",
            (episode.turn_count + len(turns), _stamp(now), episode.episode_id),
        )
        return (
            replace(
                episode, turn_count=episode.turn_count + len(turns), updated_at=now
            ),
            turns,
        )

    def commit_proactive(
        self,
        conversation_id: str,
        event_id: str,
        brief: EventBrief,
        *,
        received_epoch: int,
        text: str,
        now: datetime,
        policy: ProactivePolicy,
        title: str,
        max_turns: int,
        gap_minutes: int,
    ) -> Result[ProactiveCommit, AppError]:
        def run(connection: sqlite3.Connection) -> ProactiveCommit:
            state = self._state_or_default(connection, conversation_id)
            if state.epoch != received_epoch:
                self._downgrade(
                    connection,
                    event_id,
                    brief,
                    EventDelivery.AMBIENT,
                    "epoch_conflict",
                    EventStatus.COMPLETED,
                )
                return ProactiveCommit(EventDelivery.AMBIENT, "epoch_conflict")
            decision = decide_merge(brief, state, now, policy)
            if decision.delivery is not EventDelivery.PROACTIVE:
                match decision.delivery:
                    case EventDelivery.SILENT:
                        status = EventStatus.SUPPRESSED
                    case EventDelivery.AMBIENT | EventDelivery.PROACTIVE:
                        status = EventStatus.COMPLETED
                    case _:
                        assert_never(decision.delivery)
                self._downgrade(
                    connection,
                    event_id,
                    brief,
                    decision.delivery,
                    decision.reason,
                    status,
                )
                return ProactiveCommit(decision.delivery, decision.reason)
            episode = self._episode_or_none(
                connection,
                "SELECT * FROM episodes WHERE conversation_id = ? AND closed_at IS NULL",
                (conversation_id,),
            )
            entry: TurnEntry = ("assistant", TurnOrigin.PROACTIVE, text, event_id)
            episode, turns = self._record_turns(
                connection,
                conversation_id,
                None if episode is None else episode.episode_id,
                (entry,),
                now=now,
                title=title,
                max_turns=max_turns,
                gap_minutes=gap_minutes,
            )
            turn = turns[-1]
            connection.execute(
                "UPDATE events SET status = ?, delivery = ?, merge_reason = ?, "
                "brief_json = ?, failure_reason = '', surfaced_at = ?, "
                "linked_turn_id = ? WHERE event_id = ?",
                (
                    EventStatus.COMPLETED.value,
                    EventDelivery.PROACTIVE.value,
                    decision.reason,
                    brief.model_dump_json(),
                    _stamp(now),
                    f"{episode.episode_id}:{turn.turn_index}",
                    event_id,
                ),
            )
            self._record_proactive(connection, conversation_id, now)
            return ProactiveCommit(
                EventDelivery.PROACTIVE,
                decision.reason,
                episode.episode_id,
                turn,
            )

        return self._write(run)

    def _downgrade(
        self,
        connection: sqlite3.Connection,
        event_id: str,
        brief: EventBrief,
        delivery: EventDelivery,
        reason: str,
        status: EventStatus,
    ) -> None:
        connection.execute(
            "UPDATE events SET status = ?, delivery = ?, merge_reason = ?, "
            "brief_json = ?, failure_reason = '', surfaced_at = NULL, "
            "linked_turn_id = NULL WHERE event_id = ?",
            (
                status.value,
                delivery.value,
                reason,
                brief.model_dump_json(),
                event_id,
            ),
        )

    def _record_proactive(
        self, connection: sqlite3.Connection, conversation_id: str, now: datetime
    ) -> None:
        current = self._state_or_default(connection, conversation_id)
        today = now.date().isoformat()
        count = (
            1 if current.proactive_day != today else current.proactive_count_today + 1
        )
        connection.execute(
            "UPDATE conversation_state SET last_proactive_at = ?, "
            "proactive_count_today = ?, proactive_day = ? WHERE conversation_id = ?",
            (_stamp(now), count, today, conversation_id),
        )

    def _insert_episode(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        title: str,
        now: datetime,
    ) -> Episode:
        episode = Episode(
            f"ep_{uuid4().hex[:12]}",
            conversation_id,
            title,
            "",
            "",
            now,
            now,
            0,
            0,
            None,
        )
        connection.execute(
            "INSERT INTO episodes (episode_id, conversation_id, title, started_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (episode.episode_id, conversation_id, title, _stamp(now), _stamp(now)),
        )
        return episode

    def _next_seq(self, connection: sqlite3.Connection, conversation_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM dialogue_turns WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return int(row[0]) + 1

    def search_episodes(
        self, conversation_id: str, query: str, *, limit: int = 10
    ) -> Result[tuple[Episode, ...], AppError]:
        if limit <= 0:
            return Ok(())
        pattern = f"%{query}%"

        def run(connection: sqlite3.Connection) -> tuple[Episode, ...]:
            rows = connection.execute(
                "SELECT * FROM episodes WHERE conversation_id = ? AND "
                "(title LIKE ? OR overview LIKE ? OR summary LIKE ?) "
                "ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (conversation_id, pattern, pattern, pattern, limit),
            ).fetchall()
            return tuple(_episode(row) for row in rows)

        return self._read(run, empty=())

    def list_turns(
        self, episode_id: str, *, limit: int = 100
    ) -> Result[tuple[DialogueTurn, ...], AppError]:
        if limit <= 0:
            return Ok(())

        def run(connection: sqlite3.Connection) -> tuple[DialogueTurn, ...]:
            rows = connection.execute(
                "SELECT * FROM dialogue_turns WHERE episode_id = ? ORDER BY turn_index DESC LIMIT ?",
                (episode_id, limit),
            ).fetchall()
            return tuple(_turn(row) for row in reversed(rows))

        return self._read(run, empty=())

    def recent_turns(
        self, conversation_id: str, *, limit: int = 20
    ) -> Result[tuple[DialogueTurn, ...], AppError]:
        if limit <= 0:
            return Ok(())

        def run(connection: sqlite3.Connection) -> tuple[DialogueTurn, ...]:
            rows = connection.execute(
                "SELECT * FROM dialogue_turns WHERE conversation_id = ? ORDER BY seq DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
            return tuple(_turn(row) for row in reversed(rows))

        return self._read(run, empty=())

    def update_summary(
        self,
        episode_id: str,
        *,
        title: str,
        overview: str,
        summary: str,
        summarized_turn_count: int,
    ) -> Result[Episode | None, AppError]:
        def run(connection: sqlite3.Connection) -> Episode | None:
            connection.execute(
                "UPDATE episodes SET title = ?, overview = ?, summary = ?, summarized_turn_count = ? WHERE episode_id = ?",
                (title, overview, summary, summarized_turn_count, episode_id),
            )
            return self._episode_or_none(
                connection,
                "SELECT * FROM episodes WHERE episode_id = ?",
                (episode_id,),
            )

        return self._write(run)

    def insert_event(self, record: EventRecord) -> Result[EventRecord, AppError]:
        def run(connection: sqlite3.Connection) -> EventRecord:
            connection.execute(
                "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.event_id,
                    record.conversation_id,
                    record.idempotency_key,
                    record.name,
                    record.source,
                    _stamp(record.occurred_at),
                    _stamp(record.received_at),
                    record.status.value,
                    record.delivery.value,
                    record.merge_reason,
                    record.payload_json,
                    None if record.brief is None else record.brief.model_dump_json(),
                    record.failure_reason,
                    None if record.surfaced_at is None else _stamp(record.surfaced_at),
                    record.linked_turn_id,
                ),
            )
            existing = self._event_or_none(
                connection,
                "conversation_id = ? AND idempotency_key = ?",
                (record.conversation_id, record.idempotency_key),
            )
            return record if existing is None else existing

        return self._write(run)

    def update_event(
        self,
        event_id: str,
        *,
        status: EventStatus,
        delivery: EventDelivery,
        merge_reason: str,
        brief: EventBrief | None,
        failure_reason: str,
        surfaced_at: datetime | None,
        linked_turn_id: str | None,
    ) -> Result[None, AppError]:
        def run(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE events SET status = ?, delivery = ?, merge_reason = ?, brief_json = ?, failure_reason = ?, surfaced_at = ?, linked_turn_id = ? WHERE event_id = ?",
                (
                    status.value,
                    delivery.value,
                    merge_reason,
                    None if brief is None else brief.model_dump_json(),
                    failure_reason,
                    None if surfaced_at is None else _stamp(surfaced_at),
                    linked_turn_id,
                    event_id,
                ),
            )

        return self._write(run)

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

    def counts(self, conversation_id: str) -> Result[ConversationCounts, AppError]:
        def run(connection: sqlite3.Connection) -> ConversationCounts:
            row = connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM dialogue_turns WHERE conversation_id = ?), "
                "(SELECT COUNT(*) FROM episodes WHERE conversation_id = ?), "
                "(SELECT COUNT(*) FROM events WHERE conversation_id = ?), "
                "(SELECT COUNT(*) FROM memory_candidates WHERE conversation_id = ? AND status = ?), "
                "(SELECT COUNT(*) FROM memory_candidates WHERE conversation_id = ? AND status = ?), "
                "(SELECT COUNT(*) FROM memory_candidates WHERE conversation_id = ? AND status = ?)",
                (
                    conversation_id,
                    conversation_id,
                    conversation_id,
                    conversation_id,
                    MemoryStatus.PENDING.value,
                    conversation_id,
                    MemoryStatus.PROMOTED.value,
                    conversation_id,
                    MemoryStatus.REJECTED.value,
                ),
            ).fetchone()
            return ConversationCounts(*(int(value) for value in row))

        return self._read(run, empty=ConversationCounts())

    def list_events(
        self,
        conversation_id: str,
        *,
        limit: int = 10,
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

    def _set_ids(
        self,
        table: str,
        column: str,
        assignment: str,
        leading: tuple[object, ...],
        identifiers: Sequence[str],
    ) -> Result[None, AppError]:
        def run(connection: sqlite3.Connection) -> None:
            markers = ", ".join("?" for _ in identifiers)
            connection.execute(
                f"UPDATE {table} SET {assignment} WHERE {column} IN ({markers})",
                (*leading, *identifiers),
            )

        return self._write(run)

    def mark_surfaced(
        self, event_ids: Sequence[str], now: datetime
    ) -> Result[None, AppError]:
        if not event_ids:
            return Ok(None)
        return self._set_ids(
            "events", "event_id", "surfaced_at = ?", (_stamp(now),), event_ids
        )

    def save_candidate(self, candidate: MemoryCandidate) -> Result[None, AppError]:
        def run(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT OR REPLACE INTO memory_candidates VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate.candidate_id,
                    candidate.conversation_id,
                    candidate.block.value,
                    candidate.content,
                    candidate.reason,
                    candidate.status.value,
                    _stamp(candidate.created_at),
                ),
            )

        return self._write(run)

    def list_candidates(
        self, conversation_id: str, status: MemoryStatus
    ) -> Result[tuple[MemoryCandidate, ...], AppError]:
        def run(connection: sqlite3.Connection) -> tuple[MemoryCandidate, ...]:
            rows = connection.execute(
                "SELECT * FROM memory_candidates WHERE conversation_id = ? AND status = ? ORDER BY created_at, rowid",
                (conversation_id, status.value),
            ).fetchall()
            return tuple(_candidate(row) for row in rows)

        return self._read(run, empty=())

    def set_candidate_status(
        self, candidate_ids: Sequence[str], status: MemoryStatus
    ) -> Result[None, AppError]:
        if not candidate_ids:
            return Ok(None)
        return self._set_ids(
            "memory_candidates",
            "candidate_id",
            "status = ?",
            (status.value,),
            candidate_ids,
        )

    def reset_conversation(self, conversation_id: str) -> Result[int, AppError]:
        def run(connection: sqlite3.Connection) -> int:
            count = connection.execute(
                "SELECT COUNT(*) FROM dialogue_turns WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            for table in ("dialogue_turns", "episodes", "events", "memory_candidates"):
                connection.execute(
                    f"DELETE FROM {table} WHERE conversation_id = ?",
                    (conversation_id,),
                )
            connection.execute(
                "UPDATE conversation_state SET epoch = epoch + 1, last_user_activity_at = NULL, last_proactive_at = NULL, proactive_count_today = 0, proactive_day = '' WHERE conversation_id = ?",
                (conversation_id,),
            )
            return count

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
                memory = self._read_memory_unlocked()
                if isinstance(memory, Err):
                    return memory
                updated = transform(memory.value)
                if isinstance(updated, Err):
                    return updated
                self._write_memory_unlocked(updated.value)
        except OSError as exc:
            return Err(_storage(str(exc)))
        return Ok(None)

    def _memory_lock(self) -> _FileLock:
        self._memory.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._memory.with_name(f"{self._memory.name}.lock")
        handle = os.open(lock_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        return _FileLock(handle)

    def _read_memory_unlocked(self) -> Result[MemoryBlocks, AppError]:
        try:
            text = self._memory.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Ok(MemoryBlocks())
        except OSError as exc:
            return Err(_storage(str(exc)))
        return Ok(_parse_memory(text))

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
        return self._read_memory_unlocked()


class _FileLock:
    def __init__(self, handle: int) -> None:
        self._handle = handle

    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: object) -> None:
        del args
        fcntl.flock(self._handle, fcntl.LOCK_UN)
        os.close(self._handle)


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
