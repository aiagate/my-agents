from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class EventKind(StrEnum):
    TEMPERATURE_THRESHOLD_EXCEEDED = "temperature.threshold_exceeded"
    EMAIL_RECEIVED = "email.received"
    WORKER_FAULT = "spike.worker_failure"


class EventStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class EventDelivery(StrEnum):
    PROACTIVE = "proactive"
    AMBIENT = "ambient"
    SILENT = "silent"


@dataclass(frozen=True, slots=True)
class TemperatureThresholdExceeded:
    sensor_id: str
    celsius: float
    threshold_celsius: float


@dataclass(frozen=True, slots=True)
class EmailReceived:
    sender: str
    subject: str
    body: str


@dataclass(frozen=True, slots=True)
class WorkerFault:
    reason: str


type EventPayload = TemperatureThresholdExceeded | EmailReceived | WorkerFault


@dataclass(frozen=True, slots=True)
class InboundEvent:
    event_id: str
    name: str
    kind: EventKind
    occurred_at: datetime
    payload: EventPayload


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_id: str
    name: str
    occurred_at: datetime
    recorded_at: datetime
    status: EventStatus
    delivery: EventDelivery
    payload_json: str
    summary: str
    findings: tuple[str, ...]
    recommended_action: str
    failure_reason: str
    surfaced_at: datetime | None


@dataclass(frozen=True, slots=True)
class EventReceipt:
    event_id: str
