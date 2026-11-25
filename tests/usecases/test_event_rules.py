import math
from datetime import UTC, datetime

import pytest
from flow_res import is_err, is_ok

from my_agents.contracts.events import (
    EmailReceived,
    EventKind,
    TemperatureThresholdExceeded,
    WorkerFault,
)
from my_agents.usecases._event_rules import (
    EventValidationError,
    object_mapping,
    serialize_payload,
    validate_event,
)
from tests.event_fixtures import (
    FIXED_NOW,
    email_event,
    temperature_event,
    validated,
    worker_fault_event,
)


def test_object_mapping_accepts_string_keyed_dict() -> None:
    assert object_mapping({"name": "value", "count": 1}) == {
        "name": "value",
        "count": 1,
    }


@pytest.mark.parametrize("value", [[1], "text", None, {1: "non-string key"}])
def test_object_mapping_rejects_non_objects_and_non_string_keys(
    value: object,
) -> None:
    assert object_mapping(value) is None
    assert object_mapping({"kept": "value"}) == {"kept": "value"}


def test_validates_temperature_event_with_default_timestamp() -> None:
    event = validated(temperature_event(sensor_id="lab", celsius=35.0, threshold=30.0))

    assert event.event_id == "evt_fixture"
    assert event.name == "temperature.threshold_exceeded"
    assert event.kind is EventKind.TEMPERATURE_THRESHOLD_EXCEEDED
    assert event.occurred_at == FIXED_NOW
    assert event.payload == TemperatureThresholdExceeded("lab", 35.0, 30.0)


def test_validates_email_event_with_empty_body_and_offset_timestamp() -> None:
    raw = {
        **email_event(subject="明細", body=""),
        "timestamp": "2026-05-01T21:30:00+09:00",
    }

    event = validated(raw)

    assert event.kind is EventKind.EMAIL_RECEIVED
    assert event.payload == EmailReceived("alerts@example.com", "明細", "")
    assert event.occurred_at == datetime(2026, 5, 1, 12, 30, tzinfo=UTC)


def test_validates_worker_fault_event() -> None:
    event = validated(worker_fault_event(reason="database unavailable"))

    assert event.kind is EventKind.WORKER_FAULT
    assert event.payload == WorkerFault("database unavailable")


@pytest.mark.parametrize(
    ("raw", "field", "message"),
    [
        (
            {**email_event(), "unknown": True},
            "body",
            "Unknown top-level keys",
        ),
        ({"data": {}}, "name", "name must be a non-empty string"),
        ({"name": "  ", "data": {}}, "name", "name must be a non-empty string"),
        ({"name": 3, "data": {}}, "name", "name must be a non-empty string"),
        (
            {"name": "unknown.event", "data": {}},
            "name",
            "Unsupported event name",
        ),
        ({"name": "email.received", "data": []}, "data", "must be a JSON object"),
        (
            {"name": "email.received", "data": {"sender": "a"}},
            "data",
            "Expected keys",
        ),
        (
            {**email_event(), "timestamp": 3},
            "timestamp",
            "must be a string",
        ),
        (
            {**email_event(), "timestamp": "not-a-date"},
            "timestamp",
            "ISO 8601",
        ),
        (
            {**email_event(), "timestamp": "2026-05-01T12:00:00"},
            "timestamp",
            "must include a UTC offset",
        ),
    ],
)
def test_validate_reports_top_level_and_timestamp_errors(
    raw: dict[str, object], field: str, message: str
) -> None:
    result = validate_event(raw, event_id="evt_bad", now=FIXED_NOW)

    assert is_err(result)
    assert isinstance(result.error, EventValidationError)
    assert result.error.field == field
    assert message in result.error.message


