from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from clientplatform.application.event_announcements import (
    EventAnnouncementDraft,
    draft_event_announcement,
)
from clientplatform.application.events import (
    create_event_in_transaction,
    publish_event_in_transaction,
    register_public_attendee_in_transaction,
    set_event_join_target_in_transaction,
)
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import PlatformRole, TenantContext
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from clientplatform.presentation.event_ui import EventHubAction, event_creation_prompt
from handlers.clientplatform_events import _announcement_share_markup
from services.db.schema import create_or_update_tables


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    create_or_update_tables(conn)
    return conn


def _owner(conn: sqlite3.Connection, *, user_id: int = 701) -> TenantContext:
    access = TenancyRepository(conn).create_business(
        owner_user_id=user_id,
        name="Webinar business",
    )
    return TenancyRepository(conn).resolve_context(
        user_id=user_id,
        business_id=access.business.id,
    )


def _connection(
    conn: sqlite3.Connection,
    actor: TenantContext,
    *,
    platform: str,
    connection_type: str,
    external_account_id: str,
) -> str:
    connection_id = str(uuid4())
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    purpose = "smtp_credentials" if platform == "email" else "provider_token"
    conn.execute(
        """
        INSERT INTO connections(
            id,business_id,platform,connection_type,external_account_id,
            credential_reference,permissions_json,status,created_by_member_id,
            created_at,updated_at,last_success_at,last_error_at,last_error_code
        ) VALUES(?,?,?,?,?,?,?,'active',?,?,?,NULL,NULL,NULL)
        """,
        (
            connection_id,
            actor.business_id,
            platform,
            connection_type,
            external_account_id,
            f"secret://env/TEST_{purpose.upper()}_{connection_id.replace('-', '')[:8]}",
            "[]",
            actor.membership_id,
            now,
            now,
        ),
    )
    return connection_id


