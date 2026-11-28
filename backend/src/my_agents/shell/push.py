import asyncio
import binascii
import json
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urlsplit

import httpx2
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from flow_res import Err, Ok, Result
from pydantic import AwareDatetime
from webpush import WebPush
from webpush.types import WebPushKeys, WebPushSubscription

from my_agents.core.types import (
    AppError,
    AppErrorKind,
    ProactiveNotice,
    SecretStr,
    redact_secrets,
)
from my_agents.shell.logger import IAppLogger, LogEntry, LogLevel
from my_agents.shell.settings import Settings

_PUSH_TIMEOUT_SECONDS = 10.0
_PUSH_TTL_SECONDS = 60
_VAPID_EXPIRATION_SECONDS = 12 * 60 * 60


@dataclass(frozen=True, slots=True)
class PushSubscription:
    endpoint: str
    p256dh: str
    auth: SecretStr
    created_at: AwareDatetime
    conversation_id: str | None = None


@dataclass(frozen=True, slots=True)
class PushPayload:
    conversation_id: str
    title: str
    body: str
    url: str
    tag: str
    event_id: str

    @classmethod
    def from_notice(cls, notice: ProactiveNotice) -> PushPayload:
        return cls(
            conversation_id=notice.conversation_id,
            title=notice.speaker_name,
            body=notice.content[:200],
            url=f"/?c={quote(notice.conversation_id, safe='')}",
            tag=f"proactive:{notice.event_id}",
            event_id=notice.event_id,
        )


class PushSubscriptionStore(Protocol):
    def upsert_push_subscription(
        self, subscription: PushSubscription
    ) -> Result[None, AppError]: ...

    def delete_push_subscription(self, endpoint: str) -> Result[bool, AppError]: ...

    def list_push_subscriptions(
        self, *, conversation_id: str | None = None
    ) -> Result[tuple[PushSubscription, ...], AppError]: ...


def _validation(message: str) -> AppError:
    return AppError(AppErrorKind.VALIDATION, message)


def _b64url(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _private_key(secret: SecretStr) -> ec.EllipticCurvePrivateKey:
    text = secret.get_secret_value().strip()
    raw = urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if len(raw) != 32:
        raise ValueError("VAPID private key must be 32 bytes.")
    return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())


def _public_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _application_server_key(key: ec.EllipticCurvePrivateKey) -> str:
    return _b64url(
        key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
    )


