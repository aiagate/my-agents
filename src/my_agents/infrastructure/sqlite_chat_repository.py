import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.ports.chat_history import (
    ChatStorageError,
    ChatStorageErrorType,
    DialogueTurnRecord,
    EpisodeCloseReason,
    EpisodeRecord,
    IChatQuery,
    IChatStore,
    NewDialogueTurn,
)
from my_agents.infrastructure.settings import AppSettings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    overview TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    close_reason TEXT,
    turn_count INTEGER NOT NULL DEFAULT 0,
    summarized_turn_count INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_episode ON episodes ((1)) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS episodes_recency ON episodes (updated_at DESC);
CREATE TABLE IF NOT EXISTS dialogue_turns (
    episode_id TEXT NOT NULL REFERENCES episodes(id),
    turn_index INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (episode_id, turn_index)
);
"""
_EPISODE_COLUMNS = (
    "id, title, overview, summary, started_at, updated_at, closed_at, "
    "close_reason, turn_count, summarized_turn_count"
)
_ORDER = "ORDER BY updated_at DESC, rowid DESC"


def _timestamp(value: datetime) -> str:
    if value.utcoffset() != timedelta(0):
        raise ValueError("Timestamp must use UTC.")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _read_timestamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() != timedelta(0):
        raise ValueError("Timestamp must use UTC.")
    return stamp.replace(tzinfo=UTC)


def _decode_episode(row: tuple[object, ...]) -> EpisodeRecord:
    match row:
        case (
            str(episode_id),
            str(title),
            str(overview),
            str(summary),
            str(started),
            str(updated),
            closed,
            reason,
            int(turn_count),
            int(summarized),
        ):
            pass
        case _:
            raise ValueError("Invalid episode row.")
    close_reason: EpisodeCloseReason | None
    if reason is not None and not isinstance(reason, str):
        raise ValueError("Invalid episode close reason.")
    match reason:
        case None | "clear" | "rollover":
            close_reason = reason
        case _:
            raise ValueError("Invalid episode close reason.")
    match closed:
        case None:
            closed_at = None
        case str(stamp):
            closed_at = _read_timestamp(stamp)
        case _:
            raise ValueError("Invalid episode close timestamp.")
    if (closed_at is None) != (
        close_reason is None
    ) or not 0 <= summarized <= turn_count:
        raise ValueError("Inconsistent episode state.")
    return EpisodeRecord(
        episode_id,
        title,
        overview,
        summary,
        _read_timestamp(started),
        _read_timestamp(updated),
        closed_at,
        close_reason,
        turn_count,
        summarized,
    )


def _decode_turn(row: tuple[object, ...]) -> DialogueTurnRecord:
    match row:
        case (str(episode_id), int(index), role, str(content), str(created)):
            pass
        case _:
            raise ValueError("Invalid dialogue turn row.")
    valid_role: Literal["user", "assistant"]
    if not isinstance(role, str):
        raise ValueError("Invalid dialogue turn role.")
    match role:
        case "user" | "assistant":
            valid_role = role
        case _:
            raise ValueError("Invalid dialogue turn role.")
    return DialogueTurnRecord(
        episode_id, index, valid_role, content, _read_timestamp(created)
    )


def _find_episode(
    connection: sqlite3.Connection, episode_id: str
) -> EpisodeRecord | None:
    row: tuple[object, ...] | None = connection.execute(
        f"SELECT {_EPISODE_COLUMNS} FROM episodes WHERE id = ?",
        (episode_id,),
    ).fetchone()
    return None if row is None else _decode_episode(row)


class SqliteChatRepository(IChatStore, IChatQuery):
    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def from_settings(cls, settings: AppSettings) -> SqliteChatRepository:
        return cls(settings.resolved_chats_db_path)

    @property
    def location(self) -> str:
        return str(self._path)

    def _read[T](
        self, operation: Callable[[sqlite3.Connection], T], *, empty: T
    ) -> Result[T, ChatStorageError]:
        try:
            if not self._path.exists():
                return Ok(empty)
            uri = f"{self._path.absolute().as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                return Ok(operation(connection))
        except (sqlite3.Error, OSError) as exc:
            return Err(ChatStorageError(ChatStorageErrorType.READ_FAILED, str(exc)))
        except (ValueError, OverflowError) as exc:
            return Err(ChatStorageError(ChatStorageErrorType.CORRUPT_DATA, str(exc)))

    def _write(
        self,
        operation: Callable[
            [sqlite3.Connection], Result[EpisodeRecord, ChatStorageError]
        ],
    ) -> Result[EpisodeRecord, ChatStorageError]:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self._path)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA busy_timeout = 5000")
                # A concurrent first writer can block the WAL mode change before the busy timeout applies.
                with suppress(sqlite3.OperationalError):
                    connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(_SCHEMA)
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    result = operation(connection)
                    if is_err(result):
                        connection.rollback()
                    return result
        except (sqlite3.Error, OSError) as exc:
            return Err(ChatStorageError(ChatStorageErrorType.WRITE_FAILED, str(exc)))
        except (ValueError, OverflowError) as exc:
            return Err(ChatStorageError(ChatStorageErrorType.CORRUPT_DATA, str(exc)))

    def record_turns(
        self,
        episode_id: str | None,
        turns: Sequence[NewDialogueTurn],
        *,
        title: str,
        updated_at: datetime,
        close_episode_id: str | None = None,
    ) -> Result[EpisodeRecord, ChatStorageError]:
        def record(
            connection: sqlite3.Connection,
        ) -> Result[EpisodeRecord, ChatStorageError]:
            stamp = _timestamp(updated_at)
            if close_episode_id is not None:
                connection.execute(
                    "UPDATE episodes SET closed_at = ?, close_reason = 'rollover' WHERE id = ? AND closed_at IS NULL",
                    (stamp, close_episode_id),
                )
            target_id = episode_id
            if target_id is None:
                row = connection.execute(
                    f"SELECT {_EPISODE_COLUMNS} FROM episodes WHERE closed_at IS NULL LIMIT 1"
                ).fetchone()
                if row is not None:
                    target = _decode_episode(row)
                    target_id = target.id
                else:
                    target_id = uuid4().hex[:12]
                    connection.execute(
                        "INSERT INTO episodes (id, title, started_at, updated_at) VALUES (?, ?, ?, ?)",
                        (target_id, title, stamp, stamp),
                    )
            target = _find_episode(connection, target_id)
            if target is None:
                return Err(
                    ChatStorageError(
                        ChatStorageErrorType.NOT_FOUND,
                        f"Episode not found: {target_id}",
                    )
                )
            if target.closed_at is not None:
                return Err(
                    ChatStorageError(
                        ChatStorageErrorType.WRITE_FAILED,
                        f"Episode is closed: {target_id}",
                    )
                )
            connection.executemany(
                "INSERT INTO dialogue_turns (episode_id, turn_index, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        target_id,
                        idx,
                        turn.role,
                        turn.content,
                        _timestamp(turn.created_at),
                    )
                    for idx, turn in enumerate(turns, target.turn_count + 1)
                ),
            )
            connection.execute(
                "UPDATE episodes SET turn_count = ?, updated_at = ? WHERE id = ?",
                (
                    target.turn_count + len(turns),
                    _timestamp(max(target.updated_at, updated_at)),
                    target_id,
                ),
            )
            result = _find_episode(connection, target_id)
            if result is None:
                return Err(
                    ChatStorageError(
                        ChatStorageErrorType.CORRUPT_DATA,
                        f"Episode disappeared: {target_id}",
                    )
                )
            return Ok(result)

        return self._write(record)

    def create_episode(
        self, episode_id: str, *, title: str, started_at: datetime
    ) -> Result[EpisodeRecord, ChatStorageError]:
        def create(
            connection: sqlite3.Connection,
        ) -> Result[EpisodeRecord, ChatStorageError]:
            stamp = _timestamp(started_at)
            connection.execute(
                "INSERT INTO episodes (id, title, started_at, updated_at) VALUES (?, ?, ?, ?)",
                (episode_id, title, stamp, stamp),
            )
            return Ok(
                EpisodeRecord(
                    episode_id,
                    title,
                    "",
                    "",
                    started_at.astimezone(UTC),
                    started_at.astimezone(UTC),
                    None,
                    None,
                    0,
                    0,
                )
            )

        return self._write(create)

    def _update_episode(
        self,
        episode_id: str,
        change: Callable[[sqlite3.Connection, EpisodeRecord], None],
    ) -> Result[EpisodeRecord, ChatStorageError]:
        def update(
            connection: sqlite3.Connection,
        ) -> Result[EpisodeRecord, ChatStorageError]:
            episode = _find_episode(connection, episode_id)
            if episode is None:
                return Err(
                    ChatStorageError(
                        ChatStorageErrorType.NOT_FOUND,
                        f"Episode not found: {episode_id}",
                    )
                )
            change(connection, episode)
            updated = _find_episode(connection, episode_id)
            if updated is None:
                return Err(
                    ChatStorageError(
                        ChatStorageErrorType.CORRUPT_DATA,
                        f"Episode disappeared during update: {episode_id}",
                    )
                )
            return Ok(updated)

        return self._write(update)

    def append_turns(
        self, episode_id: str, turns: Sequence[NewDialogueTurn], *, updated_at: datetime
    ) -> Result[EpisodeRecord, ChatStorageError]:
        def append(connection: sqlite3.Connection, episode: EpisodeRecord) -> None:
            if episode.closed_at is not None:
                raise sqlite3.IntegrityError(f"Episode is closed: {episode_id}")
            connection.executemany(
                "INSERT INTO dialogue_turns (episode_id, turn_index, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        episode_id,
                        index,
                        turn.role,
                        turn.content,
                        _timestamp(turn.created_at),
                    )
                    for index, turn in enumerate(turns, episode.turn_count + 1)
                ),
            )
            connection.execute(
                "UPDATE episodes SET turn_count = ?, updated_at = ? WHERE id = ?",
                (
                    episode.turn_count + len(turns),
                    _timestamp(max(episode.updated_at, updated_at)),
                    episode_id,
                ),
            )

        return self._update_episode(episode_id, append)

    def update_episode_summary(
        self,
        episode_id: str,
        *,
        title: str,
        overview: str,
        summary: str,
        summarized_turn_count: int,
    ) -> Result[EpisodeRecord, ChatStorageError]:
        def summarize(connection: sqlite3.Connection, episode: EpisodeRecord) -> None:
            if not 0 <= summarized_turn_count <= episode.turn_count:
                raise sqlite3.IntegrityError(
                    f"Invalid summarized turn count: {summarized_turn_count}"
                )
            connection.execute(
                "UPDATE episodes SET title = ?, overview = ?, summary = ?, summarized_turn_count = ? WHERE id = ? AND summarized_turn_count <= ?",
                (
                    title,
                    overview,
                    summary,
                    summarized_turn_count,
                    episode_id,
                    summarized_turn_count,
                ),
            )

        return self._update_episode(episode_id, summarize)

    def close_episode(
        self, episode_id: str, *, closed_at: datetime, reason: EpisodeCloseReason
    ) -> Result[EpisodeRecord, ChatStorageError]:
        def close(connection: sqlite3.Connection, episode: EpisodeRecord) -> None:
            connection.execute(
                "UPDATE episodes SET closed_at = CASE WHEN closed_at IS NULL THEN ? ELSE closed_at END, close_reason = ? WHERE id = ? AND (closed_at IS NULL OR (close_reason = 'rollover' AND ? = 'clear'))",
                (_timestamp(closed_at), reason, episode.id, reason),
            )

        return self._update_episode(episode_id, close)

    def get_latest_episode(self) -> Result[EpisodeRecord | None, ChatStorageError]:
        def latest(connection: sqlite3.Connection) -> EpisodeRecord | None:
            row: tuple[object, ...] | None = connection.execute(
                f"SELECT {_EPISODE_COLUMNS} FROM episodes ORDER BY (closed_at IS NULL) DESC, rowid DESC LIMIT 1",
            ).fetchone()
            return None if row is None else _decode_episode(row)

        return self._read(latest, empty=None)

    def get_open_episode(self) -> Result[EpisodeRecord | None, ChatStorageError]:
        def opened(connection: sqlite3.Connection) -> EpisodeRecord | None:
            row = connection.execute(
                f"SELECT {_EPISODE_COLUMNS} FROM episodes WHERE closed_at IS NULL LIMIT 1"
            ).fetchone()
            return None if row is None else _decode_episode(row)

        return self._read(opened, empty=None)

    def get_previous_episode(
        self, episode_id: str
    ) -> Result[EpisodeRecord | None, ChatStorageError]:
        def previous(connection: sqlite3.Connection) -> EpisodeRecord | None:
            row: tuple[object, ...] | None = connection.execute(
                f"SELECT {_EPISODE_COLUMNS} FROM episodes "
                "WHERE rowid < (SELECT rowid FROM episodes WHERE id = ?) "
                "ORDER BY rowid DESC LIMIT 1",
                (episode_id,),
            ).fetchone()
            return None if row is None else _decode_episode(row)

        return self._read(previous, empty=None)

    def get_episode(
        self, episode_id: str
    ) -> Result[EpisodeRecord | None, ChatStorageError]:
        return self._read(
            lambda connection: _find_episode(connection, episode_id), empty=None
        )

    def list_episodes(
        self, *, limit: int | None = None
    ) -> Result[tuple[EpisodeRecord, ...], ChatStorageError]:
        if limit is not None and limit <= 0:
            return Ok(())

        def episodes(connection: sqlite3.Connection) -> tuple[EpisodeRecord, ...]:
            rows: list[tuple[object, ...]] = connection.execute(
                f"SELECT {_EPISODE_COLUMNS} FROM episodes {_ORDER} LIMIT ?",
                (-1 if limit is None else min(limit, 2**63 - 1),),
            ).fetchall()
            return tuple(map(_decode_episode, rows))

        return self._read(episodes, empty=())

    def list_turns(
        self, episode_id: str, *, limit: int | None = None
    ) -> Result[tuple[DialogueTurnRecord, ...], ChatStorageError]:
        if limit is not None and limit <= 0:
            return Ok(())

        def turns(connection: sqlite3.Connection) -> tuple[DialogueTurnRecord, ...]:
            rows: list[tuple[object, ...]] = connection.execute(
                "SELECT episode_id, turn_index, role, content, created_at FROM dialogue_turns "
                "WHERE episode_id = ? ORDER BY turn_index DESC LIMIT ?",
                (episode_id, -1 if limit is None else min(limit, 2**63 - 1)),
            ).fetchall()
            return tuple(map(_decode_turn, reversed(rows)))

        return self._read(turns, empty=())

    def search_episodes(
        self, query: str, *, limit: int = 10
    ) -> Result[tuple[EpisodeRecord, ...], ChatStorageError]:
        if limit <= 0:
            return Ok(())
        pattern = (
            "%"
            + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )

        def search(connection: sqlite3.Connection) -> tuple[EpisodeRecord, ...]:
            rows: list[tuple[object, ...]] = connection.execute(
                f"""SELECT {_EPISODE_COLUMNS} FROM episodes
                WHERE title LIKE ? ESCAPE '\\' OR overview LIKE ? ESCAPE '\\'
                    OR summary LIKE ? ESCAPE '\\'
                    OR EXISTS (SELECT 1 FROM dialogue_turns
                               WHERE episode_id = episodes.id AND content LIKE ? ESCAPE '\\')
                {_ORDER} LIMIT ?""",
                (pattern, pattern, pattern, pattern, min(limit, 2**63 - 1)),
            ).fetchall()
            return tuple(map(_decode_episode, rows))

        return self._read(search, empty=())
