from __future__ import annotations

from dataclasses import dataclass

from core.callbacks import ADMIN_TARIFFS
from core.time_utils import utc_now
from services.db import db

GROWTH_APPLY_REVIEW_PERMISSION = "admin:growth:apply:review"
SALES_DESK_PERMISSION = "admin:sales"
SALES_WRITE_PERMISSION = "admin:sales:write"
SALES_MESSAGE_PERMISSION = "admin:sales:message"

SUPPORT_ROLE = "support"
MARKETING_ROLE = "marketing"
ADMIN_ROLE = "admin"

_ALWAYS_ALLOWED_CALLBACKS = {"admin:menu", "admin:back"}
_SUPERADMIN_ONLY_PREFIXES = ("admin:add_admin", "admin:roles:", "admin:perms")

_SUPPORT_PERMISSIONS = {
    "admin:demo:brief",
    "admin:demo:full",
    "admin:users:today",
    "admin:user:card",
    "admin:behavior",
    "admin:messenger:overview",
    "admin:payment:problems",
}
_MARKETING_PERMISSIONS = {
    "admin:growth:autopilot",
    GROWTH_APPLY_REVIEW_PERMISSION,
    SALES_DESK_PERMISSION,
    SALES_WRITE_PERMISSION,
    SALES_MESSAGE_PERMISSION,
    "admin:adlinks",
    "admin:funnel",
    "admin:money:today",
    "admin:conversion",
    "admin:segments",
    "admin:ab",
    "admin:copy:menu",
    "admin:ai:prices",
}
_ADMIN_PERMISSIONS = {
    "admin:release:gate",
    "admin:giftshare",
    "admin:funnel2",
    "admin:retention",
    "admin:state:last",
    "admin:system:checks",
    ADMIN_TARIFFS,
}


def required_permission_for_callback(callback_data: str) -> str | None:
    """Return the canonical permission protecting an admin callback.

    Nested callbacks inherit the permission of the screen that issued them.
    Unknown callbacks return ``None`` and are denied by ``admin_callback_allowed``.
    """

    data = str(callback_data or "").strip()
    if not data or data in _ALWAYS_ALLOWED_CALLBACKS:
        return None
    if data.startswith(_SUPERADMIN_ONLY_PREFIXES):
        return None
    if data.startswith("admin:money:payment:") or data.startswith("admin:money:"):
        return "admin:money:today"
    if data.startswith("admin:adlinks:create:"):
        return "admin:adlinks"
    if data.startswith("admin:growth:autopilot:"):
        return "admin:growth:autopilot"
    if data.startswith("admin:sales:"):
        return SALES_DESK_PERMISSION
    if data.startswith("admin:user:"):
        return "admin:user:card"
    if data.startswith("admin:copy:"):
        return "admin:copy:menu"
    if data.startswith("admin:tariffs:"):
        return ADMIN_TARIFFS

    known = _SUPPORT_PERMISSIONS | _MARKETING_PERMISSIONS | _ADMIN_PERMISSIONS
    return data if data in known else None


def admin_callback_allowed(
    *,
    callback_data: str,
    roles: set[str],
    is_superadmin: bool,
    allowed_perms: set[str] | None,
) -> bool:
    """Authorize an admin callback server-side, including stale inline buttons."""

    if is_superadmin:
        return True

    data = str(callback_data or "").strip()
    normalized_roles = {str(role).strip().lower() for role in roles if str(role).strip()}
    if not data or data in _ALWAYS_ALLOWED_CALLBACKS:
        return bool(normalized_roles)
    if data.startswith(_SUPERADMIN_ONLY_PREFIXES):
        return False

    permission = required_permission_for_callback(data)
    if permission is None:
        return False

    if ADMIN_ROLE in normalized_roles:
        role_allowed = True
    elif permission in _SUPPORT_PERMISSIONS:
        role_allowed = SUPPORT_ROLE in normalized_roles
    elif permission in _MARKETING_PERMISSIONS:
        role_allowed = MARKETING_ROLE in normalized_roles
    else:
        role_allowed = False

    if not role_allowed:
        return False
    if allowed_perms is None:
        return True
    return permission in allowed_perms


# Права храним как строки. Чтобы не усложнять UX —
# используем callback_data как идентификатор права.
# Супер-админ всегда имеет доступ ко всему.


def get_allowed_perms(admin_id: int) -> set[str] | None:
    """Возвращает set разрешённых perm или None, если ограничений не настроено.

    Логика:
    - если для admin_id нет записей в admin_permissions -> None (не ограничиваем)
    - если записи есть -> возвращаем только allowed=1
    """
    admin_id = int(admin_id)
    with db() as conn:
        rows = conn.execute(
            "SELECT perm, allowed FROM admin_permissions WHERE admin_id=?",
            (admin_id,),
        ).fetchall()
    if not rows:
        return None
    return {
        str(r[0] if not hasattr(r, "keys") else r["perm"])
        for r in rows
        if int(r[1] if not hasattr(r, "keys") else r["allowed"]) == 1
    }