def _sign_vapid(key: ec.EllipticCurvePrivateKey, subject: str, endpoint: str) -> str:
    parts = urlsplit(endpoint)
    header = _b64url(b'{"alg":"ES256","typ":"JWT"}')
    payload = _b64url(
        json.dumps(
            {
                "aud": f"{parts.scheme}://{parts.netloc}",
                "exp": int(time.time()) + _VAPID_EXPIRATION_SECONDS,
                "sub": subject,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    signature = _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"{header}.{payload}.{signature}"


def _subscription(subscription: PushSubscription) -> WebPushSubscription:
    return WebPushSubscription(
        endpoint=subscription.endpoint,
        keys=WebPushKeys(
            auth=subscription.auth.get_secret_value(),
            p256dh=subscription.p256dh,
        ),
    )


class WebPushDeliverer:
    def __init__(
        self,
        *,
        private_key: ec.EllipticCurvePrivateKey,
        subject: str,
        public_key: str,
        store: PushSubscriptionStore,
        logger: IAppLogger,
        http_client: httpx2.AsyncClient,
    ) -> None:
        self._private_key = private_key
        self._subject = subject
        self._public_key = public_key
        self._store = store
        self._logger = logger
        self._http_client = http_client
        self._webpush = WebPush(
            private_key=private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            public_key=_public_pem(private_key),
            subscriber=subject.removeprefix("mailto:"),
            ttl=_PUSH_TTL_SECONDS,
            expiration=_VAPID_EXPIRATION_SECONDS,
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        store: PushSubscriptionStore,
        logger: IAppLogger,
        http_client: httpx2.AsyncClient | None = None,
    ) -> Result[WebPushDeliverer | None, AppError]:
        secret = settings.vapid_private_key
        subject = settings.vapid_subject
        if secret is None and subject is None:
            return Ok(None)
        if secret is None:
            return Err(
                _validation(
                    "VAPID_PRIVATE_KEY is required when web push is configured."
                )
            )
        if subject is None:
            return Err(
                _validation("VAPID_SUBJECT is required when web push is configured.")
            )
        if not (subject.startswith("mailto:") or subject.startswith("https://")):
            return Err(
                _validation("VAPID_SUBJECT must start with 'mailto:' or 'https://'.")
            )
        try:
            private_key = _private_key(secret)
        except ValueError, TypeError, binascii.Error:
            return Err(_validation("VAPID_PRIVATE_KEY is invalid."))
        public_key = _application_server_key(private_key)
        return Ok(
            cls(
                private_key=private_key,
                subject=subject,
                public_key=public_key,
                store=store,
                logger=logger,
                http_client=(
                    http_client if http_client is not None else httpx2.AsyncClient()
                ),
            )
        )

    @property
    def public_key(self) -> str:
        return self._public_key

    def _authorization(self, endpoint: str) -> str:
        token = _sign_vapid(self._private_key, self._subject, endpoint)
        return f"vapid t={token}, k={self._public_key}"

    async def deliver(self, notice: ProactiveNotice) -> None:
        listed = await asyncio.to_thread(
            self._store.list_push_subscriptions,
            conversation_id=notice.conversation_id,
        )
        if isinstance(listed, Err):
            self._logger.log(
                LogEntry(
                    LogLevel.ERROR,
                    "push_delivery_failed",
                    listed.error.message,
                    "PUSH_FAILED",
                )
            )
            return
        payload = PushPayload.from_notice(notice)
        body = json.dumps(
            {
                "title": payload.title,
                "body": payload.body,
                "url": payload.url,
                "tag": payload.tag,
                "conversation_id": payload.conversation_id,
                "event_id": payload.event_id,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        secrets = tuple(subscription.auth for subscription in listed.value)
        for subscription in listed.value:
            try:
                message = self._webpush.get(body, _subscription(subscription))
                response = await self._http_client.post(
                    subscription.endpoint,
                    content=message.encrypted,
                    headers={
                        "Authorization": self._authorization(subscription.endpoint),
                        "Content-Encoding": message.headers["content-encoding"],
                        "TTL": message.headers["ttl"],
                    },
                    timeout=_PUSH_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                self._logger.log(
                    LogEntry(
                        LogLevel.ERROR,
                        "push_delivery_failed",
                        redact_secrets(f"{type(exc).__name__}: {exc}", secrets),
                        "PUSH_FAILED",
                    )
                )
                continue
            if response.status_code in (404, 410):
                await asyncio.to_thread(
                    self._store.delete_push_subscription, subscription.endpoint
                )
                parts = urlsplit(subscription.endpoint)
                port = f":{parts.port}" if parts.port is not None else ""
                self._logger.log(
                    LogEntry(
                        LogLevel.INFO,
                        "push_subscription_pruned",
                        f"{parts.scheme}://{parts.hostname}{port}",
                    )
                )
            elif not 200 <= response.status_code < 300:
                self._logger.log(
                    LogEntry(
                        LogLevel.ERROR,
                        "push_delivery_failed",
                        redact_secrets(
                            f"HTTP {response.status_code}: {response.text}", secrets
                        ),
                        "PUSH_FAILED",
                    )
                )

    async def aclose(self) -> None:
        await self._http_client.aclose()


class WebPushService:
    def __init__(
        self,
        store: PushSubscriptionStore,
        deliverer: WebPushDeliverer | None = None,
    ) -> None:
        self._store = store
        self._deliverer = deliverer

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        store: PushSubscriptionStore,
        logger: IAppLogger,
        http_client: httpx2.AsyncClient | None = None,
    ) -> Result[WebPushService, AppError]:
        deliverer = WebPushDeliverer.from_settings(
            settings,
            store=store,
            logger=logger,
            http_client=http_client,
        )
        if isinstance(deliverer, Err):
            return deliverer
        return Ok(cls(store, deliverer.value))

    @property
    def public_key(self) -> str | None:
        return None if self._deliverer is None else self._deliverer.public_key

    @property
    def channel(self) -> WebPushDeliverer | None:
        return self._deliverer

    def save_subscription(
        self, subscription: PushSubscription
    ) -> Result[None, AppError]:
        return self._store.upsert_push_subscription(subscription)

    def delete_subscription(self, endpoint: str) -> Result[bool, AppError]:
        return self._store.delete_push_subscription(endpoint)
