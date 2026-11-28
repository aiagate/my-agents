import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from flow_res import Err, Ok, Result

from my_agents.core.context import AssembledContext
from my_agents.core.types import (
    DEFAULT_PERSONA,
    AppError,
    AppErrorKind,
    ChatMessage,
    ConversationCounts,
    ConversationState,
    EventAction,
    EventDecision,
    EventEnvelope,
    EventRecord,
    EventStatus,
    MemoryBlocks,
    MessageOrigin,
    ProactivePolicy,
    PushPayload,
    PushSubscription,
    SecretStr,
    TimelineItem,
    TimelineKind,
)
from my_agents.shell.engine import CharacterEngine
from my_agents.shell.llm import ChatReply
from my_agents.shell.logger import IAppLogger, LogEntry
from my_agents.shell.push import ProactiveDeliverer
from my_agents.shell.settings import Settings, load_settings
from my_agents.shell.store import EventCommit, MessageEntry, Store
from tests.helpers import assert_err


@dataclass
class _SpyLogger(IAppLogger):
    entries: list[LogEntry] = field(default_factory=list)

    def log(self, entry: LogEntry) -> None:
        self.entries.append(entry)

    def close(self) -> None:
        return None


class _FakeLlm:
    def __init__(self) -> None:
        self.start_result: Result[None, AppError] = Ok(None)
        self.chat_result: Result[ChatReply, AppError] = Ok(ChatReply("reply text", ()))
        self.eval_result: Result[EventDecision, AppError] = Ok(
            EventDecision(
                action=EventAction.NOTE, message="", note="noted", reason="ambient"
            )
        )
        self.chat_gate: asyncio.Event | None = None
        self.chat_started: asyncio.Event | None = None
        self.eval_gate: asyncio.Event | None = None
        self.eval_started: asyncio.Event | None = None
        self.chat_calls = 0
        self.last_context: AssembledContext | None = None
        self.eval_error_raises: Exception | None = None
        self.closed = False

    async def start(self) -> Result[None, AppError]:
        return self.start_result

    async def aclose(self) -> None:
        self.closed = True

    async def chat(
        self,
        conversation_id: str,
        user_text: str,
        history: Sequence[ChatMessage],
        context: AssembledContext,
    ) -> Result[ChatReply, AppError]:
        del conversation_id, user_text, history
        self.last_context = context
        self.chat_calls += 1
        if self.chat_started is not None:
            self.chat_started.set()
        if self.chat_gate is not None:
            await self.chat_gate.wait()
        return self.chat_result

    async def evaluate_event(
        self, envelope: EventEnvelope, stable_prefix: str
    ) -> Result[EventDecision, AppError]:
        del envelope, stable_prefix
        if self.eval_started is not None:
            self.eval_started.set()
        if self.eval_gate is not None:
            await self.eval_gate.wait()
        if self.eval_error_raises is not None:
            raise self.eval_error_raises
        return self.eval_result


class _FakeDeliverer:
    def __init__(self, public: str | None = "test-public-key") -> None:
        self._public = public
        self.payloads: list[PushPayload] = []
        self.closed = False
        self.failure: Exception | None = None

    @property
    def public_key(self) -> str | None:
        return self._public

    async def deliver(self, payload: PushPayload) -> None:
        self.payloads.append(payload)
        if self.failure is not None:
            raise self.failure

    async def aclose(self) -> None:
        self.closed = True