def has_explicit_allowed_perm(admin_id: int, perm: str) -> bool:
    """Return True only for an explicit allowed=1 row.

    Sensitive write permissions must not inherit the legacy ``None means
    unrestricted`` behavior used by read-only admin navigation.
    """

    with db() as conn:
        row = conn.execute(
            "SELECT allowed FROM admin_permissions WHERE admin_id=? AND perm=? LIMIT 1",
            (int(admin_id), str(perm)),
        ).fetchone()
    if row is None:
        return False
    value = row[0] if not hasattr(row, "keys") else row["allowed"]
    return int(value or 0) == 1


def set_perm(admin_id: int, perm: str, allowed: bool, *, updated_by: int | None = None) -> None:
    admin_id = int(admin_id)
    perm = str(perm)
    now = utc_now().replace(microsecond=0).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO admin_permissions(admin_id, perm, allowed, updated_at_utc, updated_by) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(admin_id, perm) DO UPDATE SET allowed=excluded.allowed, updated_at_utc=excluded.updated_at_utc, updated_by=excluded.updated_by",
            (admin_id, perm, 1 if allowed else 0, now, int(updated_by) if updated_by is not None else None),
        )
        conn.commit()


def toggle_perm(admin_id: int, perm: str, *, updated_by: int | None = None) -> bool:
    """Переключает perm и возвращает новое значение allowed."""
    admin_id = int(admin_id)
    perm = str(perm)
    now = utc_now().replace(microsecond=0).isoformat()
    with db() as conn:
        row = conn.execute(
            "SELECT allowed FROM admin_permissions WHERE admin_id=? AND perm=?",
            (admin_id, perm),
        ).fetchone()
        current = None
        if row is not None:
            current = int(row[0] if not hasattr(row, "keys") else row["allowed"])
        new_allowed = 0 if current == 1 else 1
        conn.execute(
            "INSERT INTO admin_permissions(admin_id, perm, allowed, updated_at_utc, updated_by) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(admin_id, perm) DO UPDATE SET allowed=excluded.allowed, updated_at_utc=excluded.updated_at_utc, updated_by=excluded.updated_by",
            (admin_id, perm, new_allowed, now, int(updated_by) if updated_by is not None else None),
        )
        conn.commit()
    return bool(new_allowed)


def list_admin_ids(limit: int = 200) -> list[int]:
    """Список всех пользователей, у которых есть роли (кандидаты в админы)."""
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM user_roles ORDER BY user_id LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [int(r[0] if not hasattr(r, "keys") else r["user_id"]) for r in rows or []]


@dataclass(frozen=True)
class PermItem:
    perm: str
    title: str


# Набор прав, который показывает супер-админ в UI.
# При добавлении новых кнопок — просто добавляйте сюда.
PERMS: list[PermItem] = [
    PermItem("admin:demo:brief", "📊 Демо (кратко)"),
    PermItem("admin:demo:full", "📈 Демо (подробно)"),
    PermItem("admin:users:today", "👥 Пользователи сегодня"),
    PermItem("admin:user:card", "🔎 Карточка пользователя"),
    PermItem("admin:behavior", "🧠 Поведение"),
    PermItem("admin:messenger:overview", "💬 Мессенджеры"),
    PermItem("admin:payment:problems", "⚠️ Проверить оплаты"),
    PermItem("admin:growth:autopilot", "🤖 Growth Autopilot"),
    PermItem("admin:adlinks", "📣 Рекламные ссылки"),
    PermItem(GROWTH_APPLY_REVIEW_PERMISSION, "🛡 Review Growth Apply"),
    PermItem(SALES_DESK_PERMISSION, "🧑‍💼 Sales Desk (просмотр)"),
    PermItem(SALES_WRITE_PERMISSION, "✍️ Sales Desk (изменение)"),
    PermItem(SALES_MESSAGE_PERMISSION, "✉️ Sales Desk (сообщения)"),
    PermItem("admin:funnel", "📉 Воронка"),
    PermItem("admin:money:today", "💰 Деньги и клиенты"),
    PermItem("admin:conversion", "💰 Конверсия"),
    PermItem("admin:segments", "🧲 Сегменты"),
    PermItem("admin:ab", "🧪 Тесты офферов"),
    PermItem("admin:copy:menu", "🤖 ИИ-копирайтер"),
    PermItem("admin:ai:prices", "🤖 ИИ-цены"),
    PermItem("admin:giftshare", "🎁 Подарки и рекомендации"),
    PermItem("admin:funnel2", "🧲 Воронка 2.0"),
    PermItem("admin:retention", "🧩 Удержание"),
    PermItem("admin:state:last", "🧾 Мои состояния (10)"),
    PermItem("admin:release:gate", "🚦 Release gate"),
    PermItem("admin:system:checks", "🧪 Системные проверки"),
    # Тарифы — обычно только супер-админ, но если вдруг нужно делегировать.
    PermItem(ADMIN_TARIFFS, "💳 Тарифы"),
]
