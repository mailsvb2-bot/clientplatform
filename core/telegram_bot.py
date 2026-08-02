from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientSession
from aiohttp.hdrs import USER_AGENT
from aiohttp.http import SERVER_SOFTWARE
from aiogram import Bot
from aiogram.__meta__ import __version__
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError

from core.runtime_env import env_float, env_int


log = logging.getLogger(__name__)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_POLLING_METHOD = "getupdates"
_CALLBACK_METHOD = "answercallbackquery"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return bool(default)
    normalized = str(raw).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    log.warning("Invalid boolean env %s; using default=%s", name, default)
    return bool(default)


def telegram_ip_family() -> int:
    """Return the explicit socket family used by Telegram polling.

    Production defaults to IPv4 because the server can publish an IPv6 address
    while Docker has no working IPv6 route. The setting remains reversible for
    environments where IPv6 or Happy Eyeballs is known to work.
    """

    raw = (os.getenv("TELEGRAM_IP_FAMILY") or "ipv4").strip().lower()
    if raw in {"ipv4", "4", "inet"}:
        return socket.AF_INET
    if raw in {"ipv6", "6", "inet6"}:
        return socket.AF_INET6
    if raw in {"auto", "any", "unspecified"}:
        return socket.AF_UNSPEC
    log.warning("Invalid TELEGRAM_IP_FAMILY; forcing IPv4")
    return socket.AF_INET


def telegram_connector_options() -> dict[str, Any]:
    """Build the aiohttp connector policy used by Telegram.

    A short keep-alive window is deliberately preferred over ``force_close``.
    It reuses the TLS connection while a person is navigating several buttons,
    but expires idle connections long before an upstream/NAT flow can become a
    silently stale one. Every observed network failure still resets the complete
    connector generation before retrying.
    """

    force_close = _env_bool("TELEGRAM_FORCE_CLOSE", False)
    options: dict[str, Any] = {
        "family": telegram_ip_family(),
        "ttl_dns_cache": env_int(
            "TELEGRAM_DNS_TTL_SEC",
            60,
            minimum=0,
            maximum=3600,
        ),
        "force_close": force_close,
        "limit_per_host": env_int(
            "TELEGRAM_CONNECTIONS_PER_HOST",
            20,
            minimum=2,
            maximum=100,
        ),
        "enable_cleanup_closed": True,
    }
    if not force_close:
        options["keepalive_timeout"] = env_float(
            "TELEGRAM_KEEPALIVE_TIMEOUT_SEC",
            20.0,
            minimum=1.0,
            maximum=120.0,
        )
    return options


def _native_http_proxy(proxy: Any) -> str | None:
    """Validate a native aiohttp HTTP CONNECT proxy without exposing credentials.

    Aiogram normally routes every proxy scheme through the optional
    ``aiohttp-socks`` package. A plain ``http://`` CONNECT relay is already
    supported natively by aiohttp 3.14, so production can use a narrowly scoped
    relay without adding another transport dependency to the locked runtime.
    """

    if proxy in (None, ""):
        return None
    if not isinstance(proxy, str):
        return None

    try:
        parsed = urlsplit(proxy)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid_telegram_http_proxy_url") from exc

    if parsed.scheme.lower() != "http":
        return None
    if not parsed.hostname or port is None:
        raise ValueError("invalid_telegram_http_proxy_url")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("invalid_telegram_http_proxy_url")
    return proxy


