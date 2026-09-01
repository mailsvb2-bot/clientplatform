from __future__ import annotations

from services import admin


def test_platform_admin_requires_explicit_operator_id(monkeypatch):
    monkeypatch.setattr(admin, "ADMIN_IDS", [1001], raising=False)

    assert admin.is_platform_admin(1001) is True
    assert admin.is_platform_admin(1002) is False


def test_platform_admin_rejects_invalid_identifiers(monkeypatch):
    monkeypatch.setattr(admin, "ADMIN_IDS", [1001], raising=False)

    assert admin.is_platform_admin(None) is False
    assert admin.is_platform_admin(0) is False
    assert admin.is_platform_admin("bad") is False
