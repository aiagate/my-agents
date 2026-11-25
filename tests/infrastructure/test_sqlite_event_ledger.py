import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from flow_res import is_ok

from my_agents.contracts.events import (
    EventDelivery,
    EventRecord,
    EventStatus,
)
from my_agents.contracts.ports.event_ledger import EventLedgerErrorType
from my_agents.contracts.secret import SecretStr
from my_agents.infrastructure.settings import AppSettings
from my_agents.infrastructure.sqlite_event_ledger import SqliteEventLedger
from tests.helpers import assert_err

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _record(
    event_id: str = "evt_one",
    *,
    recorded_at: datetime = _NOW,
    delivery: EventDelivery = EventDelivery.PROACTIVE,
    surfaced_at: datetime | None = None,
) -> EventRecord:
    return EventRecord(
        event_id,
        "temperature.threshold_exceeded",
        _NOW,
        recorded_at,
        EventStatus.COMPLETED,
        delivery,
        '{"celsius": 31.5}',
        "閾値超過",
        ("計測値: 31.5°C",),
        "換気する。",
        "",
        surfaced_at,
    )


def test_records_orders_filters_and_marks_surfaced(tmp_path: Path) -> None:
    path = tmp_path / "chats.db"
    ledger = SqliteEventLedger(path)
    later = _NOW + timedelta(minutes=1)

    assert is_ok(ledger.record(_record("evt_surfaced", surfaced_at=_NOW)))
    assert is_ok(ledger.record(_record("evt_pending", recorded_at=later)))
    assert is_ok(ledger.record(_record("evt_ambient", delivery=EventDelivery.AMBIENT)))

    recent = ledger.list_recent(limit=2).unwrap()
    assert [event.event_id for event in recent] == ["evt_pending", "evt_ambient"]
    assert recent[0].delivery is EventDelivery.PROACTIVE
    assert recent[1].delivery is EventDelivery.AMBIENT
    assert recent[1].findings == ("計測値: 31.5°C",)
    assert recent[1].surfaced_at is None

    unsurfaced = ledger.list_recent(unsurfaced_only=True).unwrap()
    assert [event.event_id for event in unsurfaced] == ["evt_pending", "evt_ambient"]

    assert ledger.list_recent(limit=0).unwrap() == ()
    assert ledger.mark_surfaced((), surfaced_at=later).unwrap() is None
    assert (
        ledger.mark_surfaced(("evt_pending", "evt_ambient"), surfaced_at=later).unwrap()
        is None
    )
    assert ledger.list_recent(unsurfaced_only=True).unwrap() == ()

    fetched = ledger.get_event("evt_pending").unwrap()
    assert fetched is not None
    assert (fetched.event_id, fetched.surfaced_at) == ("evt_pending", later)
    assert ledger.get_event("evt_missing").unwrap() is None


def test_reads_an_absent_database_as_empty(tmp_path: Path) -> None:
    ledger = SqliteEventLedger(tmp_path / "missing.db")

    assert ledger.list_recent().unwrap() == ()
    assert ledger.get_event("evt_one").unwrap() is None


def test_from_settings_uses_the_resolved_chats_path(tmp_path: Path) -> None:
    settings = AppSettings(
        model_name="model",
        base_url="http://localhost",
        api_key=SecretStr("key"),
        memory_dir=tmp_path,
    )
    ledger = SqliteEventLedger.from_settings(settings)

    assert is_ok(ledger.record(_record()))
    assert (tmp_path / "chats.db").is_file()


def test_read_failures_are_typed(tmp_path: Path) -> None:
    broken = tmp_path / "broken.db"
    broken.write_text("not a database")
    ledger = SqliteEventLedger(broken)

    assert_err(
        ledger.list_recent(),
        expected_type=EventLedgerErrorType.READ_FAILED,
        message_contains="file is not a database",
    )
    assert_err(
        ledger.get_event("evt_one"),
        expected_type=EventLedgerErrorType.READ_FAILED,
        message_contains="file is not a database",
    )


def test_write_failures_are_typed(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    assert_err(
        SqliteEventLedger(directory).record(_record()),
        expected_type=EventLedgerErrorType.WRITE_FAILED,
        message_contains="unable to open database file",
    )

    path = tmp_path / "duplicates.db"
    ledger = SqliteEventLedger(path)
    assert is_ok(ledger.record(_record()))
    assert_err(
        ledger.record(_record()),
        expected_type=EventLedgerErrorType.WRITE_FAILED,
        message_contains="UNIQUE constraint failed",
    )


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime(2026, 5, 1, 12, 0, tzinfo=timezone(timedelta(hours=9))),
        datetime.fromisoformat("2026-05-01T12:00:00"),
    ],
)
def test_non_utc_timestamps_are_corrupt_data(
    tmp_path: Path, timestamp: datetime
) -> None:
    ledger = SqliteEventLedger(tmp_path / "chats.db")

    assert_err(
        ledger.record(_record(recorded_at=timestamp)),
        expected_type=EventLedgerErrorType.CORRUPT_DATA,
        message_contains="Timestamp must use UTC.",
    )
    assert_err(
        ledger.mark_surfaced(("evt_one",), surfaced_at=timestamp),
        expected_type=EventLedgerErrorType.CORRUPT_DATA,
        message_contains="Timestamp must use UTC.",
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("UPDATE events SET status = 'bogus'", "Invalid event status or delivery"),
        ("UPDATE events SET delivery = 'bogus'", "Invalid event status or delivery"),
        ("UPDATE events SET surfaced_at = 5", "Invalid isoformat string"),
        (
            "UPDATE events SET surfaced_at = CAST('x' AS BLOB)",
            "Invalid event surfaced timestamp",
        ),
        ("UPDATE events SET findings_json = 'not-json'", "Expecting value"),
        ("UPDATE events SET findings_json = '{\"a\": 1}'", "Invalid event findings"),
        ("UPDATE events SET findings_json = '[1]'", "Invalid event findings"),
        (
            "UPDATE events SET occurred_at = '2026-05-01T12:00:00'",
            "Timestamp must use UTC.",
        ),
        ("UPDATE events SET name = CAST('x' AS BLOB)", "Invalid event row"),
    ],
)
def test_corrupt_rows_are_reported(tmp_path: Path, mutation: str, message: str) -> None:
    path = tmp_path / "chats.db"
    ledger = SqliteEventLedger(path)
    assert is_ok(ledger.record(_record()))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(mutation)

    assert_err(
        ledger.list_recent(),
        expected_type=EventLedgerErrorType.CORRUPT_DATA,
        message_contains=message,
    )
    assert_err(
        ledger.get_event("evt_one"),
        expected_type=EventLedgerErrorType.CORRUPT_DATA,
        message_contains=message,
    )


def test_operational_errors_other_than_missing_tables_are_read_failures(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chats.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE events (wrong TEXT)")

    assert_err(
        SqliteEventLedger(path).list_recent(),
        expected_type=EventLedgerErrorType.READ_FAILED,
        message_contains="no such column",
    )
