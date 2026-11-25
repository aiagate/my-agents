from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from flow_res import Result


@dataclass(frozen=True, slots=True)
class EventChatRecordError:
    message: str


class IEventChatRecorder(ABC):
    @abstractmethod
    def record(
        self, event_id: str, content: str, *, occurred_at: datetime
    ) -> Result[None, EventChatRecordError]: ...
