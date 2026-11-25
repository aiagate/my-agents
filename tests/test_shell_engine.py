import asyncio
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from flow_res import Err, Ok, is_err

from my_agents.core.context import AssembledContext
from my_agents.core.types import (
    DEFAULT_PERSONA,
    AppError,
    AppErrorKind,
    DialogueTurn,
    EventBrief,
    EventDelivery,
    EventEnvelope,
    EventRecord,
    EventStatus,
    MemoryBlock,
    MemoryBlocks,
    MemoryCandidate,
    MemoryStatus,
    TimelineKind,
    TurnOrigin,
)
from my_agents.shell.engine import CharacterEngine, CurateOutcome
from my_agents.shell.llm import ChatReply, Llm, SummaryOutput
from my_agents.shell.logger import NOOP_LOGGER
from my_agents.shell.settings import load_settings
from my_agents.shell.store import Store
from tests.helpers import assert_err

_NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)


def _error(result):
    assert is_err(result)
    return result.error


def _brief(**overrides: object) -> EventBrief:
    values: dict[str, object] = {
        "headline": "室内の温度が高いです",
        "facts": ("33°C",),
        "impact": "暑い",
        "suggested_angle": "換気",
        "urgency": 8,
        "relevance": 8,
        "confidence": 0.9,
        "requires_user_action": True,
        "sensitive": False,
        "dedupe_key": "k",
    }
    values.update(overrides)
    return EventBrief.model_validate(values)


class FakeLlm:
    def __init__(
        self,
        *,
        brief: EventBrief | None = None,
        compose_text: str = "少し暑いので換気しませんか。",
        compose_gate: asyncio.Event | None = None,
        distill_error: AppError | None = None,
        compose_error: AppError | None = None,
        summary_error: AppError | None = None,
        chat_error: AppError | None = None,
        raise_distill: bool = False,
    ) -> None:
        self.brief = brief or _brief()
        self.compose_text = compose_text
        self.compose_gate = compose_gate
        self.compose_started = asyncio.Event()
        self.distill_error = distill_error
        self.compose_error = compose_error
        self.summary_error = summary_error
        self.chat_error = chat_error
        self.raise_distill = raise_distill
        self.started = 0
        self.closed = 0

    async def start(self):
        self.started += 1
        return Ok(None)

    async def aclose(self) -> None:
        self.closed += 1

    async def chat(
        self,
        conversation_id: str,
        user_text: str,
        history: tuple[DialogueTurn, ...],
        context: AssembledContext,
    ):
        del conversation_id, history, context
        if self.chat_error is not None:
            return Err(self.chat_error)
        return Ok(ChatReply(f"返信: {user_text}", ()))

    async def distill(self, envelope: EventEnvelope):
        del envelope
        if self.raise_distill:
            raise RuntimeError("distiller exploded")
        if self.distill_error is not None:
            return Err(self.distill_error)
        return Ok(self.brief)

    async def compose(self, brief: EventBrief, context: AssembledContext):
        del brief, context
        self.compose_started.set()
        if self.compose_gate is not None:
            await self.compose_gate.wait()
        if self.compose_error is not None:
            return Err(self.compose_error)
        return Ok(self.compose_text)

    async def summarize(self, turns: tuple[DialogueTurn, ...], previous: str):
        del turns, previous
        if self.summary_error is not None:
            return Err(self.summary_error)
        return Ok(SummaryOutput("title", "overview", "summary"))


def _settings(tmp_path: Path, **overrides: str):
    env = {
        "MEMORY_PATH": str(tmp_path),
        "CHAT_BACKEND": "local",
        "PROACTIVE_USER_ACTIVE_MINUTES": "0",
        "PROACTIVE_COOLDOWN_MINUTES": "0",
        "PROACTIVE_DAILY_CAP": "99",
        "EVENT_ANALYSIS_DELAY_MS": "0",
        **overrides,
    }
    return load_settings(env=env).unwrap()


