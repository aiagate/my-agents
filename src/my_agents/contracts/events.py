from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto


class EventKind(Enum):
    TEMPERATURE_THRESHOLD_EXCEEDED = auto()
    EMAIL_RECEIVED = auto()
    WORKER_FAULT = auto()


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
