from flow_res import is_err

from my_agents.contracts.messages import ChatMessage, ChatWarning
from my_agents.contracts.ports.chat_history import (
    ChatStorageError,
    EpisodeRecord,
    IChatQuery,
    IChatStore,
)
from my_agents.contracts.ports.episode_summarizer import (
    EpisodeSummaryRequest,
    IEpisodeSummarizer,
)
from my_agents.domain.chat_history import normalize_summary_fields


def storage_warning(
    event: str, error: ChatStorageError, display_message: str
) -> ChatWarning:
    return ChatWarning(event, error.type.name, error.message, display_message)


async def summarize_episode(
    episode: EpisodeRecord,
    *,
    chat_store: IChatStore,
    chat_query: IChatQuery,
    summarizer: IEpisodeSummarizer,
) -> tuple[EpisodeRecord, tuple[ChatWarning, ...]]:
    if episode.turn_count <= episode.summarized_turn_count:
        return episode, ()
    turns = chat_query.list_turns(episode.id)
    if is_err(turns):
        return episode, (
            storage_warning(
                "summary_turns_read_failed",
                turns.error,
                "要約する会話を読み込めませんでした。",
            ),
        )
    result = await summarizer.summarize(
        EpisodeSummaryRequest(
            episode.title,
            episode.summary,
            tuple(
                ChatMessage(turn.role, turn.content)
                for turn in turns.value[
                    episode.summarized_turn_count : episode.turn_count
                ]
            ),
        )
    )
    if is_err(result):
        error = result.error
        return episode, (
            ChatWarning(
                "episode_summary_failed",
                error.type.name,
                error.message,
                "会話を要約できませんでした。",
                error.detail,
            ),
        )
    title, overview, summary = normalize_summary_fields(
        result.value.title,
        result.value.overview,
        result.value.summary,
        fallback_title=episode.title,
    )
    if not summary:
        return episode, (
            ChatWarning(
                event="episode_summary_empty",
                error_type="EMPTY_SUMMARY",
                message="Episode summarizer returned an empty summary.",
                display_message="会話の要約が空だったため既存の要約を保持しました。",
            ),
        )
    saved = chat_store.update_episode_summary(
        episode.id,
        title=title,
        overview=overview,
        summary=summary,
        summarized_turn_count=episode.turn_count,
    )
    if is_err(saved):
        return episode, (
            storage_warning(
                "episode_summary_write_failed",
                saved.error,
                "会話の要約を保存できませんでした。",
            ),
        )
    return saved.value, ()
