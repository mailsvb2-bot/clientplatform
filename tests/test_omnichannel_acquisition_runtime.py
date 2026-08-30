from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest

from clientplatform.application import promotions
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.promotions import (
    PromotionChannel,
    PromotionCreative,
    PromotionInvariantViolation,
    stable_creative_id,
)
from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.booking_repository import BookingRepository
from clientplatform.infrastructure.promotion_repository import PromotionRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.runtime import messenger_channel_ingress
from runtime import messenger_webhooks
from services.db.schema import (
    clientplatform_activity,
    clientplatform_attribution,
    clientplatform_bookings,
    clientplatform_customers,
    clientplatform_promotions,
    clientplatform_tenancy,
)


def test_provider_metadata_extracts_same_promotion_token() -> None:
    source = "sourceToken123"
    assert messenger_channel_ingress._promotion_token_for_event(
        platform=ConnectionPlatform.MAX,
        payload={"update_type": "bot_started", "payload": f"cpa_{source}"},
        raw_text=f"cpa_{source}",
    ) == source
    assert messenger_channel_ingress._promotion_token_for_event(
        platform=ConnectionPlatform.VK,
        payload={
            "type": "message_new",
            "object": {"message": {"text": "Хочу записаться", "ref": f"cpa_{source}"}},
        },
        raw_text="Хочу записаться",
    ) == source
    assert messenger_channel_ingress._promotion_token_for_event(
        platform=ConnectionPlatform.MAX,
        payload={"update_type": "message_created"},
        raw_text="Хочу записаться",
    ) is None


@pytest.mark.asyncio
async def test_neutral_landing_keeps_one_source_across_connected_messengers(monkeypatch) -> None:
    source = "sourceToken123"
    destinations = (
        SimpleNamespace(platform=ConnectionPlatform.TELEGRAM, url=f"https://t.me/demo?start=cpa_{source}"),
        SimpleNamespace(platform=ConnectionPlatform.VK, url=f"https://vk.com/write-42?ref=cpa_{source}"),
        SimpleNamespace(platform=ConnectionPlatform.MAX, url=f"https://max.ru/demo?start=cpa_{source}"),
    )
    monkeypatch.setattr(
        messenger_webhooks,
        "_messenger_public_base_url",
        lambda: "https://client.example.test",
    )
    monkeypatch.setattr(
        messenger_webhooks,
        "resolve_acquisition_destination",
        lambda **_: SimpleNamespace(messenger_destinations=destinations),
    )
    response = await messenger_webhooks._clientplatform_acquisition_landing(
        SimpleNamespace(query={"source": f"cpa_{source}"})
    )
    body = response.text
    assert response.status == 200
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "https://t.me/demo?start=cpa_sourceToken123" in body
    assert "https://vk.com/write-42?ref=cpa_sourceToken123" in body
    assert "https://max.ru/demo?start=cpa_sourceToken123" in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "target"),
    (
        ("telegram", "https://t.me/clientplatform_bot?start=cpo_landing"),
        ("vk", "https://vk.com/im?sel=-123456&ref=cpo_landing"),
        ("max", "https://max.ru/clientplatform?start=cpo_landing"),
    ),
)
async def test_owner_landing_redirect_uses_stable_server_route(monkeypatch, platform, target) -> None:
    status = SimpleNamespace(telegram_ok=True, vk_ok=True, max_ok=True)
    monkeypatch.setattr(messenger_webhooks, "build_setup_status", lambda: status)
    monkeypatch.setattr(messenger_webhooks, "telegram_runtime_enabled", lambda: True)
    monkeypatch.setattr(messenger_webhooks, "vk_webhook_enabled", lambda: True)
    monkeypatch.setattr(messenger_webhooks, "max_webhook_enabled", lambda: True)
    monkeypatch.setattr(
        messenger_webhooks,
        "build_owner_entry_target",
        lambda requested, source="site": (
            {"platform": requested, "url": target} if requested == platform and source == "landing" else None
        ),
    )

    response = await messenger_webhooks._clientplatform_owner_entry_redirect(
        SimpleNamespace(match_info={"platform": platform})
    )

    assert response.status == 302
    assert response.headers["Location"] == target
    assert response.headers["Cache-Control"] == "no-store, max-age=0"


@pytest.mark.asyncio
async def test_owner_landing_redirect_fails_closed_when_provider_is_not_ready(monkeypatch) -> None:
    monkeypatch.setattr(
        messenger_webhooks,
        "build_owner_entry_target",
        lambda *_args, **_kwargs: {"platform": "vk", "url": "https://vk.example"},
    )
    monkeypatch.setattr(
        messenger_webhooks,
        "build_setup_status",
        lambda: SimpleNamespace(telegram_ok=True, vk_ok=True, max_ok=True),
    )
    monkeypatch.setattr(messenger_webhooks, "vk_webhook_enabled", lambda: False)

    response = await messenger_webhooks._clientplatform_owner_entry_redirect(
        SimpleNamespace(match_info={"platform": "vk"})
    )

    assert response.status == 503
    assert "пока не подключён" in response.text
    assert "vk.example" not in response.text


@pytest.mark.asyncio
async def test_owner_landing_redirect_rejects_unknown_platform() -> None:
    with pytest.raises(Exception) as exc_info:
        await messenger_webhooks._clientplatform_owner_entry_redirect(
            SimpleNamespace(match_info={"platform": "icq"})
        )
    assert getattr(exc_info.value, "status", None) == 404


