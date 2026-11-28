import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from flow_res import Err, Ok, Result

from my_agents.core.types import (
    AppError,
    AppErrorKind,
    PushPayload,
    PushSubscription,
    SecretStr,
)
from my_agents.shell.logger import IAppLogger, LogEntry
from my_agents.shell.push import NoopDeliverer, WebPushDeliverer
from my_agents.shell.settings import Settings, load_settings
from my_agents.shell.store import Store
from tests.helpers import assert_err

_ENDPOINT = "https://push.example.com/sub/1"


@dataclass
class _SpyLogger(IAppLogger):
    entries: list[LogEntry] = field(default_factory=list)

    def log(self, entry: LogEntry) -> None:
        self.entries.append(entry)

    def close(self) -> None:
        return None


@dataclass
class _Calls:
    requests: list[tuple[str, bytes, dict[str, str]]] = field(default_factory=list)


class _FaultStore(Store):
    def list_push_subscriptions(
        self, *, conversation_id: str | None = None
    ) -> Result[tuple[PushSubscription, ...], AppError]:
        del conversation_id
        return Err(AppError(AppErrorKind.STORAGE, "subscription read failed"))


def _b64url(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _vapid_private() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return _b64url(key.private_numbers().private_value.to_bytes(32, "big"))


def _expected_public(private: str) -> str:
    raw = urlsafe_b64decode(private + "=" * (-len(private) % 4))
    key = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
    return _b64url(
        key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
    )


def _client(
    calls: _Calls,
    *,
    status: int = 201,
    text: str = "",
    failure: Exception | None = None,
) -> httpx2.AsyncClient:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        calls.requests.append(
            (str(request.url), request.content, dict(request.headers))
        )
        if failure is not None:
            raise failure
        return httpx2.Response(status, text=text)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _subscription(
    endpoint: str = _ENDPOINT,
    *,
    conversation_id: str | None = "default",
    p256dh: str | None = None,
    auth: str = "auth-secret",
    created_at: datetime | None = None,
) -> PushSubscription:
    if p256dh is None:
        key = ec.generate_private_key(ec.SECP256R1())
        p256dh = _b64url(
            key.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
        )
    return PushSubscription(
        endpoint=endpoint,
        p256dh=p256dh,
        auth=SecretStr(auth),
        created_at=created_at or datetime(2026, 5, 1, tzinfo=UTC),
        conversation_id=conversation_id,
    )


def _settings(tmp_path: Path, **overrides: str) -> Settings:
    loaded = load_settings(env={"DATA_DIR": str(tmp_path), **overrides})
    assert isinstance(loaded, Ok)
    return loaded.value


def _deliverer(
    tmp_path: Path,
    client: httpx2.AsyncClient,
    *,
    subject: str = "mailto:ops@example.com",
    private_key: str | None = None,
) -> tuple[WebPushDeliverer, Store, _SpyLogger]:
    settings = _settings(
        tmp_path,
        VAPID_PRIVATE_KEY=private_key or _vapid_private(),
        VAPID_SUBJECT=subject,
    )
    store = Store(settings.db_path, settings.core_memory_path)
    logger = _SpyLogger()
    result = WebPushDeliverer.from_settings(
        settings, store=store, logger=logger, http_client=client
    )
    assert isinstance(result, Ok)
    assert isinstance(result.value, WebPushDeliverer)
    return result.value, store, logger


def _payload() -> PushPayload:
    return PushPayload(
        conversation_id="default",
        title="Sora",
        body="urgent",
        url="/?c=default",
        tag="proactive:evt_1",
        event_id="evt_1",
    )


def _token_payload(authorization: str) -> dict[str, object]:
    token = authorization.removeprefix("vapid t=").split(",", 1)[0]
    encoded = token.split(".")[1]
    decoded = urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    parsed = json.loads(decoded)
    assert isinstance(parsed, dict)
    return {str(key): value for key, value in parsed.items()}


@pytest.mark.anyio
async def test_noop_deliverer_and_default_client(tmp_path: Path) -> None:
    noop = NoopDeliverer()
    assert noop.public_key is None
    await noop.deliver(_payload())
    await noop.aclose()

    private = _vapid_private()
    settings = _settings(
        tmp_path,
        VAPID_PRIVATE_KEY=private,
        VAPID_SUBJECT="mailto:ops@example.com",
    )
    configured = WebPushDeliverer.from_settings(
        settings,
        store=Store(settings.db_path, settings.core_memory_path),
        logger=_SpyLogger(),
    )
    assert isinstance(configured, Ok)
    assert isinstance(configured.value, WebPushDeliverer)
    assert configured.value.public_key == _expected_public(private)
    await configured.value.aclose()


@pytest.mark.anyio
async def test_from_settings_returns_noop_when_unconfigured(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    result = WebPushDeliverer.from_settings(
        settings,
        store=Store(settings.db_path, settings.core_memory_path),
        logger=_SpyLogger(),
    )
    assert isinstance(result, Ok)
    assert isinstance(result.value, NoopDeliverer)
    assert result.value.public_key == NoopDeliverer().public_key
    await result.value.deliver(_payload())
    await result.value.aclose()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"VAPID_SUBJECT": "mailto:a@b.c"}, "VAPID_PRIVATE_KEY is required"),
        ({"VAPID_PRIVATE_KEY": "private"}, "VAPID_SUBJECT is required"),
        (
            {"VAPID_PRIVATE_KEY": "private", "VAPID_SUBJECT": "ftp://example.com"},
            "must start with",
        ),
        (
            {"VAPID_PRIVATE_KEY": "not-base64!", "VAPID_SUBJECT": "mailto:a@b.c"},
            "VAPID_PRIVATE_KEY is invalid",
        ),
        (
            {
                "VAPID_PRIVATE_KEY": _b64url(b"short"),
                "VAPID_SUBJECT": "mailto:a@b.c",
            },
            "VAPID_PRIVATE_KEY is invalid",
        ),
    ],
)
def test_from_settings_rejects_partial_or_invalid_configuration(
    tmp_path: Path, overrides: dict[str, str], message: str
) -> None:
    settings = _settings(tmp_path, **overrides)
    result = WebPushDeliverer.from_settings(
        settings,
        store=Store(settings.db_path, settings.core_memory_path),
        logger=_SpyLogger(),
    )
    error = assert_err(result, kind=AppErrorKind.VALIDATION, message_contains=message)
    assert "not-base64" not in error.message


