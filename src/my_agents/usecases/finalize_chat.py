from dataclasses import dataclass
from typing import Protocol

from flow_res import is_err

from my_agents.contracts.messages import ChatWarning
from my_agents.contracts.ports.chat_history import IChatQuery, IChatStore
from my_agents.contracts.ports.episode_summarizer import IEpisodeSummarizer
from my_agents.usecases._episode_summary import storage_warning, summarize_episode


@dataclass(frozen=True, slots=True)
class FinalizeChatResponse:
    warnings: tuple[ChatWarning, ...] = ()


class FinalizeChatUseCase(Protocol):
    async def execute(self) -> FinalizeChatResponse: ...


class FinalizeChatHandler:
    def __init__(
        self,
        chat_store: IChatStore,
        chat_query: IChatQuery,
        summarizer: IEpisodeSummarizer,
    ) -> None:
        self._chat_store = chat_store
        self._chat_query = chat_query
        self._summarizer = summarizer

    async def execute(self) -> FinalizeChatResponse:
        latest = self._chat_query.get_open_episode()
        if is_err(latest):
            return FinalizeChatResponse(
                (
                    storage_warning(
                        "finalize_chat_read_failed",
                        latest.error,
                        "終了時に現在の会話を読み込めませんでした。",
                    ),
                )
            )
        episode = latest.value
        if episode is None or episode.closed_at is not None:
            return FinalizeChatResponse()
        _, warnings = await summarize_episode(
            episode,
            chat_store=self._chat_store,
            chat_query=self._chat_query,
            summarizer=self._summarizer,
        )
        return FinalizeChatResponse(warnings)
