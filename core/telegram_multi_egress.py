from __future__ import annotations

import asyncio
import ipaddress
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from aiogram.exceptions import TelegramNetworkError

from core.runtime_env import env_float
from core import telegram_bot as base


_BASE_RESILIENT_BOT = base.ResilientBot
_POLLING_METHOD = "getupdates"
_CALLBACK_METHOD = "answercallbackquery"


@dataclass(frozen=True, slots=True)
class TelegramEgressSnapshot:
    ui_mode: str
    polling_mode: str
    ui_route: str
    polling_route: str
    route_pool_size: int
    egress_redundant: bool
    polling_ready: bool
    polling_in_flight: bool
    polling_last_success_age_sec: float | None
    ui_last_success_age_sec: float | None
    ui_failures: int
    polling_failures: int


class _TransportState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.ui_mode = "unknown"
        self.polling_mode = "unknown"
        self.ui_route = "unknown"
        self.polling_route = "unknown"
        self.route_pool_size = 0
        self.egress_redundant = False
        self.last_polling_success: float | None = None
        self.polling_started_at: float | None = None
        self.polling_in_flight = False
        self.last_ui_success: float | None = None
        self.ui_failures = 0
        self.polling_failures = 0

    def configure(
        self,
        *,
        ui_mode: str,
        polling_mode: str,
        ui_route: str,
        polling_route: str,
        route_pool_size: int,
        egress_redundant: bool,
    ) -> None:
        with self._lock:
            self.ui_mode = ui_mode
            self.polling_mode = polling_mode
            self.ui_route = ui_route
            self.polling_route = polling_route
            self.route_pool_size = route_pool_size
            self.egress_redundant = egress_redundant

    def begin(self, policy: str, route: str) -> None:
        if policy != "polling":
            return
        with self._lock:
            self.polling_in_flight = True
            self.polling_started_at = time.monotonic()
            self.polling_route = route

    def success(self, policy: str, route: str) -> None:
        now = time.monotonic()
        with self._lock:
            if policy == "polling":
                self.last_polling_success = now
                self.polling_started_at = None
                self.polling_in_flight = False
                self.polling_route = route
            else:
                self.last_ui_success = now
                self.ui_route = route

    def failure(self, policy: str, route: str) -> None:
        with self._lock:
            if policy == "polling":
                self.polling_failures += 1
                self.polling_started_at = None
                self.polling_in_flight = False
                self.polling_route = route
            else:
                self.ui_failures += 1
                self.ui_route = route

    def snapshot(self) -> TelegramEgressSnapshot:
        now = time.monotonic()
        with self._lock:
            polling_age = (
                None
                if self.last_polling_success is None
                else max(0.0, now - self.last_polling_success)
            )
            ui_age = (
                None
                if self.last_ui_success is None
                else max(0.0, now - self.last_ui_success)
            )
            max_polling_age = env_float(
                "CLIENTPLATFORM_TELEGRAM_POLLING_READY_MAX_AGE_SEC",
                120.0,
                minimum=30.0,
                maximum=600.0,
            )
            max_in_flight_age = env_float(
                "CLIENTPLATFORM_TELEGRAM_POLLING_INFLIGHT_MAX_AGE_SEC",
                70.0,
                minimum=10.0,
                maximum=180.0,
            )
            in_flight_age = (
                None
                if self.polling_started_at is None
                else max(0.0, now - self.polling_started_at)
            )
            polling_in_flight = bool(
                self.polling_in_flight
                and in_flight_age is not None
                and in_flight_age <= max_in_flight_age
            )
            return TelegramEgressSnapshot(
                ui_mode=self.ui_mode,
                polling_mode=self.polling_mode,
                ui_route=self.ui_route,
                polling_route=self.polling_route,
                route_pool_size=self.route_pool_size,
                egress_redundant=self.egress_redundant,
                polling_ready=(
                    polling_in_flight
                    or (polling_age is not None and polling_age <= max_polling_age)
                ),
                polling_in_flight=polling_in_flight,
                polling_last_success_age_sec=(
                    None if polling_age is None else round(polling_age, 3)
                ),
                ui_last_success_age_sec=None if ui_age is None else round(ui_age, 3),
                ui_failures=self.ui_failures,
                polling_failures=self.polling_failures,
            )


_STATE = _TransportState()


def _proxy(name: str, fallback: str | None) -> str | None:
    raw = (os.getenv(name) or "").strip()
    return raw or fallback


