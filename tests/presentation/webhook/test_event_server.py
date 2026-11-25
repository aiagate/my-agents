import json
import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from flow_res import Ok, is_ok

from my_agents.contracts.events import InboundEvent
from my_agents.contracts.ports.event_webhook import EventWebhookStartError
from my_agents.presentation.webhook.background_scheduler import (
    BackgroundEventScheduler,
)
from my_agents.presentation.webhook.event_server import (
    EventWebhookServer,
    _accepting_server,
)
from my_agents.usecases.accept_event import AcceptEventHandler
from my_agents.usecases.handle_event import EventCompletion
from tests.event_fixtures import FIXED_NOW, temperature_event


class FakeHandleEvent:
    def __init__(self) -> None:
        self.events: list[InboundEvent] = []

    def execute(self, event: InboundEvent) -> Ok[EventCompletion]:
        self.events.append(event)
        return Ok(EventCompletion(event.event_id, "done"))


def _start(
    handle: FakeHandleEvent,
) -> tuple[EventWebhookServer, int, BackgroundEventScheduler]:
    scheduler = BackgroundEventScheduler(handle)
    accept = AcceptEventHandler(
        scheduler,
        clock=lambda: FIXED_NOW,
        event_id_factory=lambda: "evt_http",
    )
    server = EventWebhookServer(accept, scheduler, port=0)
    started = server.start()
    assert is_ok(started)
    return server, started.value, scheduler


@pytest.fixture
def running_server() -> Iterator[tuple[EventWebhookServer, int, FakeHandleEvent]]:
    handle = FakeHandleEvent()
    server, port, _ = _start(handle)
    try:
        yield server, port, handle
    finally:
        server.stop()


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = json.loads(response.read())
        return response.status, payload
    finally:
        connection.close()


def _raw_request(port: int, payload: bytes, *, shutdown_write: bool = False) -> int:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(payload)
        if shutdown_write:
            sock.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    return int(b"".join(chunks).split(b" ", 2)[1])


def _valid_body() -> bytes:
    return json.dumps(temperature_event()).encode("utf-8")


def test_health_endpoint_reports_ok(
    running_server: tuple[EventWebhookServer, int, FakeHandleEvent],
) -> None:
    _, port, _ = running_server

    status, body = _request(port, "GET", "/health")

    assert status == 200
    assert body == {"status": "ok"}


def test_unknown_get_and_post_paths_return_404(
    running_server: tuple[EventWebhookServer, int, FakeHandleEvent],
) -> None:
    _, port, _ = running_server

    get_status, get_body = _request(port, "GET", "/missing")
    post_status, post_body = _request(port, "POST", "/missing", body=b"{}")

    assert (get_status, get_body) == (404, {"error": "not_found"})
    assert (post_status, post_body) == (404, {"error": "not_found"})


def test_accepts_valid_event_and_schedules_worker(
    running_server: tuple[EventWebhookServer, int, FakeHandleEvent],
) -> None:
    server, port, handle = running_server

    status, body = _request(port, "POST", "/events", body=_valid_body())
    server.stop()

    assert status == 202
    assert body == {"event_id": "evt_http", "status": "accepted"}
    assert len(handle.events) == 1
    assert handle.events[0].event_id == "evt_http"


def test_returns_422_for_schema_violation(
    running_server: tuple[EventWebhookServer, int, FakeHandleEvent],
) -> None:
    _, port, handle = running_server
    body = json.dumps({"name": "email.received", "data": {}}).encode()

    status, payload = _request(port, "POST", "/events", body=body)

    assert status == 422
    assert payload["error"] == "schema_violation"
    assert "形式が不正" in str(payload["message"])
    assert handle.events == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"not-json", {"error": "invalid_json"}),
        (b"\xff\xfe", {"error": "invalid_json"}),
        (b"[1, 2]", {"error": "event_must_be_object"}),
    ],
)
def test_returns_400_for_malformed_body(
    running_server: tuple[EventWebhookServer, int, FakeHandleEvent],
    body: bytes,
    expected: dict[str, str],
) -> None:
    _, port, _ = running_server

    status, payload = _request(port, "POST", "/events", body=body)

    assert status == 400
    assert payload == expected


def test_returns_411_without_content_length(
    running_server: tuple[EventWebhookServer, int, FakeHandleEvent],
) -> None:
    _, port, _ = running_server
    request = b"POST /events HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"

    assert _raw_request(port, request) == 411


def test_returns_400_for_invalid_content_length(
    running_server: tuple[EventWebhookServer, int, FakeHandleEvent],
) -> None:
    _, port, _ = running_server
    request = (
        b"POST /events HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Length: nope\r\n"
        b"Connection: close\r\n\r\n"
    )

    assert _raw_request(port, request) == 400


def test_returns_413_for_oversized_body(
    running_server: tuple[EventWebhookServer, int, FakeHandleEvent],
) -> None:
    _, port, _ = running_server
    request = (
        b"POST /events HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Length: 65537\r\n"
        b"Connection: close\r\n\r\n"
    )

    assert _raw_request(port, request) == 413


def test_returns_400_for_incomplete_body(
    running_server: tuple[EventWebhookServer, int, FakeHandleEvent],
) -> None:
    _, port, _ = running_server
    request = (
        b"POST /events HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Length: 10\r\n"
        b"Connection: close\r\n\r\nabc"
    )

    assert _raw_request(port, request, shutdown_write=True) == 400


def test_start_reports_bind_failure_and_stop_without_start_is_noop() -> None:
    handle = FakeHandleEvent()
    first, port, _ = _start(handle)
    try:
        scheduler = BackgroundEventScheduler(handle)
        accept = AcceptEventHandler(
            scheduler,
            clock=lambda: datetime.now(UTC),
            event_id_factory=lambda: "evt_other",
        )
        second = EventWebhookServer(accept, scheduler, port=port)
        second.stop()
        result = second.start()
        second.stop()
    finally:
        first.stop()

    assert not is_ok(result)
    assert isinstance(result.error, EventWebhookStartError)
    assert "起動できませんでした" in result.error.display_message


def test_accepting_server_helper_rejects_unrelated_server() -> None:
    plain = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        with pytest.raises(RuntimeError, match="unexpected server type"):
            _accepting_server(plain)
    finally:
        plain.server_close()
