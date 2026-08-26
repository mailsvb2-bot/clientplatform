from __future__ import annotations

from types import SimpleNamespace

from clientplatform.application import acquisition_destination, sales_workspace
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.promotions import PromotionChannel


def test_acquisition_destination_keeps_attribution_separate_from_transport(monkeypatch):
    campaign = SimpleNamespace(
        id="campaign-1",
        business_id="business-1",
        channel=PromotionChannel.WEBSITE,
        source_token="sourceToken123",
    )
    promotion = SimpleNamespace(campaign=campaign)
    messenger_destinations = (
        SimpleNamespace(
            platform=ConnectionPlatform.TELEGRAM,
            url="https://t.me/example?start=cpa_sourceToken123",
        ),
        SimpleNamespace(
            platform=ConnectionPlatform.VK,
            url="https://vk.com/write-1?ref=cpa_sourceToken123",
        ),
        SimpleNamespace(
            platform=ConnectionPlatform.MAX,
            url="https://max.ru/example?start=cpa_sourceToken123",
        ),
    )

    monkeypatch.setattr(
        acquisition_destination,
        "promotion_public_url",
        lambda *, base_url, source_token: (
            f"{base_url}/clientplatform/acquire?source=cpa_{source_token}"
        ),
    )
    monkeypatch.setattr(
        acquisition_destination,
        "promotion_start_payload",
        lambda source_token: f"cpa_{source_token}",
    )
    monkeypatch.setattr(
        acquisition_destination,
        "list_public_messenger_destinations",
        lambda *, business_id, start_payload: messenger_destinations,
    )

    result = acquisition_destination.build_acquisition_destination(
        promotion=promotion,
        public_base_url="https://client.example",
    )

    assert result.attribution_channel == PromotionChannel.WEBSITE
    assert result.public_url == (
        "https://client.example/clientplatform/acquire?source=cpa_sourceToken123"
    )
    assert [item.platform for item in result.messenger_destinations] == [
        ConnectionPlatform.TELEGRAM,
        ConnectionPlatform.VK,
        ConnectionPlatform.MAX,
    ]
    assert result.has_native_messenger_destination is True


def test_sales_workspace_snapshot_uses_one_transport_neutral_read_surface(monkeypatch):
    actor = object()
    monkeypatch.setattr(
        sales_workspace,
        "list_sales_work",
        lambda *, actor, limit: [{"id": "lead-open"}],
    )
    monkeypatch.setattr(
        sales_workspace,
        "list_sales_handoff_work",
        lambda *, actor, limit: [{"id": "lead-handoff"}],
    )
    monkeypatch.setattr(
        sales_workspace,
        "list_recent_closed_sales_work",
        lambda *, actor, limit: [{"id": "lead-won"}],
    )
    monkeypatch.setattr(
        sales_workspace,
        "count_sales_handoff_work",
        lambda *, actor: 1,
    )

    snapshot = sales_workspace.sales_workspace_snapshot(actor=actor, limit=7)

    assert snapshot.open_work == ({"id": "lead-open"},)
    assert snapshot.handoff_work == ({"id": "lead-handoff"},)
    assert snapshot.recent_closed == ({"id": "lead-won"},)
    assert snapshot.handoff_count == 1


def test_sales_workspace_mutation_delegates_to_canonical_operation(monkeypatch):
    actor = SimpleNamespace(membership_id="member-7")
    calls = []

    def fake_assign_sales_lead(*, actor, lead_id, member_id):
        calls.append((actor, lead_id, member_id))
        return "updated-lead"

    monkeypatch.setattr(sales_workspace, "assign_sales_lead", fake_assign_sales_lead)

    result = sales_workspace.assign_sales_workspace_to_actor(
        actor=actor,
        lead_id="lead-42",
    )

    assert result == "updated-lead"
    assert calls == [(actor, "lead-42", "member-7")]