@pytest.mark.parametrize(
    ("raw", "field", "message"),
    [
        (
            {
                "name": "temperature.threshold_exceeded",
                "data": {
                    "sensor_id": "s",
                    "celsius": 30,
                    "threshold_celsius": 30,
                    "extra": 1,
                },
            },
            "data",
            "Expected keys",
        ),
        (
            {
                "name": "temperature.threshold_exceeded",
                "data": {"sensor_id": 3, "celsius": 30, "threshold_celsius": 30},
            },
            "sensor_id",
            "must be a string",
        ),
        (
            {
                "name": "temperature.threshold_exceeded",
                "data": {"sensor_id": " ", "celsius": 30, "threshold_celsius": 30},
            },
            "sensor_id",
            "must not be empty",
        ),
        (
            {
                "name": "temperature.threshold_exceeded",
                "data": {
                    "sensor_id": "s" * 81,
                    "celsius": 30,
                    "threshold_celsius": 30,
                },
            },
            "sensor_id",
            "at most 80",
        ),
        (
            {
                "name": "temperature.threshold_exceeded",
                "data": {"sensor_id": "s", "celsius": True, "threshold_celsius": 30},
            },
            "celsius",
            "must be a number",
        ),
        (
            {
                "name": "temperature.threshold_exceeded",
                "data": {"sensor_id": "s", "celsius": "30", "threshold_celsius": 30},
            },
            "celsius",
            "must be a number",
        ),
        (
            {
                "name": "temperature.threshold_exceeded",
                "data": {
                    "sensor_id": "s",
                    "celsius": math.inf,
                    "threshold_celsius": 30,
                },
            },
            "celsius",
            "must be finite",
        ),
        (
            {
                "name": "temperature.threshold_exceeded",
                "data": {
                    "sensor_id": "s",
                    "celsius": 30,
                    "threshold_celsius": math.nan,
                },
            },
            "threshold_celsius",
            "must be finite",
        ),
    ],
)
def test_validate_reports_temperature_schema_errors(
    raw: dict[str, object], field: str, message: str
) -> None:
    result = validate_event(raw, event_id="evt_bad", now=FIXED_NOW)

    assert is_err(result)
    assert result.error.field == field
    assert message in result.error.message


@pytest.mark.parametrize(
    ("data", "field", "message"),
    [
        ({"sender": "a", "subject": "s"}, "data", "Expected keys"),
        ({"sender": 3, "subject": "s", "body": ""}, "sender", "must be a string"),
        ({"sender": " ", "subject": "s", "body": ""}, "sender", "must not be empty"),
        (
            {"sender": "s" * 201, "subject": "s", "body": ""},
            "sender",
            "at most 200",
        ),
        ({"sender": "a", "subject": 3, "body": ""}, "subject", "must be a string"),
        ({"sender": "a", "subject": " ", "body": ""}, "subject", "must not be empty"),
        (
            {"sender": "a", "subject": "s" * 201, "body": ""},
            "subject",
            "at most 200",
        ),
        ({"sender": "a", "subject": "s", "body": 3}, "body", "must be a string"),
        (
            {"sender": "a", "subject": "s", "body": "b" * 4001},
            "body",
            "at most 4000",
        ),
    ],
)
def test_validate_reports_email_schema_errors(
    data: dict[str, object], field: str, message: str
) -> None:
    result = validate_event(
        {"name": "email.received", "data": data},
        event_id="evt_bad",
        now=FIXED_NOW,
    )

    assert is_err(result)
    assert result.error.field == field
    assert message in result.error.message


@pytest.mark.parametrize(
    ("data", "field", "message"),
    [
        ({}, "data", "Expected keys"),
        ({"reason": 3}, "reason", "must be a string"),
        ({"reason": " "}, "reason", "must not be empty"),
        ({"reason": "r" * 501}, "reason", "at most 500"),
    ],
)
def test_validate_reports_worker_fault_schema_errors(
    data: dict[str, object], field: str, message: str
) -> None:
    result = validate_event(
        {"name": "spike.worker_failure", "data": data},
        event_id="evt_bad",
        now=FIXED_NOW,
    )

    assert is_err(result)
    assert result.error.field == field
    assert message in result.error.message


def test_serialize_payload_is_stable_structured_json() -> None:
    assert serialize_payload(TemperatureThresholdExceeded("lab", 31.5, 30.0)) == (
        '{"celsius": 31.5, "sensor_id": "lab", "threshold_celsius": 30.0}'
    )


def test_validate_success_shape_carries_event_id() -> None:
    result = validate_event(temperature_event(), event_id="evt_success", now=FIXED_NOW)

    assert is_ok(result)
    assert result.value.event_id == "evt_success"
