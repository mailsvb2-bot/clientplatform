from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from aiogram.exceptions import TelegramAPIError

from config.settings import ADMIN_IDS
from core.runtime_env import env_int
from core.task_manager import TaskManager
from services.db import get_db, get_db_ro
from services.platform_resource_limits import (
    crossed_thresholds,
    current_levels,
    get_platform_resource_snapshot,
    render_threshold_notification,
)
from services.visual_provider_health import (
    VisualProviderHealthSnapshot,
    get_visual_provider_health_snapshot,
)
from services.yandex_billing_health import (
    YandexBillingSnapshot,
    crossed_balance_threshold,
    get_yandex_billing_snapshot,
)


log = logging.getLogger(__name__)
_STATE_KEY = "clientplatform:platform_resource_monitor:visual_gateway"
_OPERATOR_ALERT_CHAT_IDS_ENV = "CLIENTPLATFORM_RESOURCE_ALERT_CHAT_IDS"
_task_manager = TaskManager()
_task: asyncio.Task[None] | None = None
_last_tick_monotonic: float | None = None
_last_error = ""


def _interval_seconds() -> int:
    return env_int(
        "CLIENTPLATFORM_RESOURCE_MONITOR_INTERVAL_SEC",
        60,
        minimum=60,
        maximum=3600,
    )


def _superadmin_ids() -> tuple[int, ...]:
    return tuple(sorted({int(value) for value in ADMIN_IDS or []}))


def _resource_alert_chat_ids() -> tuple[int, ...]:
    """Return explicit operator chats for forced infrastructure telemetry alerts.

    Resource telemetry failures are operational events, not product messages. They
    must never fall back to ``ADMIN_IDS`` because an administrator can use the same
    private chat as an ordinary ClientPlatform customer. Positive private chat IDs
    and negative Telegram group/channel IDs are both valid destinations.
    """

    raw = str(os.getenv(_OPERATOR_ALERT_CHAT_IDS_ENV) or "")
    targets: set[int] = set()
    for item in raw.split(","):
        normalized = item.strip()
        if not normalized:
            continue
        try:
            chat_id = int(normalized)
        except ValueError:
            continue
        if chat_id != 0:
            targets.add(chat_id)
    return tuple(sorted(targets))


