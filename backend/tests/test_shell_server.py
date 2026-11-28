import asyncio
import importlib
import json
import time
from base64 import urlsafe_b64encode
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flow_res import Err, Ok, Result
from starlette.testclient import TestClient

from my_agents.core.context import AssembledContext
from my_agents.core.types import (
    DEFAULT_PERSONA,
    AppError,
    AppErrorKind,
    ChatMessage,
    ConversationSummary,
    EventAction,
    EventDecision,
    EventEnvelope,
    EventRecord,
    MemoryBlocks,
    PushPayload,
    PushSubscription,
    TimelineItem,
    TimelineKind,
)
from my_agents.shell.engine import CharacterEngine
from my_agents.shell.llm import ChatReply
from my_agents.shell.logger import IAppLogger, LogEntry
from my_agents.shell.push import ProactiveDeliverer
from my_agents.shell.server import (
    MAX_BODY_BYTES,
    create_app,
    main,
    serve,
)
from my_agents.shell.settings import load_settings
from my_agents.shell.store import Store

_EVENT_KEYS = {
    "event_id",
    "conversation_id",
    "idempotency_key",
    "name",
    "source",
    "occurred_at",
    "received_at",
    "status",
    "action",
    "reason",
    "note",
    "payload_json",
    "failure_reason",
    "surfaced_at",
}

