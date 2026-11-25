import json
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing, suppress
from datetime import datetime
from pathlib import Path

from flow_res import Err, Ok, Result

from my_agents.contracts.events import (
    EventDelivery,
    EventRecord,
    EventStatus,
)
from my_agents.contracts.ports.event_ledger import (
    EventLedgerError,
    EventLedgerErrorType,
    IEventLedger,
)
from my_agents.infrastructure.settings import AppSettings
from my_agents.infrastructure.sqlite_time import format_timestamp, parse_timestamp

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    status TEXT NOT NULL,
    delivery TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    recommended_action TEXT NOT NULL,
    failure_reason TEXT NOT NULL,
    surfaced_at TEXT
);
CREATE INDEX IF NOT EXISTS events_recency ON events (recorded_at DESC);
"""
_COLUMNS = (
    "event_id, name, occurred_at, recorded_at, status, delivery, payload_json, "
    "summary, findings_json, recommended_action, failure_reason, surfaced_at"
)
_ORDER = "ORDER BY recorded_at DESC, rowid DESC"


def _decode_findings(value: str) -> tuple[str, ...]:
    findings = json.loads(value)
    if not isinstance(findings, list) or not all(
        isinstance(item, str) for item in findings
    ):
        raise ValueError("Invalid event findings.")
    return tuple(findings)


def _decode_event(row: tuple[object, ...]) -> EventRecord:
    match row:
        case (
            str(event_id),
            str(name),
            str(occurred),
            str(recorded),
            str(status),
            str(delivery),
            str(payload_json),
            str(summary),
            str(findings_json),
            str(recommended_action),
            str(failure_reason),
            surfaced,
        ):
            pass
        case _:
            raise ValueError("Invalid event row.")
    try:
        status_value = EventStatus(status)
        delivery_value = EventDelivery(delivery)
    except ValueError as exc:
        raise ValueError("Invalid event status or delivery.") from exc
    match surfaced:
        case None:
            surfaced_at = None
        case str(stamp):
            surfaced_at = parse_timestamp(stamp)
        case _:
            raise ValueError("Invalid event surfaced timestamp.")
    return EventRecord(
        event_id,
        name,
        parse_timestamp(occurred),
        parse_timestamp(recorded),
        status_value,
        delivery_value,
        payload_json,
        summary,
        _decode_findings(findings_json),
        recommended_action,
        failure_reason,
        surfaced_at,
    )


class SqliteEventLedger(IEventLedger):
    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def from_settings(cls, settings: AppSettings) -> SqliteEventLedger:
        return cls(settings.resolved_chats_db_path)

    def _read[T](
        self, operation: Callable[[sqlite3.Connection], T], *, empty: T
    ) -> Result[T, EventLedgerError]:
        try:
            if not self._path.exists():
                return Ok(empty)
            uri = f"{self._path.absolute().as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                connection.execute("PRAGMA busy_timeout = 5000")
                return Ok(operation(connection))
        except sqlite3.OperationalError as exc:
            if "no such table: events" in str(exc):
                return Ok(empty)
            return Err(EventLedgerError(EventLedgerErrorType.READ_FAILED, str(exc)))
        except (sqlite3.Error, OSError) as exc:
            return Err(EventLedgerError(EventLedgerErrorType.READ_FAILED, str(exc)))
        except (ValueError, OverflowError) as exc:
            return Err(EventLedgerError(EventLedgerErrorType.CORRUPT_DATA, str(exc)))

    def _write(
        self, operation: Callable[[sqlite3.Connection], None]
    ) -> Result[None, EventLedgerError]:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self._path)) as connection:
                connection.execute("PRAGMA busy_timeout = 5000")
                with suppress(sqlite3.OperationalError):
                    connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(_SCHEMA)
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    operation(connection)
                    return Ok(None)
        except (sqlite3.Error, OSError) as exc:
            return Err(EventLedgerError(EventLedgerErrorType.WRITE_FAILED, str(exc)))
        except (ValueError, OverflowError) as exc:
            return Err(EventLedgerError(EventLedgerErrorType.CORRUPT_DATA, str(exc)))

    def record(self, entry: EventRecord) -> Result[None, EventLedgerError]:
        def insert(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO events (event_id, name, occurred_at, recorded_at, "
                "status, delivery, payload_json, summary, findings_json, "
                "recommended_action, failure_reason, surfaced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.event_id,
                    entry.name,
                    format_timestamp(entry.occurred_at),
                    format_timestamp(entry.recorded_at),
                    entry.status.value,
                    entry.delivery.value,
                    entry.payload_json,
                    entry.summary,
                    json.dumps(list(entry.findings), ensure_ascii=False),
                    entry.recommended_action,
                    entry.failure_reason,
                    None
                    if entry.surfaced_at is None
                    else format_timestamp(entry.surfaced_at),
                ),
            )

        return self._write(insert)

    def list_recent(
        self, *, limit: int = 10, unsurfaced_only: bool = False
    ) -> Result[tuple[EventRecord, ...], EventLedgerError]:
        if limit <= 0:
            return Ok(())

        def recent(connection: sqlite3.Connection) -> tuple[EventRecord, ...]:
            where = "WHERE surfaced_at IS NULL " if unsurfaced_only else ""
            rows: list[tuple[object, ...]] = connection.execute(
                f"SELECT {_COLUMNS} FROM events {where}{_ORDER} LIMIT ?",
                (limit,),
            ).fetchall()
            return tuple(map(_decode_event, rows))

        return self._read(recent, empty=())

    def get_event(self, event_id: str) -> Result[EventRecord | None, EventLedgerError]:
        def fetch(connection: sqlite3.Connection) -> EventRecord | None:
            row: tuple[object, ...] | None = connection.execute(
                f"SELECT {_COLUMNS} FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            return None if row is None else _decode_event(row)

        return self._read(fetch, empty=None)

    def mark_surfaced(
        self, event_ids: Sequence[str], *, surfaced_at: datetime
    ) -> Result[None, EventLedgerError]:
        if not event_ids:
            return Ok(None)
        ids = tuple(event_ids)

        def update(connection: sqlite3.Connection) -> None:
            placeholders = ", ".join("?" for _ in ids)
            connection.execute(
                f"UPDATE events SET surfaced_at = ? WHERE event_id IN ({placeholders})",
                (format_timestamp(surfaced_at), *ids),
            )

        return self._write(update)
