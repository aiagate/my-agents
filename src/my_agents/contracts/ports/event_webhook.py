from abc import ABC, abstractmethod
from dataclasses import dataclass

from flow_res import Result


@dataclass(frozen=True, slots=True)
class EventWebhookStartError:
    message: str
    display_message: str


class IEventWebhookServer(ABC):
    @abstractmethod
    def start(self) -> Result[int, EventWebhookStartError]: ...

    @abstractmethod
    def stop(self) -> None: ...
