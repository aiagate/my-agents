from abc import ABC, abstractmethod

from my_agents.contracts.events import InboundEvent


class IEventScheduler(ABC):
    @abstractmethod
    def schedule(self, event: InboundEvent, /) -> None: ...
