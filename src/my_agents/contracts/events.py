from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class EventKind(StrEnum):
    TEMPERATURE_THRESHOLD_EXCEEDED = "temperature.threshold_exceeded"
    EMAIL_RECEIVED = "email.received"
    WORKER_FAULT = "spike.worker_failure"


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
    data_block: str


@dataclass(frozen=True, slots=True)
class EventReceipt:
    event_id: str
