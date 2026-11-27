import json
import time
from collections.abc import Sequence
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
    EventRecord,
    MemoryBlocks,
)
from my_agents.shell.engine import CharacterEngine
from my_agents.shell.llm import ChatReply
from my_agents.shell.logger import IAppLogger, LogEntry
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

_ENV = {
    "LLM_MODEL": "gpt-4o-mini",
    "LLM_API_KEY": "sk-test-key",
    "DATA_DIR": "/tmp/my-agents-server-test",
}


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


def _build_engine(tmp_path: Path):
    settings = load_settings(
        env={"DATA_DIR": str(tmp_path), "EVENT_EVAL_DELAY_MS": "0"}
    )
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
        assert index.status_code == 200
        assert "EventSource(" in index.text

        accepted = client.post(
            "/events",
            json={"name": "email.received", "conversation_id": ""},
        )
        assert accepted.status_code == HTTPStatus.ACCEPTED
        event_id = accepted.json()["event_id"]
        assert accepted.json()["duplicate"] is False

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
            assert len(item) == 11
            assert next(iter(item)) == "kind"
        ctx = client.get("/context").json()
        assert set(ctx) == _CONTEXT_KEYS
        assert set(ctx["counts"]) == _COUNT_KEYS
        assert ctx["counts"]["events"] == 2
        mem = client.get("/memory").json()
        assert set(mem) == _MEMORY_KEYS
        assert set(mem["counts"]) == _COUNT_KEYS
        assert mem["user"] == "green tea"

        detail = client.get(f"/events/{event_id}")
        assert detail.status_code == HTTPStatus.OK
        detail_body = detail.json()
        assert set(detail_body) == _EVENT_KEYS
        assert len(detail_body) == 13
        assert detail_body["event_id"] == event_id
        assert "idempotency_key" not in detail_body
        assert "payload_json" not in detail_body
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
        (AppErrorKind.DENIED, HTTPStatus.FORBIDDEN),
        (AppErrorKind.STORAGE, HTTPStatus.INTERNAL_SERVER_ERROR),
        (AppErrorKind.LLM, HTTPStatus.BAD_GATEWAY),
        (AppErrorKind.UNEXPECTED, HTTPStatus.INTERNAL_SERVER_ERROR),
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
    calls: list[tuple[object, str, int, str]] = []
    with patch(
        "my_agents.shell.server.uvicorn.run",
        side_effect=lambda app, host, port, log_level: calls.append(
            (app, host, port, log_level)
        ),
    ):
        serve(engine, "127.0.0.1", 8123)
    assert calls[0][1:] == ("127.0.0.1", 8123, "warning")


def test_main_help_rejects_unknown_flags_and_the_serve_subcommand(capsys) -> None:
    assert main(["--help"]) == 0
    help_text = capsys.readouterr().out
    assert "usage: my-agents" in help_text
    assert "serve" not in help_text

    assert main(["--unknown"]) == 2
    assert "unrecognized arguments: --unknown" in capsys.readouterr().err

    assert main(["serve"]) == 2
    assert "unrecognized arguments: serve" in capsys.readouterr().err


def test_main_reports_settings_persona_and_engine_failures(
    tmp_path: Path, capsys
) -> None:
    assert main([], env={"SERVER_PORT": "bad"}) == 1
    assert "SERVER_PORT" in capsys.readouterr().err

    missing_persona = tmp_path / "missing.json"
    assert (
        main(
            [],
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
            [],
            env={
                **_ENV,
                "DATA_DIR": str(tmp_path),
                "PERSONA_PATH": str(bad_persona),
            },
        )
        == 1
    )
    assert "Persona must be a JSON object." in capsys.readouterr().err

    assert (
        main(
            [],
            env={**_ENV, "DATA_DIR": str(tmp_path), "LLM_API_KEY": ""},
        )
        == 1
    )
    assert "LLM_API_KEY is required." in capsys.readouterr().err


