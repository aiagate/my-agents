import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, assert_never
from uuid import uuid4

from flow_res import Err, Ok, Result, is_err

from my_agents.core.context import AssembledContext, assemble_context
from my_agents.core.events import (
    decide_merge,
    sanitize_single_line,
    validate_envelope,
    validate_proactive_message,
)
from my_agents.core.memory import merge_candidates, should_summarize
from my_agents.core.types import (
    AppError,
    AppErrorKind,
    CharacterPersona,
    DeliveryMode,
    DialogueTurn,
    EventBrief,
    EventDelivery,
    EventEnvelope,
    EventReceipt,
    EventRecord,
    EventStatus,
    MemoryBlocks,
    MemoryStatus,
    TimelineItem,
    TimelineKind,
    TurnOrigin,
    TurnOutcome,
)
from my_agents.shell.llm import ChatReply, SummaryOutput
from my_agents.shell.logger import IAppLogger, LogEntry, LogLevel
from my_agents.shell.settings import Settings
from my_agents.shell.store import Store, TurnEntry


class EngineLlm(Protocol):
    async def chat(
        self,
        conversation_id: str,
        user_text: str,
        history: tuple[DialogueTurn, ...],
        context: AssembledContext,
    ) -> Result[ChatReply, AppError]: ...

    async def distill(
        self, envelope: EventEnvelope
    ) -> Result[EventBrief, AppError]: ...

    async def compose(
        self, brief: EventBrief, context: AssembledContext
    ) -> Result[str, AppError]: ...

    async def summarize(
        self, turns: tuple[DialogueTurn, ...], previous_summary: str
    ) -> Result[SummaryOutput, AppError]: ...


@dataclass(frozen=True, slots=True)
class CurateOutcome:
    summarized: int
    promoted: int
    rejected: int


