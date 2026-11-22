from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from enum import Enum, auto
from typing import Protocol, assert_never

from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.messages import ChatMessage, ChatWarning
from my_agents.contracts.ports.agent_runner import (
    AgentRunnerError,
    AgentRunnerErrorType,
    IAgentRunner,
)
from my_agents.contracts.ports.chat_history import (
    EpisodeCloseReason,
    EpisodeRecord,
    IChatQuery,
    IChatStore,
    NewDialogueTurn,
)
from my_agents.contracts.ports.episode_summarizer import IEpisodeSummarizer
from my_agents.domain.chat_history import (
    DEFAULT_EPISODE_GAP_MINUTES,
    DEFAULT_EPISODE_MAX_MESSAGES,
    DEFAULT_RECENT_MESSAGES_LIMIT,
    DEFAULT_SUMMARY_EVERY_MESSAGES,
    DEFAULT_SUMMARY_INDEX_CHAR_BUDGET,
    ConversationContext,
    EpisodeIndexEntry,
    LatestTopicSummary,
    make_fallback_title,
    should_rollover_episode,
    should_summarize_episode,
)
from my_agents.domain.context_assembler import assemble_instructions
from my_agents.usecases._episode_summary import storage_warning, summarize_episode
from my_agents.usecases.get_core_profile import (
    GetCoreProfileUseCase,
)


class SendMessageErrorType(Enum):
    EMPTY_INPUT = auto()
    CORE_PROFILE_ERROR = auto()
    API_ERROR = auto()
    UNEXPECTED_BEHAVIOR = auto()
    USAGE_LIMIT_EXCEEDED = auto()
    UNEXPECTED = auto()


