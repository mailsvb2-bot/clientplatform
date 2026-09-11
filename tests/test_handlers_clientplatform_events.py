from __future__ import annotations

from clientplatform.domain.tenancy import PlatformRole, TenantContext
from handlers import clientplatform_button_surface_contract as surface_contract
from handlers import clientplatform_interaction_safety as safety
from handlers import clientplatform_one_click_experience as one_click

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_MEMBER = "22222222-2222-4222-8222-222222222222"
_TOKEN = "ERERERERQRGBEREREREREQ"


def _actor(role: PlatformRole) -> TenantContext:
    return TenantContext(
        business_id=_BUSINESS,
        user_id=101,
        membership_id=_MEMBER,
        role=role,
    )


def _callbacks(rows):
    return [callback for row in rows for _label, callback in row]


def test_online_event_entry_is_visible_only_to_business_management_roles() -> None:
    owner_rows, _ = one_click._content_tools_rows(_TOKEN, _actor(PlatformRole.OWNER))
    admin_rows, _ = one_click._content_tools_rows(_TOKEN, _actor(PlatformRole.ADMINISTRATOR))
    manager_rows, _ = one_click._content_tools_rows(_TOKEN, _actor(PlatformRole.MANAGER))
    assert f"cpev:new:{_TOKEN}" in _callbacks(owner_rows)
    assert f"cpev:new:{_TOKEN}" in _callbacks(admin_rows)
    assert f"cpev:new:{_TOKEN}" not in _callbacks(manager_rows)
    assert len(owner_rows) <= 6


def test_event_cancel_callback_participates_in_interaction_safety() -> None:
    surface_contract.install_button_surface_contract(safety)
    assert safety._is_clientplatform_callback(f"cpev:new:{_TOKEN}")
    assert safety._state_local_callback_allowed(
        "ClientPlatformEventState:waiting_details",
        f"cpev:cancel:{_TOKEN}",
    )
