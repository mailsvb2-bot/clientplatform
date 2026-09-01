from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.validators import privacy as privacy_validator


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_fresh_clientplatform_validates_global_and_tenant_manifests(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(privacy_validator, "get_connection", lambda: _Conn())
    monkeypatch.setattr(privacy_validator, "_clientplatform_schema_present", lambda _conn: True)
    monkeypatch.setattr(
        privacy_validator,
        "validate_privacy_manifest",
        lambda _conn, *, strict: calls.append("global") or SimpleNamespace(discovered_user_tables=("users",)),
    )
    monkeypatch.setattr(
        privacy_validator,
        "validate_clientplatform_privacy_manifest",
        lambda _conn, *, strict, require_complete: calls.append("tenant") or SimpleNamespace(discovered_business_tables=("businesses",)),
    )
    privacy_validator.validate_privacy_schema(strict=True)
    assert calls == ["global", "tenant"]


def test_global_manifest_remains_fail_closed_without_tenant_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(privacy_validator, "get_connection", lambda: _Conn())
    monkeypatch.setattr(privacy_validator, "_clientplatform_schema_present", lambda _conn: False)

    def fail(_conn, *, strict: bool):
        assert strict is True
        raise RuntimeError("global_privacy_invalid")

    monkeypatch.setattr(privacy_validator, "validate_privacy_manifest", fail)
    with pytest.raises(privacy_validator.ValidationError, match="global_privacy_invalid"):
        privacy_validator.validate_privacy_schema(strict=True)
