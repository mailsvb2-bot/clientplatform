from __future__ import annotations

from services.admin_permissions import (
    ADMIN_ROLE,
    MARKETING_ROLE,
    SUPPORT_ROLE,
    PERMS,
    admin_callback_allowed,
    required_permission_for_callback,
)


def test_stale_callback_is_denied_after_permission_revocation() -> None:
    assert admin_callback_allowed(
        callback_data="admin:money:payment:42",
        roles={MARKETING_ROLE},
        is_superadmin=False,
        allowed_perms={"admin:funnel"},
    ) is False


def test_nested_callback_inherits_parent_permission() -> None:
    assert required_permission_for_callback("admin:money:payment:42") == "admin:money:today"
    assert admin_callback_allowed(
        callback_data="admin:money:payment:42",
        roles={MARKETING_ROLE},
        is_superadmin=False,
        allowed_perms={"admin:money:today"},
    ) is True


def test_role_boundary_blocks_forged_marketing_callback() -> None:
    assert admin_callback_allowed(
        callback_data="admin:adlinks",
        roles={SUPPORT_ROLE},
        is_superadmin=False,
        allowed_perms=None,
    ) is False


def test_admin_role_can_use_all_declared_sections_when_unrestricted() -> None:
    assert admin_callback_allowed(
        callback_data="admin:payment:problems",
        roles={ADMIN_ROLE},
        is_superadmin=False,
        allowed_perms=None,
    ) is True
    assert admin_callback_allowed(
        callback_data="admin:release:gate",
        roles={ADMIN_ROLE},
        is_superadmin=False,
        allowed_perms=None,
    ) is True


def test_unknown_callback_fails_closed() -> None:
    assert admin_callback_allowed(
        callback_data="admin:unknown:legacy",
        roles={ADMIN_ROLE},
        is_superadmin=False,
        allowed_perms=None,
    ) is False


def test_permission_catalog_contains_every_visible_audit_section() -> None:
    perms = {item.perm for item in PERMS}
    assert {
        "admin:messenger:overview",
        "admin:payment:problems",
        "admin:adlinks",
        "admin:release:gate",
        "admin:system:checks",
    } <= perms