def test_main_opens_explicit_log_and_reports_debug_path(tmp_path: Path, capsys) -> None:
    assert (
        main(
            ["--log-file", str(tmp_path)],
            env={**_ENV, "DATA_DIR": str(tmp_path)},
        )
        == 1
    )
    assert "Error: cannot open log file:" in capsys.readouterr().err

    explicit_log = tmp_path / "explicit.jsonl"
    outputs: list[str] = []
    with patch("my_agents.shell.server.serve"):
        assert (
            main(
                ["--log-file", str(explicit_log), "--debug"],
                env={
                    **_ENV,
                    "DATA_DIR": str(tmp_path),
                    "SERVER_HOST": "127.0.0.1",
                    "SERVER_PORT": "8123",
                },
                write_output=outputs.append,
            )
            == 0
        )
    assert f"Debug: log file = {explicit_log}" in capsys.readouterr().err
    assert explicit_log.exists()
    assert explicit_log.read_text(encoding="utf-8") == ""
    assert outputs == ["my-agents listening on http://127.0.0.1:8123"]


def test_main_writes_settings_failure_to_the_explicit_jsonl(
    tmp_path: Path, capsys
) -> None:
    log_path = tmp_path / "error.jsonl"

    assert (
        main(
            ["--log-file", str(log_path)],
            env={"SERVER_PORT": "bad", "DATA_DIR": str(tmp_path)},
        )
        == 1
    )
    logged = log_path.read_text(encoding="utf-8")
    assert "settings_load_failed" in logged
    assert "SERVER_PORT" in logged
    assert "Error: Invalid setting" in capsys.readouterr().err


def test_main_uses_log_path_fallback_and_debug_none(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    env_log = tmp_path / "env.jsonl"
    with patch("my_agents.shell.server.serve"):
        assert (
            main(
                ["--debug"],
                env={
                    **_ENV,
                    "DATA_DIR": str(tmp_path),
                    "LOG_PATH": str(env_log),
                },
                write_output=lambda _text: None,
            )
            == 0
        )
    assert f"Debug: log file = {env_log}" in capsys.readouterr().err
    assert env_log.exists()

    with patch("my_agents.shell.server.serve"):
        assert (
            main(
                ["--debug"],
                env={
                    **_ENV,
                    "DATA_DIR": str(tmp_path),
                    "LOG_PATH": str(tmp_path),
                },
                write_output=lambda _text: None,
            )
            == 0
        )
    warning = capsys.readouterr().err
    assert "Warning: cannot open log file:" in warning
    assert f"Debug: log file = {tmp_path}" in warning

    with patch("my_agents.shell.server.serve"):
        assert (
            main(
                ["--debug"],
                env={**_ENV, "DATA_DIR": str(tmp_path)},
                write_output=lambda _text: None,
            )
            == 0
        )
    assert "Debug: log file = <none>" in capsys.readouterr().err


def test_main_starts_the_server_and_closes_the_logger(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    outputs: list[str] = []
    calls: list[tuple[object, str, int]] = []
    log_path = tmp_path / "run.jsonl"

    def fake_serve(engine: object, host: str, port: int) -> None:
        calls.append((engine, host, port))

    with patch("my_agents.shell.server.serve", side_effect=fake_serve):
        assert (
            main(
                ["--log-file", str(log_path)],
                env={
                    **_ENV,
                    "DATA_DIR": str(tmp_path),
                    "SERVER_HOST": "0.0.0.0",
                    "SERVER_PORT": "9001",
                },
                write_output=outputs.append,
            )
            == 0
        )

    assert outputs == ["my-agents listening on http://0.0.0.0:9001"]
    assert calls[0][1:] == ("0.0.0.0", 9001)
    assert log_path.read_text(encoding="utf-8") == ""


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
        assert main([], write_output=outputs.append) == 0

    assert outputs == ["my-agents listening on http://127.0.0.1:8124"]
