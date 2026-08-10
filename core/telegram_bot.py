from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import os
import socket
import time
from collections import deque
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientSession
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.hdrs import USER_AGENT
from aiohttp.http import SERVER_SOFTWARE
from aiohttp.resolver import ThreadedResolver
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
_TELEGRAM_API_HOST = "api.telegram.org"
_ROUTE_POOL_ENV_NAMES = (
    "CLIENTPLATFORM_TELEGRAM_API_IPV4_POOL",
    "TELEGRAM_API_IPV4_POOL",
    "CLIENTPLATFORM_TELEGRAM_API_IPV4",
)
_LATENCY_SAMPLE_LIMIT = 256


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
    """Return the explicit socket family used by Telegram polling."""

    raw = (os.getenv("TELEGRAM_IP_FAMILY") or "ipv4").strip().lower()
    if raw in {"ipv4", "4", "inet"}:
        return socket.AF_INET
    if raw in {"ipv6", "6", "inet6"}:
        return socket.AF_INET6
    if raw in {"auto", "any", "unspecified"}:
        return socket.AF_UNSPEC
    log.warning("Invalid TELEGRAM_IP_FAMILY; forcing IPv4")
    return socket.AF_INET


def telegram_route_pool() -> tuple[str, ...]:
    """Return a validated, ordered and duplicate-free Telegram IPv4 pool."""

    raw_values: list[str] = []
    for name in _ROUTE_POOL_ENV_NAMES:
        raw = (os.getenv(name) or "").strip()
        if raw:
            raw_values.extend(raw.replace(";", ",").split(","))

    routes: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        candidate = raw.strip()
        if not candidate:
            continue
        try:
            normalized = str(ipaddress.IPv4Address(candidate))
        except ipaddress.AddressValueError:
            log.warning("Ignoring invalid Telegram route address from environment")
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        routes.append(normalized)
    return tuple(routes)


class TelegramRouteResolver(AbstractResolver):
    """Resolve api.telegram.org through an independently rotatable IPv4 pool."""

    def __init__(
        self,
        routes: tuple[str, ...],
        *,
        start_index: int = 0,
    ) -> None:
        self._routes = routes
        self._index = start_index % len(routes) if routes else 0
        self._fallback = ThreadedResolver()

    @property
    def routes(self) -> tuple[str, ...]:
        return self._routes

    @property
    def active_route(self) -> str | None:
        if not self._routes:
            return None
        return self._routes[self._index]

    @property
    def route_count(self) -> int:
        return len(self._routes)

    def rotate(self) -> bool:
        if len(self._routes) < 2:
            return False
        self._index = (self._index + 1) % len(self._routes)
        return True

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        route = self.active_route
        if host.casefold() != _TELEGRAM_API_HOST or route is None:
            return await self._fallback.resolve(host, port, family)
        return [
            ResolveResult(
                hostname=host,
                host=route,
                port=port,
                family=socket.AF_INET,
                proto=socket.IPPROTO_TCP,
                flags=0,
            )
        ]

    async def close(self) -> None:
        await self._fallback.close()


def telegram_connector_options(
    *,
    resolver: AbstractResolver | None = None,
) -> dict[str, Any]:
    """Build a short-lived keep-alive connector policy for Telegram."""

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
    if resolver is not None:
        options["resolver"] = resolver
    if not force_close:
        options["keepalive_timeout"] = env_float(
            "TELEGRAM_KEEPALIVE_TIMEOUT_SEC",
            20.0,
            minimum=1.0,
            maximum=120.0,
        )
    return options