def test_from_settings_rejects_non_ec_material(tmp_path: Path) -> None:
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_pem = rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    settings = _settings(
        tmp_path,
        VAPID_PRIVATE_KEY=rsa_pem,
        VAPID_SUBJECT="mailto:ops@example.com",
    )
    assert_err(
        WebPushDeliverer.from_settings(
            settings,
            store=Store(settings.db_path, settings.core_memory_path),
            logger=_SpyLogger(),
        ),
        kind=AppErrorKind.VALIDATION,
        message_contains="VAPID_PRIVATE_KEY is invalid",
    )


@pytest.mark.anyio
async def test_deliver_encrypts_and_uses_webpush_headers(tmp_path: Path) -> None:
    calls = _Calls()
    client = _client(calls)
    deliverer, store, _logger = _deliverer(tmp_path, client)
    assert isinstance(store.upsert_push_subscription(_subscription()), Ok)
    assert isinstance(
        store.upsert_push_subscription(
            _subscription("https://push.example.com/global", conversation_id=None)
        ),
        Ok,
    )

    await deliverer.deliver(_payload())

    assert len(calls.requests) == 2
    for url, content, headers in calls.requests:
        assert url.startswith("https://push.example.com/")
        assert content
        assert headers["content-encoding"] == "aes128gcm"
        assert headers["ttl"] == "60"
        assert headers["authorization"].startswith("vapid t=")
        assert ", k=" in headers["authorization"]
    assert _token_payload(calls.requests[0][2]["authorization"])["sub"] == (
        "mailto:ops@example.com"
    )
    assert deliverer.public_key is not None

    await deliverer.aclose()


@pytest.mark.anyio
async def test_deliver_https_subject_signs_its_own_vapid_token(tmp_path: Path) -> None:
    calls = _Calls()
    client = _client(calls)
    deliverer, store, _logger = _deliverer(
        tmp_path, client, subject="https://example.com/contact"
    )
    assert isinstance(store.upsert_push_subscription(_subscription()), Ok)

    await deliverer.deliver(_payload())

    authorization = calls.requests[0][2]["authorization"]
    payload = _token_payload(authorization)
    assert payload["sub"] == "https://example.com/contact"
    assert payload["aud"] == "https://push.example.com"
    assert authorization.endswith(f", k={deliverer.public_key}")