class PollingAiohttpSession(AiohttpSession):
    """Aiohttp transport hardened for responsive Telegram polling and UI calls."""

    # Aiogram 3.29.1 creates these fields dynamically in its concrete session
    # class. State their pinned compatibility types explicitly so strict mypy can
    # validate our override without weakening checks or reaching into Any.
    _session: ClientSession | None
    _should_reset_connector: bool

    def __init__(
        self,
        *,
        proxy: str | None = None,
        limit: int = 100,
        **kwargs: Any,
    ) -> None:
        native_http_proxy = _native_http_proxy(proxy)
        super().__init__(
            proxy=None if native_http_proxy is not None else proxy,
            limit=limit,
            **kwargs,
        )
        # aiogram 3.29.1 stores the exact TCPConnector kwargs here. The project
        # pins that version, and regression tests lock this compatibility seam.
        self._connector_init.update(telegram_connector_options())
        self._native_http_proxy = native_http_proxy
        self._transport_generation = 0
        self._transport_reset_lock = asyncio.Lock()

    @property
    def transport_generation(self) -> int:
        return self._transport_generation

    @property
    def connector_options(self) -> dict[str, Any]:
        """Expose a read-only snapshot for health checks and contract tests."""

        return dict(self._connector_init)

    @property
    def proxy_mode(self) -> str:
        """Expose only the transport kind, never the relay URL or credentials."""

        if self._native_http_proxy is not None:
            return "http_connect"
        if self.proxy is not None:
            return "connector_proxy"
        return "direct"

    async def create_session(self) -> ClientSession:
        """Create an aiohttp session with a native HTTP CONNECT relay when set."""

        if self._native_http_proxy is None:
            return await super().create_session()

        if self._should_reset_connector:
            await self.close()
        if self._session is None or self._session.closed:
            self._session = ClientSession(
                connector=self._connector_type(**self._connector_init),
                headers={
                    USER_AGENT: f"{SERVER_SOFTWARE} aiogram/{__version__}",
                },
                proxy=self._native_http_proxy,
            )
            self._should_reset_connector = False
        return self._session

    async def reset_transport(
        self,
        *,
        observed_generation: int | None = None,
    ) -> bool:
        """Close a failed connector once and force a clean TCP/TLS session.

        Several concurrent Bot API calls may observe the same outage. The
        generation guard prevents each caller from repeatedly closing the fresh
        session created by another retrying caller.
        """

        async with self._transport_reset_lock:
            if (
                observed_generation is not None
                and observed_generation != self._transport_generation
            ):
                return False
            await super().close()
            self._should_reset_connector = True
            self._transport_generation += 1
            return True