def _native_http_proxy(proxy: Any) -> str | None:
    """Validate a native aiohttp HTTP CONNECT proxy without exposing secrets."""

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
    """One independently resettable Telegram transport lane."""

    _session: ClientSession | None
    _should_reset_connector: bool

    def __init__(
        self,
        *,
        proxy: str | None = None,
        limit: int = 100,
        route_role: str = "ui",
        route_offset: int = 0,
        route_pool: tuple[str, ...] | None = None,
        **kwargs: Any,
    ) -> None:
        native_http_proxy = _native_http_proxy(proxy)
        super().__init__(
            proxy=None if native_http_proxy is not None else proxy,
            limit=limit,
            **kwargs,
        )
        routes = telegram_route_pool() if route_pool is None else route_pool
        self._route_resolver = (
            TelegramRouteResolver(routes, start_index=route_offset)
            if routes and proxy is None
            else None
        )
        self._connector_init.update(
            telegram_connector_options(resolver=self._route_resolver)
        )
        self._native_http_proxy = native_http_proxy
        self._transport_generation = 0
        self._transport_reset_lock = asyncio.Lock()
        self._transport_role = route_role
        self._companion_sessions: list[PollingAiohttpSession] = []

    @property
    def transport_generation(self) -> int:
        return self._transport_generation

    @property
    def transport_role(self) -> str:
        return self._transport_role

    @property
    def connector_options(self) -> dict[str, Any]:
        return dict(self._connector_init)

    @property
    def proxy_mode(self) -> str:
        if self._native_http_proxy is not None:
            return "http_connect"
        if self.proxy is not None:
            return "connector_proxy"
        return "direct"

    @property
    def active_route(self) -> str:
        route = (
            self._route_resolver.active_route
            if self._route_resolver is not None
            else None
        )
        return route or "system"

    @property
    def route_count(self) -> int:
        if self._route_resolver is None:
            return 0
        return self._route_resolver.route_count

    def attach_companion(self, session: PollingAiohttpSession) -> None:
        if session is self or session in self._companion_sessions:
            return
        self._companion_sessions.append(session)

    async def create_session(self) -> ClientSession:
        # Session construction and transport reset share one lifecycle lock.
        # Without it, a request can install a new ClientSession while reset is
        # awaiting close(), after which the reset path marks that fresh connector
        # stale and the next request interrupts the one already using it.
        async with self._transport_reset_lock:
            if self._should_reset_connector:
                # Bypass this class' close() here: connector recreation for one
                # lane must not close its independently owned companion lane.
                await super().close()
            if self._session is None or self._session.closed:
                session_kwargs: dict[str, Any] = {
                    "connector": self._connector_type(**self._connector_init),
                    "headers": {
                        USER_AGENT: f"{SERVER_SOFTWARE} aiogram/{__version__}",
                    },
                }
                if self._native_http_proxy is not None:
                    session_kwargs["proxy"] = self._native_http_proxy
                self._session = ClientSession(**session_kwargs)
                self._should_reset_connector = False
            return self._session

    async def reset_transport(
        self,
        *,
        observed_generation: int | None = None,
        rotate_route: bool = True,
    ) -> bool:
        """Close one failed connector generation and rotate its route once."""

        async with self._transport_reset_lock:
            if (
                observed_generation is not None
                and observed_generation != self._transport_generation
            ):
                return False
            previous_route = self.active_route
            rotated = False
            if rotate_route and self._route_resolver is not None:
                rotated = self._route_resolver.rotate()
            # Publish reset intent before the first await. Even callers that only
            # inspect the inherited reset flag can never observe an in-progress
            # close as a healthy connector generation.
            self._should_reset_connector = True
            await super().close()
            self._transport_generation += 1
            log.warning(
                "Telegram transport reset role=%s generation=%s route=%s next_route=%s rotated=%s",
                self._transport_role,
                self._transport_generation,
                previous_route,
                self.active_route,
                rotated,
            )
            return True

    async def close(self) -> None:
        # Companion ownership belongs to this lane for the lifetime of the
        # reusable session object. Aiogram may reopen a closed ClientSession on
        # a later polling retry, so dropping this association on the first close
        # would leak that reopened polling connector during the next shutdown.
        companions = tuple(self._companion_sessions)
        await super().close()
        for companion in companions:
            await companion.close()