class _FaultStore(Store):
    def __init__(
        self,
        db_path: Path,
        memory_path: Path,
        *,
        failures: dict[str, AppError] | None = None,
    ) -> None:
        super().__init__(db_path, memory_path)
        self.failures: dict[str, AppError] = dict(failures or {})

    def ensure_conversation(
        self, conversation_id: str, now: datetime
    ) -> Result[ConversationState, AppError]:
        failure = self.failures.get("ensure_conversation")
        if failure is not None:
            return Err(failure)
        return super().ensure_conversation(conversation_id, now)

    def bump_epoch(
        self, conversation_id: str, now: datetime
    ) -> Result[ConversationState, AppError]:
        failure = self.failures.get("bump_epoch")
        if failure is not None:
            return Err(failure)
        return super().bump_epoch(conversation_id, now)

    def read_memory(self) -> Result[MemoryBlocks, AppError]:
        failure = self.failures.get("read_memory")
        if failure is not None:
            return Err(failure)
        return super().read_memory()

    def list_pending_notes(
        self, conversation_id: str, *, limit: int = 5
    ) -> Result[tuple[tuple[EventRecord, ...], int], AppError]:
        failure = self.failures.get("list_pending_notes")
        if failure is not None:
            return Err(failure)
        return super().list_pending_notes(conversation_id, limit=limit)

    def list_timeline(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
        after_id: str | None = None,
    ) -> Result[tuple[TimelineItem, ...], AppError]:
        failure = self.failures.get("list_timeline")
        if failure is not None:
            return Err(failure)
        return super().list_timeline(conversation_id, limit=limit, after_id=after_id)

    def recent_messages(
        self, conversation_id: str, *, limit: int = 30
    ) -> Result[tuple[ChatMessage, ...], AppError]:
        failure = self.failures.get("recent_messages")
        if failure is not None:
            return Err(failure)
        return super().recent_messages(conversation_id, limit=limit)

    def append_messages(
        self,
        conversation_id: str,
        entries: Sequence[MessageEntry],
        now: datetime,
    ) -> Result[tuple[ChatMessage, ...], AppError]:
        failure = self.failures.get("append_messages")
        if failure is not None:
            return Err(failure)
        return super().append_messages(conversation_id, entries, now)

    def mark_surfaced(
        self, event_ids: Sequence[str], now: datetime
    ) -> Result[None, AppError]:
        failure = self.failures.get("mark_surfaced")
        if failure is not None:
            return Err(failure)
        return super().mark_surfaced(event_ids, now)

    def insert_event(self, record: EventRecord) -> Result[EventRecord, AppError]:
        failure = self.failures.get("insert_event")
        if failure is not None:
            return Err(failure)
        return super().insert_event(record)

    def finalize_event(
        self,
        conversation_id: str,
        event_id: str,
        decision: EventDecision,
        *,
        received_epoch: int,
        now: datetime,
        policy: ProactivePolicy,
    ) -> Result[EventCommit, AppError]:
        failure = self.failures.get("finalize_event")
        if failure is not None:
            return Err(failure)
        return super().finalize_event(
            conversation_id,
            event_id,
            decision,
            received_epoch=received_epoch,
            now=now,
            policy=policy,
        )

    def fail_event(
        self, conversation_id: str, event_id: str, failure_reason: str
    ) -> Result[EventRecord, AppError]:
        failure = self.failures.get("fail_event")
        if failure is not None:
            return Err(failure)
        return super().fail_event(conversation_id, event_id, failure_reason)

    def get_state(self, conversation_id: str) -> Result[ConversationState, AppError]:
        failure = self.failures.get("get_state")
        if failure is not None:
            return Err(failure)
        return super().get_state(conversation_id)

    def counts(self, conversation_id: str) -> Result[ConversationCounts, AppError]:
        failure = self.failures.get("counts")
        if failure is not None:
            return Err(failure)
        return super().counts(conversation_id)


def _settings(tmp_path: Path, **overrides: str) -> Settings:
    loaded = load_settings(
        env={
            "DATA_DIR": str(tmp_path),
            "PROACTIVE_QUIET_HOURS_START_UTC": "0",
            "PROACTIVE_QUIET_HOURS_END_UTC": "0",
            "PROACTIVE_USER_ACTIVE_MINUTES": "0",
            **overrides,
        }
    )
    assert isinstance(loaded, Ok)
    return loaded.value


def _make_engine(
    tmp_path: Path,
    *,
    deliverer: ProactiveDeliverer | None = None,
    redactions: Sequence[SecretStr] = (),
    **overrides: str,
) -> tuple[CharacterEngine, _FaultStore, _FakeLlm, _SpyLogger]:
    settings = _settings(tmp_path, **overrides)
    store = _FaultStore(settings.db_path, settings.core_memory_path)
    llm = _FakeLlm()
    logger = _SpyLogger()
    clock_now = [datetime(2026, 5, 1, 10, 0, tzinfo=UTC)]

    def clock() -> datetime:
        current = clock_now[0]
        clock_now[0] = current + timedelta(seconds=1)
        return current

    engine = CharacterEngine(
        store,
        llm,
        settings,
        DEFAULT_PERSONA,
        logger,
        clock=clock,
        deliverer=deliverer,
        redactions=redactions,
    )
    return engine, store, llm, logger


