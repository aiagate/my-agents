import asyncio
import json
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol, assert_never
from uuid import uuid4

from flow_res import Err, Ok, Result, is_err

from my_agents.core.context import (
    AssembledContext,
    assemble_context,
    stable_prefix_sha256,
)
from my_agents.core.events import sanitize_single_line, validate_envelope
from my_agents.core.memory import memory_char_count
from my_agents.core.types import (
    AppError,
    AppErrorKind,
    CharacterPersona,
    ChatMessage,
    ContextSnapshot,
    EventAction,
    EventDecision,
    EventEnvelope,
    EventReceipt,
    EventRecord,
    EventStatus,
    MemoryBlocks,
    MemorySnapshot,
    MessageOrigin,
    TimelineItem,
    TimelineKind,
    TurnOutcome,
)
from my_agents.shell.llm import ChatReply
from my_agents.shell.logger import IAppLogger, LogEntry, LogLevel
from my_agents.shell.settings import Settings
from my_agents.shell.store import MessageEntry, Store


class EngineLlm(Protocol):
    async def start(self) -> Result[None, AppError]: ...

    async def aclose(self) -> None: ...

    async def chat(
        self,
        conversation_id: str,
        user_text: str,
        history: Sequence[ChatMessage],
        context: AssembledContext,
    ) -> Result[ChatReply, AppError]: ...

    async def evaluate_event(
        self, envelope: EventEnvelope, stable_prefix: str
    ) -> Result[EventDecision, AppError]: ...


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

    @property
    def logger(self) -> IAppLogger:
        return self._logger

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

    async def _load_context(
        self, conversation_id: str, now: datetime
    ) -> Result[
        tuple[AssembledContext, tuple[EventRecord, ...], MemoryBlocks], AppError
    ]:
        memory = await self._call_store(self._store.read_memory)
        if is_err(memory):
            return memory
        events = await self._call_store(
            lambda: self._store.list_events(conversation_id, limit=20)
        )
        if is_err(events):
            return events
        assembled = assemble_context(
            self._persona,
            memory.value,
            events.value,
            now=now,
        )
        return Ok((assembled, events.value, memory.value))

    async def submit_message(
        self, conversation_id: str, text: str
    ) -> Result[TurnOutcome, AppError]:
        normalized = text.strip()
        if not normalized:
            return Err(AppError(AppErrorKind.VALIDATION, "Message must not be empty."))
        async with self._locked(conversation_id):
            now = self._clock()
            bumped = await self._call_store(
                lambda: self._store.bump_epoch(conversation_id, now)
            )
            if is_err(bumped):
                return bumped
            loaded = await self._load_context(conversation_id, now)
            if is_err(loaded):
                return loaded
            assembled, events, _ = loaded.value
            history = await self._call_store(
                lambda: self._store.recent_messages(
                    conversation_id, limit=self._settings.recent_messages_limit
                )
            )
            if is_err(history):
                return history
            reply = await self._llm.chat(
                conversation_id, normalized, history.value, assembled
            )
            if is_err(reply):
                return reply
            entries: tuple[MessageEntry, ...] = (
                ("user", MessageOrigin.USER, normalized, None),
                ("assistant", MessageOrigin.REPLY, reply.value.text, None),
            )
            saved = await self._call_store(
                lambda: self._store.append_messages(conversation_id, entries, now)
            )
            if is_err(saved):
                return saved
            surfaced = tuple(
                event.event_id
                for event in events
                if event.action is EventAction.NOTE and event.surfaced_at is None
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
                    origin=MessageOrigin.REPLY,
                )
            )
            return Ok(
                TurnOutcome(
                    conversation_id,
                    reply.value.text,
                    saved.value[-1].message_id,
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
                    EventAction.IGNORE,
                    "",
                    "",
                    json.dumps(dict(envelope.data), ensure_ascii=False, sort_keys=True),
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
            delay = self._settings.event_eval_delay_ms / 1000
            if delay > 0:
                await asyncio.sleep(delay)
            memory = await self._call_store(self._store.read_memory)
            if is_err(memory):
                await self._fail(envelope, memory.error.message)
                return
            stable_prefix = assemble_context(
                self._persona, memory.value, (), now=self._clock()
            ).stable_prefix
            decision = await self._llm.evaluate_event(envelope, stable_prefix)
            if is_err(decision):
                await self._fail(envelope, decision.error.message)
                return
            async with self._locked(envelope.conversation_id):
                now = self._clock()
                committed = await self._call_store(
                    lambda: self._store.finalize_event(
                        envelope.conversation_id,
                        envelope.event_id,
                        decision.value,
                        received_epoch=received_epoch,
                        now=now,
                        policy=self._settings.proactive_policy,
                    )
                )
                if is_err(committed):
                    await self._fail(envelope, committed.error.message)
                    return
                result = committed.value
                item = TimelineItem(
                    TimelineKind.EVENT,
                    envelope.conversation_id,
                    result.note or envelope.name,
                    now,
                    envelope.event_id,
                    envelope.event_id,
                    status=EventStatus.COMPLETED,
                    action=result.action,
                    reason=result.reason,
                )
                if result.action is EventAction.SPEAK and result.message is not None:
                    item = replace(
                        item,
                        kind=TimelineKind.TURN,
                        content=result.message.content,
                        origin=MessageOrigin.PROACTIVE,
                    )
                self._publish(item)
        except Exception as exc:
            await self._fail(envelope, f"{type(exc).__name__}: {exc}")

    async def _fail(self, envelope: EventEnvelope, reason: str) -> None:
        message = sanitize_single_line(reason)[:500]
        await self._call_store(
            lambda: self._store.fail_event(envelope.event_id, message)
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
                envelope.event_id,
                status=EventStatus.FAILED,
                action=EventAction.IGNORE,
                reason="worker_failed",
            )
        )

    def get_timeline(
        self, conversation_id: str, *, limit: int = 50
    ) -> Result[tuple[TimelineItem, ...], AppError]:
        messages = self._store.recent_messages(conversation_id, limit=limit)
        if is_err(messages):
            return messages
        events = self._store.list_events(conversation_id, limit=limit)
        if is_err(events):
            return events
        items = [
            TimelineItem(
                TimelineKind.TURN,
                conversation_id,
                message.content,
                message.created_at,
                source_event_id=message.source_event_id,
                origin=message.origin,
            )
            for message in messages.value
        ]
        items.extend(_event_item(conversation_id, event) for event in events.value)
        return Ok(tuple(sorted(items, key=lambda item: item.created_at)))

    def get_event(self, event_id: str) -> Result[EventRecord | None, AppError]:
        return self._store.get_event(event_id)

    def get_memory_snapshot(
        self, conversation_id: str
    ) -> Result[MemorySnapshot, AppError]:
        memory = self._store.read_memory()
        if is_err(memory):
            return memory
        counts = self._store.counts(conversation_id)
        if is_err(counts):
            return counts
        return Ok(MemorySnapshot(conversation_id, memory.value, counts.value))

    async def context_snapshot(
        self, conversation_id: str
    ) -> Result[ContextSnapshot, AppError]:
        state = await self._call_store(lambda: self._store.get_state(conversation_id))
        if is_err(state):
            return state
        loaded = await self._load_context(conversation_id, self._clock())
        if is_err(loaded):
            return loaded
        counts = await self._call_store(lambda: self._store.counts(conversation_id))
        if is_err(counts):
            return counts
        assembled, _, memory = loaded.value
        return Ok(
            ContextSnapshot(
                conversation_id=conversation_id,
                epoch=state.value.epoch,
                stable_prefix=assembled.stable_prefix,
                stable_prefix_sha256=stable_prefix_sha256(assembled.stable_prefix),
                dynamic_section=assembled.dynamic_section,
                event_section=assembled.event_section,
                omitted_events=assembled.omitted_events,
                counts=counts.value,
                memory_chars=memory_char_count(memory),
            )
        )

    def reset_conversation(self, conversation_id: str) -> Result[int, AppError]:
        return self._store.reset_conversation(conversation_id)

    async def aclose(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._subscribers.clear()
        await self._llm.aclose()


def _event_item(conversation_id: str, event: EventRecord) -> TimelineItem:
    match event.status:
        case EventStatus.FAILED:
            kind, content = TimelineKind.EVENT_FAILED, event.failure_reason
        case EventStatus.RECEIVED | EventStatus.COMPLETED:
            kind, content = TimelineKind.EVENT, event.note or event.name
        case _:
            assert_never(event.status)
    return TimelineItem(
        kind,
        conversation_id,
        content,
        event.received_at,
        event.event_id,
        event.event_id,
        status=event.status,
        action=event.action,
        reason=event.reason,
    )