class ResilientBot(Bot):
    """Method-aware Bot API policy with isolated polling and UI transports."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        legacy_timeout = env_float(
            "TELEGRAM_REQUEST_TIMEOUT_SEC",
            20.0,
            minimum=1.0,
            maximum=120.0,
        )
        legacy_retries = env_int(
            "TELEGRAM_NETWORK_RETRIES",
            2,
            minimum=0,
            maximum=10,
        )
        legacy_retry_delay = env_float(
            "TELEGRAM_NETWORK_RETRY_DELAY_SEC",
            0.75,
            minimum=0.0,
            maximum=30.0,
        )

        self._ui_request_timeout = env_float(
            "TELEGRAM_UI_REQUEST_TIMEOUT_SEC",
            2.0,
            minimum=0.5,
            maximum=20.0,
        )
        self._callback_request_timeout = env_float(
            "TELEGRAM_CALLBACK_TIMEOUT_SEC",
            0.75,
            minimum=0.25,
            maximum=5.0,
        )
        self._polling_request_timeout = env_float(
            "TELEGRAM_POLLING_REQUEST_TIMEOUT_SEC",
            55.0,
            minimum=10.0,
            maximum=120.0,
        )

        supplied_session = kwargs.get("session")
        polling_session: PollingAiohttpSession | Any
        if supplied_session is None:
            proxy = (os.getenv("TELEGRAM_PROXY_URL") or "").strip() or None
            routes = telegram_route_pool()
            ui_session = PollingAiohttpSession(
                proxy=proxy,
                timeout=self._ui_request_timeout,
                route_role="ui",
                route_offset=0,
                route_pool=routes,
            )
            polling_session = PollingAiohttpSession(
                proxy=proxy,
                timeout=self._polling_request_timeout,
                route_role="polling",
                route_offset=1 if len(routes) > 1 else 0,
                route_pool=routes,
            )
            ui_session.attach_companion(polling_session)
            kwargs["session"] = ui_session
        else:
            polling_session = supplied_session

        super().__init__(*args, **kwargs)
        self._polling_session = polling_session

        self._request_timeout = legacy_timeout
        self._network_retries = legacy_retries
        self._network_retry_delay = legacy_retry_delay

        old_retries_explicit = os.getenv("TELEGRAM_NETWORK_RETRIES") not in (
            None,
            "",
        )
        old_delay_explicit = os.getenv("TELEGRAM_NETWORK_RETRY_DELAY_SEC") not in (
            None,
            "",
        )
        ui_retries_default = legacy_retries if old_retries_explicit else 1
        ui_retry_delay_default = legacy_retry_delay if old_delay_explicit else 0.1

        self._ui_network_retries = env_int(
            "TELEGRAM_UI_NETWORK_RETRIES",
            ui_retries_default,
            minimum=0,
            maximum=3,
        )
        self._callback_network_retries = env_int(
            "TELEGRAM_CALLBACK_NETWORK_RETRIES",
            1,
            minimum=0,
            maximum=1,
        )
        self._polling_network_retries = env_int(
            "TELEGRAM_POLLING_NETWORK_RETRIES",
            2,
            minimum=0,
            maximum=10,
        )
        self._ui_network_retry_delay = env_float(
            "TELEGRAM_UI_NETWORK_RETRY_DELAY_SEC",
            ui_retry_delay_default,
            minimum=0.0,
            maximum=5.0,
        )
        self._callback_network_retry_delay = env_float(
            "TELEGRAM_CALLBACK_NETWORK_RETRY_DELAY_SEC",
            0.0,
            minimum=0.0,
            maximum=1.0,
        )
        self._polling_network_retry_delay = env_float(
            "TELEGRAM_POLLING_NETWORK_RETRY_DELAY_SEC",
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
        self._slow_ui_warning_seconds = env_float(
            "TELEGRAM_SLOW_UI_WARNING_SEC",
            1.0,
            minimum=0.1,
            maximum=30.0,
        )
        self._latency_samples: deque[dict[str, Any]] = deque(
            maxlen=_LATENCY_SAMPLE_LIMIT
        )

    @property
    def polling_session(self) -> Any:
        return self._polling_session

    def request_policy(
        self,
        method_name: str,
        request_timeout: Any = None,
    ) -> tuple[Any, int, str]:
        normalized = str(method_name or "").casefold()
        if normalized == _POLLING_METHOD:
            if request_timeout is None:
                timeout = self._polling_request_timeout
            elif isinstance(request_timeout, (int, float)):
                # aiogram derives getUpdates request_timeout from bot.session.timeout
                # + polling_timeout. ClientPlatform intentionally keeps bot.session
                # on the short UI lane, so that derived value can be far below the
                # separately configured polling transport budget. Enforce the
                # configured polling value as a floor while preserving an
                # intentionally longer caller timeout.
                timeout = max(float(request_timeout), self._polling_request_timeout)
            else:
                timeout = request_timeout
            return timeout, self._polling_network_retries, "polling"
        if normalized == _CALLBACK_METHOD:
            timeout = (
                request_timeout
                if request_timeout is not None
                else self._callback_request_timeout
            )
            return timeout, self._callback_network_retries, "callback"
        timeout = (
            request_timeout
            if request_timeout is not None
            else self._ui_request_timeout
        )
        return timeout, self._ui_network_retries, "ui"

    def retry_delay(self, policy_name: str, attempt: int) -> float:
        if policy_name == "polling":
            base = self._polling_network_retry_delay
        elif policy_name == "callback":
            base = self._callback_network_retry_delay
        else:
            base = self._ui_network_retry_delay
        return min(base * (2**attempt), self._network_retry_max_delay)

    def session_for_policy(self, policy_name: str) -> Any:
        if policy_name == "polling":
            return self._polling_session
        return self.session

    async def _reset_failed_transport(
        self,
        session: Any,
        observed_generation: int | None,
    ) -> None:
        reset_transport = getattr(session, "reset_transport", None)
        try:
            if callable(reset_transport):
                await reset_transport(
                    observed_generation=observed_generation,
                    rotate_route=True,
                )
            else:
                await session.close()
        except RuntimeError:
            log.warning("Telegram transport reset failed", exc_info=True)
        except OSError:
            log.warning("Telegram transport reset failed", exc_info=True)
        except AttributeError:
            log.warning("Telegram transport reset failed", exc_info=True)

    def latency_snapshot(self, *, policy: str | None = None) -> dict[str, Any]:
        samples = [
            sample
            for sample in self._latency_samples
            if policy is None or sample["policy"] == policy
        ]
        elapsed = sorted(float(sample["elapsed_ms"]) for sample in samples)
        if not elapsed:
            return {
                "count": 0,
                "successes": 0,
                "failures": 0,
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "max_ms": 0.0,
            }

        def percentile(fraction: float) -> float:
            index = max(0, math.ceil(len(elapsed) * fraction) - 1)
            return round(elapsed[index], 1)

        successes = sum(1 for sample in samples if sample["success"])
        return {
            "count": len(samples),
            "successes": successes,
            "failures": len(samples) - successes,
            "p50_ms": percentile(0.50),
            "p95_ms": percentile(0.95),
            "max_ms": round(elapsed[-1], 1),
        }

    async def __call__(self, method: Any, request_timeout: Any = None) -> Any:
        method_name = str(
            getattr(method, "__api_method__", type(method).__name__)
        )[:120]
        timeout, retry_limit, policy_name = self.request_policy(
            method_name,
            request_timeout,
        )
        session = self.session_for_policy(policy_name)
        last_exc: Exception | None = None
        started = time.monotonic()
        success = False

        try:
            for attempt in range(retry_limit + 1):
                observed_generation = getattr(
                    session,
                    "transport_generation",
                    None,
                )
                try:
                    if policy_name == "polling" and session is not self.session:
                        result = await session(self, method, timeout=timeout)
                    else:
                        result = await super().__call__(
                            method,
                            request_timeout=timeout,
                        )
                    success = True
                    return result
                except (TelegramNetworkError, asyncio.TimeoutError) as exc:
                    last_exc = exc
                    await self._reset_failed_transport(
                        session,
                        observed_generation,
                    )
                    if attempt >= retry_limit:
                        raise
                    delay = self.retry_delay(policy_name, attempt)
                    log.warning(
                        "Telegram network request failed; route failover and retry scheduled",
                        extra={
                            "telegram_method": method_name,
                            "telegram_policy": policy_name,
                            "telegram_transport_role": getattr(
                                session,
                                "transport_role",
                                policy_name,
                            ),
                            "telegram_route": getattr(
                                session,
                                "active_route",
                                "unknown",
                            ),
                            "retry_attempt": attempt + 1,
                            "retry_limit": retry_limit,
                            "retry_delay_sec": delay,
                        },
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
        finally:
            elapsed = time.monotonic() - started
            route = getattr(session, "active_route", "unknown")
            generation = getattr(session, "transport_generation", None)
            if policy_name != "polling":
                self._latency_samples.append(
                    {
                        "method": method_name,
                        "policy": policy_name,
                        "elapsed_ms": round(elapsed * 1000.0, 1),
                        "success": success,
                        "route": route,
                        "generation": generation,
                    }
                )
                if elapsed >= self._slow_ui_warning_seconds:
                    log.warning(
                        "Slow Telegram UI request method=%s policy=%s elapsed_ms=%.1f route=%s generation=%s",
                        method_name,
                        policy_name,
                        elapsed * 1000.0,
                        route,
                        generation,
                    )

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Unreachable resilient bot state")


def build_bot(token: str) -> ResilientBot:
    return ResilientBot(token=token)


__all__ = [
    "PollingAiohttpSession",
    "ResilientBot",
    "TelegramRouteResolver",
    "build_bot",
    "telegram_connector_options",
    "telegram_ip_family",
    "telegram_route_pool",
]