def _published(
    conn: sqlite3.Connection,
    actor: TenantContext,
    *,
    join_url: str | None,
    notification_connection_id: str | None = None,
    hours: int = 30,
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    event = create_event_in_transaction(
        conn,
        actor=actor,
        title="Вебинар без лишней магии",
        description="Практический эфир для зарегистрировавшихся участников.",
        starts_at=now + timedelta(hours=hours),
        timezone_name="Europe/Moscow",
        join_url=join_url,
        notification_connection_id=notification_connection_id,
        now=now,
    )
    return publish_event_in_transaction(
        conn,
        actor=actor,
        event_id=event.id,
        now=now,
    )


def test_webinar_can_publish_before_platform_and_keep_same_registration_identity() -> None:
    conn = _conn()
    actor = _owner(conn)
    event = _published(conn, actor, join_url=None)
    assert event.join_url is None
    assert event.provider_key == "pending"
    assert event.join_is_ready is False

    result = register_public_attendee_in_transaction(
        conn,
        public_slug=event.public_slug,
        name="Новый участник",
        email="new@example.test",
        consent=True,
    )
    token = result.registration.token
    before = conn.execute(
        "SELECT first_join_click_at FROM clientplatform_event_registrations WHERE id=?",
        (result.registration.id,),
    ).fetchone()
    assert before["first_join_click_at"] is None

    updated = set_event_join_target_in_transaction(
        conn,
        actor=actor,
        event_id=event.id,
        join_url="https://zoom.us/j/123456789",
    )
    assert updated.id == event.id
    assert updated.public_slug == event.public_slug
    assert updated.join_is_ready is True
    assert updated.provider_key == "zoom"
    assert updated.join_url == "https://zoom.us/j/123456789"
    stored = conn.execute(
        "SELECT token FROM clientplatform_event_registrations WHERE id=?",
        (result.registration.id,),
    ).fetchone()
    assert stored["token"] == token
    conn.close()


def test_registration_queues_email_telegram_vk_and_max_when_identity_is_known() -> None:
    conn = _conn()
    actor = _owner(conn, user_id=702)
    email_connection = _connection(
        conn,
        actor,
        platform="email",
        connection_type="email_smtp",
        external_account_id="mail@example.test",
    )
    for platform, connection_type in (
        ("telegram", "telegram_managed_bot"),
        ("vk", "vk_community"),
        ("max", "max_shared_bot"),
    ):
        _connection(
            conn,
            actor,
            platform=platform,
            connection_type=connection_type,
            external_account_id=f"{platform}-business",
        )

    customers = CustomerRepository(conn)
    customer = customers.create_customer(actor=actor, display_name="Омниканальный лид")
    for platform, subject in (
        ("email", "known@example.test"),
        ("telegram", "100200300"),
        ("vk", "200300400"),
        ("max", "300400500"),
    ):
        customers.attach_identity(
            actor=actor,
            customer_id=customer.id,
            platform=platform,
            external_subject=subject,
            display_name="Омниканальный лид",
        )

    event = _published(
        conn,
        actor,
        join_url="https://stream.example.test/live",
        notification_connection_id=email_connection,
    )
    with patch(
        "clientplatform.application.event_notifications._public_base_url",
        return_value="https://clientplatform.example.test",
    ):
        result = register_public_attendee_in_transaction(
            conn,
            public_slug=event.public_slug,
            name="Омниканальный лид",
            email="known@example.test",
            consent=True,
        )
        replay = register_public_attendee_in_transaction(
            conn,
            public_slug=event.public_slug,
            name="Омниканальный лид",
            email="known@example.test",
            consent=True,
        )

    assert result.customer_id == customer.id
    assert result.notifications.channels == ("email", "telegram", "vk", "max")
    assert result.notifications.queued == 16
    assert replay.created is False
    assert replay.notifications.queued == 0

    rows = conn.execute(
        """
        SELECT platform,idempotency_key,payload_kind
        FROM provider_dispatch_outbox
        WHERE business_id=? AND source_kind='event_message'
        ORDER BY platform,idempotency_key
        """,
        (actor.business_id,),
    ).fetchall()
    assert len(rows) == 16
    counts = {platform: 0 for platform in ("email", "telegram", "vk", "max")}
    for row in rows:
        counts[row["platform"]] += 1
        if row["platform"] == "email":
            assert row["payload_kind"] == "mixed"
            assert ":message:" in row["idempotency_key"]
        else:
            assert row["payload_kind"] == "text"
            assert f":{row['platform']}" in row["idempotency_key"]
    assert counts == {"email": 4, "telegram": 4, "vk": 4, "max": 4}
    conn.close()


def test_personal_notification_never_uses_telegram_channel_connection() -> None:
    conn = _conn()
    actor = _owner(conn, user_id=703)
    _connection(
        conn,
        actor,
        platform="telegram",
        connection_type="telegram_channel",
        external_account_id="public-channel",
    )
    customers = CustomerRepository(conn)
    customer = customers.create_customer(actor=actor, display_name="Channel user")
    customers.attach_identity(
        actor=actor,
        customer_id=customer.id,
        platform="email",
        external_subject="channel@example.test",
    )
    customers.attach_identity(
        actor=actor,
        customer_id=customer.id,
        platform="telegram",
        external_subject="99887766",
    )
    event = _published(conn, actor, join_url="https://stream.example.test/live")
    result = register_public_attendee_in_transaction(
        conn,
        public_slug=event.public_slug,
        name="Channel user",
        email="channel@example.test",
        consent=True,
    )
    assert result.customer_id == customer.id
    assert "telegram" not in result.notifications.channels
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM provider_dispatch_outbox WHERE platform='telegram'"
    ).fetchone()["c"] == 0
    conn.close()


def _announcement_event() -> SimpleNamespace:
    return SimpleNamespace(
        id="11111111-1111-4111-8111-111111111111",
        title="Вебинар о продажах без давления",
        description="Разберём путь клиента и практические сценарии.",
        kind="webinar",
        public_slug="A" * 32,
        local_start_label=lambda: "16.09.2026 19:00",
    )


