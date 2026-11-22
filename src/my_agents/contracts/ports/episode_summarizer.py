from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto

from flow_res import Result

from my_agents.contracts.messages import ChatMessage


@dataclass(frozen=True, slots=True)
class EpisodeSummaryRequest:
    previous_title: str
    previous_summary: str
    turns: tuple[ChatMessage, ...]


@dataclass(frozen=True, slots=True)
class EpisodeSummaryOutcome:
    title: str
    overview: str
    summary: str


class EpisodeSummarizerErrorType(Enum):
    API_ERROR = auto()
    UNEXPECTED_BEHAVIOR = auto()
    UNEXPECTED = auto()


@dataclass(frozen=True, slots=True)
class EpisodeSummarizerError:
    type: EpisodeSummarizerErrorType
    message: str
    detail: str | None = None


class IEpisodeSummarizer(ABC):
    @abstractmethod
    async def summarize(
        self, request: EpisodeSummaryRequest
    ) -> Result[EpisodeSummaryOutcome, EpisodeSummarizerError]: ...
