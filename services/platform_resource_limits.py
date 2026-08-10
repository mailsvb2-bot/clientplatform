from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services import visual_creative_gateway as visual_gateway
from services.visual_creative_gateway import VisualCreativeGatewayError


_WARNING_THRESHOLDS = (70, 85, 95, 100)
_RESOURCE_LABELS = {
    "jobs": "Все visual-задачи",
    "image": "Изображения",
    "video": "Видео",
    "active": "Одновременные задачи",
}


@dataclass(frozen=True, slots=True)
class ResourceCounter:
    used: int
    limit: int
    remaining: int

    @property
    def percent(self) -> int:
        if self.limit <= 0:
            return 0
        return min(100, max(0, round(self.used * 100 / self.limit)))


@dataclass(frozen=True, slots=True)
class PlatformResourceSnapshot:
    configured: bool
    telemetry_available: bool
    base_url: str
    token_configured: bool
    day_utc: str = ""
    resets_at: str = ""
    usage_semantics: str = ""
    jobs: ResourceCounter | None = None
    image: ResourceCounter | None = None
    video: ResourceCounter | None = None
    active: ResourceCounter | None = None
    error_code: str = ""

    def counters(self) -> dict[str, ResourceCounter]:
        return {
            key: value
            for key, value in {
                "jobs": self.jobs,
                "image": self.image,
                "video": self.video,
                "active": self.active,
            }.items()
            if value is not None
        }


