from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from typing import Literal, Protocol

from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.events import EventDelivery, EventRecord, EventStatus
from my_agents.contracts.ports.chat_history import (
    ChatStorageError,
    EpisodeCloseReason,
    EpisodeRecord,
    IChatQuery,
)
from my_agents.contracts.ports.event_ledger import IEventLedger

HISTORY_EVENT_LIMIT = 100


class GetChatHistoryErrorType(Enum):
    INVALID_INDEX = auto()
    STORAGE_ERROR = auto()


@dataclass(frozen=True, slots=True)
class GetChatHistoryError:
    type: GetChatHistoryErrorType
    message: str
    display_message: str


@dataclass(frozen=True, slots=True)
class EpisodeSummaryView:
    index: int
    episode_id: str
    title: str
    overview: str
    summary: str
    started_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    close_reason: EpisodeCloseReason | None
    turn_count: int
    summarized_turn_count: int
    is_active: bool


@dataclass(frozen=True, slots=True)
class DialogueTurnView:
    turn_index: int
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChatHistoryListResponse:
    episodes: tuple[EpisodeSummaryView, ...]
    location: str


@dataclass(frozen=True, slots=True)
class EventTimelineView:
    event_id: str
    status: EventStatus
    delivery: EventDelivery
    occurred_at: datetime
    recorded_at: datetime
    summary: str
    failure_reason: str
    surfaced_at: datetime | None


@dataclass(frozen=True, slots=True)
class ChatHistoryDetailResponse:
    episode: EpisodeSummaryView
    turns: tuple[DialogueTurnView, ...]
    events: tuple[EventTimelineView, ...] = ()


class GetChatHistoryUseCase(Protocol):
    def list_episodes(self) -> Result[ChatHistoryListResponse, GetChatHistoryError]: ...

    def get_episode_detail(
        self, index: int
    ) -> Result[ChatHistoryDetailResponse, GetChatHistoryError]: ...


def _view(episode: EpisodeRecord, index: int) -> EpisodeSummaryView:
    return EpisodeSummaryView(
        index,
        episode.id,
        episode.title,
        episode.overview,
        episode.summary,
        episode.started_at,
        episode.updated_at,
        episode.closed_at,
        episode.close_reason,
        episode.turn_count,
        episode.summarized_turn_count,
        episode.closed_at is None,
    )


def _storage_error(error: ChatStorageError) -> GetChatHistoryError:
    return GetChatHistoryError(
        GetChatHistoryErrorType.STORAGE_ERROR,
        error.message,
        "会話履歴を読み込めませんでした。",
    )


def _event_view(event: EventRecord) -> EventTimelineView:
    return EventTimelineView(
        event.event_id,
        event.status,
        event.delivery,
        event.occurred_at,
        event.recorded_at,
        event.summary,
        event.failure_reason,
        event.surfaced_at,
    )


class GetChatHistoryHandler:
    def __init__(
        self, chat_query: IChatQuery, *, event_ledger: IEventLedger | None = None
    ) -> None:
        self._chat_query = chat_query
        self._event_ledger = event_ledger

    def list_episodes(self) -> Result[ChatHistoryListResponse, GetChatHistoryError]:
        result = self._chat_query.list_episodes()
        if is_err(result):
            return Err(_storage_error(result.error))
        return Ok(
            ChatHistoryListResponse(
                tuple(
                    _view(episode, index)
                    for index, episode in enumerate(result.value, 1)
                ),
                self._chat_query.location,
            )
        )

    def get_episode_detail(
        self, index: int
    ) -> Result[ChatHistoryDetailResponse, GetChatHistoryError]:
        result = self._chat_query.list_episodes()
        if is_err(result):
            return Err(_storage_error(result.error))
        if not 1 <= index <= len(result.value):
            return Err(
                GetChatHistoryError(
                    GetChatHistoryErrorType.INVALID_INDEX,
                    f"Invalid history index: {index}",
                    "一覧に表示された会話の番号を指定してください。",
                )
            )
        episode = result.value[index - 1]
        turns = self._chat_query.list_turns(episode.id)
        if is_err(turns):
            return Err(_storage_error(turns.error))
        if self._event_ledger is None:
            events: tuple[EventTimelineView, ...] = ()
        else:
            recent = self._event_ledger.list_recent(limit=HISTORY_EVENT_LIMIT)
            if is_err(recent):
                return Err(
                    GetChatHistoryError(
                        GetChatHistoryErrorType.STORAGE_ERROR,
                        recent.error.message,
                        "イベント台帳を読み込めませんでした。",
                    )
                )
            deadline = episode.closed_at
            events = tuple(
                reversed(
                    [
                        _event_view(event)
                        for event in recent.value
                        if event.recorded_at >= episode.started_at
                        and (deadline is None or event.recorded_at <= deadline)
                    ]
                )
            )
        return Ok(
            ChatHistoryDetailResponse(
                _view(episode, index),
                tuple(
                    DialogueTurnView(
                        turn.turn_index, turn.role, turn.content, turn.created_at
                    )
                    for turn in turns.value
                ),
                events,
            )
        )
