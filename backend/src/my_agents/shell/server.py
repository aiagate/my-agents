import asyncio
import binascii
import json
import re
from base64 import urlsafe_b64decode
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from os import environ
from pathlib import Path
from typing import Annotated, assert_never
from urllib.parse import urlsplit
from uuid import UUID

import uvicorn
from cryptography.hazmat.primitives.asymmetric import ec
from flow_res import Result, is_err
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictStr,
    ValidationError,
    field_validator,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from my_agents.core.memory import memory_char_count
from my_agents.core.types import (
    AppError,
    AppErrorKind,
    CharacterPersona,
    MemorySnapshot,
    TimelineItem,
)
from my_agents.shell.engine import CharacterEngine
from my_agents.shell.llm import Llm, static_secrets
from my_agents.shell.logger import (
    NOOP_LOGGER,
    IAppLogger,
    JsonlLogger,
    LogEntry,
    LogLevel,
    write_stderr,
    write_stdout,
)
from my_agents.shell.push import PushSubscription, WebPushService
from my_agents.shell.settings import Settings, load_persona, load_settings
from my_agents.shell.store import Store

MAX_BODY_BYTES = 64 * 1024
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_CONVERSATION_ID = 128


def _status_for(kind: AppErrorKind) -> int:
    match kind:
        case AppErrorKind.VALIDATION:
            return HTTPStatus.UNPROCESSABLE_ENTITY
        case AppErrorKind.NOT_FOUND:
            return HTTPStatus.NOT_FOUND
        case AppErrorKind.CONFLICT:
            return HTTPStatus.CONFLICT
        case AppErrorKind.STORAGE:
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
    status_code: int = HTTPStatus.OK,
) -> Response:
    if is_err(result):
        return _error(result.error)
    return JSONResponse(_to_json(result.value), status_code=status_code)


def _to_json(value: object) -> object:
    match value:
        case datetime():
            return value.isoformat()
        case StrEnum():
            return value.value
        case MemorySnapshot():
            blocks = value.blocks
            return {
                "conversation_id": value.conversation_id,
                "user": blocks.user,
                "relationship": blocks.relationship,
                "arc": blocks.arc,
                "chars": memory_char_count(blocks),
                "counts": _to_json(value.counts),
            }
        case _ if is_dataclass(value):
            return {
                field.name: _to_json(getattr(value, field.name))
                for field in fields(value)
            }
        case list() | tuple() as items:
            return [_to_json(item) for item in items]
        case _:
            return value


def _collection[T](name: str, result: Result[tuple[T, ...], AppError]) -> Response:
    if is_err(result):
        return _error(result.error)
    return JSONResponse({name: _to_json(result.value)})


def _cursor(raw: str) -> str:
    parsed = UUID(raw)
    if parsed.version != 7:
        raise ValueError("Cursor must be a UUIDv7.")
    return str(parsed)


def _decode_base64url(value: str) -> bytes:
    if not _BASE64URL.fullmatch(value):
        raise ValueError("invalid base64url")
    try:
        return urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64url") from exc