def _load_state() -> dict[str, Any]:
    with get_db_ro() as conn:
        row = conn.execute(
            "SELECT value FROM engine_state WHERE key=? LIMIT 1",
            (_STATE_KEY,),
        ).fetchone()
    if row is None:
        return {}
    raw = row["value"] if hasattr(row, "keys") else row[0]
    try:
        value = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _save_state(value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO engine_state(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (_STATE_KEY, payload, int(time.time())),
        )


def _recipient_ids(values: Iterable[object]) -> tuple[int, ...]:
    configured = set(_superadmin_ids())
    recipients: set[int] = set()
    for value in values:
        try:
            candidate = int(value)
        except (TypeError, ValueError):
            continue
        if candidate in configured:
            recipients.add(candidate)
    return tuple(sorted(recipients))


def _resource_alert_recipient_ids(values: Iterable[object]) -> tuple[int, ...]:
    configured = set(_resource_alert_chat_ids())
    recipients: set[int] = set()
    for value in values:
        try:
            candidate = int(value)
        except (TypeError, ValueError):
            continue
        if candidate in configured:
            recipients.add(candidate)
    return tuple(sorted(recipients))


async def _send_superadmins(
    bot: Any,
    text: str,
    *,
    recipient_ids: Iterable[object] | None = None,
) -> tuple[set[int], set[int]]:
    targets = _superadmin_ids() if recipient_ids is None else _recipient_ids(recipient_ids)
    delivered: set[int] = set()
    failed: set[int] = set()
    for admin_id in targets:
        try:
            await bot.send_message(admin_id, text)
        except TelegramAPIError:
            log.warning(
                "Failed to send platform resource alert to superadmin=%s",
                admin_id,
                exc_info=True,
            )
            failed.add(admin_id)
            continue
        except asyncio.TimeoutError:
            log.warning(
                "Timed out sending platform resource alert to superadmin=%s",
                admin_id,
                exc_info=True,
            )
            failed.add(admin_id)
            continue
        delivered.add(admin_id)
    return delivered, failed


async def _send_resource_operators(
    bot: Any,
    text: str,
    *,
    recipient_ids: Iterable[object] | None = None,
) -> tuple[set[int], set[int]]:
    targets = (
        _resource_alert_chat_ids()
        if recipient_ids is None
        else _resource_alert_recipient_ids(recipient_ids)
    )
    delivered: set[int] = set()
    failed: set[int] = set()
    for chat_id in targets:
        try:
            await bot.send_message(chat_id, text)
        except TelegramAPIError:
            log.warning(
                "Failed to send platform resource telemetry alert to operator chat=%s",
                chat_id,
                exc_info=True,
            )
            failed.add(chat_id)
            continue
        except asyncio.TimeoutError:
            log.warning(
                "Timed out sending platform resource telemetry alert to operator chat=%s",
                chat_id,
                exc_info=True,
            )
            failed.add(chat_id)
            continue
        delivered.add(chat_id)
    return delivered, failed


def _telemetry_warning(error_code: str) -> str:
    return (
        "⚠️ ClientPlatform: контроль лимитов Visual Creative недоступен\n\n"
        f"Причина: {error_code or 'unknown'}\n\n"
        "Что делать:\n"
        "1. Проверить, что контейнер visual-creative-gateway запущен.\n"
        "2. Проверить защищённый endpoint /v1/usage.\n"
        "3. Пока телеметрия не восстановлена, сверять расход и квоты в Yandex Cloud вручную."
    )


async def _deliver_telemetry_warning(
    bot: Any,
    *,
    state: dict[str, Any],
    today: str,
    error_code: str,
) -> None:
    pending = state.get("telemetry_pending")
    same_pending = (
        isinstance(pending, dict)
        and str(pending.get("day") or "") == today
        and str(pending.get("error") or "") == error_code
    )
    if same_pending:
        message = str(pending.get("message") or _telemetry_warning(error_code))
        recipients = _resource_alert_recipient_ids(pending.get("pending_chat_ids") or [])
    else:
        already_reported = (
            str(state.get("telemetry_day") or "") == today
            and str(state.get("telemetry_error") or "") == error_code
        )
        if already_reported:
            return
        message = _telemetry_warning(error_code)
        recipients = _resource_alert_chat_ids()
        log.warning(
            "Visual Creative resource telemetry unavailable code=%s operator_alert_chat_count=%s",
            error_code,
            len(recipients),
        )

    if recipients:
        _delivered, failed = await _send_resource_operators(
            bot,
            message,
            recipient_ids=recipients,
        )
        if failed:
            state["telemetry_pending"] = {
                "day": today,
                "error": error_code,
                "message": message,
                "pending_chat_ids": sorted(failed),
            }
            await asyncio.to_thread(_save_state, state)
            return

    # Do not retry legacy pending_admin_ids after upgrade: forced infrastructure
    # telemetry is no longer allowed to fall back to personal ADMIN_IDS chats.
    state.pop("telemetry_pending", None)
    state["telemetry_day"] = today
    state["telemetry_error"] = error_code
    await asyncio.to_thread(_save_state, state)


async def _finish_pending_threshold(
    bot: Any,
    *,
    state: dict[str, Any],
) -> bool:
    pending = state.get("threshold_pending")
    if not isinstance(pending, dict):
        return True

    recipients = _recipient_ids(pending.get("pending_admin_ids") or [])
    if recipients:
        message = str(pending.get("message") or "").strip()
        if not message:
            state.pop("threshold_pending", None)
            return True
        _delivered, failed = await _send_superadmins(
            bot,
            message,
            recipient_ids=recipients,
        )
        if failed:
            pending["pending_admin_ids"] = sorted(failed)
            state["threshold_pending"] = pending
            await asyncio.to_thread(_save_state, state)
            return False

    target_levels = pending.get("target_levels")
    if isinstance(target_levels, dict):
        state["levels"] = {
            str(key): int(value)
            for key, value in target_levels.items()
            if str(value).lstrip("-").isdigit()
        }
    state.pop("threshold_pending", None)
    await asyncio.to_thread(_save_state, state)
    return True


def _lifecycle_level(days_remaining: object, status: object) -> int:
    if str(status or "") == "deprecated":
        return 5
    try:
        days = int(days_remaining)
    except (TypeError, ValueError):
        return 0
    if days <= 1:
        return 4
    if days <= 7:
        return 3
    if days <= 14:
        return 2
    if days <= 30:
        return 1
    return 0


def _provider_state_and_alerts(
    snapshot: VisualProviderHealthSnapshot,
    previous: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    prev = previous if isinstance(previous, dict) else {}
    alerts: list[str] = []
    current: dict[str, Any] = {
        "available": snapshot.available,
        "configured_image": list(snapshot.configured_image),
        "configured_video": list(snapshot.configured_video),
        "models": {},
        "runtime": {},
        "circuits": {},
    }

    if not snapshot.available:
        current["error_code"] = snapshot.error_code
        if bool(prev.get("available", True)):
            alerts.append(
                "🔴 Visual Provider Gateway недоступен\n"
                f"Причина: {snapshot.error_code or 'unknown'}\n"
                "Генерация должна считаться деградированной до восстановления."
            )
        return current, alerts

    if prev and not bool(prev.get("available", True)):
        alerts.append("🟢 Visual Provider Gateway восстановился.")

    for kind, providers in (
        ("изображений", snapshot.configured_image),
        ("видео", snapshot.configured_video),
    ):
        previous_values = prev.get(
            "configured_image" if kind == "изображений" else "configured_video"
        )
        if not providers and previous_values != []:
            alerts.append(
                f"🔴 Не осталось настроенных провайдеров для {kind}. "
                "Пользовательская генерация этого типа недоступна."
            )
        elif providers and previous_values == []:
            alerts.append(
                f"🟢 Провайдеры для {kind} снова доступны: {', '.join(providers)}."
            )

    prev_models = prev.get("models") if isinstance(prev.get("models"), dict) else {}
    for provider, raw in snapshot.models.items():
        if not isinstance(raw, dict):
            continue
        model = str(raw.get("model") or "")
        model_id = str(raw.get("model_id") or model)
        lifecycle = _lifecycle_level(raw.get("days_remaining"), raw.get("status"))
        available_art_models_raw = raw.get("available_art_models")
        available_art_models = (
            tuple(
                str(item)
                for item in available_art_models_raw
                if str(item).strip()
            )
            if isinstance(available_art_models_raw, (list, tuple))
            else ()
        )
        catalog_available = bool(raw.get("catalog_available"))
        configured_model_present = bool(raw.get("configured_model_present"))
        catalog_error = str(raw.get("catalog_error") or "")
        current["models"][provider] = {
            "model": model,
            "model_id": model_id,
            "lifecycle_level": lifecycle,
            "deprecated_at": str(raw.get("deprecated_at") or ""),
            "catalog_available": catalog_available,
            "configured_model_present": configured_model_present,
            "catalog_error": catalog_error,
            "available_art_models": list(available_art_models),
        }
        previous_model = prev_models.get(provider)
        if catalog_available:
            previous_catalog_available = (
                bool(previous_model.get("catalog_available"))
                if isinstance(previous_model, dict)
                else False
            )
            previous_models = (
                set(str(item) for item in previous_model.get("available_art_models", []))
                if isinstance(previous_model, dict)
                and isinstance(previous_model.get("available_art_models"), list)
                else set()
            )
            discovered = [
                item
                for item in available_art_models
                if item not in previous_models
            ]
            if (
                isinstance(previous_model, dict)
                and not previous_catalog_available
            ):
                alerts.append(
                    f"🟢 Каталог моделей {provider} снова доступен."
                )
            if discovered and previous_models:
                alerts.append(
                    "🆕 Yandex AI Studio: появились новые image-модели\n"
                    + "\n".join(discovered[:5])
                    + (
                        f"\n… и ещё {len(discovered) - 5}"
                        if len(discovered) > 5
                        else ""
                    )
                    + "\nНовая модель не включается автоматически, пока не разрешена политикой."
                )
            previously_missing = (
                isinstance(previous_model, dict)
                and str(previous_model.get("model") or "") == model
                and previous_model.get("configured_model_present") is False
            )
            if model and not configured_model_present and not previously_missing:
                alerts.append(
                    "🔴 Текущая image-модель отсутствует в каталоге Yandex AI Studio\n"
                    f"{provider}: {model}\n"
                    + (
                        "Доступные art-модели:\n"
                        + "\n".join(available_art_models[:5])
                        if available_art_models
                        else "Доступных art-моделей каталог не вернул."
                    )
                )
        elif catalog_error:
            previous_error = (
                str(previous_model.get("catalog_error") or "")
                if isinstance(previous_model, dict)
                else ""
            )
            if previous_error != catalog_error:
                alerts.append(
                    "🟠 Каталог моделей Yandex AI Studio недоступен\n"
                    f"Причина: {catalog_error}\n"
                    "Проверка появления/исчезновения моделей временно невозможна."
                )

        if isinstance(previous_model, dict):
            old_model = str(previous_model.get("model") or "")
            if old_model and model and old_model != model:
                alerts.append(
                    "🔄 Модель генерации автоматически/операторски изменена\n"
                    f"{provider}: {old_model} → {model}"
                )
            old_level = int(previous_model.get("lifecycle_level") or 0)
        else:
            old_level = 0

        if lifecycle > old_level and lifecycle > 0:
            deprecated_at = str(raw.get("deprecated_at") or "")
            replacement = str(raw.get("replacement") or "")
            days = raw.get("days_remaining")
            if lifecycle >= 5:
                headline = "🔴 Модель уже снята с поддержки"
            else:
                headline = f"🟠 Модель скоро снимается с поддержки: осталось {days} дн."
            message = f"{headline}\n{provider}: {model_id}"
            if deprecated_at:
                message += f"\nДата: {deprecated_at}"
            if replacement:
                message += f"\nЗамена: {replacement}"
            alerts.append(message)

    runtime = snapshot.runtime if isinstance(snapshot.runtime, dict) else {}
    prev_runtime = prev.get("runtime") if isinstance(prev.get("runtime"), dict) else {}
    for kind in ("image", "video"):
        raw = runtime.get(kind)
        if not isinstance(raw, dict) or not raw:
            continue
        signature = {
            "provider": str(raw.get("provider") or ""),
            "model": str(raw.get("model") or ""),
            "error_code": str(raw.get("error_code") or ""),
            "updated_at_epoch": int(raw.get("updated_at_epoch") or 0),
            "failover": bool(raw.get("failover")),
        }
        current["runtime"][kind] = signature
        previous_signature = prev_runtime.get(kind)
        changed = not isinstance(previous_signature, dict) or any(
            previous_signature.get(key) != value
            for key, value in signature.items()
        )
        if not changed:
            continue
        if signature["failover"] and signature["provider"] not in {"", "none"}:
            alerts.append(
                "🟠 Сработал автоматический fallback Visual Creative\n"
                f"Тип: {kind}\n"
                f"Рабочий провайдер: {signature['provider']}\n"
                f"Модель: {signature['model'] or 'не указана'}"
            )
        elif signature["provider"] == "none" and signature["error_code"]:
            alerts.append(
                "🔴 Генерация завершилась без рабочего провайдера\n"
                f"Тип: {kind}\nОшибка: {signature['error_code']}"
            )
        elif isinstance(previous_signature, dict):
            old_provider = str(previous_signature.get("provider") or "")
            if (
                old_provider
                and old_provider != signature["provider"]
                and signature["provider"] not in {"", "none"}
            ):
                alerts.append(
                    "🔄 Рабочий visual-провайдер изменился\n"
                    f"Тип: {kind}\n{old_provider} → {signature['provider']}"
                )

    circuits_raw = runtime.get("circuits_open_seconds")
    circuits = circuits_raw if isinstance(circuits_raw, dict) else {}
    current["circuits"] = {
        str(name): int(seconds)
        for name, seconds in circuits.items()
        if str(seconds).lstrip("-").isdigit() and int(seconds) > 0
    }
    previous_circuits = prev.get("circuits") if isinstance(prev.get("circuits"), dict) else {}
    for provider, seconds in current["circuits"].items():
        if provider not in previous_circuits:
            alerts.append(
                "🟠 Провайдер временно исключён circuit breaker'ом\n"
                f"{provider}: повторная проверка примерно через {seconds} сек."
            )
    for provider in previous_circuits:
        if provider not in current["circuits"]:
            alerts.append(f"🟢 Circuit breaker снят: {provider} снова допускается в маршрут.")

    return current, alerts


async def _deliver_provider_alerts(
    bot: Any,
    *,
    alerts: list[str],
) -> bool:
    if not alerts:
        return True
    message = "🧠 ClientPlatform · Visual Provider Watch\n\n" + "\n\n".join(alerts)
    _delivered, failed = await _send_superadmins(bot, message)
    return not failed


def _billing_state_and_alerts(
    snapshot: YandexBillingSnapshot,
    previous: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    prev = previous if isinstance(previous, dict) else {}
    if not snapshot.configured:
        return {"configured": False}, []

    current: dict[str, Any] = {
        "configured": True,
        "available": snapshot.available,
        "active": snapshot.active,
        "currency": snapshot.currency,
        "balance": "" if snapshot.balance is None else str(snapshot.balance),
        "auth_mode": snapshot.auth_mode,
        "auth_expires_at_epoch": snapshot.auth_expires_at_epoch,
        "error_code": snapshot.error_code,
    }
    alerts: list[str] = []

    if not snapshot.available:
        if bool(prev.get("available", True)) or str(prev.get("error_code") or "") != snapshot.error_code:
            alerts.append(
                "🟠 Yandex Billing telemetry недоступна\n"
                f"Причина: {snapshot.error_code or 'unknown'}\n"
                "Проверка реального денежного баланса временно невозможна."
            )
        return current, alerts

    if prev.get("configured") and not bool(prev.get("available", True)):
        alerts.append("🟢 Yandex Billing telemetry восстановилась.")

    previous_auth_mode = str(prev.get("auth_mode") or "")
    if snapshot.auth_mode == "static_iam_token" and previous_auth_mode != "static_iam_token":
        alerts.append(
            "🟠 Yandex Billing использует статический IAM-token\n"
            "Он ограничен по времени и требует ручной замены. "
            "Для production лучше подключить authorized key, чтобы ClientPlatform "
            "обновлял IAM-token автоматически."
        )
    elif (
        snapshot.auth_mode == "authorized_key"
        and previous_auth_mode
        and previous_auth_mode != "authorized_key"
    ):
        alerts.append(
            "🟢 Yandex Billing переведён на автоматически обновляемый IAM-token."
        )

    if not snapshot.active and prev.get("active") is not False:
        alerts.append(
            "🔴 Платёжный аккаунт Yandex Cloud неактивен. "
            "Платные генерации могут остановиться."
        )

    previous_balance = None
    raw_previous_balance = str(prev.get("balance") or "").strip()
    if raw_previous_balance:
        try:
            previous_balance = Decimal(raw_previous_balance)
        except InvalidOperation:
            previous_balance = None

    if snapshot.balance is not None:
        threshold = crossed_balance_threshold(snapshot.balance, previous_balance)
        if threshold is not None:
            alerts.append(
                "💰 Yandex Cloud: заканчиваются деньги\n"
                f"Баланс: {snapshot.balance} {snapshot.currency or ''}\n"
                f"Порог: {threshold} {snapshot.currency or ''}"
            )

    return current, alerts


async def _tick(bot: Any) -> None:
    global _last_error
    global _last_tick_monotonic

    snapshot, provider_snapshot, billing_snapshot = await asyncio.gather(
        asyncio.to_thread(get_platform_resource_snapshot),
        asyncio.to_thread(get_visual_provider_health_snapshot),
        asyncio.to_thread(get_yandex_billing_snapshot),
    )
    state = await asyncio.to_thread(_load_state)
    today = datetime.now(timezone.utc).date().isoformat()

    provider_state, provider_alerts = _provider_state_and_alerts(
        provider_snapshot,
        state.get("provider_state") if isinstance(state, dict) else None,
    )
    if provider_alerts:
        if not await _deliver_provider_alerts(bot, alerts=provider_alerts):
            _last_error = "visual_provider_alert_delivery_failed"
            _last_tick_monotonic = time.monotonic()
            return
        state["provider_state"] = provider_state
        await asyncio.to_thread(_save_state, state)
    elif state.get("provider_state") != provider_state:
        state["provider_state"] = provider_state

    billing_state, billing_alerts = _billing_state_and_alerts(
        billing_snapshot,
        state.get("billing_state") if isinstance(state, dict) else None,
    )
    if billing_alerts:
        if not await _deliver_provider_alerts(bot, alerts=billing_alerts):
            _last_error = "yandex_billing_alert_delivery_failed"
            _last_tick_monotonic = time.monotonic()
            return
        state["billing_state"] = billing_state
        await asyncio.to_thread(_save_state, state)
    elif state.get("billing_state") != billing_state:
        state["billing_state"] = billing_state

    if not snapshot.telemetry_available:
        error_code = snapshot.error_code or "visual_gateway_usage_unavailable"
        await _deliver_telemetry_warning(
            bot,
            state=state,
            today=today,
            error_code=error_code,
        )
        _last_error = error_code
        _last_tick_monotonic = time.monotonic()
        return

    if not await _finish_pending_threshold(bot, state=state):
        _last_error = "platform_resource_alert_delivery_failed"
        _last_tick_monotonic = time.monotonic()
        return

    day = snapshot.day_utc or today
    if str(state.get("day_utc") or "") != day:
        state = {
            "day_utc": day,
            "levels": {},
            "telemetry_day": "",
            "telemetry_error": "",
            "provider_state": state.get("provider_state", provider_state),
            "billing_state": state.get("billing_state", billing_state),
        }

    previous_levels = state.get("levels") if isinstance(state.get("levels"), dict) else {}
    crossed = crossed_thresholds(snapshot, previous_levels)
    levels = current_levels(snapshot)

    if crossed:
        message = render_threshold_notification(snapshot, crossed)
        _delivered, failed = await _send_superadmins(bot, message)
        if failed:
            state["threshold_pending"] = {
                "day": day,
                "message": message,
                "pending_admin_ids": sorted(failed),
                "target_levels": levels,
            }
            await asyncio.to_thread(_save_state, state)
            _last_error = "platform_resource_alert_delivery_failed"
            _last_tick_monotonic = time.monotonic()
            return

    await asyncio.to_thread(
        _save_state,
        {
            "day_utc": day,
            "levels": levels,
            "telemetry_day": "",
            "telemetry_error": "",
            "provider_state": provider_state,
            "billing_state": billing_state,
        },
    )
    _last_error = ""
    _last_tick_monotonic = time.monotonic()


def _record_tick_failure() -> None:
    global _last_error
    global _last_tick_monotonic
    _last_error = "platform_resource_monitor_tick_failed"
    _last_tick_monotonic = time.monotonic()
    log.exception("ClientPlatform platform resource monitor tick failed")


async def _monitor_loop(bot: Any) -> None:
    try:
        while True:
            try:
                await _tick(bot)
            except Exception:  # validator: allow-wide-except
                # A process-owned monitor must survive one transient database,
                # driver or transport failure and retry on the next tick.
                _record_tick_failure()
            await asyncio.sleep(_interval_seconds())
    except asyncio.CancelledError:
        raise


async def start_platform_resource_monitor(bot: Any) -> None:
    global _task
    if _task is not None and not _task.done():
        return
    _task = _task_manager.create(
        _monitor_loop(bot),
        name="clientplatform-platform-resource-monitor",
    )


async def stop_platform_resource_monitor(bot: Any) -> None:
    del bot
    global _task
    task = _task
    _task = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return


def platform_resource_monitor_snapshot() -> dict[str, Any]:
    age = (
        None
        if _last_tick_monotonic is None
        else max(0.0, time.monotonic() - _last_tick_monotonic)
    )
    return {
        "running": _task is not None and not _task.done(),
        "last_tick_age_sec": None if age is None else round(age, 3),
        "last_error": _last_error,
    }


__all__ = [
    "platform_resource_monitor_snapshot",
    "start_platform_resource_monitor",
    "stop_platform_resource_monitor",
]