_TIMELINE_KEYS = {
    "kind",
    "conversation_id",
    "content",
    "created_at",
    "item_id",
    "event_id",
    "origin",
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

_ENV = {
    "LLM_MODEL": "gpt-4o-mini",
    "LLM_API_KEY": "sk-test-key",
    "DATA_DIR": "/tmp/my-agents-server-test",
}


def _b64url(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _valid_keys() -> dict[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    p256dh = _b64url(
        key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
    )
    return {"p256dh": p256dh, "auth": _b64url(b"0123456789abcdef")}


@dataclass
class _SpyLogger(IAppLogger):
    entries: list[LogEntry] = field(default_factory=list)
    closed: bool = False

    def log(self, entry: LogEntry) -> None:
        self.entries.append(entry)

    def close(self) -> None:
        self.closed = True


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


class _KeyDeliverer:
    def __init__(self, public: str | None = None) -> None:
        self._public = public
        self.closed = False

    @property
    def public_key(self) -> str | None:
        return self._public

    async def deliver(self, payload: PushPayload) -> None:
        del payload

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

    def get_event(self, event_id: str) -> Result[EventRecord | None, AppError]:
        failure = self.failures.get("get_event")
        if failure is not None:
            return Err(failure)
        return super().get_event(event_id)

    def list_conversations(
        self, *, limit: int = 100
    ) -> Result[tuple[ConversationSummary, ...], AppError]:
        failure = self.failures.get("list_conversations")
        if failure is not None:
            return Err(failure)
        return super().list_conversations(limit=limit)

    def upsert_push_subscription(
        self, subscription: PushSubscription
    ) -> Result[None, AppError]:
        failure = self.failures.get("upsert_push_subscription")
        if failure is not None:
            return Err(failure)
        return super().upsert_push_subscription(subscription)

    def delete_push_subscription(self, endpoint: str) -> Result[bool, AppError]:
        failure = self.failures.get("delete_push_subscription")
        if failure is not None:
            return Err(failure)
        return super().delete_push_subscription(endpoint)

    def reset_conversation(self, conversation_id: str) -> Result[int, AppError]:
        failure = self.failures.get("reset_conversation")
        if failure is not None:
            return Err(failure)
        return super().reset_conversation(conversation_id)


def _build_engine(tmp_path: Path, *, deliverer: ProactiveDeliverer | None = None):
    settings = load_settings(env={"DATA_DIR": str(tmp_path)})
    assert isinstance(settings, Ok)
    store = _FaultStore(settings.value.db_path, settings.value.core_memory_path)
    llm = _FakeLlm()
    logger = _SpyLogger()
    engine = CharacterEngine(
        store,
        llm,
        settings.value,
        DEFAULT_PERSONA,
        logger,
        clock=lambda: datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        deliverer=deliverer,
    )
    return engine, store, llm, logger


def _assert_error(
    response,
    *,
    status: int,
    error: str,
    message_contains: str | None = None,
) -> None:
    assert response.status_code == status
    body = response.json()
    assert body["error"] == error
    if message_contains is None:
        assert "message" not in body
    else:
        assert message_contains in body["message"]


def test_health_index_events_chat_timeline_context_memory_and_reset(
    tmp_path: Path,
) -> None:
    engine, store, _llm, _logger = _build_engine(tmp_path)
    assert isinstance(
        store.update_memory(lambda _blocks: Ok(MemoryBlocks(user="green tea"))),
        Ok,
    )

    with TestClient(create_app(engine)) as client:
        assert client.get("/health").json() == {"status": "ok", "service": "my-agents"}
        index = client.get("/")
        assert index.status_code == HTTPStatus.NOT_FOUND
        assert index.json() == {
            "error": "web_ui_not_built",
            "message": (
                "Web UI is not built. Run 'cd web && npm run build' "
                "or set WEB_DIST_DIR."
            ),
        }

        accepted = client.post(
            "/events",
            json={"name": "email.received", "conversation_id": ""},
        )
        assert accepted.status_code == HTTPStatus.ACCEPTED
        event_id = accepted.json()["event_id"]
        assert accepted.json()["duplicate"] is False
        assert accepted.json()["status"] == "received"

        accepted_default = client.post(
            "/events",
            json={"name": "email.received", "conversation_id": 123},
        )
        assert accepted_default.status_code == HTTPStatus.ACCEPTED
        assert accepted_default.json()["duplicate"] is False

        chat_res = client.post("/chat", json={"message": "hello"})
        assert chat_res.status_code == HTTPStatus.OK
        assert chat_res.json()["reply"] == "server reply"
        assert chat_res.json()["conversation_id"] == "default"

        default_chat = client.post(
            "/chat", json={"message": "hello", "conversation_id": 123}
        )
        assert default_chat.json()["conversation_id"] == "default"

        timeline_response = client.get("/timeline")
        assert timeline_response.status_code == HTTPStatus.OK
        timeline_items = timeline_response.json()["items"]
        assert timeline_items
        for item in timeline_items:
            assert set(item) == _TIMELINE_KEYS
            assert len(item) == 7
            assert next(iter(item)) == "kind"
            assert item["item_id"]
        conversations = client.get("/conversations")
        assert conversations.status_code == HTTPStatus.OK
        summaries = conversations.json()["conversations"]
        assert [summary["conversation_id"] for summary in summaries] == ["default"]
        assert set(summaries[0]) == {
            "conversation_id",
            "epoch",
            "created_at",
            "updated_at",
            "message_count",
            "event_count",
            "last_message_preview",
            "last_message_origin",
        }
        assert summaries[0]["message_count"] == 4
        assert summaries[0]["event_count"] == 2
        assert summaries[0]["last_message_preview"] == "server reply"
        assert summaries[0]["last_message_origin"] == "reply"
        _assert_error(
            client.get("/conversations?limit=bad"),
            status=HTTPStatus.BAD_REQUEST,
            error="invalid_limit",
        )
        ctx = client.get("/context").json()
        assert set(ctx) == _CONTEXT_KEYS
        assert set(ctx["counts"]) == _COUNT_KEYS
        assert ctx["counts"]["events"] == 2
        mem = client.get("/memory").json()
        assert set(mem) == _MEMORY_KEYS
        assert set(mem["counts"]) == _COUNT_KEYS
        assert mem["user"] == "green tea"
        assert mem["chars"] == len("green tea")

        detail = client.get(f"/events/{event_id}")
        assert detail.status_code == HTTPStatus.OK
        detail_body = detail.json()
        assert set(detail_body) == _EVENT_KEYS
        assert len(detail_body) == 14
        assert detail_body["event_id"] == event_id
        assert detail_body["idempotency_key"] == event_id
        _assert_error(
            client.get("/events/evt_missing"),
            status=HTTPStatus.NOT_FOUND,
            error="not_found",
        )

        store.failures["get_event"] = AppError(AppErrorKind.STORAGE, "db err")
        _assert_error(
            client.get(f"/events/{event_id}"),
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
            error="storage",
            message_contains="db err",
        )
        store.failures.clear()

        store.failures["reset_conversation"] = AppError(
            AppErrorKind.STORAGE, "reset failed"
        )
        failed_reset = client.post("/reset")
        assert failed_reset.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
        assert failed_reset.json()["message"] == "reset failed"
        store.failures.clear()

        reset = client.post("/reset")
        assert reset.status_code == HTTPStatus.OK
        assert reset.json()["deleted_messages"] == 4


def test_http_guards_and_validation_errors_include_json_bodies(tmp_path: Path) -> None:
    engine, _store, _llm, _logger = _build_engine(tmp_path)
    with TestClient(create_app(engine)) as client:
        _assert_error(
            client.post("/events", content=b"{"),
            status=HTTPStatus.BAD_REQUEST,
            error="invalid_json",
        )
        _assert_error(
            client.post("/events", content=b"\xff"),
            status=HTTPStatus.BAD_REQUEST,
            error="invalid_json",
        )
        _assert_error(
            client.post("/events", content=b"[]"),
            status=HTTPStatus.BAD_REQUEST,
            error="body_must_be_object",
        )
        _assert_error(
            client.post(
                "/events",
                content=b"{}",
                headers={"content-length": "not-an-int"},
            ),
            status=HTTPStatus.BAD_REQUEST,
            error="invalid_content_length",
        )
        _assert_error(
            client.post("/events", content=b"x" * (MAX_BODY_BYTES + 1)),
            status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            error="payload_too_large",
        )

        def chunked_big():
            yield b"a" * (MAX_BODY_BYTES + 1)

        _assert_error(
            client.post("/events", content=chunked_big()),
            status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            error="payload_too_large",
        )
        _assert_error(
            client.post("/reset", content=b"a" * (MAX_BODY_BYTES + 1)),
            status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            error="payload_too_large",
        )
        _assert_error(
            client.post(
                "/reset",
                content=b"",
                headers={"content-length": "not-an-int"},
            ),
            status=HTTPStatus.BAD_REQUEST,
            error="invalid_content_length",
        )
        _assert_error(
            client.post("/events", json={"name": "BAD"}),
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
            error="validation",
            message_contains="Invalid event field",
        )
        _assert_error(
            client.post("/chat", content=b"{"),
            status=HTTPStatus.BAD_REQUEST,
            error="invalid_json",
        )
        _assert_error(
            client.post("/chat", json={"message": 123}),
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
            error="message_required",
        )
        _assert_error(
            client.get("/timeline?limit=bad"),
            status=HTTPStatus.BAD_REQUEST,
            error="invalid_limit",
        )


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        (AppErrorKind.VALIDATION, HTTPStatus.UNPROCESSABLE_ENTITY),
        (AppErrorKind.NOT_FOUND, HTTPStatus.NOT_FOUND),
        (AppErrorKind.CONFLICT, HTTPStatus.CONFLICT),
        (AppErrorKind.STORAGE, HTTPStatus.INTERNAL_SERVER_ERROR),
        (AppErrorKind.LLM, HTTPStatus.BAD_GATEWAY),
    ],
)
def test_every_app_error_kind_maps_to_status_and_body(
    tmp_path: Path, kind: AppErrorKind, status: int
) -> None:
    engine, _store, llm, _logger = _build_engine(tmp_path)
    llm.chat_result = Err(AppError(kind, f"{kind.value} boom"))

    with TestClient(create_app(engine)) as client:
        response = client.post("/chat", json={"message": "hello"})

    _assert_error(
        response,
        status=status,
        error=kind.value,
        message_contains=f"{kind.value} boom",
    )


def test_stream_endpoint_emits_sse_frames(tmp_path: Path) -> None:
    engine, _store, _llm, _logger = _build_engine(tmp_path)
    with (
        TestClient(create_app(engine)) as client,
        client.stream("GET", "/stream?conversation_id=sse") as response,
    ):
        assert response.status_code == HTTPStatus.OK
        assert response.headers["content-type"].startswith("text/event-stream")
        time.sleep(0.1)
        chat = client.post("/chat", json={"conversation_id": "sse", "message": "hello"})
        assert chat.status_code == HTTPStatus.OK
        line = next(item for item in response.iter_lines() if item.startswith("data: "))

    payload = json.loads(line.removeprefix("data: "))
    assert payload["kind"] == "turn"
    assert payload["conversation_id"] == "sse"
    assert payload["content"] == "server reply"


def test_lifespan_start_failure_logs_and_serve_delegates_to_uvicorn(
    tmp_path: Path,
) -> None:
    engine, _store, llm, logger = _build_engine(tmp_path)
    llm.start_result = Err(AppError(AppErrorKind.LLM, "mcp fail"))

    with pytest.raises(RuntimeError, match="mcp fail"), TestClient(create_app(engine)):
        pass
    assert logger.entries[0].event == "server_start_failed"

    llm.start_result = Ok(None)
    calls: list[tuple[object, str, int, str, int]] = []
    with patch(
        "my_agents.shell.server.uvicorn.run",
        side_effect=lambda app, host, port, log_level, timeout_graceful_shutdown: (
            calls.append((app, host, port, log_level, timeout_graceful_shutdown))
        ),
    ):
        serve(engine, "127.0.0.1", 8123)
    assert calls[0][1:] == ("127.0.0.1", 8123, "warning", 5)


def test_main_reports_settings_persona_and_engine_failures(
    tmp_path: Path, capsys
) -> None:
    assert main(env={"SERVER_PORT": "bad"}) == 1
    assert "SERVER_PORT" in capsys.readouterr().err

    missing_persona = tmp_path / "missing.json"
    assert (
        main(
            env={
                **_ENV,
                "DATA_DIR": str(tmp_path),
                "PERSONA_PATH": str(missing_persona),
            },
        )
        == 1
    )
    assert "Cannot read persona" in capsys.readouterr().err

    bad_persona = tmp_path / "persona.json"
    bad_persona.write_text("[]", encoding="utf-8")
    assert (
        main(
            env={
                **_ENV,
                "DATA_DIR": str(tmp_path),
                "PERSONA_PATH": str(bad_persona),
            },
        )
        == 1
    )
    assert "Persona must be a JSON object." in capsys.readouterr().err

    assert main(env={**_ENV, "DATA_DIR": str(tmp_path), "LLM_API_KEY": ""}) == 1
    assert "LLM_API_KEY is required." in capsys.readouterr().err


def test_main_reports_log_path_open_failure(tmp_path: Path, capsys) -> None:
    assert main(env={**_ENV, "DATA_DIR": str(tmp_path), "LOG_PATH": str(tmp_path)}) == 1
    assert "Error: cannot open log file:" in capsys.readouterr().err


def test_main_uses_log_path_and_starts_the_server(tmp_path: Path) -> None:
    log_path = tmp_path / "run.jsonl"
    outputs: list[str] = []
    calls: list[tuple[object, str, int, dict[str, object]]] = []

    def fake_serve(engine: object, host: str, port: int, **options: object) -> None:
        calls.append((engine, host, port, options))

    with patch("my_agents.shell.server.serve", side_effect=fake_serve):
        assert (
            main(
                env={
                    **_ENV,
                    "DATA_DIR": str(tmp_path),
                    "SERVER_HOST": "0.0.0.0",
                    "SERVER_PORT": "9001",
                    "WEB_DIST_DIR": str(tmp_path / "dist"),
                    "LOG_PATH": str(log_path),
                },
                write_output=outputs.append,
            )
            == 0
        )

    assert outputs == ["my-agents listening on http://0.0.0.0:9001"]
    assert calls[0][1:3] == ("0.0.0.0", 9001)
    assert calls[0][3] == {"web_dist_dir": tmp_path / "dist"}
    assert log_path.read_text(encoding="utf-8") == ""


def test_main_uses_noop_logger_when_log_path_is_unset(
    tmp_path: Path,
) -> None:
    with patch("my_agents.shell.server.serve"):
        assert (
            main(
                env={
                    **_ENV,
                    "DATA_DIR": str(tmp_path),
                    "SERVER_HOST": "127.0.0.1",
                    "SERVER_PORT": "8123",
                },
                write_output=lambda _text: None,
            )
            == 0
        )


def test_main_reports_push_configuration_failure(tmp_path: Path, capsys) -> None:
    assert (
        main(
            env={
                **_ENV,
                "DATA_DIR": str(tmp_path),
                "VAPID_SUBJECT": "mailto:ops@example.com",
            },
        )
        == 1
    )
    assert "VAPID_PRIVATE_KEY is required" in capsys.readouterr().err


def test_main_uses_process_environment_when_env_is_omitted(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    process_env = {
        **_ENV,
        "DATA_DIR": str(tmp_path),
        "SERVER_HOST": "127.0.0.1",
        "SERVER_PORT": "8124",
    }
    outputs: list[str] = []
    with (
        patch("my_agents.shell.server.environ", process_env),
        patch("my_agents.shell.server.serve"),
    ):
        assert main(write_output=outputs.append) == 0

    assert outputs == ["my-agents listening on http://127.0.0.1:8124"]


def test_web_dist_serving_static_assets_and_guards(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "nested").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app shell</html>", encoding="utf-8")
    (dist / "sw.js").write_text(
        "self.addEventListener('push', () => {});", encoding="utf-8"
    )
    (dist / "manifest.webmanifest").write_text('{"name":"my-agents"}', encoding="utf-8")
    (dist / "nested" / "app.css").write_text("body{}", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("do not serve", encoding="utf-8")
    (dist / "leak.txt").symlink_to(secret)

    engine, _store, _llm, _logger = _build_engine(tmp_path / "data")
    with TestClient(create_app(engine, web_dist_dir=dist)) as client:
        index = client.get("/")
        assert index.status_code == HTTPStatus.OK
        assert index.text == "<html>app shell</html>"
        assert index.headers["cache-control"] == "no-cache"

        service_worker = client.get("/sw.js")
        assert service_worker.status_code == HTTPStatus.OK
        assert service_worker.headers["cache-control"] == "no-cache"
        assert service_worker.headers["service-worker-allowed"] == "/"

        manifest = client.get("/manifest.webmanifest")
        assert manifest.status_code == HTTPStatus.OK
        assert manifest.headers["content-type"].startswith("application/manifest+json")
        assert manifest.headers["cache-control"] == "no-cache"

        asset = client.get("/nested/app.css")
        assert asset.status_code == HTTPStatus.OK
        assert asset.text == "body{}"

        for path in ("/missing.js", "/nested", "/leak.txt"):
            missing = client.get(path)
            assert missing.status_code == HTTPStatus.NOT_FOUND
            assert missing.json() == {"error": "not_found"}

    empty = tmp_path / "empty-dist"
    empty.mkdir()
    (empty / "asset.js").write_text("x", encoding="utf-8")
    with TestClient(create_app(engine, web_dist_dir=empty)) as client:
        missing_index = client.get("/")
        assert missing_index.status_code == HTTPStatus.NOT_FOUND
        assert missing_index.json()["error"] == "web_ui_not_built"


def test_push_endpoints_validate_register_and_delete(tmp_path: Path) -> None:
    engine, store, _llm, _logger = _build_engine(tmp_path / "push")
    valid_keys = _valid_keys()
    valid_p256dh = valid_keys["p256dh"]
    valid_auth = valid_keys["auth"]
    with TestClient(create_app(engine)) as client:
        vapid = client.get("/push/vapid-public-key")
        assert vapid.json() == {"public_key": None}

        bad_json = client.post("/push/subscriptions", content=b"{")
        assert bad_json.status_code == HTTPStatus.BAD_REQUEST
        assert bad_json.json() == {"error": "invalid_json"}

        invalid_bodies = [
            {},
            {"endpoint": 123, "keys": valid_keys},
            {"endpoint": "", "keys": valid_keys},
            {"endpoint": "http://remote.example/push", "keys": valid_keys},
            {"endpoint": "ftp://example.com/push", "keys": valid_keys},
            {"endpoint": "https://", "keys": valid_keys},
            {"endpoint": "https://push.example.com/1"},
            {"endpoint": "https://push.example.com/1", "keys": []},
            {"endpoint": "https://push.example.com/1", "keys": {"auth": valid_auth}},
            {
                "endpoint": "https://push.example.com/1",
                "keys": {"p256dh": "", "auth": valid_auth},
            },
            {
                "endpoint": "https://push.example.com/1",
                "keys": {"p256dh": "!!!", "auth": valid_auth},
            },
            {
                "endpoint": "https://push.example.com/1",
                "keys": {"p256dh": "a", "auth": valid_auth},
            },
            {
                "endpoint": "https://push.example.com/1",
                "keys": {
                    "p256dh": _b64url(b"\x04" + b"\x00" * 64),
                    "auth": valid_auth,
                },
            },
            {
                "endpoint": "https://push.example.com/1",
                "keys": {"p256dh": _b64url(b"\x01" * 64), "auth": valid_auth},
            },
            {
                "endpoint": "https://push.example.com/1",
                "keys": {"p256dh": valid_p256dh, "auth": ""},
            },
            {
                "endpoint": "https://push.example.com/1",
                "keys": {"p256dh": valid_p256dh, "auth": "!!!"},
            },
            {
                "endpoint": "https://push.example.com/1",
                "keys": {"p256dh": valid_p256dh, "auth": "YXV0aA"},
            },
            {
                "endpoint": "https://push.example.com/1",
                "keys": valid_keys,
                "conversation_id": 123,
            },
            {
                "endpoint": "https://push.example.com/1",
                "keys": valid_keys,
                "conversation_id": "",
            },
            {
                "endpoint": "https://push.example.com/1",
                "keys": valid_keys,
                "conversation_id": "x" * 129,
            },
            {
                "endpoint": "https://push.example.com/1",
                "keys": valid_keys,
                "conversation_id": "bad\x01id",
            },
            {"endpoint": "https://push.example.com/a b", "keys": valid_keys},
            {"endpoint": "https://push.example.com/a\x01", "keys": valid_keys},
            {
                "endpoint": "https://user:pass@push.example.com/1",
                "keys": valid_keys,
            },
            {"endpoint": "https://push.example.com/1#frag", "keys": valid_keys},
            {"endpoint": "https://push.example.com:bad/1", "keys": valid_keys},
        ]
        for body in invalid_bodies:
            response = client.post("/push/subscriptions", json=body)
            assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, body
            assert response.json() == {"error": "invalid_subscription"}

        for endpoint in (
            "https://push.example.com/1",
            "http://127.0.0.1:8080/push",
            "http://localhost/push",
            "http://[::1]:9000/push",
            "https://push.example.com/2",
            "https://push.example.com/3",
        ):
            response = client.post(
                "/push/subscriptions",
                json={
                    "endpoint": endpoint,
                    "keys": valid_keys,
                    "conversation_id": "default",
                },
            )
            assert response.status_code == HTTPStatus.CREATED, endpoint
            assert response.json() == {"status": "subscribed", "endpoint": endpoint}

        global_subscription = client.post(
            "/push/subscriptions",
            json={
                "endpoint": "https://push.example.com/4",
                "keys": valid_keys,
                "conversation_id": None,
            },
        )
        assert global_subscription.status_code == HTTPStatus.CREATED

        saved = store.list_push_subscriptions(conversation_id="default")
        assert isinstance(saved, Ok)
        assert len(saved.value) == 7
        assert saved.value[0].auth.get_secret_value() == valid_auth
        assert saved.value[0].conversation_id == "default"

        by_body = client.request(
            "DELETE",
            "/push/subscriptions",
            content=json.dumps({"endpoint": "https://push.example.com/2"}).encode(),
        )
        assert by_body.status_code == HTTPStatus.OK
        assert by_body.json() == {"deleted": True}
        missing = client.request(
            "DELETE",
            "/push/subscriptions",
            content=json.dumps(
                {"endpoint": "https://push.example.com/absent"}
            ).encode(),
        )
        assert missing.json() == {"deleted": False}

        empty = client.request("DELETE", "/push/subscriptions", content=b"")
        assert empty.status_code == HTTPStatus.BAD_REQUEST
        assert empty.json() == {"error": "invalid_json"}

        bad_json = client.request("DELETE", "/push/subscriptions", content=b"{")
        assert bad_json.status_code == HTTPStatus.BAD_REQUEST
        assert bad_json.json() == {"error": "invalid_json"}

        bad_shape = client.request("DELETE", "/push/subscriptions", content=b"[]")
        assert bad_shape.status_code == HTTPStatus.BAD_REQUEST
        assert bad_shape.json() == {"error": "body_must_be_object"}

        bad_endpoint = client.request(
            "DELETE",
            "/push/subscriptions",
            content=json.dumps({"endpoint": 123}).encode(),
        )
        assert bad_endpoint.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        assert bad_endpoint.json() == {"error": "invalid_endpoint"}

        query_only = client.delete(
            "/push/subscriptions",
            params={"endpoint": "https://push.example.com/3"},
        )
        assert query_only.status_code == HTTPStatus.BAD_REQUEST
        assert query_only.json() == {"error": "invalid_json"}

        bad_length = client.request(
            "DELETE",
            "/push/subscriptions",
            content=b"",
            headers={"content-length": "nope"},
        )
        assert bad_length.status_code == HTTPStatus.BAD_REQUEST
        assert bad_length.json() == {"error": "invalid_content_length"}

        oversized = client.request(
            "DELETE",
            "/push/subscriptions",
            content=b"x" * (MAX_BODY_BYTES + 1),
        )
        assert oversized.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE

        store.failures["upsert_push_subscription"] = AppError(
            AppErrorKind.STORAGE, "save failed"
        )
        failed_save = client.post(
            "/push/subscriptions",
            json={"endpoint": "https://push.example.com/9", "keys": valid_keys},
        )
        assert failed_save.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
        assert failed_save.json()["message"] == "save failed"
        store.failures.clear()

        store.failures["delete_push_subscription"] = AppError(
            AppErrorKind.STORAGE, "delete failed"
        )
        failed_delete = client.request(
            "DELETE",
            "/push/subscriptions",
            content=json.dumps({"endpoint": "https://push.example.com/3"}).encode(),
        )
        assert failed_delete.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
        store.failures.clear()


def test_push_vapid_key_enabled_with_deliverer(tmp_path: Path) -> None:
    deliverer = _KeyDeliverer("public-key")
    engine, _store, _llm, _logger = _build_engine(
        tmp_path / "vapid", deliverer=deliverer
    )
    with TestClient(create_app(engine)) as client:
        response = client.get("/push/vapid-public-key")
        assert response.json() == {"public_key": "public-key"}
    assert deliverer.closed is True


def test_conversations_endpoint_propagates_storage_errors(tmp_path: Path) -> None:
    engine, store, _llm, _logger = _build_engine(tmp_path / "conv")
    store.failures["list_conversations"] = AppError(AppErrorKind.STORAGE, "list failed")
    with TestClient(create_app(engine)) as client:
        response = client.get("/conversations")
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.json()["message"] == "list failed"


def test_stream_rejects_invalid_cursors(tmp_path: Path) -> None:
    engine, _store, _llm, _logger = _build_engine(tmp_path / "cursor")
    with TestClient(create_app(engine)) as client:
        for raw in (
            "nope",
            "",
            "2026-05-01T12:00:00",
            "2026-05-01T12:00:00|msg:1",
            "00000000-0000-4000-8000-000000000000",
        ):
            response = client.get(
                f"/stream?conversation_id=default&since={quote(raw, safe='')}"
            )
            assert response.status_code == HTTPStatus.BAD_REQUEST
            assert response.json() == {"error": "invalid_since"}


def _data_items(response, count: int) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            items.append(json.loads(line.removeprefix("data: ")))
            if len(items) >= count:
                return items
    raise AssertionError("stream ended before expected items")


def test_stream_replays_cursor_and_continues_live(tmp_path: Path) -> None:
    engine, _store, _llm, _logger = _build_engine(tmp_path / "replay")
    with TestClient(create_app(engine)) as client:
        for text in ("one", "two"):
            assert (
                client.post(
                    "/chat", json={"conversation_id": "replay", "message": text}
                ).status_code
                == HTTPStatus.OK
            )
        timeline = client.get("/timeline?conversation_id=replay&limit=100").json()[
            "items"
        ]
        cursor = str(timeline[1]["item_id"])
        with client.stream(
            "GET",
            f"/stream?conversation_id=replay&since={quote(cursor, safe='')}",
        ) as response:
            assert response.status_code == HTTPStatus.OK
            time.sleep(0.1)
            chat_response = client.post(
                "/chat",
                json={"conversation_id": "replay", "message": "three"},
            )
            assert chat_response.status_code == HTTPStatus.OK
            replayed = _data_items(response, 3)
        assert replayed[0]["item_id"] == timeline[2]["item_id"]
        assert replayed[1]["item_id"] == timeline[3]["item_id"]
        assert replayed[2]["item_id"] == chat_response.json()["message_id"]
        assert (replayed[2]["content"], replayed[2]["origin"]) == (
            "server reply",
            "reply",
        )

        updated_timeline = client.get(
            "/timeline?conversation_id=replay&limit=100"
        ).json()["items"]
        assert len(updated_timeline) == 6

        header_cursor = str(timeline[3]["item_id"])
        with client.stream(
            "GET",
            f"/stream?conversation_id=replay&since={quote(header_cursor, safe='')}",
            headers={"Last-Event-ID": "invalid"},
        ) as response:
            assert response.status_code == HTTPStatus.OK
            items = _data_items(response, 1)
        assert items[0]["item_id"] == updated_timeline[4]["item_id"]
        assert (items[0]["content"], items[0]["origin"]) == ("three", "user")

        with client.stream(
            "GET",
            "/stream?conversation_id=replay",
            headers={"Last-Event-ID": header_cursor},
        ) as response:
            assert response.status_code == HTTPStatus.OK
            header_items = _data_items(response, 1)
        assert header_items[0]["item_id"] == updated_timeline[4]["item_id"]


class _ScriptedEngine(CharacterEngine):
    def __init__(
        self,
        replay: Result[tuple[TimelineItem, ...], AppError],
        live: tuple[TimelineItem, ...],
        wait: asyncio.Event | None = None,
    ) -> None:
        self._replay = replay
        self._live = live
        self._wait = wait

    async def subscribe(self, conversation_id: str) -> AsyncGenerator[TimelineItem]:
        del conversation_id
        if self._wait is not None:
            await self._wait.wait()
        for item in self._live:
            yield item

    def get_timeline(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
        after_id: str | None = None,
    ) -> Result[tuple[TimelineItem, ...], AppError]:
        del conversation_id, limit, after_id
        return self._replay


@pytest.mark.anyio
async def test_stream_helper_deduplicates_replay_and_handles_errors() -> None:
    server_module = importlib.import_module("my_agents.shell.server")
    stream_name = "_stream"
    stream_fn = getattr(server_module, stream_name)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    first = TimelineItem(
        TimelineKind.TURN,
        "default",
        "first",
        now,
        item_id="01960000-0000-7000-8000-000000000001",
    )
    second = TimelineItem(
        TimelineKind.TURN,
        "default",
        "second",
        now,
        item_id="01960000-0000-7000-8000-000000000002",
    )
    duplicate_engine = _ScriptedEngine(Ok((first,)), (first, second))
    frames = [
        frame async for frame in stream_fn(duplicate_engine, "default", first.item_id)
    ]
    assert len(frames) == 2
    assert f"id: {first.item_id}" in frames[0]
    assert f"id: {second.item_id}" in frames[1]

    error_engine = _ScriptedEngine(
        Err(AppError(AppErrorKind.STORAGE, "replay failed")), (second,)
    )
    error_frames = [
        frame async for frame in stream_fn(error_engine, "default", first.item_id)
    ]
    assert len(error_frames) == 1
    assert error_frames[0].startswith(f"id: {second.item_id}\n")

    stopped_frames = [
        frame
        async for frame in stream_fn(
            _ScriptedEngine(Ok((first,)), ()), "default", first.item_id
        )
    ]
    assert len(stopped_frames) == 1

    cancellable = stream_fn(
        _ScriptedEngine(Ok((first,)), (), asyncio.Event()),
        "default",
        first.item_id,
    )
    assert (await anext(cancellable)).startswith("id: ")
    await cancellable.aclose()


@pytest.mark.anyio
async def test_stream_helper_skips_equal_and_marks_late_events_without_id() -> None:
    server_module = importlib.import_module("my_agents.shell.server")
    stream_name = "_stream"
    stream_fn = getattr(server_module, stream_name)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    first = TimelineItem(
        TimelineKind.TURN,
        "default",
        "first",
        now,
        item_id="01960000-0000-7000-8000-000000000001",
    )
    second = TimelineItem(
        TimelineKind.TURN,
        "default",
        "second",
        now,
        item_id="01960000-0000-7000-8000-000000000002",
    )
    third = TimelineItem(
        TimelineKind.TURN,
        "default",
        "third",
        now,
        item_id="01960000-0000-7000-8000-000000000003",
    )
    engine = _ScriptedEngine(Ok(()), (third, first, second, third))
    frames = [frame async for frame in stream_fn(engine, "default", first.item_id)]
    assert len(frames) == 2
    assert frames[0].startswith(f"id: {third.item_id}\n")
    assert frames[1].startswith("data: ")
    assert f"id: {second.item_id}" not in frames[1]

    no_cursor = [frame async for frame in stream_fn(engine, "default", None)]
    assert len(no_cursor) == 4
    assert all(frame.startswith("id: ") for frame in no_cursor)


def test_conversation_id_validation_across_routes(tmp_path: Path) -> None:
    engine, _store, _llm, _logger = _build_engine(tmp_path / "ids")
    bad_values = ("x" * 129, "bad\x01id", "bad\x7fid")
    with TestClient(create_app(engine)) as client:
        for value in bad_values:
            for path in ("/timeline", "/context", "/memory"):
                response = client.get(path, params={"conversation_id": value})
                assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
                assert response.json() == {"error": "invalid_conversation_id"}
            stream_response = client.get("/stream", params={"conversation_id": value})
            assert stream_response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
            assert stream_response.json() == {"error": "invalid_conversation_id"}
            reset_response = client.post(
                "/reset", params={"conversation_id": value}, content=b""
            )
            assert reset_response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
            assert reset_response.json() == {"error": "invalid_conversation_id"}
            events_response = client.post(
                "/events",
                json={"name": "email.received", "conversation_id": value},
            )
            assert events_response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
            assert events_response.json() == {"error": "invalid_conversation_id"}
            chat_response = client.post(
                "/chat", json={"message": "hi", "conversation_id": value}
            )
            assert chat_response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
            assert chat_response.json() == {"error": "invalid_conversation_id"}

        defaulted = client.get("/context", params={"conversation_id": ""})
        assert defaulted.status_code == HTTPStatus.OK
        assert defaulted.json()["conversation_id"] == "default"
        defaulted_chat = client.post(
            "/chat", json={"message": "hi", "conversation_id": 123}
        )
        assert defaulted_chat.json()["conversation_id"] == "default"
