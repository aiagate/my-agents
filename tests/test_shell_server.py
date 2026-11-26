from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

import pytest
from flow_res import Err, Ok, Result
from starlette.testclient import TestClient

from my_agents.core.context import AssembledContext
from my_agents.core.types import (
    DEFAULT_PERSONA,
    AppError,
    AppErrorKind,
    ChatMessage,
    EventAction,
    EventDecision,
    EventEnvelope,
    MemoryBlocks,
    TimelineItem,
    TimelineKind,
)
from my_agents.shell.engine import CharacterEngine
from my_agents.shell.llm import ChatReply
from my_agents.shell.logger import IAppLogger, LogEntry
from my_agents.shell.server import (
    MAX_BODY_BYTES,
    _sse,
    _status_for,
    _stream,
    create_app,
    serve,
)
from my_agents.shell.settings import load_settings
from my_agents.shell.store import Store

_EVENT_KEYS = {
    "event_id",
    "conversation_id",
    "name",
    "source",
    "occurred_at",
    "received_at",
    "status",
    "action",
    "reason",
    "note",
    "failure_reason",
    "surfaced_at",
    "linked_message_id",
}

_TIMELINE_KEYS = {
    "kind",
    "conversation_id",
    "content",
    "created_at",
    "event_id",
    "source_event_id",
    "origin",
    "delivery",
    "status",
    "action",
    "reason",
}

_CONTEXT_KEYS = {
    "conversation_id",
    "epoch",
    "stable_prefix",
    "stable_prefix_sha256",
    "dynamic_section",
    "event_section",
    "omitted_events",
    "counts",
    "memory_chars",
}

_MEMORY_KEYS = {
    "conversation_id",
    "user",
    "relationship",
    "arc",
    "chars",
    "counts",
}

_COUNT_KEYS = {"messages", "events"}


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
        self.chat_result: Result[ChatReply, AppError] = Ok(
            ChatReply("server reply", ())
        )
        self.eval_result: Result[EventDecision, AppError] = Ok(
            EventDecision(
                action=EventAction.NOTE, message="", note="noted", reason="info"
            )
        )

    async def start(self) -> Result[None, AppError]:
        return self.start_result

    async def aclose(self) -> None:
        return None

    async def chat(
        self,
        conversation_id: str,
        user_text: str,
        history: Sequence[ChatMessage],
        context: AssembledContext,
    ) -> Result[ChatReply, AppError]:
        del conversation_id, user_text, history, context
        return self.chat_result

    async def evaluate_event(
        self, envelope: EventEnvelope, stable_prefix: str
    ) -> Result[EventDecision, AppError]:
        del envelope, stable_prefix
        return self.eval_result


def _engine(tmp_path: Path):
    settings = load_settings(
        env={"DATA_DIR": str(tmp_path), "EVENT_EVAL_DELAY_MS": "0"}
    )
    assert isinstance(settings, Ok)
    store = Store(settings.value.db_path, settings.value.core_memory_path)
    llm = _FakeLlm()
    logger = _SpyLogger()
    engine = CharacterEngine(
        store,
        llm,
        settings.value,
        DEFAULT_PERSONA,
        logger,
        clock=lambda: datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
    )
    return engine, store, llm, logger


def test_status_mapping_covers_every_error_kind() -> None:
    expected = {
        AppErrorKind.VALIDATION: HTTPStatus.UNPROCESSABLE_ENTITY,
        AppErrorKind.NOT_FOUND: HTTPStatus.NOT_FOUND,
        AppErrorKind.CONFLICT: HTTPStatus.CONFLICT,
        AppErrorKind.DENIED: HTTPStatus.FORBIDDEN,
        AppErrorKind.STORAGE: HTTPStatus.INTERNAL_SERVER_ERROR,
        AppErrorKind.LLM: HTTPStatus.BAD_GATEWAY,
        AppErrorKind.UNEXPECTED: HTTPStatus.INTERNAL_SERVER_ERROR,
    }
    for kind, status in expected.items():
        assert _status_for(kind) == status


