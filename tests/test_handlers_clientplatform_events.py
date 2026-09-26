from __future__ import annotations

from unittest.mock import patch

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


def test_webinar_hub_entry_matches_canonical_event_funnel_roles() -> None:
    owner_rows, owner_help = one_click._content_tools_rows(_TOKEN, _actor(PlatformRole.OWNER))
    admin_rows, _ = one_click._content_tools_rows(_TOKEN, _actor(PlatformRole.ADMINISTRATOR))
    manager_rows, manager_help = one_click._content_tools_rows(_TOKEN, _actor(PlatformRole.MANAGER))
    marketer_rows, _ = one_click._content_tools_rows(_TOKEN, _actor(PlatformRole.MARKETER))
    analyst_rows, analyst_help = one_click._content_tools_rows(_TOKEN, _actor(PlatformRole.ANALYST))

    for rows in (owner_rows, admin_rows, manager_rows):
        assert f"cpev:home:{_TOKEN}" in _callbacks(rows)
        assert f"cpev:new:{_TOKEN}" not in _callbacks(rows)
    assert f"cpev:home:{_TOKEN}" not in _callbacks(marketer_rows)
    assert _callbacks(analyst_rows) == [f"cpo:more:{_TOKEN}", f"cpj:home:{_TOKEN}"]
    assert analyst_help == []
    assert any("воронку и автосообщения" in line for line in owner_help)
    assert any("воронку и автосообщения" in line for line in manager_help)
    assert len(owner_rows) <= 6


def test_webinar_hub_remains_visible_if_event_access_outlives_promotion_management() -> None:
    actor = _actor(PlatformRole.ANALYST)
    with (
        patch.object(one_click, "_allowed", return_value=False),
        patch.object(one_click, "_event_funnel_visible", return_value=True),
    ):
        top_rows = one_click._more_rows(_TOKEN, actor)
        rows, help_lines = one_click._content_tools_rows(_TOKEN, actor)
    assert f"cpo:content:{_TOKEN}" in _callbacks(top_rows)
    assert _callbacks(rows) == [f"cpev:home:{_TOKEN}", f"cpo:more:{_TOKEN}", f"cpj:home:{_TOKEN}"]
    assert any("воронку и автосообщения" in line for line in help_lines)


def test_event_hub_is_repeatable_navigation_but_creation_remains_a_mutation() -> None:
    assert "cpev:" in safety._CLIENTPLATFORM_CALLBACK_PREFIXES
    assert "cpev:home:" in safety._STATE_ESCAPE_PREFIXES
    assert "cpev:home:" in safety._REPEATABLE_NAVIGATION_PREFIXES
    surface_contract.install_button_surface_contract(safety)
    assert safety._is_clientplatform_callback(f"cpev:home:{_TOKEN}")
    assert safety._is_repeatable_navigation(f"cpev:home:{_TOKEN}")
    assert safety._is_repeatable_navigation(f"cpev:settings:{_TOKEN}")
    assert not safety._is_repeatable_navigation(f"cpev:new:{_TOKEN}")
    state_name = "ClientPlatformControlState:activity_description"
    assert not safety._callback_conflicts_with_state(state_name, f"cpev:home:{_TOKEN}")
    assert safety._callback_should_clear_state(state_name, f"cpev:home:{_TOKEN}")
    assert safety._callback_should_clear_state(state_name, f"cpev:settings:{_TOKEN}")
    for event_state in ("waiting_details", "waiting_time", "waiting_join_url"):
        assert safety._state_local_callback_allowed(
            f"ClientPlatformEventState:{event_state}",
            f"cpev:cancel:{_TOKEN}",
        )


def test_client_tools_hide_customer_actions_without_customer_record_access() -> None:
    rows, help_lines = one_click._client_tools_rows(_TOKEN, _actor(PlatformRole.MARKETER))
    assert _callbacks(rows) == [f"cpj:home:{_TOKEN}", f"cpj:home:{_TOKEN}"]
    assert help_lines == []
