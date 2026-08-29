from __future__ import annotations

import asyncio
import hashlib
import json
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any, Protocol

from clientplatform.domain.connections import ClaimedDispatch, ConnectionPlatform
from clientplatform.domain.email_outbound import EmailPayload, normalize_email_address
from clientplatform.domain.programs import ContentKind


_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_SECURITY = frozenset({"ssl", "starttls"})

@dataclass(frozen=True, slots=True, repr=False)
class SmtpCredential:
    host: str
    port: int
    username: str
    password: str
    sender_email: str
    sender_name: str = ""
    security: str = "ssl"

    def __post_init__(self) -> None:
        host = str(self.host or "").strip().lower()
        if not _HOST_RE.fullmatch(host) or "." not in host:
            raise ValueError("SMTP host is invalid")
        port = int(self.port)
        if port < 1 or port > 65535:
            raise ValueError("SMTP port is invalid")
        username = str(self.username or "").strip()
        password = str(self.password or "")
        if not username or len(username) > 512:
            raise ValueError("SMTP username is invalid")
        if not password or len(password) > 4096:
            raise ValueError("SMTP password is invalid")
        sender = normalize_email_address(self.sender_email)
        sender_name = " ".join(str(self.sender_name or "").split())
        if len(sender_name) > 160:
            raise ValueError("SMTP sender name is too long")
        security = str(self.security or "").strip().lower()
        if security not in _SECURITY:
            raise ValueError("SMTP security must be ssl or starttls")
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "port", port)
        object.__setattr__(self, "username", username)
        object.__setattr__(self, "password", password)
        object.__setattr__(self, "sender_email", sender)
        object.__setattr__(self, "sender_name", sender_name)
        object.__setattr__(self, "security", security)

    def to_json(self) -> str:
        return json.dumps(
            {
                "host": self.host,
                "port": self.port,
                "username": self.username,
                "password": self.password,
                "sender_email": self.sender_email,
                "sender_name": self.sender_name,
                "security": self.security,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> "SmtpCredential":
        try:
            payload = json.loads(str(raw or ""))
        except json.JSONDecodeError as exc:
            raise ValueError("SMTP credential payload is invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError("SMTP credential payload is invalid")
        return cls(
            host=str(payload.get("host") or ""),
            port=int(payload.get("port") or 0),
            username=str(payload.get("username") or ""),
            password=str(payload.get("password") or ""),
            sender_email=str(payload.get("sender_email") or ""),
            sender_name=str(payload.get("sender_name") or ""),
            security=str(payload.get("security") or "ssl"),
        )


class EmailClient(Protocol):
    async def send(
        self,
        *,
        credential: SmtpCredential,
        recipient: str,
        payload: EmailPayload,
        idempotency_key: str,
    ) -> str: ...

    async def probe(self, *, credential: SmtpCredential) -> None: ...


class SmtpEmailError(RuntimeError):
    def __init__(self, code: str, *, provider_write_definitely_rejected: bool = False) -> None:
        super().__init__(str(code or "smtp_error")[:160])
        self.provider_write_definitely_rejected = bool(provider_write_definitely_rejected)


class SmtpEmailClient:
    """Bounded SMTP client. It never logs or persists resolved credentials."""

    def __init__(self, *, timeout_seconds: float = 20.0) -> None:
        timeout = float(timeout_seconds)
        if timeout <= 0 or timeout > 120:
            raise ValueError("SMTP timeout must be between 0 and 120 seconds")
        self._timeout_seconds = timeout

    def _connect(self, credential: SmtpCredential) -> smtplib.SMTP:
        context = ssl.create_default_context()
        if credential.security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                credential.host,
                credential.port,
                timeout=self._timeout_seconds,
                context=context,
            )
        else:
            client = smtplib.SMTP(
                credential.host,
                credential.port,
                timeout=self._timeout_seconds,
            )
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        client.login(credential.username, credential.password)
        return client

    def _probe_sync(self, credential: SmtpCredential) -> None:
        try:
            client = self._connect(credential)
            try:
                code, _response = client.noop()
                if int(code) >= 400:
                    raise SmtpEmailError("smtp_probe_rejected", provider_write_definitely_rejected=True)
            finally:
                try:
                    client.quit()
                except smtplib.SMTPException:
                    client.close()
        except SmtpEmailError:
            raise
        except smtplib.SMTPAuthenticationError as exc:
            raise SmtpEmailError("smtp_authentication_failed", provider_write_definitely_rejected=True) from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise SmtpEmailError("smtp_probe_failed") from exc

    async def probe(self, *, credential: SmtpCredential) -> None:
        await asyncio.to_thread(self._probe_sync, credential)

    def _send_sync(
        self,
        *,
        credential: SmtpCredential,
        recipient: str,
        payload: EmailPayload,
        idempotency_key: str,
    ) -> str:
        target = normalize_email_address(recipient)
        digest = hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()
        domain = credential.sender_email.rsplit("@", 1)[1]
        message_id = f"<cp-{digest[:40]}@{domain}>"
        message = EmailMessage()
        message["From"] = formataddr((credential.sender_name, credential.sender_email))
        message["To"] = target
        message["Subject"] = payload.subject
        message["Message-ID"] = message_id
        message["Auto-Submitted"] = "auto-generated"
        message.set_content(payload.body)
        try:
            client = self._connect(credential)
            try:
                refused = client.send_message(
                    message,
                    from_addr=credential.sender_email,
                    to_addrs=[target],
                )
                if refused:
                    raise SmtpEmailError(
                        "smtp_recipient_rejected",
                        provider_write_definitely_rejected=True,
                    )
            finally:
                try:
                    client.quit()
                except smtplib.SMTPException:
                    client.close()
        except SmtpEmailError:
            raise
        except smtplib.SMTPRecipientsRefused as exc:
            raise SmtpEmailError(
                "smtp_recipient_rejected",
                provider_write_definitely_rejected=True,
            ) from exc
        except smtplib.SMTPAuthenticationError as exc:
            raise SmtpEmailError(
                "smtp_authentication_failed",
                provider_write_definitely_rejected=True,
            ) from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise SmtpEmailError("smtp_send_outcome_ambiguous") from exc
        return message_id

    async def send(
        self,
        *,
        credential: SmtpCredential,
        recipient: str,
        payload: EmailPayload,
        idempotency_key: str,
    ) -> str:
        return await asyncio.to_thread(
            self._send_sync,
            credential=credential,
            recipient=recipient,
            payload=payload,
            idempotency_key=idempotency_key,
        )


@dataclass(frozen=True, slots=True)
class _PreparedEmail:
    item: ClaimedDispatch
    recipient: str
    payload: EmailPayload


class SmtpEmailDispatchAdapter:
    """Email sender using the canonical two-phase non-replay boundary."""

    platform = ConnectionPlatform.EMAIL

    def __init__(self, client: EmailClient | None = None) -> None:
        self._client = client or SmtpEmailClient()

    async def prepare(self, item: ClaimedDispatch, credential: str) -> object:
        if item.dispatch.platform != self.platform:
            raise ValueError("dispatch platform does not match email adapter")
        if item.dispatch.payload_kind != ContentKind.MIXED:
            raise ValueError("email dispatch requires a mixed JSON payload")
        SmtpCredential.from_json(credential)
        return _PreparedEmail(
            item=item,
            recipient=normalize_email_address(item.external_subject),
            payload=EmailPayload.from_json(item.dispatch.payload_ref),
        )

    async def send(self, item: ClaimedDispatch, credential: str) -> str:
        prepared = await self.prepare(item, credential)
        return await self.send_prepared(prepared, credential)

    async def send_prepared(self, prepared: object, credential: str) -> str:
        if not isinstance(prepared, _PreparedEmail):
            raise ValueError("prepared email has an invalid type")
        smtp = SmtpCredential.from_json(credential)
        return await self._client.send(
            credential=smtp,
            recipient=prepared.recipient,
            payload=prepared.payload,
            idempotency_key=prepared.item.dispatch.idempotency_key,
        )

    async def release_prepared(self, prepared: object) -> None:
        del prepared


__all__ = [
    "EmailClient",
    "EmailPayload",
    "SmtpCredential",
    "SmtpEmailClient",
    "SmtpEmailDispatchAdapter",
    "SmtpEmailError",
    "normalize_email_address",
]