def test_server_routes_guards_sse_and_reset(tmp_path: Path) -> None:
    engine, store, llm, _logger = _engine(tmp_path)
    assert isinstance(store.write_memory(MemoryBlocks(user="green tea")), Ok)

    with TestClient(create_app(engine)) as client:
        assert client.get("/health").json() == {"status": "ok", "service": "my-agents"}
        assert "EventSource(" in client.get("/").text

        accepted = client.post(
            "/events",
            json={"name": "email.received", "conversation_id": ""},
        )
        assert accepted.status_code == 202
        event_id = accepted.json()["event_id"]

        chat_res = client.post("/chat", json={"message": "hello"})
        assert chat_res.status_code == 200
        assert chat_res.json()["reply"] == "server reply"

        timeline_response = client.get("/timeline")
        assert timeline_response.status_code == 200
        timeline_items = timeline_response.json()["items"]
        assert timeline_items
        for item in timeline_items:
            assert set(item) == _TIMELINE_KEYS
            assert len(item) == 11
            assert next(iter(item)) == "kind"
        ctx = client.get("/context").json()
        assert set(ctx) == _CONTEXT_KEYS
        assert set(ctx["counts"]) == _COUNT_KEYS
        assert ctx["counts"] == {"messages": 2, "events": 1}
        mem = client.get("/memory").json()
        assert set(mem) == _MEMORY_KEYS
        assert set(mem["counts"]) == _COUNT_KEYS
        assert mem["user"] == "green tea"

        detail = client.get(f"/events/{event_id}")
        assert detail.status_code == 200
        detail_body = detail.json()
        assert set(detail_body) == _EVENT_KEYS
        assert len(detail_body) == 13
        assert detail_body["event_id"] == event_id
        assert "idempotency_key" not in detail_body
        assert "payload_json" not in detail_body
        assert client.get("/events/evt_missing").status_code == 404

        with patch.object(
            store,
            "get_event",
            return_value=Err(AppError(AppErrorKind.STORAGE, "db err")),
        ):
            assert client.get(f"/events/{event_id}").status_code == 500

        assert (
            client.post(
                "/events", content=b"{", headers={"content-type": "application/json"}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/events",
                content=b"[]",
                headers={"content-type": "application/json"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/events",
                content=b"{}",
                headers={"content-length": "not-an-int"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/events",
                content=b"x" * (MAX_BODY_BYTES + 1),
            ).status_code
            == 413
        )

        def chunked_big():
            yield b"a" * (MAX_BODY_BYTES + 1)

        assert client.post("/events", content=chunked_big()).status_code == 413
        assert (
            client.post("/reset", content=b"a" * (MAX_BODY_BYTES + 1)).status_code
            == 413
        )

        assert client.post("/events", json={"name": "BAD"}).status_code == 422
        assert (
            client.post(
                "/chat", content=b"{", headers={"content-type": "application/json"}
            ).status_code
            == 400
        )
        assert client.post("/chat", json={"message": 123}).status_code == 422
        assert client.get("/timeline?limit=bad").status_code == 400

        llm.chat_result = Err(AppError(AppErrorKind.LLM, "upstream down"))
        assert client.post("/chat", json={"message": "hi"}).status_code == 502

        reset = client.post("/reset")
        assert reset.status_code == 200
        assert reset.json()["deleted_messages"] == 2

        with client.stream("GET", "/stream?conversation_id=sse_test") as response:
            assert response.status_code == 200


@pytest.mark.anyio
async def test_sse_stream_helper_formats_items(tmp_path: Path) -> None:
    engine, _store, _llm, _logger = _engine(tmp_path)
    item = TimelineItem(
        TimelineKind.EVENT,
        "c1",
        "alert",
        datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        "evt_1",
        "evt_1",
        status=None,
        action=EventAction.NOTE,
        reason="info",
    )
    assert _sse(item).startswith('data: {"kind": "event"')

    async def fake_sub(conversation_id: str) -> AsyncIterator[TimelineItem]:
        assert conversation_id == "c1"
        yield item

    with patch.object(engine, "subscribe", side_effect=fake_sub):
        chunks = [chunk async for chunk in _stream(engine, "c1")]
    assert len(chunks) == 1
    assert '"evt_1"' in chunks[0]


def test_lifespan_start_failure_logs_and_serve_delegates_to_uvicorn(
    tmp_path: Path,
) -> None:
    engine, _store, llm, logger = _engine(tmp_path)
    llm.start_result = Err(AppError(AppErrorKind.LLM, "mcp fail"))

    with pytest.raises(RuntimeError, match="mcp fail"), TestClient(create_app(engine)):
        pass
    assert logger.entries[0].event == "server_start_failed"

    llm.start_result = Ok(None)
    calls: list[tuple[object, str, int, str]] = []
    with patch(
        "my_agents.shell.server.uvicorn.run",
        side_effect=lambda app, host, port, log_level: calls.append(
            (app, host, port, log_level)
        ),
    ):
        serve(engine, "127.0.0.1", 8123)
    assert calls[0][1:] == ("127.0.0.1", 8123, "warning")
