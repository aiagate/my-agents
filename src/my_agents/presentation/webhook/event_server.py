import json
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import BaseServer
from threading import Thread

from flow_res import Err, Ok, Result, is_err

from my_agents.contracts.ports.event_webhook import (
    EventWebhookStartError,
    IEventWebhookServer,
)
from my_agents.contracts.ports.logger import (
    NOOP_LOGGER,
    IAppLogger,
    LogEntry,
    LogLevel,
)
from my_agents.presentation.webhook.background_scheduler import (
    BackgroundEventScheduler,
)
from my_agents.usecases._event_rules import object_mapping
from my_agents.usecases.accept_event import (
    AcceptEventUseCase,
    EventAcceptError,
)

MAX_REQUEST_BODY_BYTES = 64 * 1024
EVENTS_PATH = "/events"
HEALTH_PATH = "/health"


class EventAcceptingServer(ThreadingHTTPServer):
    accept_event: AcceptEventUseCase

    def __init__(
        self,
        address: tuple[str, int],
        accept_event: AcceptEventUseCase,
    ) -> None:
        super().__init__(address, _EventRequestHandler)
        self.accept_event = accept_event


def _accepting_server(server: BaseServer) -> EventAcceptingServer:
    if isinstance(server, EventAcceptingServer):
        return server
    raise RuntimeError("Event webhook handler received an unexpected server type.")


class _EventRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        _ = (format, args)

    def _send_json(self, status: HTTPStatus, body: Mapping[str, object]) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def do_GET(self) -> None:
        if self.path == HEALTH_PATH:
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _read_body(self) -> bytes | None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._send_json(
                HTTPStatus.LENGTH_REQUIRED, {"error": "content_length_required"}
            )
            return None
        try:
            length = int(raw_length)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"})
            return None
        if length <= 0 or length > MAX_REQUEST_BODY_BYTES:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "invalid_body_size"},
            )
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "incomplete_body"})
            return None
        return body

    def _parse_event(self, body: bytes) -> Mapping[str, object] | None:
        try:
            parsed = json.loads(body.decode("utf-8"))
        except UnicodeDecodeError, json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
            return None
        raw = object_mapping(parsed)
        if raw is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "event_must_be_object"})
            return None
        return raw

    def _accept(self, raw: Mapping[str, object]) -> None:
        result = _accepting_server(self.server).accept_event.execute(raw)
        if is_err(result):
            self._send_schema_error(result.error)
            return
        self._send_json(
            HTTPStatus.ACCEPTED,
            {"event_id": result.value.event_id, "status": "accepted"},
        )

    def _send_schema_error(self, error: EventAcceptError) -> None:
        self._send_json(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            {
                "error": "schema_violation",
                "message": error.display_message,
                "detail": error.message,
            },
        )

    def do_POST(self) -> None:
        if self.path != EVENTS_PATH:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        body = self._read_body()
        if body is None:
            return
        raw = self._parse_event(body)
        if raw is None:
            return
        self._accept(raw)


class EventWebhookServer(IEventWebhookServer):
    def __init__(
        self,
        accept_event: AcceptEventUseCase,
        scheduler: BackgroundEventScheduler,
        *,
        host: str = "127.0.0.1",
        port: int,
        logger: IAppLogger = NOOP_LOGGER,
    ) -> None:
        self._accept_event = accept_event
        self._scheduler = scheduler
        self._host = host
        self._port = port
        self._logger = logger
        self._httpd: EventAcceptingServer | None = None
        self._thread: Thread | None = None

    def start(self) -> Result[int, EventWebhookStartError]:
        try:
            httpd = EventAcceptingServer((self._host, self._port), self._accept_event)
        except OSError as exc:
            self._logger.log(
                LogEntry(
                    level=LogLevel.ERROR,
                    event="event_webhook_start_failed",
                    message=str(exc),
                )
            )
            return Err(
                EventWebhookStartError(
                    message=str(exc),
                    display_message="イベント Webhook サーバーを起動できませんでした。",
                )
            )
        self._scheduler.start()
        thread = Thread(target=httpd.serve_forever, name="event-webhook", daemon=True)
        thread.start()
        self._httpd = httpd
        self._thread = thread
        return Ok(int(httpd.server_port))

    def stop(self) -> None:
        if self._httpd is None or self._thread is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()
        self._scheduler.stop()
        self._httpd = None
        self._thread = None
