from __future__ import annotations

from types import SimpleNamespace

import pytest

from clientplatform.domain.support_cases import SupportCaseCategory, SupportCaseStatus
from services.messenger import clientplatform_entry as entry


def test_support_parser_is_channel_neutral() -> None:
    parsed = entry.parse_clientplatform_entry_command("support technical Messenger is unavailable")
    assert parsed is not None
    assert parsed.action == "support_create"
    assert parsed.value == "technical Messenger is unavailable"
    listed = entry.parse_clientplatform_entry_command("поддержка список")
    assert listed is not None and listed.action == "support_list"


@pytest.mark.parametrize("platform", ["telegram", "vk", "max"])
def test_support_create_uses_one_application_owner_on_all_channels(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    business_id = "ad67e150-0d91-48c9-a879-44a44782250d"
    access = SimpleNamespace(business=SimpleNamespace(id=business_id, name="Acme"))
    actor = SimpleNamespace(business_id=business_id)
    case = SimpleNamespace(
        id="f3b3c9dd-fcb1-43ad-b911-32dfd81222ac",
        business_id=business_id,
        category=SupportCaseCategory.TECHNICAL,
        status=SupportCaseStatus.OPEN,
        summary="Messenger is unavailable",
    )
    calls = []
    monkeypatch.setattr(
        entry,
        "register_user_entry",
        lambda user_id, **_kwargs: SimpleNamespace(user_id=user_id),
    )
    monkeypatch.setattr(entry, "list_accessible_businesses", lambda **_kwargs: [access])
    monkeypatch.setattr(entry, "resolve_tenant_context", lambda **_kwargs: actor)

    def create(**kwargs):
        calls.append(kwargs)
        return case

    monkeypatch.setattr(entry, "create_support_case", create)
    user_id, replies = entry.handle_clientplatform_entry(
        101,
        platform=platform,
        external_user_id="101",
        text="support technical Messenger is unavailable",
        event_key=f"{platform}:event:1",
    )
    assert user_id == 101
    assert len(calls) == 1
    assert calls[0]["actor"] is actor
    assert calls[0]["category"] == "technical"
    assert "Case:" in replies[0].text
