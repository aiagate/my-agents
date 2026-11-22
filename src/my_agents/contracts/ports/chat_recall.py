from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from typing import Literal

from flow_res import Result


class ChatRecallErrorType(Enum):
    INVALID_INPUT = auto()
    NOT_FOUND = auto()
    STORAGE_ERROR = auto()


@dataclass(frozen=True, slots=True)
class ChatRecallError:
    type: ChatRecallErrorType
    message: str


@dataclass(frozen=True, slots=True)
class RecalledTurn:
    turn_index: int
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RecalledEpisode:
    episode_id: str
    title: str
    overview: str
    summary: str
    started_at: datetime
    updated_at: datetime
    turns: tuple[RecalledTurn, ...]
    total_turn_count: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ChatSearchHit:
    episode_id: str
    title: str
    overview: str
    started_at: datetime
    updated_at: datetime
    turn_count: int


class IChatRecall(ABC):
    @abstractmethod
    def recall_episode(
        self, episode_id: str, *, limit: int = 40
    ) -> Result[RecalledEpisode, ChatRecallError]: ...

    @abstractmethod
    def search_episodes(
        self, query: str, *, limit: int = 10
    ) -> Result[tuple[ChatSearchHit, ...], ChatRecallError]: ...
