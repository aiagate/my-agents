import asyncio
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import Protocol, assert_never
from uuid import uuid7
from weakref import WeakValueDictionary

from flow_res import Err, Ok, Result, is_err

from my_agents.core.context import (
    MAX_CONTEXT_EVENTS,
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
    ConversationSummary,
    EventAction,
    EventDecision,
    EventEnvelope,
    EventReceipt,
    EventRecord,
    EventStatus,
    MemoryBlocks,
    MemorySnapshot,
    MessageOrigin,
    ProactiveNotice,
    SecretStr,
    TimelineItem,
    TimelineKind,
    TurnOutcome,
    redact_secrets,
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


class ProactiveChannel(Protocol):
    async def deliver(self, notice: ProactiveNotice) -> None: ...

    async def aclose(self) -> None: ...


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
        proactive_channel: ProactiveChannel | None = None,
        redactions: Sequence[SecretStr] = (),
    ) -> None:
        self._store = store
        self._llm = llm
        self._settings = settings
        self._persona = persona
        self._logger = logger
        self._clock = clock or (lambda: datetime.now(UTC))
        self._proactive_channel = proactive_channel
        self._redactions: tuple[SecretStr, ...] = tuple(redactions)
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._subscribers: dict[str, set[asyncio.Queue[TimelineItem | None]]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._event_semaphore = asyncio.Semaphore(settings.event_max_concurrency)
        self._turn_tasks: set[asyncio.Task[Result[TurnOutcome, AppError]]] = set()

    def _lock_for(self, conversation_id: str) -> asyncio.Lock:
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conversation_id] = lock
        return lock

    @property
    def logger(self) -> IAppLogger:
        return self._logger

    async def _call_store[T](
        self, operation: Callable[[], Result[T, AppError]]
    ) -> Result[T, AppError]:
        return await asyncio.to_thread(operation)

    def _spawn_event_task(self, envelope: EventEnvelope, received_epoch: int) -> None:
        task = asyncio.create_task(self._process_event(envelope, received_epoch))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def start(self) -> Result[None, AppError]:
        started = await self._llm.start()
        if is_err(started):
            return started
        pending = await self._call_store(self._store.list_received_events)
        if is_err(pending):
            return pending
        for item in pending.value:
            self._spawn_event_task(item.envelope, item.epoch)
        return Ok(None)

    def _publish(self, item: TimelineItem) -> None:
        for queue in tuple(self._subscribers.get(item.conversation_id, set())):
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
            else:
                queue.put_nowait(item)

    async def subscribe(self, conversation_id: str) -> AsyncGenerator[TimelineItem]:
        queue: asyncio.Queue[TimelineItem | None] = asyncio.Queue(maxsize=64)
        self._subscribers.setdefault(conversation_id, set()).add(queue)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            group = self._subscribers.get(conversation_id)
            if group is not None:
                group.discard(queue)
                if not group:
                    self._subscribers.pop(conversation_id, None)

    async def _load_context(
        self, conversation_id: str, now: datetime
    ) -> Result[tuple[AssembledContext, MemoryBlocks], AppError]:
        memory = await self._call_store(self._store.read_memory)
        if is_err(memory):
            return memory
        pending = await self._call_store(
            lambda: self._store.list_pending_notes(
                conversation_id, limit=MAX_CONTEXT_EVENTS
            )
        )
        if is_err(pending):
            return pending
        events, total_pending = pending.value
        assembled = assemble_context(
            self._persona,
            memory.value,
            events,
            now=now,
            tz=self._settings.tzinfo,
            pending_event_count=total_pending,
        )
        return Ok((assembled, memory.value))

    async def submit_message(
        self, conversation_id: str, text: str
    ) -> Result[TurnOutcome, AppError]:
        normalized = text.strip()
        if not normalized:
            return Err(AppError(AppErrorKind.VALIDATION, "Message must not be empty."))
        task = asyncio.create_task(self._submit_message(conversation_id, normalized))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)
        try:
            return await task
        except asyncio.CancelledError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise

    async def _submit_message(
        self, conversation_id: str, normalized: str
    ) -> Result[TurnOutcome, AppError]:
        async with self._lock_for(conversation_id):
            now = self._clock()
            bumped = await self._call_store(
                lambda: self._store.bump_epoch(conversation_id, now)
            )
            if is_err(bumped):
                return bumped
            loaded = await self._load_context(conversation_id, now)
            if is_err(loaded):
                return loaded
            assembled, _ = loaded.value
            history = await self._call_store(
                lambda: self._store.recent_messages(
                    conversation_id, limit=self._settings.recent_messages_limit
                )
            )
            if is_err(history):
                return history
            user_entries: tuple[MessageEntry, ...] = (
                ("user", MessageOrigin.USER, normalized, None),
            )
            saved_user = await self._call_store(
                lambda: self._store.append_messages(conversation_id, user_entries, now)
            )
            if is_err(saved_user):
                return saved_user
            self._publish(_message_item(saved_user.value[0]))
            reply = await self._llm.chat(
                conversation_id, normalized, history.value, assembled
            )
            if is_err(reply):
                return reply
            reply_now = self._clock()
            reply_entries: tuple[MessageEntry, ...] = (
                ("assistant", MessageOrigin.REPLY, reply.value.text, None),
            )
            saved_reply = await self._call_store(
                lambda: self._store.append_messages(
                    conversation_id, reply_entries, reply_now
                )
            )
            if is_err(saved_reply):
                return saved_reply
            surfaced = assembled.rendered_event_ids
            if surfaced and is_err(
                await self._call_store(
                    lambda: self._store.mark_surfaced(
                        conversation_id, surfaced, reply_now
                    )
                )
            ):
                return Err(
                    AppError(AppErrorKind.STORAGE, "Could not mark ambient events.")
                )
            self._publish(_message_item(saved_reply.value[0]))
            return Ok(
                TurnOutcome(
                    conversation_id,
                    reply.value.text,
                    saved_reply.value[0].message_id,
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
            event_id=str(uuid7()),
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
                    envelope.payload_json,
                    "",
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
        self._spawn_event_task(envelope, ensured.value.epoch)
        return Ok(EventReceipt(envelope.event_id, False, EventStatus.RECEIVED))

    async def _process_event(
        self, envelope: EventEnvelope, received_epoch: int
    ) -> None:
        async with self._event_semaphore:
            try:
                memory = await self._call_store(self._store.read_memory)
                if is_err(memory):
                    await self._fail(envelope, memory.error.message)
                    return
                stable_prefix = assemble_context(
                    self._persona,
                    memory.value,
                    (),
                    now=self._clock(),
                    tz=self._settings.tzinfo,
                ).stable_prefix
                decision = await self._llm.evaluate_event(envelope, stable_prefix)
                if is_err(decision):
                    await self._fail(envelope, decision.error.message)
                    return
                notice: ProactiveNotice | None = None
                async with self._lock_for(envelope.conversation_id):
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
                    if (
                        result.action is EventAction.SPEAK
                        and result.message is not None
                    ):
                        self._publish(_message_item(result.message))
                        notice = ProactiveNotice(
                            conversation_id=envelope.conversation_id,
                            speaker_name=self._persona.name,
                            content=result.message.content,
                            event_id=envelope.event_id,
                            created_at=result.message.created_at,
                        )
                    else:
                        self._publish(_event_item(result.record))
                if notice is not None and self._proactive_channel is not None:
                    try:
                        await self._proactive_channel.deliver(notice)
                    except Exception as exc:
                        self._logger.log(
                            LogEntry(
                                LogLevel.ERROR,
                                "proactive_delivery_failed",
                                redact_secrets(
                                    f"{type(exc).__name__}: {exc}", self._redactions
                                ),
                                "PROACTIVE_DELIVERY_FAILED",
                            )
                        )
            except Exception as exc:
                await self._fail(envelope, f"{type(exc).__name__}: {exc}")

    async def _fail(self, envelope: EventEnvelope, reason: str) -> None:
        message = sanitize_single_line(redact_secrets(reason, self._redactions))[:500]
        failed = await self._call_store(
            lambda: self._store.fail_event(
                envelope.conversation_id, envelope.event_id, message
            )
        )
        self._logger.log(
            LogEntry(LogLevel.ERROR, "event_worker_failed", message, "EVENT_FAILED")
        )
        if is_err(failed):
            return
        self._publish(_event_item(failed.value))

    def get_timeline(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
        after_id: str | None = None,
    ) -> Result[tuple[TimelineItem, ...], AppError]:
        return self._store.list_timeline(
            conversation_id, limit=limit, after_id=after_id
        )

    def list_conversations(
        self, *, limit: int = 100
    ) -> Result[tuple[ConversationSummary, ...], AppError]:
        return self._store.list_conversations(limit=limit)

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
        assembled, memory = loaded.value
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
        tasks: tuple[
            asyncio.Task[None] | asyncio.Task[Result[TurnOutcome, AppError]], ...
        ] = (*self._tasks, *self._turn_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._turn_tasks.clear()
        for group in self._subscribers.values():
            for queue in group:
                if queue.full():
                    while not queue.empty():
                        queue.get_nowait()
                queue.put_nowait(None)
        self._subscribers.clear()
        await self._llm.aclose()
        if self._proactive_channel is not None:
            await self._proactive_channel.aclose()


def _message_item(message: ChatMessage) -> TimelineItem:
    return TimelineItem(
        kind=TimelineKind.TURN,
        conversation_id=message.conversation_id,
        content=message.content,
        created_at=message.created_at,
        item_id=message.message_id,
        event_id=message.source_event_id,
        origin=message.origin,
    )


def _event_item(event: EventRecord) -> TimelineItem:
    match event.status:
        case EventStatus.FAILED:
            kind, content = TimelineKind.EVENT_FAILED, event.failure_reason
        case EventStatus.RECEIVED | EventStatus.COMPLETED:
            kind, content = TimelineKind.EVENT, event.note or event.name
        case _:
            assert_never(event.status)
    return TimelineItem(
        kind=kind,
        conversation_id=event.conversation_id,
        content=content,
        created_at=event.received_at,
        item_id=event.event_id,
        event_id=event.event_id,
    )
