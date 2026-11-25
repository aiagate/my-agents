from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EventNotification:
    event_id: str
    content: str
    failed: bool


class IEventNotifier(ABC):
    @abstractmethod
    def notify(self, notification: EventNotification, /) -> None: ...