class ResilientBot(Bot):
    """Centralized, method-aware Bot API retry policy.

    Long polling needs a long timeout. Interactive calls must fail over quickly;
    otherwise one broken TCP flow makes every Telegram button appear frozen for
    tens of seconds. The policies are intentionally separate.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        legacy_timeout = env_float(
            "TELEGRAM_REQUEST_TIMEOUT_SEC",
            3.0,
            minimum=0.5,
            maximum=120.0,
        )
        self._ui_request_timeout = env_float(
            "TELEGRAM_UI_REQUEST_TIMEOUT_SEC",
            legacy_timeout,
            minimum=0.5,
            maximum=20.0,
        )
        self._callback_request_timeout = env_float(
            "TELEGRAM_CALLBACK_TIMEOUT_SEC",
            1.0,
            minimum=0.25,
            maximum=5.0,
        )
        self._polling_request_timeout = env_float(
            "TELEGRAM_POLLING_REQUEST_TIMEOUT_SEC",
            55.0,
            minimum=10.0,
            maximum=120.0,
        )
        if kwargs.get("session") is None:
            proxy = (os.getenv("TELEGRAM_PROXY_URL") or "").strip() or None
            kwargs["session"] = PollingAiohttpSession(
                proxy=proxy,
                timeout=self._ui_request_timeout,
            )
        super().__init__(*args, **kwargs)

        # Compatibility attributes remain available to existing diagnostics.
        self._request_timeout = self._ui_request_timeout
        self._ui_network_retries = env_int(
            "TELEGRAM_UI_NETWORK_RETRIES",
            1,
            minimum=0,
            maximum=3,
        )
        self._polling_network_retries = env_int(
            "TELEGRAM_POLLING_NETWORK_RETRIES",
            2,
            minimum=0,
            maximum=10,
        )
        self._network_retries = self._ui_network_retries
        self._network_retry_delay = env_float(
            "TELEGRAM_NETWORK_RETRY_DELAY_SEC",
            0.15,
            minimum=0.0,
            maximum=5.0,
        )
        self._network_retry_max_delay = env_float(
            "TELEGRAM_NETWORK_RETRY_MAX_DELAY_SEC",
            1.0,
            minimum=0.0,
            maximum=10.0,
        )
        self._slow_ui_warning_seconds = env_float(
            "TELEGRAM_SLOW_UI_WARNING_SEC",
            1.0,
            minimum=0.1,
            maximum=30.0,
        )

    def request_policy(
        self,
        method_name: str,
        request_timeout: Any = None,
    ) -> tuple[Any, int, str]:
        """Return timeout, retry count and policy name for one Bot API method."""

        normalized = str(method_name or "").casefold()
        if normalized == _POLLING_METHOD:
            timeout = (
                request_timeout
                if request_timeout is not None
                else self._polling_request_timeout
            )
            return timeout, self._polling_network_retries, "polling"
        if normalized == _CALLBACK_METHOD:
            timeout = (
                request_timeout
                if request_timeout is not None
                else self._callback_request_timeout
            )
            # Callback acknowledgement must never delay the actual handler.
            return timeout, 0, "callback"
        timeout = (
            request_timeout
            if request_timeout is not None
            else self._ui_request_timeout
        )
        return timeout, self._ui_network_retries, "ui"

    async def _reset_failed_transport(self, observed_generation: int | None) -> None:
        session = self.session
        reset_transport = getattr(session, "reset_transport", None)
        try:
            if callable(reset_transport):
                await reset_transport(observed_generation=observed_generation)
            else:
                await session.close()
        except RuntimeError:
            log.warning("Telegram transport reset failed", exc_info=True)
        except OSError:
            log.warning("Telegram transport reset failed", exc_info=True)
        except AttributeError:
            log.warning("Telegram transport reset failed", exc_info=True)

    async def __call__(self, method: Any, request_timeout: Any = None) -> Any:
        method_name = str(
            getattr(method, "__api_method__", type(method).__name__)
        )[:120]
        timeout, retry_limit, policy_name = self.request_policy(
            method_name,
            request_timeout,
        )
        last_exc: Exception | None = None
        started = time.monotonic()

        try:
            for attempt in range(retry_limit + 1):
                observed_generation = getattr(
                    self.session,
                    "transport_generation",
                    None,
                )
                try:
                    return await super().__call__(method, request_timeout=timeout)
                except (TelegramNetworkError, asyncio.TimeoutError) as exc:
                    last_exc = exc
                    await self._reset_failed_transport(observed_generation)
                    if attempt >= retry_limit:
                        raise
                    delay = min(
                        self._network_retry_delay * (2**attempt),
                        self._network_retry_max_delay,
                    )
                    log.warning(
                        "Telegram network request failed; transport reset and retry scheduled",
                        extra={
                            "telegram_method": method_name,
                            "telegram_policy": policy_name,
                            "retry_attempt": attempt + 1,
                            "retry_limit": retry_limit,
                            "retry_delay_sec": delay,
                        },
                    )
                    await asyncio.sleep(delay)
        finally:
            elapsed = time.monotonic() - started
            if policy_name != "polling" and elapsed >= self._slow_ui_warning_seconds:
                log.warning(
                    "Slow Telegram UI request method=%s policy=%s elapsed_ms=%.1f",
                    method_name,
                    policy_name,
                    elapsed * 1000.0,
                )

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Unreachable resilient bot state")


def build_bot(token: str) -> ResilientBot:
    return ResilientBot(token=token)


__all__ = [
    "PollingAiohttpSession",
    "ResilientBot",
    "build_bot",
    "telegram_connector_options",
    "telegram_ip_family",
]