async def _wait_settled(store: Store, *event_ids: str) -> None:
    for event_id in event_ids:
        for _ in range(200):
            loaded = store.get_event(event_id)
            assert isinstance(loaded, Ok)
            if (
                loaded.value is not None
                and loaded.value.status is not EventStatus.RECEIVED
            ):
                break
            await asyncio.sleep(0.01)


@pytest.mark.anyio
async def test_engine_chat_events_sse_snapshots_and_reset(tmp_path: Path) -> None:
    engine, store, llm, _logger = _make_engine(tmp_path)
    assert isinstance(await engine.start(), Ok)
    assert engine.logger is not None

    assert_err(
        await engine.submit_message("default", "   "),
        kind=AppErrorKind.VALIDATION,
        message_contains="must not be empty",
    )

    streamed: list[TimelineItem] = []

    async def consume() -> None:
        async for item in engine.subscribe("default"):
            streamed.append(item)
            if len(streamed) >= 4:
                return

    subscriber = asyncio.create_task(consume())
    await asyncio.sleep(0)

    r1 = await engine.submit_message("default", "こんにちは")
    assert isinstance(r1, Ok)
    assert r1.value.reply == "reply text"

    llm.eval_result = Ok(
        EventDecision(
            action=EventAction.SPEAK,
            message="緊急です。",
            note="urgent",
            reason="alarm",
        )
    )
    e1 = await engine.ingest_event(
        "default", {"name": "alarm.high", "idempotency_key": "k1"}
    )
    assert isinstance(e1, Ok)
    dup = await engine.ingest_event(
        "default", {"name": "alarm.high", "idempotency_key": "k1"}
    )
    assert isinstance(dup, Ok)
    assert (dup.value.duplicate, dup.value.event_id) == (True, e1.value.event_id)
    await _wait_settled(store, e1.value.event_id)

    llm.eval_result = Ok(
        EventDecision(
            action=EventAction.NOTE, message="", note="ambient note", reason="info"
        )
    )
    e2 = await engine.ingest_event(
        "default", {"name": "email.received", "idempotency_key": "k2"}
    )
    assert isinstance(e2, Ok)
    await _wait_settled(store, e2.value.event_id)

    r2 = await engine.submit_message("default", "お知らせは？")
    assert isinstance(r2, Ok)
    assert r2.value.surfaced_event_ids == (e2.value.event_id,)

    await asyncio.wait_for(subscriber, timeout=5)
    assert [item.kind for item in streamed] == [
        TimelineKind.TURN,
        TimelineKind.TURN,
        TimelineKind.EVENT,
        TimelineKind.TURN,
    ]
    assert streamed[1].origin is MessageOrigin.PROACTIVE

    timeline = engine.get_timeline("default", limit=50)
    assert isinstance(timeline, Ok)
    assert any(item.origin is MessageOrigin.PROACTIVE for item in timeline.value)

    detail = engine.get_event(e1.value.event_id)
    assert isinstance(detail, Ok)
    assert detail.value is not None
    assert detail.value.action is EventAction.SPEAK

    assert isinstance(
        store.update_memory(lambda _blocks: Ok(MemoryBlocks(user="tea"))), Ok
    )
    mem_snap = engine.get_memory_snapshot("default")
    ctx_snap = await engine.context_snapshot("default")
    assert isinstance(mem_snap, Ok)
    assert mem_snap.value.blocks.user == "tea"
    assert isinstance(ctx_snap, Ok)
    assert ctx_snap.value.memory_chars == 3

    reset = engine.reset_conversation("default")
    assert isinstance(reset, Ok)
    assert reset.value >= 5

    await engine.aclose()
    assert llm.closed is True