def _promotion_fixture() -> tuple[sqlite3.Connection, object, str, str]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    for schema in (
        clientplatform_tenancy,
        clientplatform_customers,
        clientplatform_activity,
        clientplatform_bookings,
        clientplatform_promotions,
        clientplatform_attribution,
    ):
        schema.ensure(conn)
    tenancy = TenancyRepository(conn)
    activity = ActivityRepository(conn)
    bookings = BookingRepository(conn)
    promotion_repo = PromotionRepository(conn)
    business = tenancy.create_business(owner_user_id=101, name="Практика")
    actor = tenancy.resolve_context(user_id=101, business_id=business.business.id)
    activity.upsert_profile(
        actor=actor,
        activity_description="Консультации",
        timezone_name="Europe/Tallinn",
        now="2026-08-26T10:00:00+00:00",
    )
    capability = activity.enable_capability(
        actor=actor,
        connector_key="services",
        now="2026-08-26T10:00:00+00:00",
    )
    offering = activity.create_offering(
        actor=actor,
        capability_id=capability.id,
        title="Консультация",
        description="Первая встреча",
        now="2026-08-26T10:00:00+00:00",
    )
    slot = bookings.create_slot(
        actor=actor,
        offering_id=offering.id,
        local_start="10.09.2026 12:00",
        duration_minutes=60,
        now="2026-08-26T10:00:00+00:00",
    )
    creative = PromotionCreative(
        creative_id=stable_creative_id("omnichannel-acquisition"),
        headline="Есть свободное время",
        primary_text="Можно записаться на консультацию.",
        description="Запись онлайн",
    )
    campaign, _ = promotion_repo.create_or_refresh_campaign(
        actor=actor,
        slot_id=slot.slot.id,
        channel=PromotionChannel.WEBSITE,
        creative=creative,
        now="2026-08-26T10:10:00+00:00",
    )
    alias = promotion_repo.ensure_source_alias(
        actor=actor,
        campaign_id=campaign.id,
        source_kind="yandex_direct",
        source_key="campaign:neutral-runtime",
        now="2026-08-26T10:11:00+00:00",
    )
    invite = activity.issue_customer_invite(actor=actor, now="2026-08-26T10:12:00+00:00")
    claim = activity.claim_customer_invite(
        token=invite.token,
        telegram_user_id=700001,
        username="customer",
        display_name="Клиент",
        now="2026-08-26T10:13:00+00:00",
    )
    return conn, actor, alias.source_token, claim.customer_id


def test_channel_promotion_captures_exact_alias_without_transport_state(monkeypatch) -> None:
    conn, actor, alias_token, customer_id = _promotion_fixture()
    monkeypatch.setattr(promotions, "get_db", lambda: nullcontext(conn))
    try:
        landing = promotions.open_channel_promotion(
            source_token=alias_token,
            business_id=actor.business_id,
            customer_id=customer_id,
        )
        assert landing.attribution_token == alias_token
        event = conn.execute(
            "SELECT source_token,event_type FROM promotion_events WHERE customer_id=?",
            (customer_id,),
        ).fetchone()
        assert event["source_token"] == alias_token
        assert event["event_type"] == "opened"
        touch = conn.execute(
            """
            SELECT ai.source_ref_type,ai.source_ref_id
            FROM attribution_links al
            JOIN acquisition_touches at ON at.id=al.touch_id AND at.business_id=al.business_id
            JOIN attribution_identities ai ON ai.id=at.attribution_identity_id AND ai.business_id=at.business_id
            WHERE al.customer_id=?
            """,
            (customer_id,),
        ).fetchone()
        assert touch["source_ref_type"] == "yandex_direct"
        assert touch["source_ref_id"] == "campaign:neutral-runtime"
        with pytest.raises(PromotionInvariantViolation):
            promotions.open_channel_promotion(
                source_token=alias_token,
                business_id=str(uuid4()),
                customer_id=customer_id,
            )
    finally:
        conn.close()


def test_channel_promotion_resolves_existing_telegram_identity(monkeypatch) -> None:
    conn, actor, alias_token, customer_id = _promotion_fixture()
    monkeypatch.setattr(promotions, "get_db", lambda: nullcontext(conn))
    try:
        first = promotions.open_channel_promotion(
            source_token=alias_token,
            business_id=actor.business_id,
            customer_id=customer_id,
        )
        landing = promotions.open_channel_promotion_for_identity(
            source_token=alias_token,
            business_id=actor.business_id,
            platform="telegram",
            external_subject="700001",
        )
        assert first.customer_id == landing.customer_id == customer_id
        assert landing.attribution_token == alias_token
        assert conn.execute(
            "SELECT COUNT(*) FROM promotion_events WHERE customer_id=? AND event_type='opened'",
            (customer_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM attribution_links WHERE customer_id=?",
            (customer_id,),
        ).fetchone()[0] == 1
    finally:
        conn.close()

def test_channel_promotion_identity_lookup_fails_closed(monkeypatch) -> None:
    conn, actor, alias_token, _customer_id = _promotion_fixture()
    monkeypatch.setattr(promotions, "get_db", lambda: nullcontext(conn))
    try:
        with pytest.raises(
            PromotionInvariantViolation,
            match="active channel customer identity",
        ):
            promotions.open_channel_promotion_for_identity(
                source_token=alias_token,
                business_id=actor.business_id,
                platform="telegram",
                external_subject="999999",
            )
    finally:
        conn.close()
