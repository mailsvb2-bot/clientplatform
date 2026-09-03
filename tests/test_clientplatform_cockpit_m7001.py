from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from clientplatform.application import cockpit
from clientplatform.runtime import cockpit_links
from clientplatform.domain.tenancy import (
    Business,
    BusinessAccess,
    BusinessMember,
    BusinessStatus,
    MembershipStatus,
    PlatformRole,
    TenantAccessDenied,
    TenantContext,
)
from clientplatform.runtime.telegram_webapp_auth import (
    TelegramWebAppAuthError,
    TelegramWebAppPrincipal,
    verify_telegram_webapp_init_data,
)

_TOKEN = "123456:unit-test-token"
_BUSINESS_A = "11111111-1111-4111-8111-111111111111"
_BUSINESS_B = "22222222-2222-4222-8222-222222222222"
_MEMBER_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_MEMBER_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _signed_init_data(
    *,
    user_id: int = 101,
    auth_date: int = 1_700_000_000,
    token: str = _TOKEN,
    third_party_signature: str | None = None,
) -> str:
    values = {
        "auth_date": str(auth_date),
        "query_id": "AAE-unit-test",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    if third_party_signature is not None:
        values["signature"] = third_party_signature
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def _access(*, business_id: str, member_id: str, user_id: int, role: PlatformRole, name: str) -> BusinessAccess:
    return BusinessAccess(
        business=Business(
            id=business_id,
            name=name,
            status=BusinessStatus.ACTIVE,
            created_by_user_id=user_id,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        ),
        membership=BusinessMember(
            id=member_id,
            business_id=business_id,
            user_id=user_id,
            role=role,
            status=MembershipStatus.ACTIVE,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        ),
    )


def test_telegram_init_data_verifies_signature_and_identity() -> None:
    principal = verify_telegram_webapp_init_data(
        _signed_init_data(), bot_token=_TOKEN, now=1_700_000_120
    )
    assert principal.user_id == 101
    assert principal.auth_date == 1_700_000_000
    assert principal.query_id == "AAE-unit-test"


def test_telegram_init_data_hash_covers_current_signature_field() -> None:
    signed = _signed_init_data(third_party_signature="telegram-third-party-signature")
    principal = verify_telegram_webapp_init_data(
        signed,
        bot_token=_TOKEN,
        now=1_700_000_120,
    )
    assert principal.user_id == 101
    with pytest.raises(TelegramWebAppAuthError, match="signature"):
        verify_telegram_webapp_init_data(
            signed.replace("telegram-third-party-signature", "tampered-signature"),
            bot_token=_TOKEN,
            now=1_700_000_120,
        )


def test_telegram_init_data_rejects_tamper_duplicate_expiry_and_future() -> None:
    signed = _signed_init_data()
    with pytest.raises(TelegramWebAppAuthError, match="signature"):
        verify_telegram_webapp_init_data(
            signed.replace("first_name", "last_name"), bot_token=_TOKEN, now=1_700_000_120
        )
    with pytest.raises(TelegramWebAppAuthError, match="duplicate"):
        verify_telegram_webapp_init_data(
            signed + "&auth_date=1700000000", bot_token=_TOKEN, now=1_700_000_120
        )
    with pytest.raises(TelegramWebAppAuthError, match="expired"):
        verify_telegram_webapp_init_data(signed, bot_token=_TOKEN, now=1_700_000_301)
    with pytest.raises(TelegramWebAppAuthError, match="future"):
        verify_telegram_webapp_init_data(
            _signed_init_data(auth_date=1_700_000_100), bot_token=_TOKEN, now=1_700_000_000
        )


def test_telegram_init_data_requires_real_user_and_server_token() -> None:
    with pytest.raises(TelegramWebAppAuthError, match="credential"):
        verify_telegram_webapp_init_data(_signed_init_data(), bot_token="", now=1_700_000_120)
    values = {
        "auth_date": "1700000000",
        "query_id": "q",
        "user": json.dumps({"id": 0}),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", _TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(TelegramWebAppAuthError, match="user id"):
        verify_telegram_webapp_init_data(urlencode(values), bot_token=_TOKEN, now=1_700_000_120)


def test_cockpit_context_uses_canonical_alias_and_saved_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _access(
        business_id=_BUSINESS_A,
        member_id=_MEMBER_A,
        user_id=202,
        role=PlatformRole.OWNER,
        name="Практика",
    )
    second = _access(
        business_id=_BUSINESS_B,
        member_id=_MEMBER_B,
        user_id=202,
        role=PlatformRole.MANAGER,
        name="Школа",
    )
    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(cockpit, "resolve_canonical_user_id", lambda value: 202 if value == 101 else value)
    monkeypatch.setattr(cockpit, "list_accessible_businesses", lambda *, user_id: [first, second])
    monkeypatch.setattr(
        cockpit,
        "get_owner_control_workspace",
        lambda *, user_id, platform: _BUSINESS_B if (user_id, platform) == (202, "telegram") else None,
    )

    def resolve(*, user_id: int, business_id: str) -> TenantContext:
        seen.append((business_id, user_id))
        if business_id != _BUSINESS_B or user_id != 202:
            raise TenantAccessDenied("denied")
        return TenantContext(
            business_id=_BUSINESS_B,
            user_id=202,
            membership_id=_MEMBER_B,
            role=PlatformRole.MANAGER,
        )

    monkeypatch.setattr(cockpit, "resolve_tenant_context", resolve)
    result = cockpit.resolve_cockpit_context(telegram_user_id=101)
    assert result.user_id == 202
    assert result.business_id == _BUSINESS_B
    assert result.business_name == "Школа"
    assert result.role == "manager"
    assert seen == [(_BUSINESS_B, 202)]
    assert [item.id for item in result.businesses if item.selected] == [_BUSINESS_B]


def test_cockpit_requested_business_is_selection_not_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _access(
        business_id=_BUSINESS_A,
        member_id=_MEMBER_A,
        user_id=101,
        role=PlatformRole.OWNER,
        name="Практика",
    )
    monkeypatch.setattr(cockpit, "resolve_canonical_user_id", lambda value: value)
    monkeypatch.setattr(cockpit, "list_accessible_businesses", lambda *, user_id: [first])
    monkeypatch.setattr(cockpit, "get_owner_control_workspace", lambda **_kwargs: None)

    def deny(*, user_id: int, business_id: str) -> TenantContext:
        assert user_id == 101
        assert business_id == _BUSINESS_B
        raise TenantAccessDenied("active business membership was not found")

    monkeypatch.setattr(cockpit, "resolve_tenant_context", deny)
    with pytest.raises(TenantAccessDenied):
        cockpit.resolve_cockpit_context(
            telegram_user_id=101,
            requested_business_id=_BUSINESS_B,
        )


def test_cockpit_without_business_returns_onboarding_not_synthetic_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cockpit, "resolve_canonical_user_id", lambda _value: 202)
    monkeypatch.setattr(cockpit, "list_accessible_businesses", lambda *, user_id: [])
    result = cockpit.resolve_cockpit_context(telegram_user_id=101)
    assert result.user_id == 202
    assert result.onboarding_required is True
    assert result.business_id is None
    assert result.businesses == ()
    assert result.navigation == ()


def test_role_navigation_is_server_projected_and_explained() -> None:
    support = TenantContext(
        business_id=_BUSINESS_A,
        user_id=101,
        membership_id=_MEMBER_A,
        role=PlatformRole.SUPPORT,
    )
    navigation = {item.id: item for item in cockpit.cockpit_navigation(support)}
    assert navigation["customers"].status == "available"
    assert navigation["growth"].status == "restricted"
    assert navigation["connections"].status == "restricted"
    assert navigation["billing"].status == "planned"
    assert all(item.summary and item.when_to_use for item in navigation.values())


def test_cockpit_url_reuses_first_party_https_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cockpit_links.settings,
        "MESSENGER_PUBLIC_BASE_URL",
        "https://app.example.test",
    )
    assert cockpit_links.cockpit_web_app_url() == "https://app.example.test/clientplatform/cockpit"


def test_cockpit_url_rejects_non_https_or_credentialed_base(monkeypatch: pytest.MonkeyPatch) -> None:
    for raw in (
        "http://app.example.test",
        "https://user:secret@app.example.test",
        "https://app.example.test?redirect=elsewhere",
    ):
        monkeypatch.setattr(cockpit_links.settings, "MESSENGER_PUBLIC_BASE_URL", raw)
        assert cockpit_links.cockpit_web_app_url() is None
