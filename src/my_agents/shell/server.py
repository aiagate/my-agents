import argparse
import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from enum import StrEnum
from functools import cache
from http import HTTPStatus
from importlib.resources import files
from os import environ
from pathlib import Path
from typing import assert_never

import uvicorn
from flow_res import Result, is_err
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from my_agents.core.memory import memory_char_count
from my_agents.core.types import (
    AppError,
    AppErrorKind,
    CharacterPersona,
    ContextSnapshot,
    EventRecord,
    MemorySnapshot,
    TimelineItem,
)
from my_agents.shell.engine import CharacterEngine
from my_agents.shell.llm import Llm
from my_agents.shell.logger import (
    NOOP_LOGGER,
    IAppLogger,
    JsonlLogger,
    LogEntry,
    LogLevel,
    write_stderr,
    write_stdout,
)
from my_agents.shell.settings import Settings, load_persona, load_settings
from my_agents.shell.store import Store

MAX_BODY_BYTES = 64 * 1024
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@cache
def _ui_html() -> str:
    return files("my_agents.shell").joinpath("ui.html").read_text(encoding="utf-8")


_EVENT_KEYS = (
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
)

_TIMELINE_KEYS = (
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
)


def _status_for(kind: AppErrorKind) -> int:
    match kind:
        case AppErrorKind.VALIDATION:
            return HTTPStatus.UNPROCESSABLE_ENTITY
        case AppErrorKind.NOT_FOUND:
            return HTTPStatus.NOT_FOUND
        case AppErrorKind.CONFLICT:
            return HTTPStatus.CONFLICT
        case AppErrorKind.DENIED:
            return HTTPStatus.FORBIDDEN
        case AppErrorKind.STORAGE | AppErrorKind.UNEXPECTED:
            return HTTPStatus.INTERNAL_SERVER_ERROR
        case AppErrorKind.LLM:
            return HTTPStatus.BAD_GATEWAY
        case _:
            assert_never(kind)


def _error(error: AppError) -> JSONResponse:
    return JSONResponse(
        {"error": error.kind.value, "message": error.message},
        status_code=_status_for(error.kind),
    )


def _ok[T](
    result: Result[T, AppError],
    render: Callable[[T], dict[str, object]],
    status_code: int = HTTPStatus.OK,
) -> Response:
    if is_err(result):
        return _error(result.error)
    return JSONResponse(render(result.value), status_code=status_code)


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    return value


def _pick(item: object, keys: tuple[str, ...]) -> dict[str, object]:
    return {key: _json_value(getattr(item, key)) for key in keys}


def _event_json(event: EventRecord) -> dict[str, object]:
    return _pick(event, _EVENT_KEYS)


def _timeline_json(item: TimelineItem) -> dict[str, object]:
    return _pick(item, _TIMELINE_KEYS)


def _counts_json(snapshot: ContextSnapshot | MemorySnapshot) -> dict[str, int]:
    return {
        "messages": snapshot.counts.messages,
        "events": snapshot.counts.events,
    }


def _context_json(snapshot: ContextSnapshot) -> dict[str, object]:
    return {
        "conversation_id": snapshot.conversation_id,
        "epoch": snapshot.epoch,
        "stable_prefix": snapshot.stable_prefix,
        "stable_prefix_sha256": snapshot.stable_prefix_sha256,
        "dynamic_section": snapshot.dynamic_section,
        "event_section": snapshot.event_section,
        "omitted_events": snapshot.omitted_events,
        "counts": _counts_json(snapshot),
        "memory_chars": snapshot.memory_chars,
    }


def _memory_json(snapshot: MemorySnapshot) -> dict[str, object]:
    blocks = snapshot.blocks
    return {
        "conversation_id": snapshot.conversation_id,
        "user": blocks.user,
        "relationship": blocks.relationship,
        "arc": blocks.arc,
        "chars": memory_char_count(blocks),
        "counts": _counts_json(snapshot),
    }