class CharacterEngine:
    def __init__(
        self,
        store: Store,
        llm: EngineLlm,
        settings: Settings,
        persona: CharacterPersona,
        logger: IAppLogger,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._llm = llm
        self._settings = settings
        self._persona = persona
        self._logger = logger
        self._clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}
        self._subscribers: dict[str, set[asyncio.Queue[TimelineItem]]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def _lock(self, conversation_id: str) -> asyncio.Lock:
        return self._locks.setdefault(conversation_id, asyncio.Lock())

    def _publish(self, item: TimelineItem) -> None:
        for queue in self._subscribers.get(item.conversation_id, set()):
            queue.put_nowait(item)

    async def subscribe(self, conversation_id: str) -> AsyncGenerator[TimelineItem]:
        queue: asyncio.Queue[TimelineItem] = asyncio.Queue()
        self._subscribers.setdefault(conversation_id, set()).add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            group = self._subscribers.get(conversation_id)
            if group is not None:
                group.discard(queue)
                if not group:
                    self._subscribers.pop(conversation_id, None)

    async def _context(
        self, conversation_id: str, now: datetime
    ) -> Result[AssembledContext, AppError]:
        memory = self._store.read_memory()
        if is_err(memory):
            return memory
        episodes = self._store.list_episodes(conversation_id, limit=20)
        if is_err(episodes):
            return episodes
        events = self._store.list_events(conversation_id, limit=10)
        if is_err(events):
            return events
        return Ok(
            assemble_context(
                self._persona,
                memory.value,
                episodes.value,
                events.value,
                now=now,
            )
        )

    async def submit_message(
        self, conversation_id: str, text: str
    ) -> Result[TurnOutcome, AppError]:
        normalized = text.strip()
        if not normalized:
            return Err(AppError(AppErrorKind.VALIDATION, "Message must not be empty."))
        async with self._lock(conversation_id):
            now = self._clock()
            for step in (
                self._store.ensure_conversation(conversation_id, now),
                self._store.bump_epoch(conversation_id, now),
            ):
                if is_err(step):
                    return step
            context = await self._context(conversation_id, now)
            if is_err(context):
                return context
            history = self._store.recent_turns(
                conversation_id, limit=self._settings.recent_turns_limit
            )
            if is_err(history):
                return history
            reply = await self._llm.chat(
                conversation_id, normalized, history.value, context.value
            )
            if is_err(reply):
                return reply
            open_episode = self._store.get_open_episode(conversation_id)
            if is_err(open_episode):
                return open_episode
            entries: tuple[TurnEntry, ...] = (
                ("user", TurnOrigin.USER, normalized, None),
                ("assistant", TurnOrigin.REPLY, reply.value.text, None),
            )
            saved = self._store.record_turns(
                conversation_id,
                None if open_episode.value is None else open_episode.value.episode_id,
                entries,
                now=now,
                title=normalized[:40],
                max_turns=self._settings.episode_max_turns,
                gap_minutes=self._settings.episode_gap_minutes,
            )
            if is_err(saved):
                return saved
            _, turns = saved.value
            events = self._store.list_events(conversation_id, limit=20)
            if is_err(events):
                return events
            surfaced = tuple(
                event.event_id
                for event in events.value
                if event.delivery is EventDelivery.AMBIENT and event.surfaced_at is None
            )
            if surfaced and is_err(self._store.mark_surfaced(surfaced, now)):
                return Err(
                    AppError(AppErrorKind.STORAGE, "Could not mark ambient events.")
                )
            self._publish(
                TimelineItem(
                    TimelineKind.TURN,
                    conversation_id,
                    reply.value.text,
                    now,
                    origin=TurnOrigin.REPLY,
                )
            )
            return Ok(
                TurnOutcome(
                    conversation_id,
                    reply.value.text,
                    turns[-1].turn_index,
                    surfaced,
                    reply.value.memory_updates,
                )
            )

    async def ingest_event(
        self, conversation_id: str, raw: dict[str, object]
    ) -> Result[EventReceipt, AppError]:
        now = self._clock()
        validated = validate_envelope(
            raw,
            conversation_id=conversation_id,
            event_id=f"evt_{uuid4().hex[:12]}",
            now=now,
        )
        if is_err(validated):
            return validated
        envelope = validated.value
        ensured = self._store.ensure_conversation(conversation_id, now)
        if is_err(ensured):
            return ensured
        inserted = self._store.insert_event(
            EventRecord(
                envelope.event_id,
                conversation_id,
                envelope.idempotency_key,
                envelope.name,
                envelope.source,
                envelope.occurred_at,
                now,
                EventStatus.RECEIVED,
                EventDelivery.SILENT,
                "",
                json.dumps(envelope.data, ensure_ascii=False, sort_keys=True),
                None,
                "",
                None,
                None,
            )
        )
        if is_err(inserted):
            return inserted
        if inserted.value.event_id != envelope.event_id:
            return Ok(
                EventReceipt(inserted.value.event_id, True, inserted.value.status)
            )
        task = asyncio.create_task(self._process_event(envelope))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return Ok(EventReceipt(envelope.event_id, False, EventStatus.RECEIVED))

    async def _process_event(self, envelope: EventEnvelope) -> None:
        try:
            delay = self._settings.event_analysis_delay_ms / 1000
            if delay > 0:
                await asyncio.sleep(delay)
            brief = await self._llm.distill(envelope)
            if is_err(brief):
                self._fail(envelope, brief.error.message)
                return
            now = self._clock()
            state = self._store.get_state(envelope.conversation_id)
            if is_err(state):
                self._fail(envelope, state.error.message)
                return
            decision = decide_merge(
                brief.value, state.value, now, self._settings.proactive_policy
            )
            if decision.delivery is not EventDelivery.PROACTIVE:
                self._quiet(envelope, brief.value, decision.delivery, decision.reason)
                return
            context = await self._context(envelope.conversation_id, now)
            if is_err(context):
                self._fail(envelope, context.error.message)
                return
            composed = await self._llm.compose(brief.value, context.value)
            if is_err(composed) or not validate_proactive_message(
                "" if is_err(composed) else composed.value
            ):
                self._quiet(
                    envelope, brief.value, EventDelivery.AMBIENT, "invalid_proactive"
                )
                return
            async with self._lock(envelope.conversation_id):
                current = self._store.get_state(envelope.conversation_id)
                if is_err(current):
                    self._fail(envelope, current.error.message)
                    return
                if current.value.epoch != state.value.epoch:
                    self._quiet(
                        envelope, brief.value, EventDelivery.AMBIENT, "epoch_conflict"
                    )
                    return
                episode = self._store.get_open_episode(envelope.conversation_id)
                if is_err(episode):
                    self._fail(envelope, episode.error.message)
                    return
                entries: tuple[TurnEntry, ...] = (
                    (
                        "assistant",
                        TurnOrigin.PROACTIVE,
                        composed.value,
                        envelope.event_id,
                    ),
                )
                saved = self._store.record_turns(
                    envelope.conversation_id,
                    None if episode.value is None else episode.value.episode_id,
                    entries,
                    now=now,
                    title=brief.value.headline[:40],
                    max_turns=self._settings.episode_max_turns,
                    gap_minutes=self._settings.episode_gap_minutes,
                )
                if is_err(saved):
                    self._fail(envelope, saved.error.message)
                    return
                turn_episode, turns = saved.value
                updated = self._store.update_event(
                    envelope.event_id,
                    status=EventStatus.COMPLETED,
                    delivery=EventDelivery.PROACTIVE,
                    merge_reason=decision.reason,
                    brief=brief.value,
                    failure_reason="",
                    surfaced_at=now,
                    linked_turn_id=f"{turn_episode.episode_id}:{turns[-1].turn_index}",
                )
                if is_err(updated):
                    self._fail(envelope, updated.error.message)
                    return
                self._store.record_proactive(envelope.conversation_id, now)
                self._publish(
                    TimelineItem(
                        TimelineKind.TURN,
                        envelope.conversation_id,
                        composed.value,
                        now,
                        envelope.event_id,
                        envelope.event_id,
                        TurnOrigin.PROACTIVE,
                        DeliveryMode.PUSH,
                    )
                )
        except Exception as exc:
            self._fail(envelope, f"{type(exc).__name__}: {exc}")

    def _quiet(
        self,
        envelope: EventEnvelope,
        brief: EventBrief,
        delivery: EventDelivery,
        reason: str,
    ) -> None:
        match delivery:
            case EventDelivery.SILENT:
                status = EventStatus.SUPPRESSED
            case EventDelivery.AMBIENT | EventDelivery.PROACTIVE:
                status = EventStatus.COMPLETED
            case _:
                assert_never(delivery)
        self._store.update_event(
            envelope.event_id,
            status=status,
            delivery=delivery,
            merge_reason=reason,
            brief=brief,
            failure_reason="",
            surfaced_at=None,
            linked_turn_id=None,
        )

    def _fail(self, envelope: EventEnvelope, reason: str) -> None:
        message = sanitize_single_line(reason)[:500]
        self._store.update_event(
            envelope.event_id,
            status=EventStatus.FAILED,
            delivery=EventDelivery.PROACTIVE,
            merge_reason="worker_failed",
            brief=None,
            failure_reason=message,
            surfaced_at=None,
            linked_turn_id=None,
        )
        self._logger.log(
            LogEntry(LogLevel.ERROR, "event_worker_failed", message, "EVENT_FAILED")
        )
        self._publish(
            TimelineItem(
                TimelineKind.EVENT_FAILED,
                envelope.conversation_id,
                message,
                self._clock(),
                envelope.event_id,
            )
        )

    def get_timeline(
        self, conversation_id: str, *, limit: int = 50
    ) -> Result[tuple[TimelineItem, ...], AppError]:
        turns = self._store.recent_turns(conversation_id, limit=limit)
        if is_err(turns):
            return turns
        events = self._store.list_events(conversation_id, limit=limit)
        if is_err(events):
            return events
        items = [
            TimelineItem(
                TimelineKind.TURN,
                conversation_id,
                turn.content,
                turn.created_at,
                source_event_id=turn.source_event_id,
                origin=turn.origin,
                delivery=_turn_delivery(turn.origin),
            )
            for turn in turns.value
        ]
        items.extend(_event_item(conversation_id, event) for event in events.value)
        return Ok(tuple(sorted(items, key=lambda item: item.created_at)))

    def list_events(
        self, conversation_id: str, *, limit: int = 20
    ) -> Result[tuple[EventRecord, ...], AppError]:
        return self._store.list_events(conversation_id, limit=limit)

    def get_event(self, event_id: str) -> Result[EventRecord | None, AppError]:
        return self._store.get_event(event_id)

    def get_memory(self) -> Result[MemoryBlocks, AppError]:
        return self._store.read_memory()

    def reset_conversation(self, conversation_id: str) -> Result[int, AppError]:
        return self._store.reset_conversation(conversation_id)

    async def curate_memory(
        self, conversation_id: str
    ) -> Result[CurateOutcome, AppError]:
        async with self._lock(conversation_id):
            summarized = 0
            episodes = self._store.list_episodes(conversation_id, limit=1)
            if is_err(episodes):
                return episodes
            if episodes.value and should_summarize(
                episodes.value[0].turn_count,
                episodes.value[0].summarized_turn_count,
                self._settings.summary_every_turns,
            ):
                episode = episodes.value[0]
                turns = self._store.list_turns(episode.episode_id, limit=200)
                if is_err(turns):
                    return turns
                summary = await self._llm.summarize(turns.value, episode.summary)
                if is_err(summary):
                    return summary
                saved = self._store.update_summary(
                    episode.episode_id,
                    title=summary.value.title,
                    overview=summary.value.overview,
                    summary=summary.value.summary,
                    summarized_turn_count=episode.turn_count,
                )
                if is_err(saved):
                    return saved
                summarized = 1
            pending = self._store.list_candidates(conversation_id, MemoryStatus.PENDING)
            if is_err(pending):
                return pending
            memory = self._store.read_memory()
            if is_err(memory):
                return memory
            merged, promoted, rejected = merge_candidates(memory.value, pending.value)
            if promoted:
                written = self._store.write_memory(merged)
                if is_err(written):
                    return written
                marked = self._store.set_candidate_status(
                    promoted, MemoryStatus.PROMOTED
                )
                if is_err(marked):
                    return marked
            if rejected:
                marked = self._store.set_candidate_status(
                    rejected, MemoryStatus.REJECTED
                )
                if is_err(marked):
                    return marked
            self._logger.log(
                LogEntry(
                    LogLevel.INFO,
                    "sleep_time_curation",
                    "curation completed",
                    detail=f"summarized={summarized} promoted={len(promoted)}",
                )
            )
            return Ok(CurateOutcome(summarized, len(promoted), len(rejected)))

    async def aclose(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._subscribers.clear()
        self._locks.clear()


def _turn_delivery(origin: TurnOrigin) -> DeliveryMode:
    match origin:
        case TurnOrigin.USER | TurnOrigin.REPLY:
            return DeliveryMode.REPLY
        case TurnOrigin.PROACTIVE:
            return DeliveryMode.PUSH
        case _:
            assert_never(origin)


def _event_item(conversation_id: str, event: EventRecord) -> TimelineItem:
    match event.status:
        case EventStatus.FAILED:
            return TimelineItem(
                TimelineKind.EVENT_FAILED,
                conversation_id,
                event.failure_reason,
                event.received_at,
                event.event_id,
                event.event_id,
            )
        case EventStatus.RECEIVED | EventStatus.COMPLETED | EventStatus.SUPPRESSED:
            return TimelineItem(
                TimelineKind.EVENT,
                conversation_id,
                event.name,
                event.received_at,
                event.event_id,
                event.event_id,
            )
        case _:
            assert_never(event.status)