def test_announcement_falls_back_safely_and_tracks_each_channel() -> None:
    event = _announcement_event()
    config = SimpleNamespace(enabled=False)
    with (
        patch(
            "clientplatform.application.event_announcements._event_for_owner",
            return_value=event,
        ),
        patch(
            "clientplatform.application.event_announcements.SalesAIRuntimeConfig.from_env",
            return_value=config,
        ),
    ):
        draft = asyncio.run(
            draft_event_announcement(
                actor=SimpleNamespace(assert_can_manage_business=lambda: None),
                event_id=event.id,
            )
        )
    assert draft.generated_by == "template"
    assert event.title in draft.text
    assert "16.09.2026 19:00" in draft.text
    assert "Зарегистрируйтесь" in draft.text
    for source in ("telegram", "vk", "max"):
        url = draft.registration_url(
            public_base_url="https://clientplatform.example.test",
            source=source,
        )
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        assert query["source"] == [source]
        assert query["campaign_ref"] == [f"event-announcement:{event.id}"]


def test_announcement_uses_configured_ai_but_never_auto_publishes() -> None:
    event = _announcement_event()
    config = SimpleNamespace(enabled=True, provider="openai", model="gpt-test")
    ai = AsyncMock(return_value="AI-анонс только по подтверждённым фактам")
    with (
        patch(
            "clientplatform.application.event_announcements._event_for_owner",
            return_value=event,
        ),
        patch(
            "clientplatform.application.event_announcements.SalesAIRuntimeConfig.from_env",
            return_value=config,
        ),
        patch(
            "clientplatform.application.event_announcements.generate_bounded_marketing_text",
            ai,
        ),
    ):
        draft = asyncio.run(
            draft_event_announcement(
                actor=SimpleNamespace(assert_can_manage_business=lambda: None),
                event_id=event.id,
            )
        )
    assert draft.text == "AI-анонс только по подтверждённым фактам"
    assert draft.generated_by == "ai:openai:gpt-test"
    ai.assert_awaited_once()


def test_share_buttons_use_explicit_owner_share_intents() -> None:
    markup = _announcement_share_markup(
        text="Текст анонса",
        title="Название",
        telegram_url="https://cp.example/e/tg?source=telegram",
        vk_url="https://cp.example/e/vk?source=vk",
        max_url="https://cp.example/e/max?source=max",
        business_token="abc",
    )
    telegram = markup.inline_keyboard[0][0].url
    vk = markup.inline_keyboard[1][0].url
    max_link = markup.inline_keyboard[2][0].url
    assert telegram and telegram.startswith("https://t.me/share/url?")
    assert vk and vk.startswith("https://vk.com/share.php?")
    assert max_link and max_link.startswith("https://max.ru/:share?text=")
    assert "source%3Dmax" in max_link


def test_native_event_announce_command_is_supported_instead_of_falling_back() -> None:
    from clientplatform.application import native_member_interactions as native

    action = EventHubAction(
        kind="announce",
        label="✨ Сделать анонс",
        key="11111111-1111-4111-8111-111111111111",
    )
    assert native._event_action_command(action) == (
        "cpm:event-announce:11111111-1111-4111-8111-111111111111"
    )

    actor = TenantContext(
        business_id="22222222-2222-4222-8222-222222222222",
        user_id=1,
        membership_id="33333333-3333-4333-8333-333333333333",
        role=PlatformRole.OWNER,
    )
    draft = EventAnnouncementDraft(
        event_id=action.key or "",
        title="Вебинар",
        text="Безопасный анонс",
        generated_by="template",
        public_slug="B" * 32,
    )
    with (
        patch.object(native, "draft_event_announcement_template", return_value=draft),
        patch.object(
            native.settings,
            "MESSENGER_PUBLIC_BASE_URL",
            "https://clientplatform.example.test",
            create=True,
        ),
    ):
        message = native._event_announcement_message(
            actor,
            draft.event_id,
            current_platform=ConnectionPlatform.VK,
        )
    assert "Безопасный анонс" in message.text
    assert "source=vk" in message.text
    assert "campaign_ref=event-announcement%3A" in message.text