def _lane_routes(name: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return fallback
    values = raw.replace(";", ",").split(",")
    routes: list[str] = []
    seen: set[str] = set()
    for value in values:
        candidate = value.strip()
        if not candidate:
            continue
        try:
            normalized = str(ipaddress.IPv4Address(candidate))
        except ipaddress.AddressValueError:
            continue
        if normalized not in seen:
            seen.add(normalized)
            routes.append(normalized)
    return tuple(routes) or fallback


def _proxy_descriptor(value: str | None) -> str:
    return "proxy" if value else "direct"


def _redundant(
    *,
    ui_proxy: str | None,
    polling_proxy: str | None,
    ui_routes: tuple[str, ...],
    polling_routes: tuple[str, ...],
    ui_route: str,
    polling_route: str,
) -> bool:
    if ui_proxy and polling_proxy:
        return ui_proxy != polling_proxy
    if bool(ui_proxy) != bool(polling_proxy):
        return True
    if ui_proxy or polling_proxy:
        return False
    if ui_route != polling_route:
        return True
    return len(set(ui_routes) | set(polling_routes)) >= 2


class MultiEgressResilientBot(_BASE_RESILIENT_BOT):
    """ResilientBot with independently configurable UI and polling egress."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        supplied_session = kwargs.get("session")
        if supplied_session is not None:
            super().__init__(*args, **kwargs)
            _STATE.configure(
                ui_mode="supplied",
                polling_mode="supplied",
                ui_route=str(getattr(self.session, "active_route", "system")),
                polling_route=str(
                    getattr(self.polling_session, "active_route", "system")
                ),
                route_pool_size=int(getattr(self.session, "route_count", 0) or 0),
                egress_redundant=self.session is not self.polling_session,
            )
            return

        common_proxy = (os.getenv("TELEGRAM_PROXY_URL") or "").strip() or None
        ui_proxy = _proxy("TELEGRAM_UI_PROXY_URL", common_proxy)
        polling_proxy = _proxy("TELEGRAM_POLLING_PROXY_URL", common_proxy)
        global_routes = base.telegram_route_pool()
        ui_routes = _lane_routes(
            "CLIENTPLATFORM_TELEGRAM_UI_IPV4_POOL",
            global_routes,
        )
        polling_routes = _lane_routes(
            "CLIENTPLATFORM_TELEGRAM_POLLING_IPV4_POOL",
            global_routes,
        )
        ui_timeout = env_float(
            "TELEGRAM_UI_REQUEST_TIMEOUT_SEC",
            2.0,
            minimum=0.5,
            maximum=20.0,
        )
        polling_timeout = env_float(
            "TELEGRAM_POLLING_REQUEST_TIMEOUT_SEC",
            55.0,
            minimum=10.0,
            maximum=120.0,
        )

        ui_session = base.PollingAiohttpSession(
            proxy=ui_proxy,
            timeout=ui_timeout,
            route_role="ui",
            route_offset=0,
            route_pool=ui_routes,
        )
        kwargs["session"] = ui_session
        super().__init__(*args, **kwargs)

        same_direct_pool = (
            ui_proxy is None
            and polling_proxy is None
            and ui_routes == polling_routes
        )
        polling_session = base.PollingAiohttpSession(
            proxy=polling_proxy,
            timeout=polling_timeout,
            route_role="polling",
            route_offset=1 if same_direct_pool and len(polling_routes) > 1 else 0,
            route_pool=polling_routes,
        )
        self._polling_session = polling_session
        ui_session.attach_companion(polling_session)

        ui_route = ui_session.active_route
        polling_route = polling_session.active_route
        _STATE.configure(
            ui_mode=_proxy_descriptor(ui_proxy),
            polling_mode=_proxy_descriptor(polling_proxy),
            ui_route=ui_route,
            polling_route=polling_route,
            route_pool_size=len(set(ui_routes) | set(polling_routes)),
            egress_redundant=_redundant(
                ui_proxy=ui_proxy,
                polling_proxy=polling_proxy,
                ui_routes=ui_routes,
                polling_routes=polling_routes,
                ui_route=ui_route,
                polling_route=polling_route,
            ),
        )

    async def __call__(self, method: Any, request_timeout: Any = None) -> Any:
        method_name = str(
            getattr(method, "__api_method__", type(method).__name__)
        ).casefold()
        policy = (
            "polling"
            if method_name == _POLLING_METHOD
            else "callback"
            if method_name == _CALLBACK_METHOD
            else "ui"
        )
        session = self.session_for_policy(policy)
        _STATE.begin(
            policy,
            str(getattr(session, "active_route", "unknown")),
        )
        try:
            result = await super().__call__(
                method,
                request_timeout=request_timeout,
            )
        except TelegramNetworkError:
            _STATE.failure(
                policy,
                str(getattr(session, "active_route", "unknown")),
            )
            raise
        except asyncio.TimeoutError:
            _STATE.failure(
                policy,
                str(getattr(session, "active_route", "unknown")),
            )
            raise
        _STATE.success(
            policy,
            str(getattr(session, "active_route", "unknown")),
        )
        return result


def install_multi_egress_bot() -> None:
    """Make the already-imported build_bot factory construct MultiEgressResilientBot."""

    if base.ResilientBot is MultiEgressResilientBot:
        return
    base.ResilientBot = MultiEgressResilientBot


def telegram_egress_snapshot() -> TelegramEgressSnapshot:
    return _STATE.snapshot()


def telegram_readiness_required() -> bool:
    raw = (
        os.getenv("CLIENTPLATFORM_REQUIRE_TELEGRAM_POLLING_READY")
        or "false"
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def telegram_redundancy_required() -> bool:
    raw = (
        os.getenv("CLIENTPLATFORM_REQUIRE_REDUNDANT_TELEGRAM_EGRESS")
        or "false"
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


__all__ = [
    "MultiEgressResilientBot",
    "TelegramEgressSnapshot",
    "install_multi_egress_bot",
    "telegram_egress_snapshot",
    "telegram_readiness_required",
    "telegram_redundancy_required",
]
