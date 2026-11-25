import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from flow_res import Err
from starlette.types import Message, Scope

from my_agents.core.types import (
    DEFAULT_PERSONA,
    AppError,
    AppErrorKind,
    EventStatus,
)
from my_agents.shell.engine import CharacterEngine
from my_agents.shell.llm import Llm
from my_agents.shell.logger import NOOP_LOGGER
from my_agents.shell.server import MAX_BODY_BYTES, create_app, serve
from my_agents.shell.settings import load_settings
from my_agents.shell.store import Store

_NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)


def _engine(tmp_path: Path, **overrides: str) -> CharacterEngine:
    env = {
        "MEMORY_PATH": str(tmp_path),
        "CHAT_BACKEND": "local",
        "PROACTIVE_USER_ACTIVE_MINUTES": "0",
        "PROACTIVE_COOLDOWN_MINUTES": "0",
        "PROACTIVE_DAILY_CAP": "99",
        **overrides,
    }
    settings = load_settings(env=env).unwrap()
    store = Store(settings.db_path, settings.core_memory_path)
    llm = Llm.from_settings(settings, store=store, logger=NOOP_LOGGER).unwrap()
    return CharacterEngine(
        store, llm, settings, DEFAULT_PERSONA, NOOP_LOGGER, clock=lambda: _NOW
    )


async def _call(
    app,
    method: str,
    target: str,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    path, _, query = target.partition("?")
    sent: list[Message] = []
    request_sent = False

    async def receive() -> Message:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": [
            (key.lower().encode(), value.encode())
            for key, value in (headers or {}).items()
        ],
        "client": ("test", 1),
        "server": ("test", 80),
        "root_path": "",
    }
    await app(scope, receive, send)
    statuses = [
        message["status"]
        for message in sent
        if message["type"] == "http.response.start"
        and isinstance(message["status"], int)
    ]
    bodies = [
        message.get("body")
        for message in sent
        if message["type"] == "http.response.body"
    ]
    return statuses[0], b"".join(body for body in bodies if isinstance(body, bytes))


async def _json(app, method: str, target: str, body: bytes = b"", headers=None):
    status, payload = await _call(app, method, target, body, headers)
    return status, json.loads(payload)