@pytest.mark.anyio
async def test_engine_concurrency_queue_overflow_and_task_cancellation(
    tmp_path: Path,
) -> None:
    engine, store, llm, _logger = _make_engine(tmp_path)
    llm.chat_started = asyncio.Event()
    llm.chat_gate = asyncio.Event()

    t1 = asyncio.create_task(engine.submit_message("default", "first"))
    await llm.chat_started.wait()
    t2 = asyncio.create_task(engine.submit_message("default", "second"))
    await asyncio.sleep(0.05)
    assert llm.chat_calls == 1
    assert not t2.done()
    llm.chat_gate.set()
    assert all(isinstance(res, Ok) for res in await asyncio.gather(t1, t2))
    assert llm.chat_calls == 2

    stream = engine.subscribe("overflow")
    sub_task = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    for idx in range(66):
        await engine.submit_message("overflow", f"msg-{idx}")
    first_kept = await asyncio.wait_for(sub_task, timeout=5)
    assert first_kept.content == "reply text"
    await stream.aclose()

    sub_task2 = asyncio.create_task(engine.subscribe("cancel").__anext__())
    await asyncio.sleep(0)
    sub_task2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sub_task2

    delayed_engine, delayed_store, delayed_llm, _ = _make_engine(tmp_path / "delayed")
    delayed_llm.eval_started = asyncio.Event()
    delayed_llm.eval_gate = asyncio.Event()
    pending = await delayed_engine.ingest_event("default", {"name": "pending.task"})
    assert isinstance(pending, Ok)
    await delayed_llm.eval_started.wait()
    await delayed_engine.aclose()
    loaded = delayed_store.get_event(pending.value.event_id)
    assert isinstance(loaded, Ok)
    assert loaded.value is not None
    assert loaded.value.status is EventStatus.RECEIVED
    await engine.aclose()