def _counter(payload: object, *, name: str) -> ResourceCounter:
    if not isinstance(payload, dict):
        raise ValueError(f"visual_usage_{name}_invalid")
    try:
        used = int(payload.get("used", 0))
        limit = int(payload.get("limit", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"visual_usage_{name}_invalid") from exc
    if used < 0 or limit < 1:
        raise ValueError(f"visual_usage_{name}_invalid")
    return ResourceCounter(
        used=used,
        limit=limit,
        remaining=max(0, limit - used),
    )


def get_platform_resource_snapshot() -> PlatformResourceSnapshot:
    """Read operator-visible Visual Gateway quotas without exposing credentials.

    The gateway usage endpoint reports reservations accepted by the gateway. It is
    intentionally not presented as Yandex billing data: provider billing remains
    authoritative in the provider console.
    """

    gateway = visual_gateway.gateway_snapshot()
    configured = bool(gateway.get("configured"))
    token_configured = bool(gateway.get("token_configured"))
    base_url = str(gateway.get("base_url") or "")
    if not configured or not token_configured:
        return PlatformResourceSnapshot(
            configured=configured,
            telemetry_available=False,
            base_url=base_url,
            token_configured=token_configured,
            error_code="visual_gateway_not_configured",
        )

    try:
        # Reuse the canonical gateway transport so auth, URL validation, bounded
        # reads and timeout handling stay in one place rather than being copied.
        payload = visual_gateway._json("GET", "/v1/usage", timeout_seconds=10)
        jobs = _counter(payload.get("jobs"), name="jobs")
        image = _counter(payload.get("image"), name="image")
        video = _counter(payload.get("video"), name="video")
        active = _counter(payload.get("active"), name="active")
    except VisualCreativeGatewayError as exc:
        return PlatformResourceSnapshot(
            configured=True,
            telemetry_available=False,
            base_url=base_url,
            token_configured=True,
            error_code=str(exc),
        )
    except (TypeError, ValueError) as exc:
        return PlatformResourceSnapshot(
            configured=True,
            telemetry_available=False,
            base_url=base_url,
            token_configured=True,
            error_code=str(exc) or "visual_gateway_usage_invalid",
        )

    return PlatformResourceSnapshot(
        configured=True,
        telemetry_available=True,
        base_url=base_url,
        token_configured=True,
        day_utc=str(payload.get("day_utc") or ""),
        resets_at=str(payload.get("resets_at") or ""),
        usage_semantics=str(payload.get("usage_semantics") or ""),
        jobs=jobs,
        image=image,
        video=video,
        active=active,
    )


def warning_level(counter: ResourceCounter) -> int:
    percent = counter.percent
    level = 0
    for threshold in _WARNING_THRESHOLDS:
        if percent >= threshold:
            level = threshold
    return level


def current_levels(snapshot: PlatformResourceSnapshot) -> dict[str, int]:
    if not snapshot.telemetry_available:
        return {}
    return {name: warning_level(counter) for name, counter in snapshot.counters().items()}


def crossed_thresholds(
    snapshot: PlatformResourceSnapshot,
    previous_levels: dict[str, int] | None,
) -> dict[str, int]:
    previous = previous_levels or {}
    return {
        name: level
        for name, level in current_levels(snapshot).items()
        if level > int(previous.get(name, 0) or 0)
    }


def _counter_line(icon: str, label: str, counter: ResourceCounter) -> str:
    return (
        f"{icon} {label}: {counter.used}/{counter.limit} "
        f"({counter.percent}%), осталось {counter.remaining}"
    )


def next_action(snapshot: PlatformResourceSnapshot) -> str:
    if not snapshot.telemetry_available:
        if snapshot.error_code == "visual_gateway_not_configured":
            return (
                "Подключить VISUAL_GATEWAY_URL и VISUAL_GATEWAY_TOKEN, затем "
                "перезапустить ClientPlatform."
            )
        return (
            "Проверить контейнер Visual Creative Gateway и endpoint /v1/usage. "
            "Пока телеметрия недоступна, автоматические напоминания о лимитах "
            "не могут считаться надёжными."
        )

    levels = current_levels(snapshot)
    highest = max(levels.values(), default=0)
    if highest >= 100:
        return (
            "Лимит достигнут. Новые visual-запросы будут блокироваться до сброса "
            "суточного окна либо увеличения лимита. Проверьте баланс/квоты Yandex "
            "Cloud, затем при необходимости увеличьте лимиты Visual Gateway и "
            "перезапустите gateway."
        )
    if highest >= 95:
        return (
            "Почти исчерпано. До следующего потока генераций проверьте баланс/квоты "
            "Yandex Cloud и увеличьте дневной лимит Visual Gateway, если рост расхода "
            "ожидаемый."
        )
    if highest >= 85:
        return (
            "Лимит скоро закончится. Оцените оставшийся спрос сегодня; если его не "
            "хватит, заранее проверьте Yandex Cloud и поднимите лимит gateway."
        )
    if highest >= 70:
        return (
            "Пока вмешательство не обязательно. Следите за расходом; если темп "
            "сохранится, подготовьте увеличение лимита до достижения 85%."
        )
    return "Запас достаточный. Ничего менять сейчас не нужно."


def render_platform_resource_status(snapshot: PlatformResourceSnapshot) -> str:
    lines = ["🧯 Лимиты и ресурсы ClientPlatform", ""]
    lines.append(
        f"Visual Creative Gateway: {'✅ подключён' if snapshot.configured else '❌ не подключён'}"
    )
    lines.append(
        f"Телеметрия лимитов: {'✅ доступна' if snapshot.telemetry_available else '⚠️ недоступна'}"
    )
    if snapshot.base_url:
        lines.append(f"Gateway: {snapshot.base_url}")

    if snapshot.telemetry_available:
        assert snapshot.jobs is not None
        assert snapshot.image is not None
        assert snapshot.video is not None
        assert snapshot.active is not None
        lines.extend(
            [
                "",
                "Текущие защитные лимиты:",
                _counter_line("📦", _RESOURCE_LABELS["jobs"], snapshot.jobs),
                _counter_line("🖼", _RESOURCE_LABELS["image"], snapshot.image),
                _counter_line("🎬", _RESOURCE_LABELS["video"], snapshot.video),
                _counter_line("⚙️", _RESOURCE_LABELS["active"], snapshot.active),
            ]
        )
        if snapshot.resets_at:
            lines.append(f"Сброс суточных счётчиков: {snapshot.resets_at}")
        lines.extend(
            [
                "",
                "Напоминания: 70% → наблюдать, 85% → готовить увеличение, "
                "95% → действовать сейчас, 100% → генерации блокируются.",
                "",
                "Важно: счётчики выше — резервирования Visual Gateway, а не "
                "официальный биллинг Yandex Cloud. Финальный расход проверяется в "
                "консоли провайдера.",
            ]
        )
    elif snapshot.error_code:
        lines.extend(["", f"Причина: {snapshot.error_code}"])

    lines.extend(["", "Что делать дальше:", next_action(snapshot)])
    return "\n".join(lines)


def render_threshold_notification(
    snapshot: PlatformResourceSnapshot,
    crossed: dict[str, int],
) -> str:
    if not crossed:
        return ""
    counters = snapshot.counters()
    severity = max(crossed.values())
    icon = "🔴" if severity >= 95 else "🟠"
    lines = [f"{icon} ClientPlatform: заканчиваются лимиты Visual Creative", ""]
    for name, level in sorted(crossed.items(), key=lambda item: (-item[1], item[0])):
        counter = counters.get(name)
        if counter is None:
            continue
        lines.append(
            f"• {_RESOURCE_LABELS.get(name, name)}: {counter.used}/{counter.limit} "
            f"({counter.percent}%), порог {level}%"
        )
    if snapshot.resets_at:
        lines.append(f"• Сброс: {snapshot.resets_at}")
    lines.extend(["", "Что делать:", next_action(snapshot)])
    return "\n".join(lines)


__all__ = [
    "PlatformResourceSnapshot",
    "ResourceCounter",
    "crossed_thresholds",
    "current_levels",
    "get_platform_resource_snapshot",
    "next_action",
    "render_platform_resource_status",
    "render_threshold_notification",
    "warning_level",
]
