import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from flow_res import Err, Ok, Result

from my_agents.core.context import AssembledContext
from my_agents.core.types import (
    DEFAULT_PERSONA,
    AppError,
    AppErrorKind,
    ChatMessage,
    DeliveryMode,
    EventAction,
    EventDecision,
    EventEnvelope,
    EventStatus,
    MemoryBlocks,
    MessageOrigin,
    TimelineItem,
    TimelineKind,
)
from my_agents.shell.engine import CharacterEngine
from my_agents.shell.llm import ChatReply
from my_agents.shell.logger import IAppLogger, LogEntry
from my_agents.shell.settings import Settings, load_settings
from my_agents.shell.store import Store
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
        del conversation_id, user_text, history, context
        if self.chat_started is not None:
            self.chat_started.set()
        if self.chat_gate is not None:
            await self.chat_gate.wait()
        return self.chat_result

    async def evaluate_event(
        self, envelope: EventEnvelope, stable_prefix: str
    ) -> Result[EventDecision, AppError]:
        del envelope, stable_prefix
        if self.eval_error_raises is not None:
            raise self.eval_error_raises
        return self.eval_result


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
    tmp_path: Path, **overrides: str
) -> tuple[CharacterEngine, Store, _FakeLlm, _SpyLogger]:
    settings = _settings(tmp_path, **overrides)
    store = Store(settings.db_path, settings.core_memory_path)
    llm = _FakeLlm()
    logger = _SpyLogger()
    clock_now = [datetime(2026, 5, 1, 10, 0, tzinfo=UTC)]

    def clock() -> datetime:
        current = clock_now[0]
        clock_now[0] = current + timedelta(seconds=1)
        return current

    engine = CharacterEngine(store, llm, settings, DEFAULT_PERSONA, logger, clock=clock)
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
    engine, store, llm, _logger = _make_engine(tmp_path, EVENT_EVAL_DELAY_MS="1")
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
    assert engine.active_lock_count() == 0

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
    assert streamed[1].delivery is DeliveryMode.PUSH

    timeline = engine.get_timeline("default", limit=50)
    assert isinstance(timeline, Ok)
    assert any(item.origin is MessageOrigin.PROACTIVE for item in timeline.value)

    detail = engine.get_event(e1.value.event_id)
    assert isinstance(detail, Ok)
    assert detail.value is not None
    assert detail.value.action is EventAction.SPEAK

    assert isinstance(store.write_memory(MemoryBlocks(user="tea")), Ok)
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
    engine, store, llm, _logger = _make_engine(tmp_path, EVENT_EVAL_DELAY_MS="0")
    llm.chat_started = asyncio.Event()
    llm.chat_gate = asyncio.Event()

    t1 = asyncio.create_task(engine.submit_message("default", "first"))
    await llm.chat_started.wait()
    t2 = asyncio.create_task(engine.submit_message("default", "second"))
    await asyncio.sleep(0)
    assert engine.active_lock_count() == 1
    llm.chat_gate.set()
    assert all(isinstance(res, Ok) for res in await asyncio.gather(t1, t2))
    assert engine.active_lock_count() == 0

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

    delayed_engine, delayed_store, _delayed_llm, _ = _make_engine(
        tmp_path / "delayed", EVENT_EVAL_DELAY_MS="5000"
    )
    pending = await delayed_engine.ingest_event("default", {"name": "pending.task"})
    assert isinstance(pending, Ok)
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
    engine, store, llm, logger = _make_engine(tmp_path, EVENT_EVAL_DELAY_MS="0")

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

    with patch.object(
        store,
        "finalize_event",
        return_value=Err(AppError(AppErrorKind.STORAGE, "fin err")),
    ):
        r_fin = await engine.ingest_event(
            "default", {"name": "a.d", "idempotency_key": "e3"}
        )
        assert isinstance(r_fin, Ok)
        await _wait_settled(store, r_fin.value.event_id)

    with patch.object(
        store,
        "read_memory",
        return_value=Err(AppError(AppErrorKind.STORAGE, "mem err")),
    ):
        r_mem = await engine.ingest_event(
            "default", {"name": "a.e", "idempotency_key": "e4"}
        )
        assert isinstance(r_mem, Ok)
        await _wait_settled(store, r_mem.value.event_id)

    assert len([e for e in logger.entries if e.event == "event_worker_failed"]) == 4
    tl = engine.get_timeline("default", limit=20)
    assert isinstance(tl, Ok)
    assert any(item.kind is TimelineKind.EVENT_FAILED for item in tl.value)

    assert_err(
        await engine.ingest_event("default", {"name": "BAD"}),
        kind=AppErrorKind.VALIDATION,
        message_contains="Invalid event field",
    )
    err = AppError(AppErrorKind.STORAGE, "io fail")

    with patch.object(store, "ensure_conversation", return_value=Err(err)):
        assert_err(
            await engine.ingest_event("default", {"name": "ok.name"}),
            kind=AppErrorKind.STORAGE,
            message_contains="io fail",
        )
    with patch.object(store, "insert_event", return_value=Err(err)):
        assert_err(
            await engine.ingest_event("default", {"name": "ok.name"}),
            kind=AppErrorKind.STORAGE,
            message_contains="io fail",
        )
    with patch.object(store, "bump_epoch", return_value=Err(err)):
        assert_err(
            await engine.submit_message("default", "hi"),
            kind=AppErrorKind.STORAGE,
            message_contains="io fail",
        )
    with patch.object(store, "read_memory", return_value=Err(err)):
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
    with patch.object(store, "list_events", return_value=Err(err)):
        assert_err(
            await engine.submit_message("default", "hi"),
            kind=AppErrorKind.STORAGE,
            message_contains="io fail",
        )
        assert_err(
            engine.get_timeline("default"),
            kind=AppErrorKind.STORAGE,
            message_contains="io fail",
        )
    with patch.object(store, "recent_messages", return_value=Err(err)):
        assert_err(
            await engine.submit_message("default", "hi"),
            kind=AppErrorKind.STORAGE,
            message_contains="io fail",
        )
        assert_err(
            engine.get_timeline("default"),
            kind=AppErrorKind.STORAGE,
            message_contains="io fail",
        )
    with patch.object(store, "append_messages", return_value=Err(err)):
        assert_err(
            await engine.submit_message("default", "hi"),
            kind=AppErrorKind.STORAGE,
            message_contains="io fail",
        )
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
    with patch.object(store, "mark_surfaced", return_value=Err(err)):
        assert_err(
            await engine.submit_message("default", "hi"),
            kind=AppErrorKind.STORAGE,
            message_contains="ambient events",
        )
    with patch.object(store, "get_state", return_value=Err(err)):
        assert_err(
            await engine.context_snapshot("default"),
            kind=AppErrorKind.STORAGE,
            message_contains="io fail",
        )
    with patch.object(store, "counts", return_value=Err(err)):
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
    await engine.aclose()
