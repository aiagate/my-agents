from datetime import UTC, datetime, timedelta


def format_timestamp(value: datetime) -> str:
    if value.utcoffset() != timedelta(0):
        raise ValueError("Timestamp must use UTC.")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def parse_timestamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() != timedelta(0):
        raise ValueError("Timestamp must use UTC.")
    return stamp.replace(tzinfo=UTC)