@pytest.mark.anyio
async def test_engine_worker_failure_and_store_error_propagation(
    tmp_path: Path,
) -> None:
    engine, store, llm, logger = _make_engine(tmp_path)
    err = AppError(AppErrorKind.STORAGE, "io fail")

    llm.eval_result = Err(AppError(AppErrorKind.LLM, "eval boom"))
    r_eval = await engine.ingest_event(
        "default", {"name": "a.b", "idempotency_key": "e1"}
    )
    assert isinstance(r_eval, Ok)
    await _wait_settled(store, r_eval.value.event_id)

    llm.eval_result = Ok(EventDecision(action=EventAction.IGNORE, reason="ignored"))
    llm.eval_error_raises = RuntimeError("unexpected boom")
    r_exc = await engine.ingest_event(
        "default", {"name": "a.c", "idempotency_key": "e2"}
    )
    assert isinstance(r_exc, Ok)
    await _wait_settled(store, r_exc.value.event_id)
    llm.eval_error_raises = None

    store.failures["finalize_event"] = AppError(AppErrorKind.STORAGE, "fin err")
    r_fin = await engine.ingest_event(
        "default", {"name": "a.d", "idempotency_key": "e3"}
    )
    assert isinstance(r_fin, Ok)
    await _wait_settled(store, r_fin.value.event_id)
    store.failures.clear()

    store.failures["read_memory"] = AppError(AppErrorKind.STORAGE, "mem err")
    r_mem = await engine.ingest_event(
        "default", {"name": "a.e", "idempotency_key": "e4"}
    )
    assert isinstance(r_mem, Ok)
    await _wait_settled(store, r_mem.value.event_id)
    store.failures.clear()

    assert len([e for e in logger.entries if e.event == "event_worker_failed"]) == 4
    tl = engine.get_timeline("default", limit=20)
    assert isinstance(tl, Ok)
    assert any(item.kind is TimelineKind.EVENT_FAILED for item in tl.value)
    failed_items = [item for item in tl.value if item.kind is TimelineKind.EVENT_FAILED]
    assert failed_items
    assert all(
        item.item_id == item.event_id and UUID(item.item_id).version == 7
        for item in failed_items
    )

    assert_err(
        await engine.ingest_event("default", {"name": "BAD"}),
        kind=AppErrorKind.VALIDATION,
        message_contains="Invalid event field",
    )

    store.failures["ensure_conversation"] = err
    assert_err(
        await engine.ingest_event("default", {"name": "ok.name"}),
        kind=AppErrorKind.STORAGE,
        message_contains="io fail",
    )
    store.failures.clear()

    store.failures["insert_event"] = err
    assert_err(
        await engine.ingest_event("default", {"name": "ok.name"}),
        kind=AppErrorKind.STORAGE,
        message_contains="io fail",
    )
    store.failures.clear()

    store.failures["bump_epoch"] = err
    assert_err(
        await engine.submit_message("default", "hi"),
        kind=AppErrorKind.STORAGE,
        message_contains="io fail",
    )
    store.failures.clear()

    store.failures["read_memory"] = err
    assert_err(
        await engine.submit_message("default", "hi"),
        kind=AppErrorKind.STORAGE,
        message_contains="io fail",
    )
    assert_err(
        engine.get_memory_snapshot("default"),
        kind=AppErrorKind.STORAGE,
        message_contains="io fail",
    )
    assert_err(
        await engine.context_snapshot("default"),
        kind=AppErrorKind.STORAGE,
        message_contains="io fail",
    )
    store.failures.clear()

    store.failures["list_pending_notes"] = err
    assert_err(
        await engine.submit_message("default", "hi"),
        kind=AppErrorKind.STORAGE,
        message_contains="io fail",
    )
    store.failures.clear()

    store.failures["list_timeline"] = err
    assert_err(
        engine.get_timeline("default"),
        kind=AppErrorKind.STORAGE,
        message_contains="io fail",
    )
    store.failures.clear()

    store.failures["recent_messages"] = err
    assert_err(
        await engine.submit_message("default", "hi"),
        kind=AppErrorKind.STORAGE,
        message_contains="io fail",
    )
    store.failures.clear()

    store.failures["append_messages"] = err
    assert_err(
        await engine.submit_message("default", "hi"),
        kind=AppErrorKind.STORAGE,
        message_contains="io fail",
    )
    store.failures.clear()

    llm.chat_result = Err(AppError(AppErrorKind.LLM, "chat err"))
    assert_err(
        await engine.submit_message("default", "hi"),
        kind=AppErrorKind.LLM,
        message_contains="chat err",
    )
    llm.chat_result = Ok(ChatReply("ok", ()))
    llm.eval_result = Ok(
        EventDecision(
            action=EventAction.NOTE, message="", note="pending", reason="info"
        )
    )
    r_note = await engine.ingest_event(
        "default", {"name": "a.f", "idempotency_key": "e5"}
    )
    assert isinstance(r_note, Ok)
    await _wait_settled(store, r_note.value.event_id)

    store.failures["mark_surfaced"] = err
    assert_err(
        await engine.submit_message("default", "hi"),
        kind=AppErrorKind.STORAGE,
        message_contains="ambient events",
    )
    store.failures.clear()

    store.failures["get_state"] = err
    assert_err(
        await engine.context_snapshot("default"),
        kind=AppErrorKind.STORAGE,
        message_contains="io fail",
    )
    store.failures.clear()

    store.failures["counts"] = err
    assert_err(
        await engine.context_snapshot("default"),
        kind=AppErrorKind.STORAGE,
        message_contains="io fail",
    )
    assert_err(
        engine.get_memory_snapshot("default"),
        kind=AppErrorKind.STORAGE,
        message_contains="io fail",
    )
    store.failures.clear()
    await engine.aclose()


