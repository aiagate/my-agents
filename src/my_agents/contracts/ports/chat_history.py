from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from typing import Literal

from flow_res import Result

type EpisodeCloseReason = Literal["clear", "rollover"]


class ChatStorageErrorType(Enum):
    READ_FAILED = auto()
    WRITE_FAILED = auto()
    NOT_FOUND = auto()
    CORRUPT_DATA = auto()


@dataclass(frozen=True, slots=True)
class ChatStorageError:
    type: ChatStorageErrorType
    message: str


@dataclass(frozen=True, slots=True)
class EpisodeRecord:
    id: str
    title: str
    overview: str
    summary: str
    started_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    close_reason: EpisodeCloseReason | None
    turn_count: int
    summarized_turn_count: int


@dataclass(frozen=True, slots=True)
class DialogueTurnRecord:
    episode_id: str
    turn_index: int
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NewDialogueTurn:
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class IChatStore(ABC):
    @property
    @abstractmethod
    def location(self) -> str: ...

    @abstractmethod
    def create_episode(
        self, episode_id: str, *, title: str, started_at: datetime
    ) -> Result[EpisodeRecord, ChatStorageError]: ...

    @abstractmethod
    def record_turns(
        self,
        episode_id: str | None,
        turns: Sequence[NewDialogueTurn],
        *,
        title: str,
        updated_at: datetime,
        close_episode_id: str | None = None,
    ) -> Result[EpisodeRecord, ChatStorageError]: ...

    @abstractmethod
    def append_turns(
        self, episode_id: str, turns: Sequence[NewDialogueTurn], *, updated_at: datetime
    ) -> Result[EpisodeRecord, ChatStorageError]: ...

    @abstractmethod
    def update_episode_summary(
        self,
        episode_id: str,
        *,
        title: str,
        overview: str,
        summary: str,
        summarized_turn_count: int,
    ) -> Result[EpisodeRecord, ChatStorageError]: ...

    @abstractmethod
    def close_episode(
        self, episode_id: str, *, closed_at: datetime, reason: EpisodeCloseReason
    ) -> Result[EpisodeRecord, ChatStorageError]: ...


class IChatQuery(ABC):
    @property
    @abstractmethod
    def location(self) -> str: ...

    @abstractmethod
    def get_latest_episode(self) -> Result[EpisodeRecord | None, ChatStorageError]: ...

    @abstractmethod
    def get_open_episode(self) -> Result[EpisodeRecord | None, ChatStorageError]: ...

    @abstractmethod
    def get_previous_episode(
        self, episode_id: str
    ) -> Result[EpisodeRecord | None, ChatStorageError]: ...

    @abstractmethod
    def get_episode(
        self, episode_id: str
    ) -> Result[EpisodeRecord | None, ChatStorageError]: ...

    @abstractmethod
    def list_episodes(
        self, *, limit: int | None = None
    ) -> Result[tuple[EpisodeRecord, ...], ChatStorageError]: ...

    @abstractmethod
    def list_turns(
        self, episode_id: str, *, limit: int | None = None
    ) -> Result[tuple[DialogueTurnRecord, ...], ChatStorageError]: ...

    @abstractmethod
    def search_episodes(
        self, query: str, *, limit: int = 10
    ) -> Result[tuple[EpisodeRecord, ...], ChatStorageError]: ...