@pytest.mark.anyio
async def test_health_index_and_memory(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    app = create_app(engine)
    status, payload = await _json(app, "GET", "/health")
    assert (status, payload["status"]) == (200, "ok")
    status, index = await _call(app, "GET", "/")
    assert status == 200
    assert b"my-agents" in index
    assert b"EventSource(" in index
    assert b"textContent" in index
    assert b"line:demo" in index
    for forbidden in (
        b"innerHTML",
        b"outerHTML",
        b"insertAdjacentHTML",
        b"document.write",
        b"onclick=",
    ):
        assert forbidden not in index
    status, memory = await _json(app, "GET", "/memory")
    assert (status, memory["chars"]) == (200, 0)
    await engine.aclose()


@pytest.mark.anyio
async def test_events_ingest_duplicate_and_errors(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    app = create_app(engine)
    event = {
        "name": "temperature.threshold_exceeded",
        "data": {"celsius": 31, "threshold_celsius": 30, "sensor_id": "kitchen"},
        "idempotency_key": "key",
        "conversation_id": "c",
    }
    status, accepted = await _json(app, "POST", "/events", json.dumps(event).encode())
    assert (status, accepted["duplicate"]) == (202, False)
    status, duplicate = await _json(app, "POST", "/events", json.dumps(event).encode())
    assert (status, duplicate["duplicate"]) == (202, True)
    status, malformed = await _json(app, "POST", "/events", b"{")
    assert (status, malformed["error"]) == (400, "invalid_json")
    status, not_object = await _json(app, "POST", "/events", b"[]")
    assert (status, not_object["error"]) == (400, "body_must_be_object")
    status, bad_length = await _json(
        app, "POST", "/events", b"{}", {"content-length": "abc"}
    )
    assert (status, bad_length["error"]) == (400, "invalid_content_length")
    status, too_large = await _json(
        app, "POST", "/events", b"{}", {"content-length": str(MAX_BODY_BYTES + 1)}
    )
    assert (status, too_large["error"]) == (413, "payload_too_large")
    status, too_large_body = await _json(
        app, "POST", "/events", b"x" * (MAX_BODY_BYTES + 1)
    )
    assert status == 413
    status, with_length = await _json(
        app,
        "POST",
        "/events",
        json.dumps({**event, "idempotency_key": "length-key"}).encode(),
        {"content-length": "200"},
    )
    assert (status, with_length["duplicate"]) == (202, False)
    status, schema = await _json(
        app, "POST", "/events", json.dumps({"name": "Bad"}).encode()
    )
    assert status == 422
    event_id = accepted["event_id"]
    for _ in range(100):
        status, detail = await _json(app, "GET", f"/events/{event_id}")
        if detail["status"] != EventStatus.RECEIVED.value:
            break
        await asyncio.sleep(0.01)
    assert status == 200
    assert detail["status"] == EventStatus.COMPLETED.value
    status, missing = await _json(app, "GET", "/events/missing")
    assert (status, missing["error"]) == (404, "not_found")
    await engine.aclose()


@pytest.mark.anyio
async def test_chat_timeline_reset_and_curate(tmp_path: Path) -> None:
    engine = _engine(tmp_path, SUMMARY_EVERY_TURNS="1")
    app = create_app(engine)
    status, chat = await _json(
        app,
        "POST",
        "/chat",
        json.dumps({"conversation_id": "c", "message": "hi"}).encode(),
    )
    assert (status, chat["turn_index"]) == (200, 2)
    status, missing = await _json(app, "POST", "/chat", b"{}")
    assert (status, missing["error"]) == (422, "message_required")
    status, bad_json = await _json(app, "POST", "/chat", b"{")
    assert (status, bad_json["error"]) == (400, "invalid_json")
    status, empty = await _json(app, "POST", "/chat", b'{"message":"  "}')
    assert status == 422
    status, timeline = await _json(app, "GET", "/timeline?conversation_id=c&limit=5")
    assert (status, timeline["items"][-1]["kind"]) == (200, "turn")
    status, bad_limit = await _json(app, "GET", "/timeline?limit=abc")
    assert (status, bad_limit["error"]) == (400, "invalid_limit")
    status, bad_curate = await _json(app, "POST", "/curate", b"{")
    assert (status, bad_curate["error"]) == (400, "invalid_json")
    status, curated = await _json(
        app, "POST", "/curate", json.dumps({"conversation_id": "c"}).encode()
    )
    assert (status, curated["summarized"]) == (200, 1)
    status, reset = await _json(app, "POST", "/reset?conversation_id=c")
    assert (status, reset["deleted_turns"]) == (200, 2)
    await engine.aclose()


@pytest.mark.anyio
@pytest.mark.anyio
async def test_context_endpoint_reports_stable_prefix_and_counts(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    app = create_app(engine)
    status, first = await _json(
        app,
        "POST",
        "/chat",
        json.dumps({"conversation_id": "c", "message": "hi"}).encode(),
    )
    assert (status, first["turn_index"]) == (200, 2)
    status, context = await _json(app, "GET", "/context?conversation_id=c")
    assert status == 200
    assert context["conversation_id"] == "c"
    assert context["epoch"] == 1
    assert len(context["stable_prefix_sha256"]) == 64
    assert context["counts"]["turns"] == 2
    assert context["counts"]["episodes"] == 1
    assert context["memory_chars"] == 0
    status, second = await _json(
        app,
        "POST",
        "/chat",
        json.dumps({"conversation_id": "c", "message": "again"}).encode(),
    )
    assert status == 200
    status, after = await _json(app, "GET", "/context?conversation_id=c")
    assert after["stable_prefix_sha256"] == context["stable_prefix_sha256"]
    assert after["epoch"] == 2
    status, memory = await _json(app, "GET", "/memory?conversation_id=c")
    assert memory["counts"]["turns"] == 4
    assert memory["chars"] == 0
    episodes = memory["episodes"]
    assert isinstance(episodes, list)
    first_episode = episodes[0]
    assert isinstance(first_episode, dict)
    assert first_episode["title"] == "hi"
    await engine.aclose()


@pytest.mark.anyio
async def test_reset_rejects_oversized_bodies(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    app = create_app(engine)
    status, payload = await _json(
        app, "POST", "/reset", b"{}", {"content-length": str(MAX_BODY_BYTES + 1)}
    )
    assert (status, payload["error"]) == (413, "payload_too_large")
    status, payload = await _json(app, "POST", "/reset", b"x" * (MAX_BODY_BYTES + 1))
    assert (status, payload["error"]) == (413, "payload_too_large")
    status, payload = await _json(app, "POST", "/reset", b"", {"content-length": "bad"})
    assert (status, payload["error"]) == (400, "invalid_content_length")
    status, payload = await _json(app, "POST", "/reset")
    assert (status, payload["deleted_turns"]) == (200, 0)
    await engine.aclose()


class LifecycleEngine(CharacterEngine):
    def __init__(self) -> None:
        self.started = 0
        self.closed = 0

    async def start(self) -> None:
        self.started += 1

    async def aclose(self) -> None:
        self.closed += 1


@pytest.mark.anyio
async def test_lifespan_starts_and_closes_the_engine() -> None:
    engine = LifecycleEngine()
    app = create_app(engine)
    async with app.router.lifespan_context(app):
        assert engine.started == 1
        assert engine.closed == 0
    assert engine.closed == 1


@pytest.mark.anyio
async def test_stream_delivers_sse_and_cleans_up(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    app = create_app(engine)
    sent: list[Message] = []
    disconnect = asyncio.Event()
    request_sent = False

    async def receive() -> Message:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/stream",
        "raw_path": b"/stream",
        "query_string": b"conversation_id=c",
        "headers": [],
        "client": ("test", 1),
        "server": ("test", 80),
        "root_path": "",
    }
    task = asyncio.create_task(app(scope, receive, send))
    await asyncio.sleep(0.05)
    await engine.submit_message("c", "hello")
    body = b""
    for _ in range(100):
        chunks: list[bytes] = []
        for chunk in sent:
            if chunk["type"] != "http.response.body":
                continue
            value = chunk.get("body")
            if isinstance(value, bytes):
                chunks.append(value)
        body = b"".join(chunks)
        if b"data:" in body:
            break
        await asyncio.sleep(0.01)
    assert b'"kind": "turn"' in body
    disconnect.set()
    await asyncio.wait_for(task, timeout=2)
    await engine.aclose()


class ErrorEngine(CharacterEngine):
    kind: AppErrorKind

    def __init__(self, kind: AppErrorKind) -> None:
        self.kind = kind

    def _error(self):
        return Err(AppError(self.kind, "mapped"))

    def get_memory(self):
        return self._error()

    def get_memory_snapshot(self, conversation_id: str):
        del conversation_id
        return self._error()

    async def context_snapshot(self, conversation_id: str):
        del conversation_id
        return self._error()

    def get_timeline(self, conversation_id: str, *, limit: int = 50):
        del conversation_id, limit
        return self._error()

    def get_event(self, event_id: str):
        del event_id
        return self._error()

    def reset_conversation(self, conversation_id: str):
        del conversation_id
        return self._error()

    async def curate_memory(self, conversation_id: str):
        del conversation_id
        return self._error()

    async def submit_message(self, conversation_id: str, text: str):
        del conversation_id, text
        return self._error()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "status"),
    [
        (AppErrorKind.VALIDATION, 422),
        (AppErrorKind.NOT_FOUND, 404),
        (AppErrorKind.CONFLICT, 409),
        (AppErrorKind.DENIED, 403),
        (AppErrorKind.STORAGE, 500),
        (AppErrorKind.LLM, 502),
        (AppErrorKind.UNEXPECTED, 500),
    ],
)
async def test_error_kinds_map_to_http_status(kind: AppErrorKind, status: int) -> None:
    app = create_app(ErrorEngine(kind))
    for method, target, body in (
        ("GET", "/memory", b""),
        ("GET", "/context", b""),
        ("GET", "/timeline", b""),
        ("GET", "/events/x", b""),
        ("POST", "/reset", b""),
        ("POST", "/curate", b"{}"),
        ("POST", "/chat", b'{"message":"hi"}'),
    ):
        actual, payload = await _json(app, method, target, body)
        assert actual == status
        assert payload["message"] == "mapped"


def test_serve_delegates_to_uvicorn(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with patch("my_agents.shell.server.uvicorn.run") as run:
        serve(engine, "127.0.0.1", 9999)
    run.assert_called_once()
    assert run.call_args.kwargs["port"] == 9999
    asyncio.run(engine.aclose())