def _harness(
    tmp_path: Path, llm=None, **overrides: str
) -> tuple[CharacterEngine, Store]:
    settings = _settings(tmp_path, **overrides)
    store = Store(settings.db_path, settings.core_memory_path)
    if llm is None:
        llm = Llm.from_settings(settings, store=store, logger=NOOP_LOGGER).unwrap()
    return (
        CharacterEngine(
            store, llm, settings, DEFAULT_PERSONA, NOOP_LOGGER, clock=lambda: _NOW
        ),
        store,
    )


def _engine(tmp_path: Path, llm=None, **overrides: str) -> CharacterEngine:
    return _harness(tmp_path, llm, **overrides)[0]


def _raw_event(name: str = "temperature.threshold_exceeded", key: str = "key"):
    return {
        "name": name,
        "data": {"celsius": 33.2, "threshold_celsius": 30.0, "sensor_id": "kitchen"},
        "idempotency_key": key,
    }


async def _wait_for_status(engine: CharacterEngine, event_id: str, status: EventStatus):
    for _ in range(200):
        event = engine.get_event(event_id).unwrap()
        if event is not None and event.status is status:
            return event
        await asyncio.sleep(0.01)
    raise AssertionError(f"event {event_id} never reached {status}")


@pytest.mark.anyio
async def test_submit_message_persists_turn_and_surfaces_ambient(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    intake = await engine.ingest_event("c", _raw_event())
    event_id = intake.unwrap().event_id
    await _wait_for_status(engine, event_id, EventStatus.COMPLETED)
    outcome = await engine.submit_message("c", " こんにちは ")
    assert outcome.unwrap().reply.startswith("ローカル応答")
    assert outcome.unwrap().turn_index == 3
    assert engine.get_memory().unwrap().user == ""
    timeline = engine.get_timeline("c").unwrap()
    assert any(item.kind is TimelineKind.EVENT for item in timeline)
    assert engine.list_events("c").unwrap()[0].event_id == event_id
    assert engine.reset_conversation("c").unwrap() >= 2
    await engine.aclose()


@pytest.mark.anyio
async def test_submit_message_rejects_empty_and_reports_chat_failure(
    tmp_path: Path,
) -> None:
    error = AppError(AppErrorKind.LLM, "chat failed")
    engine = _engine(tmp_path, FakeLlm(chat_error=error))
    assert_err(
        await engine.submit_message("c", "  "),
        kind=AppErrorKind.VALIDATION,
        message_contains="empty",
    )
    assert_err(
        await engine.submit_message("c", "hi"),
        kind=AppErrorKind.LLM,
        message_contains="chat failed",
    )
    await engine.aclose()


@pytest.mark.anyio
async def test_duplicate_event_is_idempotent(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    first = await engine.ingest_event("c", _raw_event())
    second = await engine.ingest_event("c", _raw_event())
    assert first.unwrap().event_id == second.unwrap().event_id
    assert not first.unwrap().duplicate
    assert second.unwrap().duplicate
    await engine.aclose()


@pytest.mark.anyio
async def test_proactive_message_is_published_to_subscribers(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    async with aclosing(engine.subscribe("c")) as stream:
        pending = asyncio.ensure_future(anext(stream))
        intake = await engine.ingest_event("c", _raw_event())
        event_id = intake.unwrap().event_id
        item = await asyncio.wait_for(pending, timeout=2)
        assert item.kind is TimelineKind.TURN
        assert item.origin is TurnOrigin.PROACTIVE
        assert item.source_event_id == event_id
    await engine.aclose()


@pytest.mark.anyio
async def test_epoch_change_demotes_proactive_to_ambient(tmp_path: Path) -> None:
    gate = asyncio.Event()
    fake = FakeLlm(compose_gate=gate)
    engine = _engine(tmp_path, fake)
    intake = await engine.ingest_event("c", _raw_event())
    event_id = intake.unwrap().event_id
    await asyncio.wait_for(fake.compose_started.wait(), timeout=2)
    await engine.submit_message("c", "割り込み")
    gate.set()
    event = await _wait_for_status(engine, event_id, EventStatus.COMPLETED)
    assert event.delivery is EventDelivery.AMBIENT
    assert event.merge_reason == "epoch_conflict"
    await engine.aclose()


@pytest.mark.anyio
async def test_invalid_proactive_message_demotes_to_ambient(tmp_path: Path) -> None:
    fake = FakeLlm(compose_text="[event:evt_1] raw")
    engine = _engine(tmp_path, fake)
    intake = await engine.ingest_event("c", _raw_event())
    event = await _wait_for_status(
        engine, intake.unwrap().event_id, EventStatus.COMPLETED
    )
    assert event.delivery is EventDelivery.AMBIENT
    assert event.merge_reason == "invalid_proactive"
    await engine.aclose()


@pytest.mark.anyio
async def test_sensitive_event_is_suppressed_silently(tmp_path: Path) -> None:
    engine = _engine(tmp_path, FakeLlm(brief=_brief(sensitive=True)))
    intake = await engine.ingest_event("c", _raw_event())
    event = await _wait_for_status(
        engine, intake.unwrap().event_id, EventStatus.SUPPRESSED
    )
    assert event.delivery is EventDelivery.SILENT
    await engine.aclose()


@pytest.mark.anyio
async def test_failed_distillation_records_failure_and_notifies(tmp_path: Path) -> None:
    engine = _engine(tmp_path, FakeLlm(raise_distill=True))
    async with aclosing(engine.subscribe("c")) as stream:
        pending = asyncio.ensure_future(anext(stream))
        intake = await engine.ingest_event("c", _raw_event())
        event = await _wait_for_status(
            engine, intake.unwrap().event_id, EventStatus.FAILED
        )
        assert "distiller exploded" in event.failure_reason
        item = await asyncio.wait_for(pending, timeout=2)
        assert item.kind is TimelineKind.EVENT_FAILED
    await engine.aclose()


@pytest.mark.anyio
async def test_ingest_rejects_schema_and_reports_store_failure(tmp_path: Path) -> None:
    engine, store = _harness(tmp_path)
    assert_err(
        await engine.ingest_event("c", {"name": "Bad"}),
        kind=AppErrorKind.VALIDATION,
        message_contains="Invalid event field",
    )
    with patch.object(
        store,
        "ensure_conversation",
        return_value=Err(AppError(AppErrorKind.STORAGE, "ensure failed")),
    ):
        assert_err(
            await engine.ingest_event("c", _raw_event("ok.event")),
            kind=AppErrorKind.STORAGE,
            message_contains="ensure failed",
        )
    with patch.object(
        store,
        "insert_event",
        return_value=Err(AppError(AppErrorKind.STORAGE, "insert failed")),
    ):
        assert_err(
            await engine.ingest_event("c", _raw_event("ok.event", "k2")),
            kind=AppErrorKind.STORAGE,
            message_contains="insert failed",
        )
    await engine.aclose()


@pytest.mark.anyio
async def test_curate_summarizes_and_promotes_candidates(tmp_path: Path) -> None:
    engine, store = _harness(tmp_path, FakeLlm(), SUMMARY_EVERY_TURNS="1")
    await engine.submit_message("c", "hello")
    store.save_candidate(
        MemoryCandidate("m1", "c", MemoryBlock.USER, "likes tea", "reason", _NOW)
    ).unwrap()
    outcome = await engine.curate_memory("c")
    assert outcome.unwrap() == CurateOutcome(1, 1, 0)
    assert engine.get_memory().unwrap().user == "- likes tea"
    assert (
        store.list_candidates("c", MemoryStatus.PROMOTED).unwrap()[0].candidate_id
        == "m1"
    )
    second = await engine.curate_memory("c")
    assert second.unwrap() == CurateOutcome(0, 0, 0)
    await engine.aclose()


@pytest.mark.anyio
async def test_curate_reports_summary_and_store_errors(tmp_path: Path) -> None:
    error = AppError(AppErrorKind.LLM, "summary failed")
    engine, store = _harness(
        tmp_path, FakeLlm(summary_error=error), SUMMARY_EVERY_TURNS="1"
    )
    await engine.submit_message("c", "hello")
    assert_err(
        await engine.curate_memory("c"),
        kind=AppErrorKind.LLM,
        message_contains="summary failed",
    )
    with patch.object(
        store,
        "list_episodes",
        return_value=Err(AppError(AppErrorKind.STORAGE, "episodes failed")),
    ):
        assert_err(
            await engine.curate_memory("c"),
            kind=AppErrorKind.STORAGE,
            message_contains="episodes failed",
        )
    await engine.aclose()


@pytest.mark.anyio
async def test_submit_message_store_error_paths(tmp_path: Path) -> None:
    storage = AppError(AppErrorKind.STORAGE, "store failed")
    engine, store = _harness(tmp_path)
    with patch.object(
        store,
        "bump_epoch",
        return_value=Err(storage),
    ):
        assert (
            _error(await engine.submit_message("c", "hello")).message == "store failed"
        )
    for method in (
        "ensure_conversation",
        "bump_epoch",
        "read_memory",
        "list_episodes",
        "list_events",
        "recent_turns",
        "get_open_episode",
        "record_turns",
    ):
        with patch.object(store, method, return_value=Err(storage)):
            result = await engine.submit_message("c", "hello")
            assert _error(result).message == "store failed"
    await engine.aclose()


@pytest.mark.anyio
async def test_submit_message_mark_surfaced_and_second_event_read_error(
    tmp_path: Path,
) -> None:
    storage = AppError(AppErrorKind.STORAGE, "store failed")
    engine, store = _harness(tmp_path)
    await engine.ingest_event("c", _raw_event())
    await asyncio.sleep(0.05)
    events = store.list_events("c").unwrap()
    ambient = [
        type(event)(
            event.event_id,
            event.conversation_id,
            event.idempotency_key,
            event.name,
            event.source,
            event.occurred_at,
            event.received_at,
            event.status,
            EventDelivery.AMBIENT,
            event.merge_reason,
            event.payload_json,
            event.brief,
            event.failure_reason,
            None,
            None,
        )
        for event in events
    ]
    with (
        patch.object(
            store, "list_events", side_effect=[Ok(events), Ok(tuple(ambient))]
        ),
        patch.object(store, "mark_surfaced", return_value=Err(storage)),
    ):
        result = await engine.submit_message("c", "hello")
        assert _error(result).message == "Could not mark ambient events."
    with patch.object(store, "list_events", side_effect=[Ok(()), Err(storage)]):
        result = await engine.submit_message("c", "hello")
        assert _error(result).message == "store failed"
    await engine.aclose()


@pytest.mark.anyio
async def test_process_event_error_paths(tmp_path: Path) -> None:
    storage = AppError(AppErrorKind.STORAGE, "store failed")
    llm_error = AppError(AppErrorKind.LLM, "distill failed")
    engine, store = _harness(tmp_path, FakeLlm(distill_error=llm_error))
    first = await engine.ingest_event("c", _raw_event(key="a"))
    await _wait_for_status(engine, first.unwrap().event_id, EventStatus.FAILED)
    await engine.aclose()

    engine, store = _harness(tmp_path / "state", FakeLlm())
    with patch.object(store, "get_state", return_value=Err(storage)):
        receipt = await engine.ingest_event("c", _raw_event(key="b"))
        await _wait_for_status(engine, receipt.unwrap().event_id, EventStatus.FAILED)
    await engine.aclose()

    engine, store = _harness(tmp_path / "context", FakeLlm())
    with patch.object(store, "read_memory", return_value=Err(storage)):
        receipt = await engine.ingest_event("c", _raw_event(key="c"))
        await _wait_for_status(engine, receipt.unwrap().event_id, EventStatus.FAILED)
    await engine.aclose()

    engine, store = _harness(tmp_path / "episodes", FakeLlm())
    with patch.object(store, "list_episodes", return_value=Err(storage)):
        receipt = await engine.ingest_event("c", _raw_event(key="c2"))
        await _wait_for_status(engine, receipt.unwrap().event_id, EventStatus.FAILED)
    await engine.aclose()

    engine, store = _harness(tmp_path / "events", FakeLlm())
    with patch.object(store, "list_events", return_value=Err(storage)):
        receipt = await engine.ingest_event("c", _raw_event(key="c3"))
        await _wait_for_status(engine, receipt.unwrap().event_id, EventStatus.FAILED)
    await engine.aclose()

    engine, store = _harness(tmp_path / "compose", FakeLlm(compose_error=storage))
    receipt = await engine.ingest_event("c", _raw_event(key="c4"))
    event = await _wait_for_status(
        engine, receipt.unwrap().event_id, EventStatus.COMPLETED
    )
    assert event.delivery is EventDelivery.AMBIENT
    await engine.aclose()

    engine, store = _harness(tmp_path / "update", FakeLlm())
    with patch.object(store, "commit_proactive", return_value=Err(storage)):
        receipt = await engine.ingest_event("c", _raw_event(key="g"))
        await _wait_for_status(engine, receipt.unwrap().event_id, EventStatus.FAILED)
    await engine.aclose()


@pytest.mark.anyio
async def test_delay_and_timeline_error_paths(tmp_path: Path) -> None:
    storage = AppError(AppErrorKind.STORAGE, "store failed")
    engine, store = _harness(tmp_path, FakeLlm(), EVENT_ANALYSIS_DELAY_MS="1")
    receipt = await engine.ingest_event("c", _raw_event())
    event = await _wait_for_status(
        engine, receipt.unwrap().event_id, EventStatus.COMPLETED
    )
    assert event.delivery is EventDelivery.PROACTIVE
    with patch.object(store, "recent_turns", return_value=Err(storage)):
        assert _error(engine.get_timeline("c")).message == "store failed"
    with patch.object(store, "list_events", return_value=Err(storage)):
        assert _error(engine.get_timeline("c")).message == "store failed"
    await engine.aclose()


@pytest.mark.anyio
async def test_timeline_renders_every_event_status(tmp_path: Path) -> None:
    engine, store = _harness(tmp_path)
    for status in (
        EventStatus.RECEIVED,
        EventStatus.FAILED,
        EventStatus.SUPPRESSED,
    ):
        store.insert_event(
            EventRecord(
                f"evt_{status.value}",
                "c",
                f"evt_{status.value}",
                "name",
                "source",
                _NOW,
                _NOW,
                status,
                EventDelivery.SILENT,
                "",
                "{}",
                None,
                "",
                None,
                None,
            )
        )
    kinds = [item.kind for item in engine.get_timeline("c").unwrap()]
    assert kinds == [
        TimelineKind.EVENT,
        TimelineKind.EVENT_FAILED,
        TimelineKind.EVENT,
    ]
    await engine.aclose()


@pytest.mark.anyio
async def test_curate_store_error_paths(tmp_path: Path) -> None:
    storage = AppError(AppErrorKind.STORAGE, "store failed")
    engine, store = _harness(tmp_path, FakeLlm(), SUMMARY_EVERY_TURNS="1")
    await engine.submit_message("c", "hello")
    for method in ("list_turns", "update_summary", "list_candidates"):
        with patch.object(store, method, return_value=Err(storage)):
            result = await engine.curate_memory("c")
            assert _error(result).message == "store failed"
    with patch.object(store, "update_memory", return_value=Err(storage)):
        assert _error(await engine.curate_memory("c")).message == "store failed"
    store.save_candidate(
        MemoryCandidate("m1", "c", MemoryBlock.USER, "likes tea", "r", _NOW)
    ).unwrap()
    with patch.object(store, "update_memory", return_value=Err(storage)):
        assert _error(await engine.curate_memory("c")).message == "store failed"
    with patch.object(store, "set_candidate_status", return_value=Err(storage)):
        assert _error(await engine.curate_memory("c")).message == "store failed"
    store.save_candidate(
        MemoryCandidate("m2", "c", MemoryBlock.USER, "x" * 5000, "r", _NOW)
    ).unwrap()
    with patch.object(store, "set_candidate_status", return_value=Err(storage)):
        assert _error(await engine.curate_memory("c")).message == "store failed"
    empty_engine, empty_store = _harness(tmp_path / "empty", FakeLlm())
    assert (await empty_engine.curate_memory("new")).unwrap() == CurateOutcome(0, 0, 0)
    empty_store.save_candidate(
        MemoryCandidate("big", "new", MemoryBlock.USER, "x" * 5000, "r", _NOW)
    ).unwrap()
    assert (await empty_engine.curate_memory("new")).unwrap() == CurateOutcome(0, 0, 1)
    await engine.aclose()
    await empty_engine.aclose()


@pytest.mark.anyio
async def test_aclose_cancels_background_tasks_and_clears_state(tmp_path: Path) -> None:
    gate = asyncio.Event()
    engine = _engine(tmp_path, FakeLlm(compose_gate=gate))
    await engine.ingest_event("c", _raw_event())
    await engine.aclose()
    await engine.aclose()
    assert [item.kind for item in engine.get_timeline("c").unwrap()] == [
        TimelineKind.EVENT
    ]
    gate.set()


def test_error_helpers_are_distinct() -> None:
    assert AppErrorKind.STORAGE != AppErrorKind.LLM
    assert TimelineKind.EVENT_FAILED.value == "event_failed"


@pytest.mark.anyio
async def test_context_and_memory_snapshots_report_live_counts(tmp_path: Path) -> None:
    engine, store = _harness(tmp_path)
    await engine.submit_message("c", "こんにちは")
    snapshot = (await engine.context_snapshot("c")).unwrap()
    assert snapshot.conversation_id == "c"
    assert snapshot.epoch == 1
    assert snapshot.counts.turns == 2
    assert snapshot.counts.episodes == 1
    assert snapshot.counts.events == 0
    assert snapshot.omitted_events == 0
    assert len(snapshot.stable_prefix_sha256) == 64
    assert "今日" not in snapshot.stable_prefix
    assert "今日" in snapshot.dynamic_section
    memory = engine.get_memory_snapshot("c").unwrap()
    assert memory.conversation_id == "c"
    assert memory.blocks.user == ""
    assert memory.counts.turns == 2
    assert memory.episodes[0].title == "こんにちは"
    assert store.counts("missing").unwrap().turns == 0
    await engine.aclose()


@pytest.mark.anyio
async def test_snapshot_errors_propagate(tmp_path: Path) -> None:
    storage = AppError(AppErrorKind.STORAGE, "store failed")
    engine, store = _harness(tmp_path)
    with patch.object(store, "get_state", return_value=Err(storage)):
        assert_err(
            await engine.context_snapshot("c"),
            kind=AppErrorKind.STORAGE,
            message_contains="store failed",
        )
    with patch.object(store, "read_memory", return_value=Err(storage)):
        assert_err(
            engine.get_memory_snapshot("c"),
            kind=AppErrorKind.STORAGE,
            message_contains="store failed",
        )
        assert_err(
            await engine.context_snapshot("c"),
            kind=AppErrorKind.STORAGE,
            message_contains="store failed",
        )
    with patch.object(store, "counts", return_value=Err(storage)):
        assert_err(
            engine.get_memory_snapshot("c"),
            kind=AppErrorKind.STORAGE,
            message_contains="store failed",
        )
        assert_err(
            await engine.context_snapshot("c"),
            kind=AppErrorKind.STORAGE,
            message_contains="store failed",
        )
    with patch.object(store, "list_episodes", return_value=Err(storage)):
        assert_err(
            engine.get_memory_snapshot("c"),
            kind=AppErrorKind.STORAGE,
            message_contains="store failed",
        )
    with patch.object(
        store,
        "read_memory",
        side_effect=[Ok(MemoryBlocks()), Err(storage)],
    ):
        assert_err(
            await engine.context_snapshot("c"),
            kind=AppErrorKind.STORAGE,
            message_contains="store failed",
        )
    await engine.aclose()


@pytest.mark.anyio
async def test_engine_start_delegates_to_llm(tmp_path: Path) -> None:
    llm = FakeLlm()
    engine = _engine(tmp_path, llm)
    await engine.start()
    assert llm.started == 1
    await engine.aclose()
    assert llm.closed == 1