def _valid_conversation_id(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return "default"
    if len(value) > _MAX_CONVERSATION_ID or any(
        ord(char) < 0x20 or ord(char) == 0x7F for char in value
    ):
        return None
    return value


class _PushKeysInput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    p256dh: str
    auth: str

    @field_validator("p256dh")
    @classmethod
    def _validate_p256dh(cls, value: str) -> str:
        raw = _decode_base64url(value)
        if len(raw) != 65:
            raise ValueError("p256dh must be 65 bytes")
        try:
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        except ValueError as exc:
            raise ValueError("p256dh must be a valid P-256 point") from exc
        return value

    @field_validator("auth")
    @classmethod
    def _validate_auth(cls, value: str) -> str:
        if len(_decode_base64url(value)) != 16:
            raise ValueError("auth must be 16 bytes")
        return value


class _PushSubscriptionInput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    endpoint: str
    keys: _PushKeysInput
    conversation_id: (
        Annotated[StrictStr, Field(min_length=1, max_length=_MAX_CONVERSATION_ID)]
        | None
    ) = None

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        if not value or any(char.isspace() or ord(char) < 0x20 for char in value):
            raise ValueError("endpoint contains whitespace or control characters")
        parts = urlsplit(value)
        if not parts.hostname:
            raise ValueError("endpoint must include a hostname")
        if parts.scheme != "https" and not (
            parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS
        ):
            raise ValueError("endpoint must use https or loopback http")
        if parts.username is not None or parts.password is not None:
            raise ValueError("endpoint must not include credentials")
        if parts.fragment:
            raise ValueError("endpoint must not include a fragment")
        try:
            _ = parts.port
        except ValueError as exc:
            raise ValueError("endpoint has an invalid port") from exc
        return value

    @field_validator("conversation_id")
    @classmethod
    def _validate_conversation_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > _MAX_CONVERSATION_ID or any(
            ord(char) < 0x20 or ord(char) == 0x7F for char in value
        ):
            raise ValueError("conversation_id is invalid")
        return value


def _push_subscription(raw: dict[str, object]) -> PushSubscription | None:
    try:
        parsed = _PushSubscriptionInput.model_validate(raw)
    except ValidationError:
        return None
    return PushSubscription(
        endpoint=parsed.endpoint,
        p256dh=parsed.keys.p256dh,
        auth=SecretStr(parsed.keys.auth),
        created_at=datetime.now(UTC),
        conversation_id=parsed.conversation_id,
    )


def _sse(item: TimelineItem, *, include_id: bool = True) -> str:
    data = f"data: {json.dumps(_to_json(item), ensure_ascii=False)}\n\n"
    if not include_id:
        return data
    return f"id: {item.item_id}\n{data}"


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


def _conversation(raw: dict[str, object]) -> str | None:
    return _valid_conversation_id(raw.get("conversation_id", "default"))


def _invalid_conversation() -> JSONResponse:
    return JSONResponse(
        {"error": "invalid_conversation_id"},
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
    )


async def _stream(
    engine: CharacterEngine,
    conversation_id: str,
    after_id: str | None,
) -> AsyncGenerator[str]:
    if after_id is None:
        async for item in engine.subscribe(conversation_id):
            yield _sse(item)
        return
    subscription = engine.subscribe(conversation_id)
    pending = asyncio.create_task(anext(subscription))
    last_id: str | None = after_id
    replayed_ids: set[str] = set()
    try:
        replayed = await asyncio.to_thread(
            engine.get_timeline,
            conversation_id,
            limit=200,
            after_id=after_id,
        )
        if not is_err(replayed):
            for item in replayed.value:
                if last_id is None or item.item_id > last_id:
                    last_id = item.item_id
                replayed_ids.add(item.item_id)
                yield _sse(item)
        try:
            item = await pending
        except StopAsyncIteration:
            return
        while True:
            skip = (
                item.item_id in replayed_ids
                or item.item_id == last_id
                or (after_id is not None and item.item_id <= after_id)
            )
            if not skip:
                if last_id is None or item.item_id > last_id:
                    last_id = item.item_id
                    yield _sse(item, include_id=True)
                else:
                    yield _sse(item, include_id=False)
            try:
                item = await anext(subscription)
            except StopAsyncIteration:
                return
    finally:
        if not pending.done():
            pending.cancel()
        with suppress(asyncio.CancelledError, StopAsyncIteration):
            await pending
        await subscription.aclose()


def create_app(
    engine: CharacterEngine,
    push: WebPushService,
    *,
    web_dist_dir: Path | None = None,
) -> Starlette:
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "my-agents"})

    async def ingest(request: Request) -> Response:
        raw = await _body(request)
        if isinstance(raw, JSONResponse):
            return raw
        body = dict(raw)
        conversation_id = _conversation(body)
        if conversation_id is None:
            return _invalid_conversation()
        body.pop("conversation_id", None)
        result = await engine.ingest_event(conversation_id, body)
        return _ok(result, status_code=HTTPStatus.ACCEPTED)

    async def chat(request: Request) -> Response:
        raw = await _body(request)
        if isinstance(raw, JSONResponse):
            return raw
        message = raw.get("message")
        if not isinstance(message, str):
            return JSONResponse({"error": "message_required"}, status_code=422)
        conversation_id = _conversation(raw)
        if conversation_id is None:
            return _invalid_conversation()
        result = await engine.submit_message(conversation_id, message)
        return _ok(result)

    async def stream(request: Request) -> Response:
        conversation_id = _valid_conversation_id(
            request.query_params.get("conversation_id", "default")
        )
        if conversation_id is None:
            return _invalid_conversation()
        raw_cursor = request.query_params.get("since")
        if raw_cursor is None:
            raw_cursor = request.headers.get("last-event-id")
        after_id: str | None = None
        if raw_cursor is not None:
            try:
                after_id = _cursor(raw_cursor)
            except ValueError:
                return JSONResponse({"error": "invalid_since"}, status_code=400)
        return StreamingResponse(
            _stream(engine, conversation_id, after_id),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    async def timeline(request: Request) -> Response:
        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError:
            return JSONResponse({"error": "invalid_limit"}, status_code=400)
        conversation_id = _valid_conversation_id(
            request.query_params.get("conversation_id", "default")
        )
        if conversation_id is None:
            return _invalid_conversation()
        result = await asyncio.to_thread(
            engine.get_timeline,
            conversation_id,
            limit=limit,
        )
        return _collection("items", result)

    async def conversations(request: Request) -> Response:
        try:
            limit = int(request.query_params.get("limit", "100"))
        except ValueError:
            return JSONResponse({"error": "invalid_limit"}, status_code=400)
        result = await asyncio.to_thread(engine.list_conversations, limit=limit)
        return _collection("conversations", result)

    async def context(request: Request) -> Response:
        conversation_id = _valid_conversation_id(
            request.query_params.get("conversation_id", "default")
        )
        if conversation_id is None:
            return _invalid_conversation()
        result = await engine.context_snapshot(conversation_id)
        return _ok(result)

    async def event_detail(request: Request) -> Response:
        event_id = request.path_params["event_id"]
        result = await asyncio.to_thread(engine.get_event, event_id)
        if is_err(result):
            return _error(result.error)
        if result.value is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(_to_json(result.value))

    async def memory(request: Request) -> Response:
        conversation_id = _valid_conversation_id(
            request.query_params.get("conversation_id", "default")
        )
        if conversation_id is None:
            return _invalid_conversation()
        result = await asyncio.to_thread(
            engine.get_memory_snapshot,
            conversation_id,
        )
        return _ok(result)

    async def reset(request: Request) -> Response:
        guarded = await _guard(request)
        if isinstance(guarded, JSONResponse):
            return guarded
        conversation_id = _valid_conversation_id(
            request.query_params.get("conversation_id", "default")
        )
        if conversation_id is None:
            return _invalid_conversation()
        result = await asyncio.to_thread(engine.reset_conversation, conversation_id)
        if is_err(result):
            return _error(result.error)
        return JSONResponse({"deleted_messages": result.value})

    async def vapid_public_key(_request: Request) -> JSONResponse:
        return JSONResponse({"public_key": push.public_key})

    async def subscribe_push(request: Request) -> Response:
        raw = await _body(request)
        if isinstance(raw, JSONResponse):
            return raw
        subscription = _push_subscription(raw)
        if subscription is None:
            return JSONResponse(
                {"error": "invalid_subscription"},
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        result = await asyncio.to_thread(push.save_subscription, subscription)
        if is_err(result):
            return _error(result.error)
        return JSONResponse(
            {"status": "subscribed", "endpoint": subscription.endpoint},
            status_code=HTTPStatus.CREATED,
        )

    async def unsubscribe_push(request: Request) -> Response:
        raw = await _body(request)
        if isinstance(raw, JSONResponse):
            return raw
        endpoint = raw.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            return JSONResponse(
                {"error": "invalid_endpoint"},
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        result = await asyncio.to_thread(push.delete_subscription, endpoint)
        if is_err(result):
            return _error(result.error)
        return JSONResponse({"deleted": result.value})

    async def index(_request: Request) -> Response:
        if web_dist_dir is not None and (web_dist_dir / "index.html").is_file():
            return FileResponse(
                web_dist_dir / "index.html", headers={"Cache-Control": "no-cache"}
            )
        return JSONResponse(
            {
                "error": "web_ui_not_built",
                "message": (
                    "Web UI is not built. Run 'cd web && npm run build' "
                    "or set WEB_DIST_DIR."
                ),
            },
            status_code=HTTPStatus.NOT_FOUND,
        )

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

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/events", ingest, methods=["POST"]),
        Route("/chat", chat, methods=["POST"]),
        Route("/stream", stream, methods=["GET"]),
        Route("/timeline", timeline, methods=["GET"]),
        Route("/conversations", conversations, methods=["GET"]),
        Route("/context", context, methods=["GET"]),
        Route("/events/{event_id}", event_detail, methods=["GET"]),
        Route("/memory", memory, methods=["GET"]),
        Route("/reset", reset, methods=["POST"]),
        Route("/push/vapid-public-key", vapid_public_key, methods=["GET"]),
        Route("/push/subscriptions", subscribe_push, methods=["POST"]),
        Route("/push/subscriptions", unsubscribe_push, methods=["DELETE"]),
    ]
    if web_dist_dir is not None:
        resolved_root = web_dist_dir.resolve()

        async def static_asset(request: Request) -> Response:
            asset_path = request.path_params["asset_path"]
            candidate = (resolved_root / asset_path).resolve()
            if not candidate.is_relative_to(resolved_root) or not candidate.is_file():
                return JSONResponse({"error": "not_found"}, status_code=404)
            if asset_path == "sw.js":
                return FileResponse(
                    candidate,
                    headers={
                        "Cache-Control": "no-cache",
                        "Service-Worker-Allowed": "/",
                    },
                )
            if asset_path == "manifest.webmanifest":
                return FileResponse(
                    candidate,
                    media_type="application/manifest+json",
                    headers={"Cache-Control": "no-cache"},
                )
            return FileResponse(candidate)

        routes.append(Route("/{asset_path:path}", static_asset, methods=["GET"]))
    return Starlette(debug=False, lifespan=lifespan, routes=routes)


def serve(
    engine: CharacterEngine,
    push: WebPushService,
    host: str,
    port: int,
    *,
    web_dist_dir: Path | None = None,
) -> None:
    uvicorn.run(
        create_app(engine, push, web_dist_dir=web_dist_dir),
        host=host,
        port=port,
        log_level="warning",
        timeout_graceful_shutdown=5,
    )


def _try_logger(
    path: Path | None,
    write_error: Callable[[str], None],
    *,
    redactions: Sequence[SecretStr] = (),
) -> IAppLogger | None:
    if path is None:
        return NOOP_LOGGER
    try:
        return JsonlLogger.open(path, redactions=redactions)
    except OSError as exc:
        write_error(f"Error: cannot open log file: {exc}")
        return None


def _web_dist_dir(settings: Settings) -> Path | None:
    if settings.web_dist_dir is not None:
        return settings.web_dist_dir
    default = Path(__file__).resolve().parents[4] / "web" / "dist"
    return default if (default / "index.html").is_file() else None


def _runtime(
    settings: Settings,
    persona: CharacterPersona,
    logger: IAppLogger,
    *,
    base_env: Mapping[str, str],
    redactions: Sequence[SecretStr] = (),
) -> tuple[CharacterEngine, WebPushService]:
    store = Store(settings.db_path, settings.core_memory_path)
    llm = Llm.from_settings(settings, store=store, logger=logger, base_env=base_env)
    if is_err(llm):
        raise RuntimeError(llm.error.message)
    push = WebPushService.from_settings(settings, store=store, logger=logger)
    if is_err(push):
        raise RuntimeError(push.error.message)
    engine = CharacterEngine(
        store,
        llm.value,
        settings,
        persona,
        logger,
        proactive_channel=push.value.channel,
        redactions=redactions,
    )
    return engine, push.value


def run_server(
    *,
    env: Mapping[str, str] | None,
    write_output: Callable[[str], None],
    write_error: Callable[[str], None],
) -> int:
    resolved_env: Mapping[str, str] = environ if env is None else env
    loaded = load_settings(env=resolved_env, env_path=Path.cwd() / ".env")
    if is_err(loaded):
        write_error(f"Error: {loaded.error.message}")
        return 1
    settings = loaded.value
    redactions = static_secrets(settings)
    logger = _try_logger(settings.log_path, write_error, redactions=redactions)
    if logger is None:
        return 1
    try:
        persona = load_persona(settings.persona_path)
        if is_err(persona):
            write_error(f"Error: {persona.error.message}")
            return 1
        try:
            engine, push = _runtime(
                settings,
                persona.value,
                logger,
                base_env=resolved_env,
                redactions=redactions,
            )
        except RuntimeError as exc:
            write_error(f"Error: {exc}")
            return 1
        write_output(
            f"my-agents listening on http://{settings.server_host}:{settings.server_port}"
        )
        serve(
            engine,
            push,
            settings.server_host,
            settings.server_port,
            web_dist_dir=_web_dist_dir(settings),
        )
        return 0
    finally:
        logger.close()


def main(
    *,
    env: Mapping[str, str] | None = None,
    write_output: Callable[[str], None] = write_stdout,
    write_error: Callable[[str], None] = write_stderr,
) -> int:
    return run_server(env=env, write_output=write_output, write_error=write_error)
