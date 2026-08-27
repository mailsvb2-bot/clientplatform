from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def test_sales_workspace_item_uses_direct_tenant_scoped_lookup(monkeypatch):
    actor = object()
    calls = []

    def direct_lookup(*, actor, lead_id):
        calls.append((actor, lead_id))
        return {"id": lead_id, "stage": "new"}

    def bounded_list_must_not_run(**_kwargs):
        raise AssertionError("single-item lookup must not scan bounded queues")

    monkeypatch.setattr(sales_workspace, "get_sales_work_item", direct_lookup)
    monkeypatch.setattr(sales_workspace, "list_sales_work", bounded_list_must_not_run)
    monkeypatch.setattr(
        sales_workspace,
        "list_recent_closed_sales_work",
        bounded_list_must_not_run,
    )

    result = sales_workspace.get_sales_workspace_item(
        actor=actor,
        lead_id="lead-older-than-window",
    )

    assert result == {"id": "lead-older-than-window", "stage": "new"}
    assert calls == [(actor, "lead-older-than-window")]


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


def test_sales_workspace_handoff_delegates_to_canonical_use_cases(monkeypatch):
    actor = object()
    calls = []
    monkeypatch.setattr(
        sales_workspace,
        "claim_sales_handoff",
        lambda **kwargs: calls.append(("claim", kwargs)) or {"status": "claimed"},
    )
    monkeypatch.setattr(
        sales_workspace,
        "resolve_sales_handoff",
        lambda **kwargs: calls.append(("resolve", kwargs)) or {"status": "resolved"},
    )

    sales_workspace.claim_sales_workspace_handoff(actor=actor, handoff_id="handoff-1")
    sales_workspace.resolve_sales_workspace_handoff(actor=actor, handoff_id="handoff-1")

    assert calls == [
        ("claim", {"actor": actor, "handoff_id": "handoff-1"}),
        ("resolve", {"actor": actor, "handoff_id": "handoff-1"}),
    ]


def test_sales_workspace_followup_delegates_to_canonical_scheduler(monkeypatch):
    actor = object()
    captured = {}

    def schedule(**kwargs):
        captured.update(kwargs)
        return "followup"

    monkeypatch.setattr(sales_workspace, "schedule_sales_followup", schedule)
    before = datetime.now(timezone.utc)
    result = sales_workspace.schedule_sales_workspace_followup(
        actor=actor,
        lead_id="lead-1",
        message_text="Напомнить о встрече",
        hours_from_now=24,
        interaction_key="route:event:followup",
    )
    after = datetime.now(timezone.utc)

    assert result == "followup"
    assert captured["actor"] is actor
    assert captured["lead_id"] == "lead-1"
    assert captured["message_text"] == "Напомнить о встрече"
    assert captured["request_key"] == "route:event:followup"
    delay = captured["scheduled_at"] - before
    assert timedelta(hours=24) - timedelta(seconds=1) <= delay
    assert delay <= timedelta(hours=24) + timedelta(seconds=1)