@pytest.mark.anyio
async def test_engine_push_delivery_subscriptions_and_item_ids(
    tmp_path: Path,
) -> None:
    deliverer = _FakeDeliverer()
    engine, store, llm, _logger = _make_engine(tmp_path, deliverer=deliverer)
    assert engine.push_public_key == "test-public-key"

    subscription = PushSubscription(
        endpoint="https://push.example.com/1",
        p256dh="p256dh",
        auth=SecretStr("auth"),
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        conversation_id="push:demo",
    )
    assert isinstance(engine.save_push_subscription(subscription), Ok)
    assert engine.delete_push_subscription("https://push.example.com/1") == Ok(True)
    assert engine.delete_push_subscription("https://push.example.com/1") == Ok(False)

    llm.eval_result = Ok(
        EventDecision(
            action=EventAction.SPEAK,
            message="キッチンの温度が上がっています。",
            note="urgent",
            reason="alarm",
        )
    )
    receipt = await engine.ingest_event(
        "push:demo", {"name": "alarm.high", "idempotency_key": "push-1"}
    )
    assert isinstance(receipt, Ok)
    await _wait_settled(store, receipt.value.event_id)
    for _ in range(200):
        if deliverer.payloads:
            break
        await asyncio.sleep(0.01)

    assert deliverer.payloads == [
        PushPayload(
            conversation_id="push:demo",
            title=DEFAULT_PERSONA.name,
            body="キッチンの温度が上がっています。",
            url="/?c=push%3Ademo",
            tag=f"proactive:{receipt.value.event_id}",
            event_id=receipt.value.event_id,
        )
    ]

    timeline = engine.get_timeline("push:demo", limit=20)
    assert isinstance(timeline, Ok)
    message_items = [
        item for item in timeline.value if item.origin is MessageOrigin.PROACTIVE
    ]
    assert len(message_items) == 1
    assert UUID(message_items[0].item_id).version == 7

    conversations = engine.list_conversations()
    assert isinstance(conversations, Ok)
    summary = conversations.value[0]
    assert summary.conversation_id == "push:demo"
    assert (summary.message_count, summary.event_count) == (1, 1)
    assert summary.last_message_preview == "キッチンの温度が上がっています。"
    assert summary.last_message_origin is MessageOrigin.PROACTIVE

    stream = engine.subscribe("push:demo")
    pending = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    reply = await engine.submit_message("push:demo", "ありがとう")
    assert isinstance(reply, Ok)
    item = await asyncio.wait_for(pending, timeout=5)
    assert item.item_id == reply.value.message_id
    await stream.aclose()

    await engine.aclose()
    assert deliverer.closed is True


@pytest.mark.anyio
async def test_engine_push_delivery_failure_is_logged_without_failing_event(
    tmp_path: Path,
) -> None:
    deliverer = _FakeDeliverer(public=None)
    deliverer.failure = RuntimeError("push boom")
    engine, store, llm, logger = _make_engine(tmp_path, deliverer=deliverer)
    assert engine.push_public_key is None
    llm.eval_result = Ok(
        EventDecision(
            action=EventAction.SPEAK,
            message="urgent",
            note="urgent",
            reason="alarm",
        )
    )
    receipt = await engine.ingest_event(
        "default", {"name": "alarm.high", "idempotency_key": "push-fail"}
    )
    assert isinstance(receipt, Ok)
    await _wait_settled(store, receipt.value.event_id)
    for _ in range(200):
        if any(entry.event == "push_delivery_failed" for entry in logger.entries):
            break
        await asyncio.sleep(0.01)

    loaded = store.get_event(receipt.value.event_id)
    assert isinstance(loaded, Ok)
    assert loaded.value is not None
    assert loaded.value.status is EventStatus.COMPLETED
    push_errors = [
        entry for entry in logger.entries if entry.event == "push_delivery_failed"
    ]
    assert len(push_errors) == 1
    assert "push boom" in push_errors[0].message
    assert push_errors[0].error_type == "PUSH_FAILED"
    assert not any(entry.event == "event_worker_failed" for entry in logger.entries)
    await engine.aclose()


@pytest.mark.anyio
async def test_engine_timeline_cursor_and_default_deliverer(tmp_path: Path) -> None:
    engine, _store, _llm, _logger = _make_engine(tmp_path)
    assert engine.push_public_key is None
    for text in ("first", "second", "third"):
        assert isinstance(await engine.submit_message("default", text), Ok)

    timeline = engine.get_timeline("default", limit=10)
    assert isinstance(timeline, Ok)
    items = list(timeline.value)
    assert len(items) == 6
    assert all(UUID(item.item_id).version == 7 for item in items)
    assert all(
        items[index].item_id < items[index + 1].item_id
        for index in range(len(items) - 1)
    )
    first = items[0]
    fourth = items[3]
    sixth = items[5]

    after_cursor = engine.get_timeline("default", after_id=fourth.item_id)
    after_last = engine.get_timeline("default", after_id=sixth.item_id)
    after_first = engine.get_timeline("default", after_id=first.item_id)
    zero = engine.get_timeline("default", limit=0)

    assert after_cursor == Ok(tuple(items[4:]))
    assert after_last == Ok(())
    assert after_first == Ok(tuple(items[1:]))
    assert zero == Ok(())
    await engine.aclose()


