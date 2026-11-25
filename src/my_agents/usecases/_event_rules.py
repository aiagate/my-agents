import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Annotated, assert_never

from flow_res import Err, Ok, Result, is_err
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticCustomError

from my_agents.contracts.events import (
    EmailReceived,
    EventKind,
    EventPayload,
    InboundEvent,
    TemperatureThresholdExceeded,
    WorkerFault,
)
from my_agents.domain.event_context import EventDeliveryKind


@dataclass(frozen=True, slots=True)
class EventAnalysis:
    summary: str
    findings: tuple[str, ...]
    recommended_action: str
    delivery: EventDeliveryKind


@dataclass(frozen=True, slots=True)
class EventAnalysisError:
    message: str
    display_message: str


EVENT_FAILURE_REASON_MAX_CHARS = 500
_FIELD_MESSAGES = {
    "string_type": "{field} must be a string.",
    "string_too_long": "{field} must be at most {max_length} characters.",
    "float_type": "{field} must be a number.",
    "finite_number": "{field} must be finite.",
    "value_error": "{field} must not be empty.",
}


@dataclass(frozen=True, slots=True)
class EventValidationError:
    field: str
    message: str


def object_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def _payload_mapping(
    value: object,
) -> Result[dict[str, object], EventValidationError]:
    mapping = object_mapping(value)
    if mapping is None:
        return Err(EventValidationError("data", "Event data must be a JSON object."))
    return Ok(mapping)


def _require_text(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        raise ValueError
    return value


class _Payload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _exact_keys(cls, value: object) -> object:
        if isinstance(value, dict) and set(value) != set(cls.model_fields):
            raise PydanticCustomError(
                "expected_keys",
                "Expected keys {expected}, received {received}.",
                {
                    "expected": sorted(cls.model_fields),
                    "received": sorted(value),
                },
            )
        return value


class _TemperaturePayload(_Payload):
    sensor_id: Annotated[str, Field(max_length=80), BeforeValidator(_require_text)]
    celsius: Annotated[float, Field(allow_inf_nan=False)]
    threshold_celsius: Annotated[float, Field(allow_inf_nan=False)]


class _EmailPayload(_Payload):
    sender: Annotated[str, Field(max_length=200), BeforeValidator(_require_text)]
    subject: Annotated[str, Field(max_length=200), BeforeValidator(_require_text)]
    body: Annotated[str, Field(max_length=4000)]


class _WorkerFaultPayload(_Payload):
    reason: Annotated[
        str,
        Field(max_length=EVENT_FAILURE_REASON_MAX_CHARS),
        BeforeValidator(_require_text),
    ]


def _validation_error(exc: ValidationError) -> EventValidationError:
    error = exc.errors()[0]
    if error["type"] == "expected_keys":
        return EventValidationError("data", str(error["msg"]))
    field = str(error["loc"][-1])
    context = error.get("ctx") or {}
    return EventValidationError(
        field, _FIELD_MESSAGES[error["type"]].format(field=field, **context)
    )


def serialize_payload(payload: EventPayload) -> str:
    return json.dumps(asdict(payload), ensure_ascii=False, sort_keys=True)


def _occurred_at(
    raw: Mapping[str, object], *, now: datetime
) -> Result[datetime, EventValidationError]:
    value = raw.get("timestamp")
    if value is None:
        return Ok(now)
    if not isinstance(value, str):
        return Err(EventValidationError("timestamp", "timestamp must be a string."))
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return Err(
            EventValidationError(
                "timestamp", "timestamp must be an ISO 8601 date-time."
            )
        )
    if parsed.utcoffset() is None:
        return Err(
            EventValidationError("timestamp", "timestamp must include a UTC offset.")
        )
    return Ok(parsed.astimezone(UTC))


def validate_event(
    raw: Mapping[str, object], *, event_id: str, now: datetime
) -> Result[InboundEvent, EventValidationError]:
    unknown = set(raw) - {"name", "data", "timestamp"}
    if unknown:
        return Err(
            EventValidationError("body", f"Unknown top-level keys: {sorted(unknown)}.")
        )
    name_value = raw.get("name")
    if not isinstance(name_value, str) or not name_value.strip():
        return Err(EventValidationError("name", "name must be a non-empty string."))
    try:
        kind = EventKind(name_value)
    except ValueError:
        return Err(
            EventValidationError(
                "name",
                f"Unsupported event name: {name_value}. Supported names: "
                f"{[candidate.value for candidate in EventKind]}.",
            )
        )
    data = _payload_mapping(raw.get("data"))
    if is_err(data):
        return data
    occurred = _occurred_at(raw, now=now)
    if is_err(occurred):
        return occurred
    try:
        payload: EventPayload
        match kind:
            case EventKind.TEMPERATURE_THRESHOLD_EXCEEDED:
                temperature = _TemperaturePayload.model_validate(data.value)
                payload = TemperatureThresholdExceeded(
                    temperature.sensor_id,
                    temperature.celsius,
                    temperature.threshold_celsius,
                )
            case EventKind.EMAIL_RECEIVED:
                email = _EmailPayload.model_validate(data.value)
                payload = EmailReceived(email.sender, email.subject, email.body)
            case EventKind.WORKER_FAULT:
                worker = _WorkerFaultPayload.model_validate(data.value)
                payload = WorkerFault(worker.reason)
            case _:
                assert_never(kind)
    except ValidationError as exc:
        return Err(_validation_error(exc))
    return Ok(
        InboundEvent(
            event_id=event_id,
            name=name_value,
            kind=kind,
            occurred_at=occurred.value,
            payload=payload,
        )
    )
