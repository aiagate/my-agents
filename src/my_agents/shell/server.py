import json
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import assert_never

import uvicorn
from flow_res import is_err
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from my_agents.core.types import AppError, AppErrorKind, EventRecord, TimelineItem
from my_agents.shell.engine import CharacterEngine

MAX_BODY_BYTES = 64 * 1024
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


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
        case AppErrorKind.STORAGE:
            return HTTPStatus.INTERNAL_SERVER_ERROR
        case AppErrorKind.LLM:
            return HTTPStatus.BAD_GATEWAY
        case AppErrorKind.UNEXPECTED:
            return HTTPStatus.INTERNAL_SERVER_ERROR
        case _:
            assert_never(kind)


def _error(error: AppError) -> JSONResponse:
    return JSONResponse(
        {"error": error.kind.value, "message": error.message},
        status_code=_status_for(error.kind),
    )


def _event_json(event: EventRecord) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "conversation_id": event.conversation_id,
        "name": event.name,
        "source": event.source,
        "occurred_at": event.occurred_at.isoformat(),
        "received_at": event.received_at.isoformat(),
        "status": event.status.value,
        "delivery": event.delivery.value,
        "merge_reason": event.merge_reason,
        "failure_reason": event.failure_reason,
        "surfaced_at": None
        if event.surfaced_at is None
        else event.surfaced_at.isoformat(),
        "linked_turn_id": event.linked_turn_id,
        "brief": None if event.brief is None else event.brief.model_dump(),
    }


def _timeline_json(item: TimelineItem) -> dict[str, object]:
    return {
        "kind": item.kind.value,
        "conversation_id": item.conversation_id,
        "content": item.content,
        "created_at": item.created_at.isoformat(),
        "event_id": item.event_id,
        "source_event_id": item.source_event_id,
        "origin": None if item.origin is None else item.origin.value,
        "delivery": item.delivery.value,
    }


def _sse(item: TimelineItem) -> str:
    return f"data: {json.dumps(_timeline_json(item), ensure_ascii=False)}\n\n"


async def _body(request: Request) -> dict[str, object] | JSONResponse:
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            length = int(raw_length)
        except ValueError:
            return JSONResponse({"error": "invalid_content_length"}, status_code=400)
        if length > MAX_BODY_BYTES:
            return JSONResponse({"error": "payload_too_large"}, status_code=413)
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return JSONResponse({"error": "payload_too_large"}, status_code=413)
    try:
        parsed = json.loads(body)
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
        if is_err(result):
            return _error(result.error)
        receipt = result.value
        return JSONResponse(
            {
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
        if is_err(result):
            return _error(result.error)
        outcome = result.value
        return JSONResponse(
            {
                "conversation_id": outcome.conversation_id,
                "reply": outcome.reply,
                "turn_index": outcome.turn_index,
                "source_event_ids": list(outcome.source_event_ids),
                "memory_updates": list(outcome.memory_updates),
            }
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
        result = engine.get_timeline(
            request.query_params.get("conversation_id", "default"), limit=limit
        )
        if is_err(result):
            return _error(result.error)
        return JSONResponse({"items": [_timeline_json(item) for item in result.value]})

    async def event_detail(request: Request) -> Response:
        event_id = request.path_params["event_id"]
        result = engine.get_event(event_id)
        if is_err(result):
            return _error(result.error)
        if result.value is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(_event_json(result.value))

    async def memory(_request: Request) -> Response:
        result = engine.get_memory()
        if is_err(result):
            return _error(result.error)
        blocks = result.value
        return JSONResponse(
            {
                "user": blocks.user,
                "relationship": blocks.relationship,
                "arc": blocks.arc,
                "chars": len(blocks.user) + len(blocks.relationship) + len(blocks.arc),
            }
        )

    async def reset(request: Request) -> Response:
        conversation_id = request.query_params.get("conversation_id", "default")
        result = engine.reset_conversation(conversation_id)
        if is_err(result):
            return _error(result.error)
        return JSONResponse({"deleted_turns": result.value})

    async def curate(request: Request) -> Response:
        raw = await _body(request)
        if isinstance(raw, JSONResponse):
            return raw
        result = await engine.curate_memory(_conversation(raw))
        if is_err(result):
            return _error(result.error)
        return JSONResponse(
            {
                "summarized": result.value.summarized,
                "promoted": result.value.promoted,
                "rejected": result.value.rejected,
            }
        )

    async def index(_request: Request) -> HTMLResponse:
        return HTMLResponse(
            "<!doctype html><html lang='ja'><meta charset='utf-8'>"
            "<title>my-agents</title>"
            "<body style='font-family:system-ui;max-width:46rem;margin:3rem auto'>"
            "<h1>my-agents</h1><p>Headless character engine is running.</p>"
            "<ul><li><a href='/health'>/health</a></li>"
            "<li><a href='/timeline'>/timeline</a></li>"
            "<li><a href='/memory'>/memory</a></li></ul></body></html>"
        )

    return Starlette(
        debug=False,
        routes=[
            Route("/", index, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            Route("/events", ingest, methods=["POST"]),
            Route("/chat", chat, methods=["POST"]),
            Route("/stream", stream, methods=["GET"]),
            Route("/timeline", timeline, methods=["GET"]),
            Route("/events/{event_id}", event_detail, methods=["GET"]),
            Route("/memory", memory, methods=["GET"]),
            Route("/reset", reset, methods=["POST"]),
            Route("/curate", curate, methods=["POST"]),
        ],
    )


def serve(engine: CharacterEngine, host: str, port: int) -> None:
    uvicorn.run(create_app(engine), host=host, port=port, log_level="warning")