def _sse(item: TimelineItem) -> str:
    return f"data: {json.dumps(_timeline_json(item), ensure_ascii=False)}\n\n"


async def _guard(request: Request) -> bytes | JSONResponse:
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            length = int(raw_length)
        except ValueError:
            return JSONResponse({"error": "invalid_content_length"}, status_code=400)
        if length > MAX_BODY_BYTES:
            return JSONResponse({"error": "payload_too_large"}, status_code=413)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            return JSONResponse({"error": "payload_too_large"}, status_code=413)
        chunks.append(chunk)
    return b"".join(chunks)


async def _body(request: Request) -> dict[str, object] | JSONResponse:
    guarded = await _guard(request)
    if isinstance(guarded, JSONResponse):
        return guarded
    try:
        parsed = json.loads(guarded)
    except json.JSONDecodeError, UnicodeDecodeError:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(parsed, dict):
        return JSONResponse({"error": "body_must_be_object"}, status_code=400)
    return parsed


def _conversation(raw: dict[str, object]) -> str:
    value = raw.get("conversation_id", "default")
    return value if isinstance(value, str) and value else "default"


async def _stream(engine: CharacterEngine, conversation_id: str) -> AsyncIterator[str]:
    async for item in engine.subscribe(conversation_id):
        yield _sse(item)


