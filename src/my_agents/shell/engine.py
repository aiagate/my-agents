import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, assert_never
from uuid import uuid4

from flow_res import Err, Ok, Result, is_err

from my_agents.core.context import (
    AssembledContext,
    assemble_context,
    stable_prefix_sha256,
)
from my_agents.core.events import (
    decide_merge,
    sanitize_single_line,
    sanitize_untrusted,
    validate_envelope,
    validate_proactive_message,
)
from my_agents.core.memory import merge_candidates, should_summarize
from my_agents.core.types import (
    AppError,
    AppErrorKind,
    CharacterPersona,
    ContextSnapshot,
    DeliveryMode,
    DialogueTurn,
    EventBrief,
    EventDelivery,
    EventEnvelope,
    EventReceipt,
    EventRecord,
    EventStatus,
    MemoryBlocks,
    MemorySnapshot,
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
    async def start(self) -> Result[None, AppError]: ...

    async def aclose(self) -> None: ...

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
        self._lock_users: dict[str, int] = {}
        self._subscribers: dict[str, set[asyncio.Queue[TimelineItem]]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def _locked(self, conversation_id: str) -> AsyncGenerator[None]:
        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        self._lock_users[conversation_id] = self._lock_users.get(conversation_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._lock_users.get(conversation_id, 1) - 1
            if remaining <= 0:
                self._locks.pop(conversation_id, None)
                self._lock_users.pop(conversation_id, None)
            else:
                self._lock_users[conversation_id] = remaining

    def active_lock_count(self) -> int:
        return len(self._locks)

    async def _call_store[T](
        self, operation: Callable[[], Result[T, AppError]]
    ) -> Result[T, AppError]:
        return await asyncio.to_thread(operation)

    async def start(self) -> Result[None, AppError]:
        return await self._llm.start()

    def _publish(self, item: TimelineItem) -> None:
        for queue in self._subscribers.get(item.conversation_id, set()):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(item)

    async def subscribe(self, conversation_id: str) -> AsyncGenerator[TimelineItem]:
        queue: asyncio.Queue[TimelineItem] = asyncio.Queue(maxsize=64)
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
        memory = await self._call_store(self._store.read_memory)
        if is_err(memory):
            return memory
        episodes = await self._call_store(
            lambda: self._store.list_episodes(conversation_id, limit=20)
        )
        if is_err(episodes):
            return episodes
        events = await self._call_store(
            lambda: self._store.list_events(conversation_id, limit=10)
        )
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
        async with self._locked(conversation_id):
            now = self._clock()
            for step in (
                await self._call_store(
                    lambda: self._store.ensure_conversation(conversation_id, now)
                ),
                await self._call_store(
                    lambda: self._store.bump_epoch(conversation_id, now)
                ),
            ):
                if is_err(step):
                    return step
            context = await self._context(conversation_id, now)
            if is_err(context):
                return context
            history = await self._call_store(
                lambda: self._store.recent_turns(
                    conversation_id, limit=self._settings.recent_turns_limit
                )
            )
            if is_err(history):
                return history
            reply = await self._llm.chat(
                conversation_id, normalized, history.value, context.value
            )
            if is_err(reply):
                return reply
            open_episode = await self._call_store(
                lambda: self._store.get_open_episode(conversation_id)
            )
            if is_err(open_episode):
                return open_episode
            entries: tuple[TurnEntry, ...] = (
                ("user", TurnOrigin.USER, normalized, None),
                ("assistant", TurnOrigin.REPLY, reply.value.text, None),
            )
            saved = await self._call_store(
                lambda: self._store.record_turns(
                    conversation_id,
                    None
                    if open_episode.value is None
                    else open_episode.value.episode_id,
                    entries,
                    now=now,
                    title=normalized[:40],
                    max_turns=self._settings.episode_max_turns,
                    gap_minutes=self._settings.episode_gap_minutes,
                )
            )
            if is_err(saved):
                return saved
            _, turns = saved.value
            events = await self._call_store(
                lambda: self._store.list_events(conversation_id, limit=20)
            )
            if is_err(events):
                return events
            surfaced = tuple(
                event.event_id
                for event in events.value
                if event.delivery is EventDelivery.AMBIENT and event.surfaced_at is None
            )
            if surfaced and is_err(
                await self._call_store(lambda: self._store.mark_surfaced(surfaced, now))
            ):
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
        ensured = await self._call_store(
            lambda: self._store.ensure_conversation(conversation_id, now)
        )
        if is_err(ensured):
            return ensured
        inserted = await self._call_store(
            lambda: self._store.insert_event(
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
        )
        if is_err(inserted):
            return inserted
        if inserted.value.event_id != envelope.event_id:
            return Ok(
                EventReceipt(inserted.value.event_id, True, inserted.value.status)
            )
        task = asyncio.create_task(self._process_event(envelope, ensured.value.epoch))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return Ok(EventReceipt(envelope.event_id, False, EventStatus.RECEIVED))

    async def _process_event(
        self, envelope: EventEnvelope, received_epoch: int
    ) -> None:
        try:
            delay = self._settings.event_analysis_delay_ms / 1000
            if delay > 0:
                await asyncio.sleep(delay)
            brief = await self._llm.distill(envelope)
            if is_err(brief):
                await self._fail(envelope, brief.error.message)
                return
            now = self._clock()
            state = await self._call_store(
                lambda: self._store.get_state(envelope.conversation_id)
            )
            if is_err(state):
                await self._fail(envelope, state.error.message)
                return
            decision = decide_merge(
                brief.value, state.value, now, self._settings.proactive_policy
            )
            if decision.delivery is not EventDelivery.PROACTIVE:
                await self._quiet(
                    envelope, brief.value, decision.delivery, decision.reason
                )
                return
            context = await self._context(envelope.conversation_id, now)
            if is_err(context):
                await self._fail(envelope, context.error.message)
                return
            composed = await self._llm.compose(brief.value, context.value)
            if is_err(composed):
                await self._quiet(
                    envelope, brief.value, EventDelivery.AMBIENT, "invalid_proactive"
                )
                return
            text = sanitize_untrusted(composed.value)
            if not validate_proactive_message(text):
                await self._quiet(
                    envelope, brief.value, EventDelivery.AMBIENT, "invalid_proactive"
                )
                return
            async with self._locked(envelope.conversation_id):
                committed = await self._call_store(
                    lambda: self._store.commit_proactive(
                        envelope.conversation_id,
                        envelope.event_id,
                        brief.value,
                        received_epoch=received_epoch,
                        text=text,
                        now=now,
                        policy=self._settings.proactive_policy,
                        title=sanitize_single_line(brief.value.headline)[:40],
                        max_turns=self._settings.episode_max_turns,
                        gap_minutes=self._settings.episode_gap_minutes,
                    )
                )
                if is_err(committed):
                    await self._fail(envelope, committed.error.message)
                    return
                if committed.value.delivery is EventDelivery.PROACTIVE:
                    self._publish(
                        TimelineItem(
                            TimelineKind.TURN,
                            envelope.conversation_id,
                            text,
                            now,
                            envelope.event_id,
                            envelope.event_id,
                            TurnOrigin.PROACTIVE,
                            DeliveryMode.PUSH,
                        )
                    )
        except Exception as exc:
            await self._fail(envelope, f"{type(exc).__name__}: {exc}")

    async def _quiet(
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
        updated = await self._call_store(
            lambda: self._store.update_event(
                envelope.event_id,
                status=status,
                delivery=delivery,
                merge_reason=reason,
                brief=brief,
                failure_reason="",
                surfaced_at=None,
                linked_turn_id=None,
            )
        )
        if is_err(updated):
            await self._fail(envelope, updated.error.message)

    async def _fail(self, envelope: EventEnvelope, reason: str) -> None:
        message = sanitize_single_line(reason)[:500]
        failed = await self._call_store(
            lambda: self._store.update_event(
                envelope.event_id,
                status=EventStatus.FAILED,
                delivery=EventDelivery.PROACTIVE,
                merge_reason="worker_failed",
                brief=None,
                failure_reason=message,
                surfaced_at=None,
                linked_turn_id=None,
            )
        )
        del failed
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

    def get_memory_snapshot(
        self, conversation_id: str
    ) -> Result[MemorySnapshot, AppError]:
        memory = self._store.read_memory()
        if is_err(memory):
            return memory
        counts = self._store.counts(conversation_id)
        if is_err(counts):
            return counts
        episodes = self._store.list_episodes(conversation_id, limit=10)
        if is_err(episodes):
            return episodes
        return Ok(
            MemorySnapshot(conversation_id, memory.value, counts.value, episodes.value)
        )

    async def context_snapshot(
        self, conversation_id: str
    ) -> Result[ContextSnapshot, AppError]:
        state = await self._call_store(lambda: self._store.get_state(conversation_id))
        if is_err(state):
            return state
        assembled = await self._context(conversation_id, self._clock())
        if is_err(assembled):
            return assembled
        counts = await self._call_store(lambda: self._store.counts(conversation_id))
        if is_err(counts):
            return counts
        memory = await self._call_store(self._store.read_memory)
        if is_err(memory):
            return memory
        context = assembled.value
        return Ok(
            ContextSnapshot(
                conversation_id=conversation_id,
                epoch=state.value.epoch,
                stable_prefix=context.stable_prefix,
                stable_prefix_sha256=stable_prefix_sha256(context.stable_prefix),
                event_section=context.event_section,
                omitted_events=context.omitted_events,
                omitted_episodes=context.omitted_episodes,
                counts=counts.value,
                memory_chars=(
                    len(memory.value.user)
                    + len(memory.value.relationship)
                    + len(memory.value.arc)
                ),
                dynamic_section=context.dynamic_section,
            )
        )

    def reset_conversation(self, conversation_id: str) -> Result[int, AppError]:
        return self._store.reset_conversation(conversation_id)

    async def curate_memory(
        self, conversation_id: str
    ) -> Result[CurateOutcome, AppError]:
        async with self._locked(conversation_id):
            summarized = 0
            episodes = await self._call_store(
                lambda: self._store.list_episodes(conversation_id, limit=1)
            )
            if is_err(episodes):
                return episodes
            if episodes.value and should_summarize(
                episodes.value[0].turn_count,
                episodes.value[0].summarized_turn_count,
                self._settings.summary_every_turns,
            ):
                episode = episodes.value[0]
                turns = await self._call_store(
                    lambda: self._store.list_turns(episode.episode_id, limit=200)
                )
                if is_err(turns):
                    return turns
                safe_turns = tuple(
                    turn
                    for turn in turns.value
                    if turn.origin is not TurnOrigin.PROACTIVE
                    and turn.source_event_id is None
                )
                if safe_turns:
                    summary = await self._llm.summarize(safe_turns, episode.summary)
                    if is_err(summary):
                        return summary
                    saved = await self._call_store(
                        lambda: self._store.update_summary(
                            episode.episode_id,
                            title=sanitize_single_line(summary.value.title),
                            overview=sanitize_single_line(summary.value.overview),
                            summary=sanitize_untrusted(summary.value.summary),
                            summarized_turn_count=episode.turn_count,
                        )
                    )
                    if is_err(saved):
                        return saved
                    summarized = 1
            pending = await self._call_store(
                lambda: self._store.list_candidates(
                    conversation_id, MemoryStatus.PENDING
                )
            )
            if is_err(pending):
                return pending
            promoted: list[str] = []
            rejected: list[str] = []

            def merge(
                memory: MemoryBlocks,
            ) -> Result[MemoryBlocks, AppError]:
                merged, promoted_ids, rejected_ids = merge_candidates(
                    memory, pending.value
                )
                promoted.extend(promoted_ids)
                rejected.extend(rejected_ids)
                return Ok(merged)

            written = await self._call_store(lambda: self._store.update_memory(merge))
            if is_err(written):
                return written
            if promoted:
                marked = await self._call_store(
                    lambda: self._store.set_candidate_status(
                        promoted, MemoryStatus.PROMOTED
                    )
                )
                if is_err(marked):
                    return marked
            if rejected:
                marked = await self._call_store(
                    lambda: self._store.set_candidate_status(
                        rejected, MemoryStatus.REJECTED
                    )
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
        self._lock_users.clear()
        await self._llm.aclose()


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
