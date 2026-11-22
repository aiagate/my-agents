from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto
from typing import Protocol

from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.messages import ChatWarning
from my_agents.contracts.ports.chat_history import IChatQuery, IChatStore
from my_agents.contracts.ports.episode_summarizer import IEpisodeSummarizer
from my_agents.usecases._episode_summary import summarize_episode


class ClearChatErrorType(Enum):
    STORAGE_ERROR = auto()


@dataclass(frozen=True, slots=True)
class ClearChatError:
    type: ClearChatErrorType
    message: str
    display_message: str
    warnings: tuple[ChatWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class ClearChatResponse:
    warnings: tuple[ChatWarning, ...] = ()


class ClearChatUseCase(Protocol):
    async def execute(self) -> Result[ClearChatResponse, ClearChatError]: ...


class ClearChatHandler:
    def __init__(
        self,
        chat_store: IChatStore,
        chat_query: IChatQuery,
        summarizer: IEpisodeSummarizer,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._chat_store = chat_store
        self._chat_query = chat_query
        self._summarizer = summarizer
        self._clock = clock

    async def execute(self) -> Result[ClearChatResponse, ClearChatError]:
        opened = self._chat_query.get_open_episode()
        if is_err(opened):
            return Err(
                ClearChatError(
                    ClearChatErrorType.STORAGE_ERROR,
                    opened.error.message,
                    "現在の会話を読み込めませんでした。",
                )
            )
        episode = opened.value
        if episode is None:
            latest = self._chat_query.get_latest_episode()
            if is_err(latest):
                return Err(
                    ClearChatError(
                        ClearChatErrorType.STORAGE_ERROR,
                        latest.error.message,
                        "現在の会話を読み込めませんでした。",
                    )
                )
            if latest.value is not None and latest.value.close_reason == "rollover":
                closed = self._chat_store.close_episode(
                    latest.value.id, closed_at=self._clock(), reason="clear"
                )
                if is_err(closed):
                    return Err(
                        ClearChatError(
                            ClearChatErrorType.STORAGE_ERROR,
                            closed.error.message,
                            "現在の会話を閉じられませんでした。",
                        )
                    )
            return Ok(ClearChatResponse())
        _, warnings = await summarize_episode(
            episode,
            chat_store=self._chat_store,
            chat_query=self._chat_query,
            summarizer=self._summarizer,
        )
        closed = self._chat_store.close_episode(
            episode.id, closed_at=self._clock(), reason="clear"
        )
        if is_err(closed):
            return Err(
                ClearChatError(
                    ClearChatErrorType.STORAGE_ERROR,
                    closed.error.message,
                    "現在の会話を閉じられませんでした。",
                    warnings,
                )
            )
        return Ok(ClearChatResponse(warnings))