@pytest.mark.anyio
async def test_engine_timeline_orders_same_timestamp_turns_via_uuid7(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = Store(settings.db_path, settings.core_memory_path)
    t0 = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
    engine = CharacterEngine(
        store,
        _FakeLlm(),
        settings,
        DEFAULT_PERSONA,
        _SpyLogger(),
        clock=lambda: t0,
    )
    for index in range(5):
        assert isinstance(await engine.submit_message("default", f"turn {index}"), Ok)

    timeline = engine.get_timeline("default", limit=20)
    assert isinstance(timeline, Ok)
    items = list(timeline.value)
    assert len(items) == 10
    assert all(item.created_at == t0 for item in items)
    assert all(UUID(item.item_id).version == 7 for item in items)
    assert all(
        items[index].item_id < items[index + 1].item_id
        for index in range(len(items) - 1)
    )
    assert [item.origin for item in items] == [
        MessageOrigin.USER,
        MessageOrigin.REPLY,
    ] * 5

    after_nine = engine.get_timeline("default", limit=20, after_id=items[8].item_id)

    assert after_nine == Ok((items[9],))
    await engine.aclose()


@pytest.mark.anyio
async def test_engine_failure_publication_skips_when_fail_event_errors(
    tmp_path: Path,
) -> None:
    engine, store, llm, logger = _make_engine(tmp_path)
    store.failures["fail_event"] = AppError(AppErrorKind.STORAGE, "fail write")
    llm.eval_result = Err(AppError(AppErrorKind.LLM, "eval boom"))
    receipt = await engine.ingest_event(
        "default", {"name": "a.fail", "idempotency_key": "fail-1"}
    )
    assert isinstance(receipt, Ok)
    for _ in range(200):
        if any(entry.event == "event_worker_failed" for entry in logger.entries):
            break
        await asyncio.sleep(0.01)
    assert any(entry.event == "event_worker_failed" for entry in logger.entries)
    timeline = engine.get_timeline("default")
    assert isinstance(timeline, Ok)
    assert all(item.kind is not TimelineKind.EVENT_FAILED for item in timeline.value)
    await engine.aclose()


def _note_record(event_id: str, minutes: int, *, conversation_id: str = "default"):
    when = datetime(2026, 5, 1, 10, 0, tzinfo=UTC) + timedelta(minutes=minutes)
    return EventRecord(
        event_id=event_id,
        conversation_id=conversation_id,
        idempotency_key=event_id,
        name="email.received",
        source="mail",
        occurred_at=when,
        received_at=when,
        status=EventStatus.COMPLETED,
        action=EventAction.NOTE,
        reason="ambient",
        note=f"note {minutes}",
        payload_json="{}",
        failure_reason="",
        surfaced_at=None,
    )


@pytest.mark.anyio
async def test_engine_surfaces_only_rendered_notes_across_turns(tmp_path: Path) -> None:
    engine, store, llm, _logger = _make_engine(tmp_path)
    for index in range(7):
        assert isinstance(store.insert_event(_note_record(f"evt_{index}", index)), Ok)

    first = await engine.submit_message("default", "first")
    assert isinstance(first, Ok)
    assert first.value.surfaced_event_ids == (
        "evt_6",
        "evt_5",
        "evt_4",
        "evt_3",
        "evt_2",
    )
    assert llm.last_context is not None
    assert llm.last_context.pending_event_count == 7
    assert llm.last_context.omitted_events == 2

    second = await engine.submit_message("default", "second")
    assert isinstance(second, Ok)
    assert second.value.surfaced_event_ids == ("evt_1", "evt_0")
    assert llm.last_context is not None
    assert llm.last_context.pending_event_count == 2
    assert llm.last_context.omitted_events == 0

    third = await engine.submit_message("default", "third")
    assert isinstance(third, Ok)
    assert third.value.surfaced_event_ids == ()
    assert llm.last_context is not None
    assert llm.last_context.pending_event_count == 0
    assert llm.last_context.omitted_events == 0

    context = await engine.context_snapshot("default")
    assert isinstance(context, Ok)
    assert context.value.omitted_events == 0
    await engine.aclose()


@pytest.mark.anyio
async def test_engine_queue_overflow_sends_sentinel_and_aclose_terminates(
    tmp_path: Path,
) -> None:
    engine, _store, _llm, _logger = _make_engine(tmp_path)
    stream = engine.subscribe("overflow")
    first_pending = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    for index in range(66):
        assert isinstance(await engine.submit_message("overflow", f"msg-{index}"), Ok)
    first = await asyncio.wait_for(first_pending, timeout=5)
    assert first.content == "reply text"
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(stream.__anext__(), timeout=5)
    await stream.aclose()

    closing = engine.subscribe("closing")
    pending = asyncio.create_task(closing.__anext__())
    await asyncio.sleep(0)
    await engine.aclose()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(pending, timeout=5)
    await closing.aclose()


@pytest.mark.anyio
async def test_engine_redacts_failure_reasons_and_push_errors(tmp_path: Path) -> None:
    deliverer = _FakeDeliverer()
    engine, store, llm, logger = _make_engine(
        tmp_path,
        deliverer=deliverer,
        redactions=(SecretStr("leak-secret"),),
    )
    llm.eval_error_raises = RuntimeError("worker boom leak-secret")
    receipt = await engine.ingest_event(
        "default", {"name": "a.fail", "idempotency_key": "redact-1"}
    )
    assert isinstance(receipt, Ok)
    await _wait_settled(store, receipt.value.event_id)
    loaded = store.get_event(receipt.value.event_id)
    assert isinstance(loaded, Ok)
    assert loaded.value is not None
    assert "leak-secret" not in loaded.value.failure_reason
    assert "***" in loaded.value.failure_reason
    assert all(
        "leak-secret" not in entry.message
        for entry in logger.entries
        if entry.event == "event_worker_failed"
    )

    llm.eval_error_raises = None
    llm.eval_result = Ok(
        EventDecision(
            action=EventAction.SPEAK,
            message="urgent",
            note="urgent",
            reason="alarm",
        )
    )
    deliverer.failure = RuntimeError("push boom leak-secret")
    speaking = await engine.ingest_event(
        "default", {"name": "alarm.high", "idempotency_key": "redact-2"}
    )
    assert isinstance(speaking, Ok)
    await _wait_settled(store, speaking.value.event_id)
    for _ in range(200):
        if any(entry.event == "push_delivery_failed" for entry in logger.entries):
            break
        await asyncio.sleep(0.01)
    push_errors = [
        entry for entry in logger.entries if entry.event == "push_delivery_failed"
    ]
    assert push_errors
    assert all("leak-secret" not in entry.message for entry in push_errors)
    assert all("***" in entry.message for entry in push_errors)
    await engine.aclose()


@pytest.mark.anyio
async def test_engine_subscriber_cleanup_survives_shared_and_full_queues(
    tmp_path: Path,
) -> None:
    engine, _store, _llm, _logger = _make_engine(tmp_path)
    first = engine.subscribe("default")
    second = engine.subscribe("default")
    first_item = asyncio.create_task(first.__anext__())
    second_item = asyncio.create_task(second.__anext__())
    await asyncio.sleep(0)
    assert isinstance(await engine.submit_message("default", "hello"), Ok)
    assert (await first_item).content == "reply text"
    assert (await second_item).content == "reply text"
    for index in range(66):
        assert isinstance(await engine.submit_message("default", f"shared-{index}"), Ok)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(first.__anext__(), timeout=5)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(second.__anext__(), timeout=5)

    full = engine.subscribe("full")
    pending = asyncio.create_task(full.__anext__())
    await asyncio.sleep(0)
    for index in range(65):
        assert isinstance(await engine.submit_message("full", f"msg-{index}"), Ok)
    assert (await pending).content == "reply text"
    await engine.aclose()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(full.__anext__(), timeout=5)
    await full.aclose()