@dataclass(frozen=True, slots=True)
class SendMessageError:
    type: SendMessageErrorType
    message: str
    display_message: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class SendMessageRequest:
    user_input: str
    history: tuple[ChatMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class SendMessageResponse:
    reply: str
    history: tuple[ChatMessage, ...]
    core_memory_updates: tuple[str, ...] = ()
    warnings: tuple[ChatWarning, ...] = ()
    pending_summary_episode_ids: tuple[str, ...] = ()


class SendMessageUseCase(Protocol):
    async def execute(
        self,
        request: SendMessageRequest,
    ) -> Result[SendMessageResponse, SendMessageError]: ...


def _to_send_message_error(error: AgentRunnerError) -> SendMessageError:
    match error.type:
        case AgentRunnerErrorType.API_ERROR:
            return SendMessageError(
                type=SendMessageErrorType.API_ERROR,
                message=error.message,
                display_message=(
                    "APIとの通信でエラーが発生しました。"
                    "しばらくしてから再度お試しください。"
                ),
                detail=error.detail,
            )
        case AgentRunnerErrorType.UNEXPECTED_BEHAVIOR:
            return SendMessageError(
                type=SendMessageErrorType.UNEXPECTED_BEHAVIOR,
                message=error.message,
                display_message="モデルから予期しない応答が返されました。",
                detail=error.detail,
            )
        case AgentRunnerErrorType.USAGE_LIMIT_EXCEEDED:
            return SendMessageError(
                type=SendMessageErrorType.USAGE_LIMIT_EXCEEDED,
                message=error.message,
                display_message=(
                    "利用上限に達しました。しばらくしてから再度お試しください。"
                ),
                detail=error.detail,
            )
        case AgentRunnerErrorType.UNEXPECTED:
            return SendMessageError(
                type=SendMessageErrorType.UNEXPECTED,
                message=error.message,
                display_message="予期しないエラーが発生しました。",
                detail=error.detail,
            )
        case _:
            assert_never(error.type)


class SendMessageHandler:
    def __init__(
        self,
        agent_runner: IAgentRunner,
        get_core_profile: GetCoreProfileUseCase,
        *,
        chat_store: IChatStore | None = None,
        chat_query: IChatQuery | None = None,
        episode_summarizer: IEpisodeSummarizer | None = None,
        display_tz: tzinfo | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        summary_index_char_budget: int = DEFAULT_SUMMARY_INDEX_CHAR_BUDGET,
        summary_every_messages: int = DEFAULT_SUMMARY_EVERY_MESSAGES,
        recent_messages_limit: int = DEFAULT_RECENT_MESSAGES_LIMIT,
        episode_max_messages: int = DEFAULT_EPISODE_MAX_MESSAGES,
        episode_gap_minutes: int = DEFAULT_EPISODE_GAP_MINUTES,
    ) -> None:
        self._agent_runner = agent_runner
        self._get_core_profile = get_core_profile
        self._clock = clock
        self._display_tz = display_tz
        self._chat = (
            None
            if chat_store is None or chat_query is None
            else _PersistentChat(
                chat_store,
                chat_query,
                episode_summarizer,
                summary_index_char_budget,
                summary_every_messages,
                recent_messages_limit,
                episode_max_messages,
                episode_gap_minutes,
            )
        )

    async def execute(
        self,
        request: SendMessageRequest,
    ) -> Result[SendMessageResponse, SendMessageError]:
        normalized_input = request.user_input.strip()
        if not normalized_input:
            return Err(
                SendMessageError(
                    type=SendMessageErrorType.EMPTY_INPUT,
                    message="User input must not be empty or whitespace only.",
                    display_message="メッセージを入力してください。",
                )
            )

        profile_result = self._get_core_profile.execute()
        if is_err(profile_result):
            return Err(
                SendMessageError(
                    type=SendMessageErrorType.CORE_PROFILE_ERROR,
                    message=profile_result.error.message,
                    display_message=profile_result.error.display_message,
                )
            )

        history = request.history
        now = self._clock()
        prepared = (
            None if self._chat is None else await self._chat.prepare(history, now)
        )
        if prepared is not None:
            history = prepared.history
        result = await self._agent_runner.run(
            normalized_input,
            history=history,
            instructions=assemble_instructions(
                profile_result.value.profile,
                conversation_context=None if prepared is None else prepared.context,
                recall_tools_enabled=self._chat is not None,
                display_tz=self._display_tz,
            ),
        )
        if is_err(result):
            return Err(_to_send_message_error(result.error))

        reply = result.value
        user = ChatMessage(role="user", content=normalized_input)
        assistant = ChatMessage(role="assistant", content=reply.content)
        next_history = (*history, user, assistant)
        warnings: tuple[ChatWarning, ...] = ()
        summary_ids: tuple[str, ...] = ()
        if self._chat is not None and prepared is not None:
            next_history = self._chat.recent_history(next_history)
            save_warnings, summary_ids = self._chat.save(prepared, user, assistant, now)
            warnings = (*prepared.warnings, *save_warnings)
        return Ok(
            SendMessageResponse(
                reply.content,
                next_history,
                reply.core_memory_updates,
                warnings,
                summary_ids,
            )
        )


def _latest_summary(episode: EpisodeRecord) -> LatestTopicSummary | None:
    if not episode.summary:
        return None
    return LatestTopicSummary(
        episode.id,
        episode.title,
        episode.summary,
        episode.started_at,
        episode.updated_at,
    )


@dataclass(frozen=True, slots=True)
class _ChatTarget:
    episode_id: str | None = None
    preceding_episode_id: str | None = None
    preceding_close_reason: EpisodeCloseReason | None = None


@dataclass(frozen=True, slots=True)
class _PreparedChat:
    history: tuple[ChatMessage, ...]
    context: ConversationContext
    target: _ChatTarget
    rollover: EpisodeRecord | None
    warnings: tuple[ChatWarning, ...]


class _PersistentChat:
    def __init__(
        self,
        store: IChatStore,
        query: IChatQuery,
        summarizer: IEpisodeSummarizer | None,
        index_budget: int,
        summary_interval: int,
        recent_limit: int,
        episode_max: int,
        episode_gap: int,
    ) -> None:
        self._store = store
        self._query = query
        self._summarizer = summarizer
        self._index_budget = index_budget
        self._summary_interval = summary_interval
        self._recent_limit = recent_limit
        self._episode_max = episode_max
        self._episode_gap = episode_gap
        self._pending_target = _ChatTarget()
        self._pending_turns: tuple[NewDialogueTurn, ...] = ()

    def recent_history(
        self, history: tuple[ChatMessage, ...]
    ) -> tuple[ChatMessage, ...]:
        return history[-self._recent_limit :]

    async def prepare(
        self, history: tuple[ChatMessage, ...], now: datetime
    ) -> _PreparedChat:
        if not history:
            self._pending_turns = ()
            self._pending_target = _ChatTarget()

        warnings: list[ChatWarning] = []
        target = self._pending_target
        rollover = None
        topic = None
        stored_history = None
        opened = self._query.get_open_episode()
        if is_err(opened):
            warnings.append(
                storage_warning(
                    "latest_episode_read_failed",
                    opened.error,
                    "現在の会話を読み込めませんでした。",
                )
            )
        else:
            episode = opened.value
            if episode is None:
                previous = self._query.get_latest_episode()
                if is_err(previous):
                    warnings.append(
                        storage_warning(
                            "latest_episode_read_failed",
                            previous.error,
                            "現在の会話を読み込めませんでした。",
                        )
                    )
                else:
                    preceding = previous.value
                    target = _ChatTarget(
                        preceding_episode_id=None
                        if preceding is None
                        else preceding.id,
                        preceding_close_reason=None
                        if preceding is None
                        else preceding.close_reason,
                    )
                    if preceding is not None or self._pending_turns:
                        stored_history = ()
                    if preceding is not None and preceding.close_reason == "rollover":
                        topic = _latest_summary(preceding)
            elif should_rollover_episode(
                closed_at=episode.closed_at,
                updated_at=episode.updated_at,
                turn_count=episode.turn_count,
                now=now,
                max_messages=self._episode_max,
                gap_minutes=self._episode_gap,
            ):
                target = _ChatTarget(preceding_episode_id=episode.id)
                stored_history = ()
                if (
                    episode.turn_count > episode.summarized_turn_count
                    and self._summarizer is not None
                ):
                    episode, summary_warnings = await summarize_episode(
                        episode,
                        chat_store=self._store,
                        chat_query=self._query,
                        summarizer=self._summarizer,
                    )
                    warnings.extend(summary_warnings)
                rollover = episode
                topic = _latest_summary(episode)
            else:
                target = _ChatTarget(episode_id=episode.id)
                topic = _latest_summary(episode)
                if topic is None:
                    previous = self._query.get_previous_episode(episode.id)
                    if is_err(previous):
                        warnings.append(
                            storage_warning(
                                "previous_episode_read_failed",
                                previous.error,
                                "前の会話を読み込めませんでした。",
                            )
                        )
                    elif (
                        previous.value is not None
                        and previous.value.close_reason == "rollover"
                    ):
                        topic = _latest_summary(previous.value)
                turns = self._query.list_turns(episode.id, limit=self._recent_limit)
                if is_err(turns):
                    warnings.append(
                        storage_warning(
                            "recent_turns_read_failed",
                            turns.error,
                            "直近の会話を読み込めませんでした。",
                        )
                    )
                else:
                    stored_history = tuple(
                        ChatMessage(turn.role, turn.content) for turn in turns.value
                    )

        if target != self._pending_target:
            self._pending_turns = ()
            self._pending_target = target
        if stored_history is not None:
            history = (
                *stored_history,
                *(ChatMessage(turn.role, turn.content) for turn in self._pending_turns),
            )

        entries = ()
        episodes = self._query.list_episodes(limit=None)
        if is_err(episodes):
            warnings.append(
                storage_warning(
                    "episode_index_read_failed",
                    episodes.error,
                    "過去の会話サマリー一覧を読み込めませんでした。",
                )
            )
        else:
            excluded = {target.episode_id}
            if topic is not None:
                excluded.add(topic.episode_id)
            entries = tuple(
                EpisodeIndexEntry(
                    item.id,
                    item.title,
                    item.overview,
                    item.started_at,
                    item.updated_at,
                    item.turn_count,
                )
                for item in episodes.value
                if item.id not in excluded
            )
        return _PreparedChat(
            self.recent_history(history),
            ConversationContext(topic, entries, self._index_budget),
            target,
            rollover,
            tuple(warnings),
        )

    def save(
        self,
        prepared: _PreparedChat,
        user: ChatMessage,
        assistant: ChatMessage,
        now: datetime,
    ) -> tuple[tuple[ChatWarning, ...], tuple[str, ...]]:
        rollover = prepared.rollover
        pending = self._pending_turns if self._pending_target == prepared.target else ()
        turns = (
            *pending,
            NewDialogueTurn(user.role, user.content, now),
            NewDialogueTurn(assistant.role, assistant.content, now),
        )
        saved = self._store.record_turns(
            prepared.target.episode_id,
            turns,
            title=make_fallback_title(user.content),
            updated_at=now,
            close_episode_id=None if rollover is None else rollover.id,
        )
        if is_err(saved):
            self._pending_target = prepared.target
            self._pending_turns = turns[-self._recent_limit :]
            return (
                storage_warning(
                    "dialogue_turns_write_failed",
                    saved.error,
                    "会話を保存できませんでした。",
                ),
            ), ()
        self._pending_turns = ()
        summary_ids: list[str] = []
        if (
            rollover is not None
            and rollover.turn_count > rollover.summarized_turn_count
        ):
            summary_ids.append(rollover.id)
        if should_summarize_episode(
            turn_count=saved.value.turn_count,
            summarized_turn_count=saved.value.summarized_turn_count,
            summary_every_messages=self._summary_interval,
        ):
            summary_ids.append(saved.value.id)
        return (), tuple(summary_ids)