def create_app(engine: CharacterEngine) -> Starlette:
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "my-agents"})

    async def ingest(request: Request) -> Response:
        raw = await _body(request)
        if isinstance(raw, JSONResponse):
            return raw
        body = dict(raw)
        conversation_id = _conversation(body)
        body.pop("conversation_id", None)
        result = await engine.ingest_event(conversation_id, body)
        return _ok(
            result,
            lambda receipt: {
                "event_id": receipt.event_id,
                "status": receipt.status.value,
                "duplicate": receipt.duplicate,
            },
            status_code=HTTPStatus.ACCEPTED,
        )

    async def chat(request: Request) -> Response:
        raw = await _body(request)
        if isinstance(raw, JSONResponse):
            return raw
        message = raw.get("message")
        if not isinstance(message, str):
            return JSONResponse({"error": "message_required"}, status_code=422)
        result = await engine.submit_message(_conversation(raw), message)
        return _ok(
            result,
            lambda outcome: {
                "conversation_id": outcome.conversation_id,
                "reply": outcome.reply,
                "message_id": outcome.message_id,
                "surfaced_event_ids": list(outcome.surfaced_event_ids),
                "memory_updates": list(outcome.memory_updates),
            },
        )

    async def stream(request: Request) -> Response:
        conversation_id = request.query_params.get("conversation_id", "default")
        return StreamingResponse(
            _stream(engine, conversation_id),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    async def timeline(request: Request) -> Response:
        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError:
            return JSONResponse({"error": "invalid_limit"}, status_code=400)
        result = await asyncio.to_thread(
            engine.get_timeline,
            request.query_params.get("conversation_id", "default"),
            limit=limit,
        )
        return _ok(
            result, lambda items: {"items": [_timeline_json(item) for item in items]}
        )

    async def context(request: Request) -> Response:
        result = await engine.context_snapshot(
            request.query_params.get("conversation_id", "default")
        )
        return _ok(result, _context_json)

    async def event_detail(request: Request) -> Response:
        event_id = request.path_params["event_id"]
        result = await asyncio.to_thread(engine.get_event, event_id)
        if is_err(result):
            return _error(result.error)
        if result.value is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(_event_json(result.value))

    async def memory(request: Request) -> Response:
        result = await asyncio.to_thread(
            engine.get_memory_snapshot,
            request.query_params.get("conversation_id", "default"),
        )
        return _ok(result, _memory_json)

    async def reset(request: Request) -> Response:
        guarded = await _guard(request)
        if isinstance(guarded, JSONResponse):
            return guarded
        conversation_id = request.query_params.get("conversation_id", "default")
        result = await asyncio.to_thread(engine.reset_conversation, conversation_id)
        return _ok(result, lambda deleted: {"deleted_messages": deleted})

    async def index(_request: Request) -> HTMLResponse:
        return HTMLResponse(_ui_html())

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        started = await engine.start()
        if is_err(started):
            engine.logger.log(
                LogEntry(LogLevel.ERROR, "server_start_failed", started.error.message)
            )
            await engine.aclose()
            raise RuntimeError(started.error.message)
        try:
            yield
        finally:
            await engine.aclose()

    return Starlette(
        debug=False,
        lifespan=lifespan,
        routes=[
            Route("/", index, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            Route("/events", ingest, methods=["POST"]),
            Route("/chat", chat, methods=["POST"]),
            Route("/stream", stream, methods=["GET"]),
            Route("/timeline", timeline, methods=["GET"]),
            Route("/context", context, methods=["GET"]),
            Route("/events/{event_id}", event_detail, methods=["GET"]),
            Route("/memory", memory, methods=["GET"]),
            Route("/reset", reset, methods=["POST"]),
        ],
    )


def serve(engine: CharacterEngine, host: str, port: int) -> None:
    uvicorn.run(create_app(engine), host=host, port=port, log_level="warning")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="my-agents")
    parser.add_argument("--log-file", type=Path, default=None)
    parser.add_argument("--debug", action="store_true")
    return parser


def _try_logger(
    path: Path | None, write_error: Callable[[str], None], *, explicit: bool
) -> IAppLogger | None:
    if path is None:
        return NOOP_LOGGER
    try:
        return JsonlLogger.open(path)
    except OSError as exc:
        if explicit:
            write_error(f"Error: cannot open log file: {exc}")
            return None
        write_error(f"Warning: cannot open log file: {exc}")
        return NOOP_LOGGER


def _engine(
    settings: Settings,
    persona: CharacterPersona,
    logger: IAppLogger,
    *,
    base_env: Mapping[str, str],
) -> CharacterEngine:
    store = Store(settings.db_path, settings.core_memory_path)
    llm = Llm.from_settings(settings, store=store, logger=logger, base_env=base_env)
    if is_err(llm):
        raise RuntimeError(llm.error.message)
    return CharacterEngine(store, llm.value, settings, persona, logger)


def run_server(
    argv: Sequence[str] | None,
    *,
    env: Mapping[str, str] | None,
    write_output: Callable[[str], None],
    write_error: Callable[[str], None],
) -> int:
    try:
        arguments = _parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code is None else int(exc.code)
    logger = _try_logger(arguments.log_file, write_error, explicit=True)
    if logger is None:
        return 1
    resolved_env: Mapping[str, str] = environ if env is None else env
    try:
        loaded = load_settings(env=resolved_env, env_path=Path.cwd() / ".env")
        if is_err(loaded):
            logger.log(
                LogEntry(
                    LogLevel.ERROR,
                    "settings_load_failed",
                    loaded.error.message,
                )
            )
            write_error(f"Error: {loaded.error.message}")
            return 1
        settings = loaded.value
        if arguments.log_file is None and settings.log_path is not None:
            logger.close()
            logger = (
                _try_logger(settings.log_path, write_error, explicit=False)
                or NOOP_LOGGER
            )
        if arguments.debug:
            resolved_path = arguments.log_file or settings.log_path
            write_error(f"Debug: log file = {resolved_path or '<none>'}")
        persona = load_persona(settings.persona_path)
        if is_err(persona):
            write_error(f"Error: {persona.error.message}")
            return 1
        try:
            engine = _engine(settings, persona.value, logger, base_env=resolved_env)
        except RuntimeError as exc:
            write_error(f"Error: {exc}")
            return 1
        write_output(
            f"my-agents listening on http://{settings.server_host}:{settings.server_port}"
        )
        serve(engine, settings.server_host, settings.server_port)
        return 0
    finally:
        logger.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    write_output: Callable[[str], None] = write_stdout,
    write_error: Callable[[str], None] = write_stderr,
) -> int:
    return run_server(argv, env=env, write_output=write_output, write_error=write_error)
