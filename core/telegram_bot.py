from __future__ import annotations

import asyncio
import logging
import os
import socket
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
    """Build the pinned aiohttp connector policy for long polling.

    ``force_close`` intentionally prevents reuse of a silently expired TCP flow.
    The production incident repeated almost exactly one hour after each successful
    connection, which is consistent with an upstream/NAT flow being discarded
    while the client still holds a reusable connection.
    """

    return {
        "family": telegram_ip_family(),
        "ttl_dns_cache": env_int(
            "TELEGRAM_DNS_TTL_SEC",
            60,
            minimum=0,
            maximum=3600,
        ),
        "force_close": _env_bool("TELEGRAM_FORCE_CLOSE", True),
    }


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
    """Aiohttp transport hardened for long-running Telegram polling."""

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
    """Centralized Bot API retry policy for transient network failures."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        request_timeout = env_float(
            "TELEGRAM_REQUEST_TIMEOUT_SEC",
            20.0,
            minimum=1.0,
            maximum=120.0,
        )
        if kwargs.get("session") is None:
            proxy = (os.getenv("TELEGRAM_PROXY_URL") or "").strip() or None
            kwargs["session"] = PollingAiohttpSession(
                proxy=proxy,
                timeout=request_timeout,
            )
        super().__init__(*args, **kwargs)
        self._request_timeout = request_timeout
        self._network_retries = env_int(
            "TELEGRAM_NETWORK_RETRIES",
            2,
            minimum=0,
            maximum=10,
        )
        self._network_retry_delay = env_float(
            "TELEGRAM_NETWORK_RETRY_DELAY_SEC",
            0.75,
            minimum=0.0,
            maximum=30.0,
        )
        self._network_retry_max_delay = env_float(
            "TELEGRAM_NETWORK_RETRY_MAX_DELAY_SEC",
            8.0,
            minimum=0.0,
            maximum=60.0,
        )

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
        timeout = (
            request_timeout
            if request_timeout is not None
            else self._request_timeout
        )
        last_exc: Exception | None = None
        method_name = str(
            getattr(method, "__api_method__", type(method).__name__)
        )[:120]

        for attempt in range(self._network_retries + 1):
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
                if attempt >= self._network_retries:
                    raise
                delay = min(
                    self._network_retry_delay * (2**attempt),
                    self._network_retry_max_delay,
                )
                log.warning(
                    "Telegram network request failed; transport reset and retry scheduled",
                    extra={
                        "telegram_method": method_name,
                        "retry_attempt": attempt + 1,
                        "retry_limit": self._network_retries,
                        "retry_delay_sec": delay,
                    },
                )
                await asyncio.sleep(delay)

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
