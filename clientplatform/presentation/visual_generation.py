from __future__ import annotations


def visual_provider_unavailable_message(kind: str) -> str:
    """Return plain-language preflight copy for an unavailable visual kind."""

    visual_kind = str(kind or "").strip().lower()
    noun = "видео" if visual_kind == "video" else "картинок"
    return (
        f"Сейчас для создания {noun} не подключён рабочий генератор. "
        "Платный запрос не запускался. Проверьте подключение AI-провайдера "
        "в инфраструктуре ClientPlatform."
    )


def visual_failure_message(job) -> str:
    """Map secret-safe gateway codes to one canonical owner-facing explanation."""

    code = str(getattr(job, "error_code", "") or "").strip().lower()
    if code in {"no_visual_provider_available", "visual_creative_disabled"}:
        return (
            "Для этого типа визуала сейчас нет подключённого рабочего генератора. "
            "Новый платный запрос не запускался."
        )
    if code in {
        "visual_provider_submit_http_401",
        "visual_provider_submit_http_403",
    }:
        return (
            "Провайдер генерации отклонил авторизацию. Нужно восстановить его ключ "
            "или доступ; повторять платный запрос вслепую ClientPlatform не будет."
        )
    if code == "visual_gateway_quota_rejected":
        return (
            "Шлюз генерации остановил запрос по лимиту. Платная генерация повторно "
            "автоматически не запускается."
        )
    if "timeout" in code or code.endswith("_transport"):
        return (
            "Генератор не подтвердил результат из-за сетевой ошибки. ClientPlatform "
            "не запускает второй платный запрос автоматически, чтобы не получить дубль."
        )
    if code:
        return (
            "Генератор вернул безопасный код ошибки. Новый запрос можно создать после "
            "устранения причины; повторного платного запуска автоматически нет."
        )
    return "Генерация завершилась ошибкой. Можно создать новый запрос."


__all__ = [
    "visual_failure_message",
    "visual_provider_unavailable_message",
]
