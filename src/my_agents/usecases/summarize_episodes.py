from dataclasses import dataclass
from typing import Protocol

from flow_res import is_err

from my_agents.contracts.messages import ChatWarning
from my_agents.contracts.ports.chat_history import IChatQuery, IChatStore
from my_agents.contracts.ports.episode_summarizer import IEpisodeSummarizer
from my_agents.usecases._episode_summary import storage_warning, summarize_episode


@dataclass(frozen=True, slots=True)
class SummarizeEpisodesResponse:
    warnings: tuple[ChatWarning, ...] = ()


class SummarizeEpisodesUseCase(Protocol):
    async def execute(
        self, episode_ids: tuple[str, ...]
    ) -> SummarizeEpisodesResponse: ...


class SummarizeEpisodesHandler:
    def __init__(
        self,
        chat_store: IChatStore,
        chat_query: IChatQuery,
        summarizer: IEpisodeSummarizer,
    ) -> None:
        self._chat_store = chat_store
        self._chat_query = chat_query
        self._summarizer = summarizer

    async def execute(self, episode_ids: tuple[str, ...]) -> SummarizeEpisodesResponse:
        warnings: list[ChatWarning] = []
        for episode_id in episode_ids:
            result = self._chat_query.get_episode(episode_id)
            if is_err(result):
                warnings.append(
                    storage_warning(
                        "summary_episode_read_failed",
                        result.error,
                        "要約する会話を読み込めませんでした。",
                    )
                )
                continue
            episode = result.value
            if episode is not None:
                _, summary_warnings = await summarize_episode(
                    episode,
                    chat_store=self._chat_store,
                    chat_query=self._chat_query,
                    summarizer=self._summarizer,
                )
                warnings.extend(summary_warnings)
        return SummarizeEpisodesResponse(tuple(warnings))