def test_creation_prompt_explicitly_allows_join_link_later() -> None:
    prompt = event_creation_prompt("Europe/Moscow")
    assert "добавить позже" in prompt.casefold()
    assert "-" in prompt


def test_registration_issues_only_digest_backed_platform_bound_channel_links() -> None:
    import hashlib

    from clientplatform.application.event_reminder_channels import (
        issue_event_reminder_channel_links_in_transaction,
    )

    conn = _conn()
    actor = _owner(conn, user_id=704)
    event = _published(conn, actor, join_url=None)
    result = register_public_attendee_in_transaction(
        conn,
        public_slug=event.public_slug,
        name="Новый лид",
        email="link-me@example.test",
        consent=True,
    )
    assert result.registration.customer_id is not None
    links = issue_event_reminder_channel_links_in_transaction(
        conn,
        registration=result.registration,
        skip_platforms=("telegram",),
    )
    assert {item.platform for item in links} == {"vk", "max"}
    rows = conn.execute(
        """
        SELECT token_digest,target_platform
        FROM customer_channel_link_tokens
        WHERE business_id=? AND customer_id=?
        ORDER BY target_platform
        """,
        (actor.business_id, result.registration.customer_id),
    ).fetchall()
    assert {row["target_platform"] for row in rows} == {"vk", "max"}
    digests = {row["token_digest"] for row in rows}
    assert digests == {
        hashlib.sha256(item.token.encode("utf-8")).hexdigest() for item in links
    }
    assert not any(item.token in digests for item in links)
    conn.close()


def test_newly_linked_messenger_reconciles_only_future_event_notifications() -> None:
    from clientplatform.application.event_notifications import (
        reconcile_future_event_notifications_for_customer,
    )

    conn = _conn()
    actor = _owner(conn, user_id=705)
    customers = CustomerRepository(conn)
    customer = customers.create_customer(actor=actor, display_name="MAX lead")
    customers.attach_identity(
        actor=actor,
        customer_id=customer.id,
        platform="email",
        external_subject="max-lead@example.test",
    )
    event = _published(conn, actor, join_url="https://stream.example.test/live")
    result = register_public_attendee_in_transaction(
        conn,
        public_slug=event.public_slug,
        name="MAX lead",
        email="max-lead@example.test",
        consent=True,
    )
    assert result.notifications.queued == 0

    _connection(
        conn,
        actor,
        platform="max",
        connection_type="max_shared_bot",
        external_account_id="max-bot",
    )
    customers.attach_identity(
        actor=actor,
        customer_id=customer.id,
        platform="max",
        external_subject="55443322",
    )
    with patch(
        "clientplatform.application.event_notifications._public_base_url",
        return_value="https://clientplatform.example.test",
    ):
        queued = reconcile_future_event_notifications_for_customer(
            conn,
            business_id=actor.business_id,
            customer_id=customer.id,
        )
        replay = reconcile_future_event_notifications_for_customer(
            conn,
            business_id=actor.business_id,
            customer_id=customer.id,
        )
    assert queued == 4
    assert replay == 0
    rows = conn.execute(
        """
        SELECT platform,idempotency_key
        FROM provider_dispatch_outbox
        WHERE business_id=? AND source_id=?
        ORDER BY available_at
        """,
        (actor.business_id, result.registration.id),
    ).fetchall()
    assert len(rows) == 4
    assert {row["platform"] for row in rows} == {"max"}
    assert all(":message:org:v3:" in row["idempotency_key"] for row in rows)
    conn.close()
