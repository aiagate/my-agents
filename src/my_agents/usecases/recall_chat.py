from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.ports.chat_history import IChatQuery
from my_agents.contracts.ports.chat_recall import (
    ChatRecallError,
    ChatRecallErrorType,
    ChatSearchHit,
    IChatRecall,
    RecalledEpisode,
    RecalledTurn,
)


class RecallChatHandler(IChatRecall):
    def __init__(self, chat_query: IChatQuery) -> None:
        self._chat_query = chat_query

    def recall_episode(
        self, episode_id: str, *, limit: int = 40
    ) -> Result[RecalledEpisode, ChatRecallError]:
        episode_id = episode_id.strip()
        if not episode_id or limit <= 0:
            return Err(
                ChatRecallError(
                    ChatRecallErrorType.INVALID_INPUT,
                    "Episode ID and a positive limit are required.",
                )
            )
        episode_result = self._chat_query.get_episode(episode_id)
        if is_err(episode_result):
            return Err(
                ChatRecallError(
                    ChatRecallErrorType.STORAGE_ERROR, episode_result.error.message
                )
            )
        episode = episode_result.value
        if episode is None:
            return Err(
                ChatRecallError(
                    ChatRecallErrorType.NOT_FOUND, f"Episode not found: {episode_id}"
                )
            )
        turns = self._chat_query.list_turns(episode_id, limit=limit)
        if is_err(turns):
            return Err(
                ChatRecallError(ChatRecallErrorType.STORAGE_ERROR, turns.error.message)
            )
        return Ok(
            RecalledEpisode(
                episode.id,
                episode.title,
                episode.overview,
                episode.summary,
                episode.started_at,
                episode.updated_at,
                tuple(
                    RecalledTurn(
                        turn.turn_index, turn.role, turn.content, turn.created_at
                    )
                    for turn in turns.value
                ),
                episode.turn_count,
                len(turns.value) < episode.turn_count,
            )
        )

    def search_episodes(
        self, query: str, *, limit: int = 10
    ) -> Result[tuple[ChatSearchHit, ...], ChatRecallError]:
        query = query.strip()
        if not query or limit <= 0:
            return Err(
                ChatRecallError(
                    ChatRecallErrorType.INVALID_INPUT,
                    "Search text and a positive limit are required.",
                )
            )
        result = self._chat_query.search_episodes(query, limit=limit)
        if is_err(result):
            return Err(
                ChatRecallError(ChatRecallErrorType.STORAGE_ERROR, result.error.message)
            )
        return Ok(
            tuple(
                ChatSearchHit(
                    episode.id,
                    episode.title,
                    episode.overview,
                    episode.started_at,
                    episode.updated_at,
                    episode.turn_count,
                )
                for episode in result.value
            )
        )