@pytest.mark.anyio
async def test_deliver_logs_subscription_read_failure(tmp_path: Path) -> None:
    logger = _SpyLogger()
    settings = _settings(
        tmp_path,
        VAPID_PRIVATE_KEY=_vapid_private(),
        VAPID_SUBJECT="mailto:ops@example.com",
    )
    calls = _Calls()
    store = _FaultStore(settings.db_path, settings.core_memory_path)
    result = WebPushDeliverer.from_settings(
        settings, store=store, logger=logger, http_client=_client(calls)
    )
    assert isinstance(result, Ok)
    assert isinstance(result.value, WebPushDeliverer)

    await result.value.deliver(_payload())

    assert calls.requests == []
    assert logger.entries[0].event == "push_delivery_failed"
    assert logger.entries[0].error_type == "PUSH_FAILED"
    assert "subscription read failed" in logger.entries[0].message


@pytest.mark.anyio
@pytest.mark.parametrize("status", [404, 410])
async def test_deliver_prunes_expired_subscriptions(
    tmp_path: Path, status: int
) -> None:
    calls = _Calls()
    deliverer, store, logger = _deliverer(tmp_path, _client(calls, status=status))
    assert isinstance(store.upsert_push_subscription(_subscription()), Ok)

    await deliverer.deliver(_payload())

    remaining = store.list_push_subscriptions()
    assert isinstance(remaining, Ok)
    assert remaining.value == ()
    assert logger.entries[0].level.value == "info"
    assert logger.entries[0].event == "push_subscription_pruned"
    assert "https://push.example.com" in logger.entries[0].message
    assert "/sub/1" not in logger.entries[0].message


@pytest.mark.anyio
@pytest.mark.parametrize("status", [302, 500])
async def test_deliver_logs_non_2xx_failure_and_continues(
    tmp_path: Path, status: int
) -> None:
    calls = _Calls()
    client = _client(calls, status=status, text="boom auth-secret")
    deliverer, store, logger = _deliverer(tmp_path, client)
    assert isinstance(store.upsert_push_subscription(_subscription()), Ok)
    assert isinstance(
        store.upsert_push_subscription(
            _subscription("https://push.example.com/z", p256dh="broken")
        ),
        Ok,
    )

    await deliverer.deliver(_payload())

    assert len(calls.requests) == 1
    failures = [
        entry for entry in logger.entries if entry.event == "push_delivery_failed"
    ]
    assert len(failures) == 2
    assert failures[0].error_type == "PUSH_FAILED"
    assert f"HTTP {status}" in failures[0].message
    assert "auth-secret" not in failures[0].message
    assert failures[1].error_type == "PUSH_FAILED"
    assert f"HTTP {status}" not in failures[1].message


@pytest.mark.anyio
async def test_deliver_prune_log_keeps_port_and_drops_credentials(
    tmp_path: Path,
) -> None:
    endpoint = "https://user:pass@push.example.com:8443/sub/1"
    calls = _Calls()
    deliverer, store, logger = _deliverer(tmp_path, _client(calls, status=410))
    assert isinstance(store.upsert_push_subscription(_subscription(endpoint)), Ok)

    await deliverer.deliver(_payload())

    assert logger.entries[0].event == "push_subscription_pruned"
    assert logger.entries[0].message == "https://push.example.com:8443"
    assert "user" not in logger.entries[0].message
    assert "pass" not in logger.entries[0].message


@pytest.mark.anyio
async def test_deliver_catches_transport_errors_per_subscription(
    tmp_path: Path,
) -> None:
    calls = _Calls()
    client = _client(calls, failure=OSError("offline"))
    deliverer, store, logger = _deliverer(tmp_path, client)
    assert isinstance(store.upsert_push_subscription(_subscription()), Ok)

    await deliverer.deliver(_payload())

    assert len(calls.requests) == 1
    assert logger.entries[0].event == "push_delivery_failed"
    assert "OSError: offline" in logger.entries[0].message
