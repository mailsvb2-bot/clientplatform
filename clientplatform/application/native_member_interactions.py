from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from clientplatform.application.activity import (
    archive_business_offering,
    disable_business_capability,
    enable_business_capability,
    create_business_offering,
    get_business_profile,
    issue_customer_invite,
    list_business_capabilities,
    save_business_profile,
    list_business_offerings,
)
from clientplatform.application.activity_directions import (
    archive_activity_direction,
    create_activity_direction,
    get_activity_direction,
    list_activity_direction_bindings,
    list_activity_directions,
    restore_activity_direction,
    update_activity_direction,
)
from clientplatform.application.ad_spend_consent import list_ad_spend_authorizations
from clientplatform.application.ad_spend_operations import (
    ad_spend_mutations_enabled,
    queue_ad_spend_launch,
)
from clientplatform.application.acquisition_destination import (
    prepare_nearest_acquisition_destination,
)
from clientplatform.application import admin_ops
from clientplatform.application.ai_commerce import (
    get_commerce_overview,
    request_subscription_expansion,
)
from clientplatform.application.admin_ops import (
    cancel_publication_schedule,
    decode_publication_schedule_version,
    encode_publication_schedule_version,
    format_publication_calendar_lines,
    get_publication_calendar_projection,
    schedule_publication,
)
from clientplatform.application.bookings import create_booking_slot, list_booking_slots
from clientplatform.application.cockpit import cockpit_navigation
from clientplatform.application.cockpit_events import resolve_event_live_snapshot, resolve_events_snapshot
from clientplatform.application.event_announcements import draft_event_announcement_template
from clientplatform.application.event_content_plans import get_event_content_plan
from clientplatform.application.event_followups import (
    get_event_followup_content_plan,
    reset_event_followup_text,
    set_event_followup_text,
)
from clientplatform.application.event_warmups import get_saved_event_warmup_plan
from clientplatform.application.event_owner_flow import (
    OnlineEventCreateRequest,
    create_and_publish_online_event,
)
from clientplatform.application.event_sessions import (
    EventSessionSpec,
    configure_event_sessions,
    get_event_warmup_window,
    list_event_sessions,
)
from clientplatform.application.event_wizard import (
    parse_session_window,
    validate_session_sequence,
)
from clientplatform.application.events import set_event_join_target
from clientplatform.application.event_followup_settings import (
    set_business_event_followup_channel_enabled,
    set_business_event_followup_segment_enabled,
    set_business_event_followups_enabled,
)
from clientplatform.application.capability_parity import (
    CapabilityAvailability,
    project_messenger_capabilities,
)
from clientplatform.application.creative_growth_optimizer import CreativeOptimizationMetric
from clientplatform.application.creative_winner import (
    CreativeWinnerApplyError,
    apply_creative_winner,
    list_creative_trial_page,
    preview_creative_winner,
    resolve_creative_trial_reference,
)
from clientplatform.application.messenger_switching import (
    available_staff_messenger_switches,
    build_staff_switch_command,
)
from clientplatform.application.connections import list_connections
from clientplatform.application.control import (
    business_delivery_summary,
    prepare_native_program_delivery,
)
from clientplatform.application.customer_timeline import (
    format_customer_timeline_lines,
    get_customer_timeline,
)
from clientplatform.application.customers import (
    get_customer,
    list_customers,
    list_customers_with_active_identity,
)
from clientplatform.application.growth_cockpit import (
    GrowthAction,
    acquisition_source_label,
    get_growth_cockpit,
)
from clientplatform.application.owner_input import (
    begin_owner_input,
    clear_owner_input,
    get_owner_input_session,
    resolve_owner_input,
)
from clientplatform.application.native_event_wizard import (
    abandon_native_event_wizard,
    begin_native_event_wizard,
    handle_native_event_wizard_action,
    handle_native_event_wizard_text,
)
from clientplatform.application.programs import (
    add_program_lesson,
    archive_program_draft,
    archive_program_draft_lesson,
    create_program,
    get_program_draft,
    get_program_draft_lesson,
    list_programs,
    move_program_draft_lesson,
    publish_program,
    replace_program_draft_lesson_content,
    update_program_draft_lesson_title,
)
from clientplatform.application.retention import (
    RetentionCandidateUnavailable,
    list_reactivation_opportunities,
    prepare_reactivation_sales_lead,
)
from clientplatform.application.progress import list_business_program_progress
from clientplatform.application.sales_workspace import (
    add_sales_workspace_note,
    assign_sales_workspace_to_actor,
    cancel_sales_workspace_followup,
    claim_sales_workspace_handoff,
    get_sales_workspace_item,
    list_sales_workspace,
    list_sales_workspace_handoffs,
    list_sales_workspace_recent_closed,
    reopen_sales_workspace,
    resolve_sales_workspace_handoff,
    schedule_sales_workspace_followup,
    set_sales_workspace_next_action,
    suppress_sales_workspace_followup,
    transition_sales_workspace,
    unassign_sales_workspace,
)
from clientplatform.application.tenancy import (
    archive_business,
    grant_business_member,
    list_accessible_businesses,
    revoke_business_member,
    resolve_tenant_context,
)
from clientplatform.domain.activity import (
    ActivityError,
    CapabilityStatus,
    OfferingStatus,
    resolve_activity_connector,
)
from clientplatform.domain.activity_directions import (
    ActivityDirectionError,
    ActivityDirectionStatus,
)
from clientplatform.domain.automation_policy import AutomationPolicyError
from clientplatform.domain.ad_spend import AdSpendAuthorizationStatus, AdSpendError
from clientplatform.domain.bookings import BookingError, BookingSlotStatus, parse_local_booking_start
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.event_content import event_content_mode_label
from clientplatform.domain.customer_interactions import (
    CustomerInteractionButton,
    CustomerInteractionMessage,
)
from clientplatform.domain.customers import CustomerPlatform
from clientplatform.domain.messenger_channels import MessengerIngressRoute
from clientplatform.domain.money import settlement_currency_minor_unit_exponent
from clientplatform.domain.owner_input import OwnerInputSession
from clientplatform.domain.programs import ProgramError
from clientplatform.domain.promotions import PromotionChannel, PromotionError
from clientplatform.domain.retention import RetentionCohort
from clientplatform.domain.sales import SalesError, SalesInvariantViolation, SalesLeadStage
from clientplatform.domain.tenancy import (
    PlatformRole,
    TenantAccessDenied,
    TenantContext,
    TenantPermissionDenied,
)
from clientplatform.infrastructure import DispatchOutboxRepository, TenancyRepository
from clientplatform.presentation import owner_navigation as nav
from clientplatform.presentation.booking_schedule_picker import (
    BOOKING_DURATIONS,
    BOOKING_DURATION_LABELS,
    BOOKING_MONTHS_RU_FULL,
    BOOKING_START_TIMES,
    parse_booking_duration,
    parse_booking_start_time,
)
from clientplatform.presentation.event_schedule_picker import (
    MAX_CALENDAR_MONTHS,
    calendar_days,
    local_today,
    month_key,
    parse_calendar_date,
    parse_month_key,
    shift_month,
)
from clientplatform.presentation.owner_quick_menu import (
    build_owner_quick_actions,
    quick_menu_intro,
)
from clientplatform.presentation.event_ui import (
    BACK_TO_EVENTS_LABEL,
    BACK_TO_GROWTH_LABEL,
    EVENT_CREATION_INPUT_GUIDANCE,
    EventHubAction,
    event_creation_failure_text,
    event_creation_prompt,
    event_creation_success_text,
    event_hub_actions,
    event_hub_text,
    event_settings_actions,
    event_settings_text,
)
from config.settings import settings
from services.accounts.identity import resolve_account_for_identity
from services.db import get_db, get_db_ro
from services.messenger.bridge import (
    consume_bridge_token_and_link,
    resolve_bridge_token,
)
from services.messenger.entrypoints import parse_start_payload


log = logging.getLogger(__name__)

_SUPPORT_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.SUPPORT,
    }
)
_MARKETING_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.MARKETER,
        PlatformRole.ANALYST,
    }
)
_CONTENT_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.CONTENT_MANAGER,
        PlatformRole.MARKETER,
    }
)
_AUTOMATION_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.MARKETER,
    }
)
_ACQUISITION_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.CONTENT_MANAGER,
        PlatformRole.MARKETER,
    }
)
_PROMOTION_ANALYTICS_ROLES = _ACQUISITION_ROLES | {PlatformRole.ANALYST}
_BOOKING_MANAGEMENT_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.CONTENT_MANAGER,
    }
)
_PROGRAM_MANAGEMENT_ROLES = frozenset(
    {
        PlatformRole.OWNER,
        PlatformRole.ADMINISTRATOR,
        PlatformRole.MANAGER,
        PlatformRole.CONTENT_MANAGER,
    }
)
_CONNECTION_ROLES = frozenset({PlatformRole.OWNER, PlatformRole.ADMINISTRATOR})
_OWNER_ROLES = frozenset({PlatformRole.OWNER})
_ROLE_LABELS = {
    PlatformRole.OWNER: "Владелец",
    PlatformRole.ADMINISTRATOR: "Администратор",
    PlatformRole.MANAGER: "Менеджер",
    PlatformRole.CONTENT_MANAGER: "Контент-менеджер",
    PlatformRole.MARKETER: "Маркетолог",
    PlatformRole.ANALYST: "Аналитик",
    PlatformRole.SUPPORT: "Поддержка",
}
_PROFILE_STATUS_LABELS = {"draft": "нужно заполнить", "ready": "готов"}
_CUSTOMER_STATUS_LABELS = {"active": "активен", "archived": "в архиве"}
_CUSTOMER_PLATFORM_LABELS = {
    "telegram": "Telegram",
    "vk": "ВКонтакте",
    "max": "MAX",
    "email": "Email",
    "phone": "Телефон",
    "web": "Сайт",
    "internal": "ClientPlatform",
}
_MEMBER_ROLE_CODES = {
    "manager": PlatformRole.MANAGER,
    "content": PlatformRole.CONTENT_MANAGER,
    "marketing": PlatformRole.MARKETER,
    "analytics": PlatformRole.ANALYST,
    "support": PlatformRole.SUPPORT,
}
_ALIASES = {
    "start": "menu",
    "/start": "menu",
    "меню": "menu",
    "кабинет": "menu",
    "админ": "menu",
    "/admin": "menu",
    "работа": "work",
    "рост": "growth",
    "управление": "manage",
    "команда": "team",
    "сегодня": "today",
    "клиенты": "customers",
    "записи": "bookings",
    "программы": "programs",
    "направления": "directions",
    "направления деятельности": "directions",
    "мессенджеры": "messengers",
    "обращения": "sales",
    "продажи": "sales",
    "вебинар": "events",
    "онлайн-мероприятие": "events",
    "обращения и продажи": "sales",
}
_COMMAND_PREFIX = "cpm:"

# Semantic parity contract: transport-specific Telegram steps map to one or more
# native actions, while both surfaces mutate/read the same application entities.
TELEGRAM_NATIVE_ACTION_EQUIVALENTS: dict[str, tuple[str, ...]] = {
    "today": ("today",),
    "today-full": ("today-full",),
    "customers": ("customers",),
    "customer-list": ("customers",),
    "customer": ("customer",),
    "behavior": ("behavior",),
    "attention": ("attention",),
    "sales": ("sales",),
    "messengers": ("messengers",),
    "messenger-connect": ("connect-telegram", "connect-vk", "connect-max"),
    "autopilot": ("autopilot",),
    "autopilot-toggle": ("autopilot-enable", "autopilot-disable"),
    "automation-approve": ("automation-approve",),
    "automation-reject": ("automation-reject",),
    "automation-revoke": ("automation-revoke",),
    "publications": ("publications",),
    "publication-new": ("publication-new", "publication-new-text"),
    "publication-channel": ("publication-new-text",),
    "publication-schedule": ("publication-schedule", "publication-schedule-text"),
    "publication-cancel": ("publication-cancel",),
    "publication-cancel-ok": ("publication-cancel-ok",),
    "publication-publish": ("publication-publish",),
    "funnel": ("funnel",),
    "money": ("money",),
    "payments": ("payments",),
    "payment-new": ("payment-new", "payment-new-text"),
    "payment-customer": ("payment-new-text",),
    "pay-customer": ("payment-new-text",),
    "pay-offer": ("payment-new-text",),
    "pay-refund": ("pay-refund",),
    "pay-refund-ok": ("pay-refund-ok",),
    "segments": ("segments",),
    "offers": ("offers",),
    "copy": ("copy", "activity-edit-help", "activity-edit-text"),
    "directions": (
        "directions",
        "direction",
        "direction-new",
        "direction-create-text",
        "direction-edit",
        "direction-edit-text",
        "direction-archive",
        "direction-archive-ok",
        "direction-restore",
    ),
    "prices": ("prices",),
    "price-set": ("price-set", "price-set-text"),
    "promotion": ("acquire",),
    "online-event": (
        "events",
        "event-new",
        "event-edit",
        "event-edit-text",
        "event-wizard",
        "event-create-text",
    ),
    "experiments": ("experiments",),
    "invites": ("invites", "invite-new"),
    "funnel2": ("funnel2",),
    "retention": ("retention",),
    "release": ("release",),
    "recent": ("recent",),
    "system": ("system",),
    "alerts-refresh": ("system",),
    "formats": ("formats",),
    "formats-edit": ("format-enable", "format-disable"),
    "tariff": ("tariff",),
    "tariff-upgrade": ("tariff-upgrade",),
    "add-member": ("member-add-help", "member-add-text"),
    "add-role": ("member-add-help", "member-add-text"),
    "members": ("members",),
    "member": ("member",),
    "member-role": ("member-role",),
    "member-revoke": ("member-revoke",),
    "permissions": ("permissions",),
}
SIMPLE_OWNER_NATIVE_INTENT_EQUIVALENTS: dict[str, tuple[str, ...]] = {
    "clients": ("customers",),
    "programs": (
        "programs",
        "program-create",
        "program-draft",
        "program-lessons",
        "program-lesson",
        "program-lesson-detail",
        "program-lesson-title-edit",
        "program-lesson-content-edit",
        "program-lesson-move",
        "program-lesson-archive",
        "program-draft-archive",
        "program-publish",
        "program-deliver",
    ),
    "booking": (
        "bookings",
        "booking-open",
        "booking-open-for",
        "booking-months",
        "booking-dates",
        "booking-date",
        "booking-times",
        "booking-time",
        "booking-duration",
    ),
    "results": ("today",),
    "customer-invite": ("invites", "invite-new"),
    "offerings": ("offers", "offering-new"),
    "advanced": ("menu-all",),
}


NativeSetupCommandIssuer = Callable[
    [TenantContext, ConnectionPlatform, str],
    str,
]


class NativeMemberBridgeRejected(RuntimeError):
    """A member link command cannot be admitted into this business."""


@dataclass(frozen=True, slots=True)
class NativeMemberResolution:
    actor: TenantContext
    account_id: int
    linked: bool = False


@dataclass(frozen=True, slots=True)
class ParsedMemberInteraction:
    action: str
    args: tuple[str, ...] = ()


def _compact(value: object) -> str:
    return " ".join(
        str(value or "").strip().casefold().replace("ё", "е").split()
    )


def _bridge_token(value: object) -> str | None:
    raw = " ".join(str(value or "").strip().split())
    lowered = raw.casefold()
    payload = raw
    if lowered.startswith("/start ") or lowered.startswith("start "):
        payload = raw.split(maxsplit=1)[1].strip()
    parsed = parse_start_payload(payload)
    if parsed.kind != "bridge" or not parsed.value:
        return None
    return parsed.value


def _active_customer_identity_exists(
    *,
    business_id: str,
    platform: str,
    external_subject: str,
) -> bool:
    with get_db_ro() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM customer_identities ci
            JOIN customers c
              ON c.id=ci.customer_id AND c.business_id=ci.business_id
             AND c.status='active'
            WHERE ci.business_id=? AND ci.platform=?
              AND ci.external_subject=? AND ci.status='active'
            LIMIT 1
            """,
            (business_id, platform, external_subject),
        ).fetchone()
    return row is not None


def _member_context(account_id: int, business_id: str) -> TenantContext | None:
    try:
        return resolve_tenant_context(user_id=int(account_id), business_id=business_id)
    except TenantAccessDenied:
        return None


def resolve_native_member(
    *,
    route: MessengerIngressRoute,
    external_subject: str,
    raw_text: object,
    display_name: str | None = None,
) -> NativeMemberResolution | None:
    """Resolve staff before Customer creation, optionally consuming an explicit bridge."""

    subject = str(external_subject or "").strip()
    if not subject:
        return None
    platform = route.platform.value

    existing_account = resolve_account_for_identity(
        platform,
        subject,
        display_name=display_name,
        allow_create=False,
    )
    if existing_account is not None:
        actor = _member_context(int(existing_account), route.business_id)
        if actor is not None:
            return NativeMemberResolution(
                actor=actor,
                account_id=int(existing_account),
                linked=False,
            )

    token = _bridge_token(raw_text)
    if token is None:
        return None
    if _active_customer_identity_exists(
        business_id=route.business_id,
        platform=platform,
        external_subject=subject,
    ):
        raise NativeMemberBridgeRejected(
            "эта учётная запись уже используется как клиент этого бизнеса"
        )

    preview = resolve_bridge_token(token)
    if preview is None or preview.consumed:
        raise NativeMemberBridgeRejected("ссылка входа устарела или уже использована")
    if preview.target_platform and preview.target_platform != platform:
        raise NativeMemberBridgeRejected("ссылка входа предназначена для другого мессенджера")

    actor = _member_context(int(preview.canonical_user_id), route.business_id)
    if actor is None:
        raise NativeMemberBridgeRejected(
            "ссылка не подтверждает доступ к этому бизнесу"
        )

    consumed = consume_bridge_token_and_link(
        token,
        platform=platform,
        external_user_id=subject,
        display_name=display_name,
    )
    if (
        consumed is None
        or int(consumed.canonical_user_id) != int(preview.canonical_user_id)
    ):
        raise NativeMemberBridgeRejected(
            "ссылка входа уже была использована; создайте новую"
        )

    current = _member_context(int(consumed.canonical_user_id), route.business_id)
    if current is None:
        raise NativeMemberBridgeRejected("доступ сотрудника больше не активен")
    return NativeMemberResolution(
        actor=current,
        account_id=int(consumed.canonical_user_id),
        linked=True,
    )


def parse_native_member_interaction(value: object) -> ParsedMemberInteraction:
    raw = str(value or "").strip()
    note_match = re.fullmatch(
        r"заметка\s+([0-9a-f-]{6,36})\s+(.{1,500})",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if note_match is not None:
        return ParsedMemberInteraction(
            "sales-note-text",
            (note_match.group(1), note_match.group(2).strip()),
        )
    next_match = re.fullmatch(
        r"(?:следующее|следующее\s+действие)\s+([0-9a-f-]{6,36})\s+(.{1,500})",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if next_match is not None:
        return ParsedMemberInteraction(
            "sales-next-text",
            (next_match.group(1), next_match.group(2).strip()),
        )
    close_match = re.fullmatch(
        r"результат\s+([0-9a-f-]{6,36})\s+"
        r"(won|lost|выиграно|потеряно)\s+(.{1,500})",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if close_match is not None:
        raw_stage = close_match.group(2).casefold()
        stage = "won" if raw_stage in {"won", "выиграно"} else "lost"
        return ParsedMemberInteraction(
            "sales-close-text",
            (close_match.group(1), stage, close_match.group(3).strip()),
        )
    followup_match = re.fullmatch(
        r"напомнить\s+([0-9a-f-]{6,36})\s+(1|24|72)\s+(.{1,4000})",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if followup_match is not None:
        return ParsedMemberInteraction(
            "sales-followup-text",
            (
                followup_match.group(1),
                followup_match.group(2),
                followup_match.group(3).strip(),
            ),
        )
    optout_match = re.fullmatch(
        r"не\s+писать\s+([0-9a-f-]{6,36})\s+подтвердить",
        raw,
        flags=re.IGNORECASE,
    )
    if optout_match is not None:
        return ParsedMemberInteraction(
            "sales-followup-optout-text",
            (optout_match.group(1),),
        )
    publication_match = re.fullmatch(
        r"публикация\s+([0-9a-f-]{6,36})\s+"
        r"([0-3][0-9]\.[01][0-9]\.[0-9]{4}\s+[0-2][0-9]:[0-5][0-9])",
        raw,
        flags=re.IGNORECASE,
    )
    if publication_match is not None:
        return ParsedMemberInteraction(
            "publication-schedule-text",
            (publication_match.group(1), publication_match.group(2)),
        )
    booking_match = re.fullmatch(
        r"время\s+([0-9a-f-]{6,36})\s+"
        r"([0-3][0-9]\.[01][0-9]\.[0-9]{4}\s+[0-2][0-9]:[0-5][0-9])"
        r"(?:\s+([1-9][0-9]{0,2}))?",
        raw,
        flags=re.IGNORECASE,
    )
    if booking_match is not None:
        return ParsedMemberInteraction(
            "booking-open-text",
            (
                booking_match.group(1),
                booking_match.group(2),
                booking_match.group(3) or "60",
            ),
        )
    publication_new_match = re.fullmatch(
        r"черновик\s+(telegram|vk|max|other|телеграм|вк|макс|другое)\s*\|\s*(.{1,200})\s*\|\s*(.{1,4000})",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if publication_new_match is not None:
        channel = {
            "telegram": "telegram",
            "телеграм": "telegram",
            "vk": "vk",
            "вк": "vk",
            "max": "max",
            "макс": "max",
            "other": "other",
            "другое": "other",
        }[publication_new_match.group(1).casefold()]
        return ParsedMemberInteraction(
            "publication-new-text",
            (channel, publication_new_match.group(2).strip(), publication_new_match.group(3).strip()),
        )
    payment_match = re.fullmatch(
        r"оплата\s+([0-9]+(?:[.,][0-9]{1,2})?)\s+([A-Za-z]{3})"
        r"(?:\s+([0-9a-f-]{6,36}|-))?(?:\s+([0-9a-f-]{6,36}|-))?"
        r"(?:\s*\|\s*(.{0,500}))?",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if payment_match is not None:
        return ParsedMemberInteraction(
            "payment-new-text",
            (
                payment_match.group(1),
                payment_match.group(2).upper(),
                payment_match.group(3) or "-",
                payment_match.group(4) or "-",
                (payment_match.group(5) or "").strip(),
            ),
        )
    price_match = re.fullmatch(
        r"цена\s+([0-9a-f-]{6,36})\s+([0-9]+(?:[.,][0-9]{1,2})?)\s+([A-Za-z]{3})",
        raw,
        flags=re.IGNORECASE,
    )
    if price_match is not None:
        return ParsedMemberInteraction(
            "price-set-text",
            (price_match.group(1), price_match.group(2), price_match.group(3).upper()),
        )
    member_match = re.fullmatch(
        r"сотрудник\s+([0-9]{1,20})\s+(manager|content|marketing|analytics|support)",
        raw,
        flags=re.IGNORECASE,
    )
    if member_match is not None:
        return ParsedMemberInteraction(
            "member-add-text",
            (member_match.group(1), member_match.group(2).casefold()),
        )

    activity_match = re.fullmatch(
        r"деятельность\s+(.{3,2000})",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if activity_match is not None:
        return ParsedMemberInteraction(
            "activity-edit-text",
            (activity_match.group(1).strip(),),
        )

    program_match = re.fullmatch(
        r"программа\s+(.{1,200})",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if program_match is not None:
        return ParsedMemberInteraction("program-create-text", (program_match.group(1).strip(),))
    lesson_match = re.fullmatch(
        r"урок\s+([0-9a-f-]{6,36})\s+"
        r"(text|link|audio|video|document|image|task|текст|ссылка|аудио|видео|документ|изображение|задание)"
        r"\s*\|\s*(.{1,200})\s*\|\s*(.{1,2048})",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if lesson_match is not None:
        kind = {
            "текст": "text",
            "ссылка": "link",
            "аудио": "audio",
            "видео": "video",
            "документ": "document",
            "изображение": "image",
            "задание": "task",
        }.get(lesson_match.group(2).casefold(), lesson_match.group(2).casefold())
        return ParsedMemberInteraction(
            "program-lesson-text",
            (lesson_match.group(1), kind, lesson_match.group(3).strip(), lesson_match.group(4).strip()),
        )
    delivery_match = re.fullmatch(
        r"выдать\s+([0-9a-f-]{6,36})\s+([0-9a-f-]{6,36})",
        raw,
        flags=re.IGNORECASE,
    )
    if delivery_match is not None:
        return ParsedMemberInteraction(
            "program-deliver-text",
            (delivery_match.group(1), delivery_match.group(2)),
        )
    offering_match = re.fullmatch(
        r"предложение\s+([a-z0-9._-]{1,80})\s*\|\s*(.{1,200})\s*\|\s*(.{1,1000})",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if offering_match is not None:
        return ParsedMemberInteraction(
            "offering-new-text",
            (offering_match.group(1).casefold(), offering_match.group(2).strip(), offering_match.group(3).strip()),
        )

    alias = _ALIASES.get(_compact(raw))
    if alias is not None:
        return ParsedMemberInteraction(alias)
    if raw.casefold().startswith(_COMMAND_PREFIX):
        parts = raw[len(_COMMAND_PREFIX) :].split(":")
        action = parts[0].strip().casefold()
        args = tuple(part.strip() for part in parts[1:] if part.strip())
        if action in {
            "menu",
            "menu-all",
            "work",
            "growth",
            "growth-more",
            "growth-sales",
            "growth-analysis",
            "growth-lifecycle",
            "events",
            "event-settings",
            "event-content",
            "event-edit",
            "event-edit-text",
            "event-content-followups",
            "event-followup-edit",
            "event-followup-reset",
            "event-followup-edit-text",
            "event-followups",
            "event-segment",
            "event-channel",
            "event-new",
            "event-new-dirs",
            "event-new-dir",
            "event-wizard",
            "event-create-text",
            "event-announce",
            "event-conduct",
            "event-join",
            "event-join-text",
            "work-more",
            "manage-more",
            "manage",
            "team",
            "today",
            "today-full",
            "customers",
            "customer",
            "bookings",
            "booking-open",
            "booking-open-for",
            "booking-months",
            "booking-dates",
            "booking-date",
            "booking-times",
            "booking-time",
            "booking-duration",
            "programs",
            "behavior",
            "attention",
            "messengers",
            "autopilot",
            "publications",
            "publication-schedule",
            "publication-schedule-text",
            "publication-cancel",
            "publication-cancel-ok",
            "funnel",
            "money",
            "payments",
            "segments",
            "offers",
            "copy",
            "prices",
            "release",
            "funnel2",
            "retention",
            "recent",
            "system",
            "formats",
            "tariff",
            "tariff-upgrade",
            "members",
            "member",
            "permissions",
            "connect-telegram",
            "connect-vk",
            "connect-max",
            "sales",
            "reactivate",
            "reactivate-approve",
            "ad-spend",
            "ad-spend-launch",
            "sales-recent",
            "sales-lead",
            "sales-actions",
            "sales-result-menu",
            "sales-assign",
            "sales-unassign",
            "sales-stage",
            "sales-note-help",
            "sales-next-help",
            "sales-close-help",
            "sales-reopen",
            "sales-handoffs",
            "sales-handoff-claim",
            "sales-handoff-resolve",
            "sales-followup-menu",
            "sales-followup-help",
            "sales-followup-cancel",
            "sales-followup-optout-help",
            "invites",
            "invite-new",
            "format-enable",
            "format-disable",
            "member-add-help",
            "member-add-role",
            "member-add-text",
            "member-role",
            "member-revoke",
            "autopilot-enable",
            "autopilot-disable",
            "automation-approve",
            "automation-reject",
            "automation-revoke",
            "publication-new",
            "publication-new-for",
            "publication-new-text",
            "publication-publish",
            "payment-new",
            "payment-new-text",
            "pay-refund",
            "pay-refund-ok",
            "price-set",
            "price-set-text",
            "activity-edit-help",
            "activity-edit-text",
            "directions",
            "directions-archived",
            "direction",
            "direction-new",
            "direction-create-text",
            "direction-edit",
            "direction-edit-text",
            "direction-archive",
            "direction-archive-ok",
            "direction-restore",
            "program-create",
            "program-create-dirs",
            "program-create-dir",
            "program-create-text",
            "program-draft",
            "program-draft-archive",
            "program-draft-archive-ok",
            "program-lessons",
            "program-lesson",
            "program-lesson-kind",
            "program-lesson-text",
            "program-lesson-detail",
            "program-lesson-title-edit",
            "program-lesson-title-text",
            "program-lesson-content-edit",
            "program-lesson-content-kind",
            "program-lesson-content-text",
            "program-lesson-move",
            "program-lesson-archive",
            "program-lesson-archive-ok",
            "program-publish",
            "program-deliver",
            "program-deliver-to",
            "program-deliver-text",
            "offering-new",
            "offering-new-for",
            "offering-new-dirs",
            "offering-new-dir",
            "offering-new-text",
            "offering-retire-list",
            "offering-retire",
            "offering-retire-ok",
            "publication-retire-list",
            "publication-retire",
            "publication-retire-ok",
            "business-retire",
            "business-retire-ok",
            "acquire",
            "experiments",
            "experiment",
            "experiment-apply",
        }:
            return ParsedMemberInteraction(action, args)
    return ParsedMemberInteraction("menu")


_NATIVE_MEMBER_TEXT_ENTRY_ACTIONS = frozenset(
    {
        "sales-note-text",
        "sales-next-text",
        "sales-close-text",
        "sales-followup-text",
        "sales-followup-optout-text",
        "publication-schedule-text",
        "booking-open-text",
        "event-create-text",
        "event-join-text",
        "event-followup-edit-text",
        "publication-new-text",
        "payment-new-text",
        "price-set-text",
        "member-add-text",
        "activity-edit-text",
        "program-create-text",
        "program-lesson-text",
        "program-deliver-text",
        "offering-new-text",
    }
)


def recognizes_native_member_interaction(value: object) -> bool:
    """Return whether raw messenger text belongs to the canonical owner grammar.

    The parser intentionally falls back to ``menu`` for unknown input, so global
    owner ingress cannot infer recognition from the parsed action alone. Keep the
    recognition decision beside the canonical grammar to prevent the legacy VK/MAX
    runtime from swallowing supported owner mutations.
    """

    raw = str(value or "").strip()
    if not raw:
        return False
    if _compact(raw) in _ALIASES:
        return True
    if raw.casefold().startswith(_COMMAND_PREFIX):
        return True
    return parse_native_member_interaction(raw).action in _NATIVE_MEMBER_TEXT_ENTRY_ACTIONS


def _pending_owner_input(
    actor: TenantContext,
    *,
    platform: ConnectionPlatform,
    surface: str,
    raw_text: object,
) -> tuple[ParsedMemberInteraction, OwnerInputSession | None]:
    raw = str(raw_text or "").strip()
    compact = " ".join(raw.split())
    session = get_owner_input_session(
        user_id=actor.user_id, platform=platform.value, surface=surface
    )
    if compact.casefold() in {"отмена", "cancel"} and session is not None:
        if session.action == "online_event":
            abandon_native_event_wizard(
                actor,
                platform=platform,
                surface=surface,
            )
        else:
            clear_owner_input(
                user_id=actor.user_id, platform=platform.value, surface=surface
            )
        return ParsedMemberInteraction("owner-input-cancelled", (session.action,)), None
    if compact.startswith(("cpm:", "cpw:", "/")):
        if (
            session is not None
            and session.action == "online_event"
            and compact.startswith("cpm:event-wizard:")
        ):
            return parse_native_member_interaction(raw), None
        if session is not None:
            if session.action == "online_event":
                abandon_native_event_wizard(
                    actor,
                    platform=platform,
                    surface=surface,
                )
            else:
                clear_owner_input(
                    user_id=actor.user_id, platform=platform.value, surface=surface
                )
        return parse_native_member_interaction(raw), None
    if session is None:
        return parse_native_member_interaction(raw), None
    if session.business_id != actor.business_id:
        clear_owner_input(
            user_id=actor.user_id, platform=platform.value, surface=surface
        )
        return parse_native_member_interaction(raw), None
    try:
        resolved = resolve_owner_input(session, raw)
    except ValueError:
        return ParsedMemberInteraction("owner-input-invalid", (session.action,)), session
    return ParsedMemberInteraction(resolved.action, resolved.args), session


def _owner_input_invalid_message(action: str) -> CustomerInteractionMessage:
    guidance = {
        "activity_description": "Напишите новое описание обычным сообщением.",
        "activity_direction_create": "Напишите: Название | Короткое описание направления.",
        "activity_direction_edit": "Напишите: Новое название | Новое описание направления.",
        "program_title": "Напишите только название материала или программы.",
        "program_lesson": "Напишите: Название | Материал.",
        "program_lesson_title_edit": "Напишите новое название урока.",
        "program_lesson_content_edit": "Пришлите новый материал урока.",
        "publication_draft": "Напишите: Заголовок | Текст публикации.",
        "booking_time": "Напишите дату и время: ДД.ММ.ГГГГ ЧЧ:ММ. При желании добавьте длительность в минутах.",
        "online_event": "Ответ не подходит текущему шагу вебинара. Используйте показанные кнопки или формат из подсказки.",
        "event_schedule": "Пришлите новое расписание: одна строка на каждый эфир в формате ДД.ММ.ГГГГ ЧЧ:ММ-ЧЧ:ММ.",
        "event_warmup_text": "Пришлите новый текст сообщения до вебинара одним сообщением длиной до 3500 символов.",
        "event_followup_text": "Пришлите новый текст дожима одним сообщением длиной до 3500 символов.",
        "event_warmup_days": "Пришлите допустимое число дней сообщений до вебинара.",
        "price": "Напишите сумму и валюту, например: 5000 RUB.",
        "payment": "Напишите сумму и валюту, например: 3500 RUB | консультация.",
        "member_user": "Напишите номер аккаунта ClientPlatform сотрудника — только цифры. Сотрудник увидит свой номер в разделе «Сотрудники и доступы».",
        "offering": "Напишите: Название | Короткое описание.",
    }.get(action, "Проверьте ответ и попробуйте ещё раз.")
    exit_hint = (
        "Чтобы выйти без изменений, отправьте «Отмена» или нажмите «🎥 К вебинарам»."
        if action in {"online_event", "event_schedule", "event_warmup_text", "event_followup_text", "event_warmup_days"}
        else "Чтобы выйти без изменений, отправьте «Отмена»."
    )
    return CustomerInteractionMessage(
        text=f"Не получилось понять ответ.\n\n{guidance}\n\n{exit_hint}",
        rows=(_back_row(),),
    )


def _begin_owner_input_message(
    actor: TenantContext,
    *,
    platform: ConnectionPlatform,
    surface: str,
    action: str,
    text: str,
    context: dict[str, object] | None = None,
    rows: tuple[tuple[CustomerInteractionButton, ...], ...] | None = None,
    append_exit_hint: bool = True,
) -> CustomerInteractionMessage:
    begin_owner_input(
        actor=actor,
        platform=platform.value,
        action=action,
        context=context,
        surface=surface,
    )
    body = text.rstrip()
    if append_exit_hint:
        body += (
            "\n\nЧтобы выйти без изменений, отправьте «Отмена» "
            "или нажмите кнопку возврата."
        )
    return CustomerInteractionMessage(
        text=body,
        rows=rows or (_back_row(),),
    )


def _button(label: str, command: str) -> CustomerInteractionButton:
    return CustomerInteractionButton(label=label[:40], command=command)


def _menu_rows(role: PlatformRole) -> tuple[tuple[CustomerInteractionButton, ...], ...]:
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (_button(nav.WORK.label, "cpm:work"),),
        (_button(nav.MESSENGERS.label, "cpm:messengers"),),
    ]
    if role in (_MARKETING_ROLES | _CONTENT_ROLES | _AUTOMATION_ROLES):
        rows.append((_button(nav.GROWTH.label, "cpm:growth"),))
    if role in _CONNECTION_ROLES:
        rows.append((_button(nav.SETTINGS.label, "cpm:manage"),))
    if role in _OWNER_ROLES:
        rows.append((_button(nav.TEAM.label, "cpm:team"),))
    return tuple(rows)

def _business_name(actor: TenantContext) -> str:
    accesses = list_accessible_businesses(user_id=actor.user_id)
    access = next(
        (
            item
            for item in accesses
            if str(item.business.id) == str(actor.business_id)
        ),
        None,
    )
    return str(access.business.name) if access is not None else "ClientPlatform"


def _native_projection_unavailable_action(actor: TenantContext) -> CustomerInteractionButton:
    if actor.role in _SUPPORT_ROLES:
        return _button("⚠️ Проверить задачи вручную", "cpm:today")
    if actor.role in (_MARKETING_ROLES | _CONTENT_ROLES | _AUTOMATION_ROLES):
        return _button("⚠️ Проверить продвижение вручную", "cpm:growth")
    return _button(nav.PROGRAMS.label, "cpm:programs")


def _native_growth_action_button(
    actor: TenantContext,
    action: GrowthAction,
) -> CustomerInteractionButton | None:
    if action.action_key == "sales_handoff" and actor.role in _SUPPORT_ROLES:
        return _button("🙋 Ответить клиентам", "cpm:sales-handoffs")
    if action.action_key.startswith("sales_plan:") and actor.role in _SUPPORT_ROLES:
        return _button("💬 Продолжить работу с клиентом", "cpm:sales")
    if action.action_key.startswith("sales_lead:") and actor.role in _SUPPORT_ROLES:
        lead_id = action.action_key.split(":", 1)[1]
        return _button("💬 Открыть клиента", f"cpm:sales-lead:{lead_id}")
    if action.action_key == "attribution_review":
        if actor.role in _MARKETING_ROLES:
            return _button("💰 Проверить источники оплат", "cpm:money")
        if actor.role in _SUPPORT_ROLES:
            return _button("📊 Проверить, что происходит", "cpm:today")
    if action.action_key == "economic_reactivation" and actor.role in _SUPPORT_ROLES:
        return _button("♻️ Вернуть клиентов без рекламы", "cpm:reactivate")
    if action.action_key == "economic_open_slots":
        if actor.role in _BOOKING_MANAGEMENT_ROLES:
            return _button("🕒 Добавить время", "cpm:booking-open")
        if actor.role in _ACQUISITION_ROLES:
            return _button("📅 Проверить расписание", "cpm:bookings")
    if action.action_key == "economic_paid_acquisition" and actor.role == PlatformRole.OWNER:
        return _button("💳 Проверить безопасный запуск", "cpm:ad-spend")
    return None


def _native_primary_action(actor: TenantContext) -> CustomerInteractionButton:
    try:
        next_action = get_growth_cockpit(
            actor=actor,
            period_days=7,
            advertising_loader=lambda **_kwargs: None,
        ).next_action
    except (TenantAccessDenied, TenantPermissionDenied, ValueError):
        return _native_projection_unavailable_action(actor)
    except OSError:
        return _native_projection_unavailable_action(actor)
    except RuntimeError:
        return _native_projection_unavailable_action(actor)

    action_button = _native_growth_action_button(actor, next_action)
    if action_button is not None:
        return action_button

    if actor.role in _ACQUISITION_ROLES:
        return _button("🚀 Новые клиенты", "cpm:acquire")
    if actor.role in _SUPPORT_ROLES:
        return _button("📊 Проверить, что происходит", "cpm:today")
    if actor.role in (_MARKETING_ROLES | _CONTENT_ROLES | _AUTOMATION_ROLES):
        return _button(nav.GROWTH.label, "cpm:growth")
    return _button(nav.PROGRAMS.label, "cpm:programs")


def _personalized_menu_message(actor: TenantContext) -> CustomerInteractionMessage | None:
    try:
        profile = get_business_profile(actor=actor)
        capabilities = list_business_capabilities(actor=actor)
    except (ActivityError, TenantPermissionDenied, ValueError):
        return None
    commands = {
        "customers": "cpm:customers",
        "booking": "cpm:bookings",
        "events": "cpm:events",
        "programs": "cpm:programs",
        "acquire": "cpm:acquire",
        "sales": "cpm:sales",
        "results": "cpm:today",
        "all": "cpm:menu-all",
    }
    actions = build_owner_quick_actions(
        activity_description=profile.activity_description,
        capabilities=capabilities,
        role=actor.role,
    )
    return CustomerInteractionMessage(
        text=quick_menu_intro(business_name=_business_name(actor)),
        rows=tuple(
            (_button(action.label, commands[action.key]),)
            for action in actions
        ),
    )


def _menu_message(
    actor: TenantContext,
    *,
    linked: bool,
) -> CustomerInteractionMessage:
    personalized = _personalized_menu_message(actor)
    if personalized is not None:
        heading = (
            "✅ Этот мессенджер подключён к Вашему рабочему аккаунту.\n\n"
            if linked
            else ""
        )
        return CustomerInteractionMessage(
            text=heading + personalized.text,
            rows=personalized.rows,
        )

    heading = (
        "✅ Этот мессенджер подключён к Вашему рабочему аккаунту.\n\n"
        if linked
        else ""
    )
    primary = _native_primary_action(actor)
    return CustomerInteractionMessage(
        text=(
            heading
            + f"🏠 {_business_name(actor)}\n\n"
            + "Выберите нужное действие или откройте полный список возможностей."
        ),
        rows=(
            (primary,),
            (_button(nav.ALL.label, "cpm:menu-all"),),
        ),
    )

def _menu_all_message(actor: TenantContext) -> CustomerInteractionMessage:
    items = [nav.WORK, nav.MESSENGERS]
    if actor.role in (_MARKETING_ROLES | _CONTENT_ROLES | _AUTOMATION_ROLES):
        items.append(nav.GROWTH)
    if actor.role in _CONNECTION_ROLES:
        items.append(nav.SETTINGS)
    if actor.role in _OWNER_ROLES:
        items.append(nav.TEAM)
    return CustomerInteractionMessage(
        text=f"{nav.ALL.label}\n\n" + nav.choice_help(*items),
        rows=(*_menu_rows(actor.role), _back_row()),
    )

def _back_row() -> tuple[CustomerInteractionButton, ...]:
    # Legacy renderer helper. Runtime responses are normalized by
    # ``_with_parent_navigation`` so this row becomes a real parent-level Back
    # button while the direct Home escape remains available where it fits.
    return (_button(nav.HOME.label, "cpm:menu"),)


_NATIVE_PARENT_COMMANDS: dict[str, str] = {
    "menu-all": "cpm:menu",
    "work": "cpm:menu-all",
    "work-more": "cpm:work",
    "today": "cpm:work",
    "today-full": "cpm:work-more",
    "customers": "cpm:work",
    "bookings": "cpm:work",
    "booking-open": "cpm:bookings",
    "booking-duration": "cpm:bookings",
    "programs": "cpm:work",
    "behavior": "cpm:work-more",
    "attention": "cpm:work-more",
    "sales": "cpm:work-more",
    "sales-recent": "cpm:sales",
    "sales-handoffs": "cpm:sales",
    "reactivate": "cpm:growth-lifecycle",
    "ad-spend": "cpm:growth-sales",
    "growth": "cpm:menu-all",
    "growth-sales": "cpm:growth",
    "growth-analysis": "cpm:growth-sales",
    "growth-more": "cpm:growth",
    "growth-lifecycle": "cpm:growth-more",
    "events": "cpm:growth",
    "event-settings": "cpm:events",
    "event-content": "cpm:events",
    "event-edit": "cpm:events",
    "event-edit-text": "cpm:events",
    "event-content-followups": "cpm:events",
    "event-followup-edit": "cpm:events",
    "event-followup-reset": "cpm:events",
    "event-followup-edit-text": "cpm:events",
    "event-followups": "cpm:events",
    "event-segment": "cpm:events",
    "event-channel": "cpm:events",
    "event-new": "cpm:events",
    "event-new-dirs": "cpm:events",
    "event-new-dir": "cpm:events",
    "event-wizard": "cpm:events",
    "event-wizard-title-text": "cpm:events",
    "event-wizard-count-text": "cpm:events",
    "event-wizard-topics-text": "cpm:events",
    "event-wizard-timezone-text": "cpm:events",
    "event-wizard-window-text": "cpm:events",
    "event-wizard-room-text": "cpm:events",
    "event-wizard-warmup-text": "cpm:events",
    "event-we-text": "cpm:events",
    "event-warmup-days-text": "cpm:events",
    "event-create-text": "cpm:events",
    "event-announce": "cpm:events",
    "event-conduct": "cpm:events",
    "event-join": "cpm:events",
    "event-join-text": "cpm:events",
    "acquire": "cpm:growth",
    "experiments": "cpm:growth",
    "autopilot": "cpm:growth",
    "publications": "cpm:growth",
    "funnel": "cpm:growth-analysis",
    "funnel2": "cpm:growth-analysis",
    "money": "cpm:growth-sales",
    "payments": "cpm:growth-sales",
    "segments": "cpm:growth-sales",
    "offers": "cpm:growth-more",
    "copy": "cpm:growth-more",
    "prices": "cpm:growth-more",
    "retention": "cpm:growth-lifecycle",
    "invites": "cpm:growth-lifecycle",
    "manage": "cpm:menu-all",
    "directions": "cpm:manage",
    "directions-archived": "cpm:directions:0",
    "direction": "cpm:directions:0",
    "direction-new": "cpm:directions:0",
    "direction-create-text": "cpm:directions:0",
    "direction-edit": "cpm:directions:0",
    "direction-edit-text": "cpm:directions:0",
    "direction-archive": "cpm:directions:0",
    "direction-archive-ok": "cpm:directions:0",
    "direction-restore": "cpm:directions-archived:0",
    "manage-more": "cpm:manage",
    "release": "cpm:manage",
    "formats": "cpm:manage",
    "tariff": "cpm:manage",
    "tariff-upgrade": "cpm:tariff",
    "recent": "cpm:manage-more",
    "system": "cpm:manage-more",
    "team": "cpm:menu-all",
    "members": "cpm:team",
    "permissions": "cpm:team",
    "member-add-help": "cpm:team",
    "messengers": "cpm:menu-all",
    "connect-telegram": "cpm:messengers",
    "connect-vk": "cpm:messengers",
    "connect-max": "cpm:messengers",
    "activity-edit-help": "cpm:manage",
    "publication-new": "cpm:publications",
    "payment-new": "cpm:payments",
    "price-set": "cpm:prices",
    "offering-new": "cpm:offers",
    "offering-new-for": "cpm:offers",
    "offering-new-dirs": "cpm:offers",
    "offering-new-dir": "cpm:offers",
    "offering-retire-list": "cpm:offers",
    "publication-retire-list": "cpm:publications",
    "business-retire": "cpm:manage-more",
    "program-create": "cpm:programs:0",
    "program-create-dirs": "cpm:programs:0",
    "program-create-dir": "cpm:programs:0",
    "owner-input-invalid": "cpm:menu",
    "owner-input-cancelled": "cpm:menu",
}


def _native_parent_command(parsed: ParsedMemberInteraction) -> str | None:
    action = parsed.action
    args = parsed.args
    if action == "menu":
        return None
    if action in {"owner-input-invalid", "owner-input-cancelled"} and args:
        if args[0] in {"online_event", "event_schedule", "event_warmup_text", "event_followup_text", "event_warmup_days"}:
            return "cpm:events"
        if args[0] in {"activity_direction_create", "activity_direction_edit"}:
            return "cpm:directions:0"
        if args[0] == "program_title":
            return "cpm:programs:0"
        if args[0] == "offering":
            return "cpm:offers"
    if action == "customer":
        return "cpm:customers:0"
    if action in {"booking-open-for", "booking-months"} and args:
        return "cpm:booking-open:0"
    if action == "booking-dates" and len(args) >= 1:
        return f"cpm:booking-months:{args[0]}:0"
    if action == "booking-date" and len(args) == 2:
        try:
            selected_month = args[1][:7].replace("-", "")
        except (AttributeError, IndexError):
            return "cpm:booking-open:0"
        return f"cpm:booking-dates:{args[0]}:{selected_month}:0"
    if action == "booking-times" and len(args) >= 2:
        selected_month = args[1][:7].replace("-", "")
        return f"cpm:booking-dates:{args[0]}:{selected_month}:0"
    if action == "booking-time" and len(args) == 3:
        return f"cpm:booking-times:{args[0]}:{args[1]}:0"
    if action == "booking-open-text":
        return "cpm:bookings"
    if action == "sales-lead":
        return "cpm:sales"
    if action in {
        "sales-actions", "sales-result-menu", "sales-note-help",
        "sales-next-help", "sales-close-help", "sales-followup-menu",
    } and args:
        return f"cpm:sales-lead:{args[0]}"
    if action in {"sales-followup-help", "sales-followup-optout-help"} and args:
        return f"cpm:sales-followup-menu:{args[0]}"
    if action in {"sales-handoff-claim", "sales-handoff-resolve"}:
        return "cpm:sales-handoffs"
    if action in _SALES_TEXT_MUTATION_ACTIONS:
        # Text commands may carry an abbreviated lead reference. The renderer
        # resolves it before mutation; navigation must never reuse the raw
        # abbreviation as an exact lead id. ``_with_parent_navigation``
        # upgrades this safe list fallback from the rendered full-id card.
        return "cpm:sales"
    if action.startswith("sales-") and args:
        return f"cpm:sales-lead:{args[0]}"
    if action == "reactivate-approve":
        return "cpm:reactivate"
    if action == "ad-spend-launch":
        return "cpm:ad-spend"
    if action == "experiment":
        return "cpm:experiments:0"
    if action == "experiment-apply" and args:
        return f"cpm:experiment:{args[0]}:{args[2] if len(args) > 2 else 'b'}"
    if action in {
        "publication-new-for", "publication-new-text", "publication-publish",
        "publication-schedule", "publication-schedule-text",
        "publication-cancel", "publication-cancel-ok",
        "publication-retire", "publication-retire-ok",
    }:
        return "cpm:publications"
    if action in {"payment-new-text", "pay-refund", "pay-refund-ok"}:
        return "cpm:payments"
    if action == "price-set-text":
        return "cpm:prices"
    if action.startswith("automation-") or action in {"autopilot-enable", "autopilot-disable"}:
        return "cpm:autopilot"
    if action == "invite-new":
        return "cpm:invites"
    if action == "member-add-role":
        return "cpm:member-add-help"
    if action == "member-add-text":
        return "cpm:team"
    if action in {"member-role", "member-revoke"} and args:
        return f"cpm:member:{args[0]}"
    if action == "member" and args:
        return "cpm:members:0"
    if action in {"format-enable", "format-disable"}:
        return "cpm:formats:0"
    if action == "activity-edit-text":
        return "cpm:manage"
    if action in {
        "program-create-text",
        "program-publish",
        "program-deliver",
        "program-deliver-to",
        "program-deliver-text",
        "program-draft",
        "program-draft-archive",
        "program-draft-archive-ok",
    }:
        return "cpm:programs:0"
    if action == "program-lessons" and args:
        return f"cpm:program-draft:{args[0]}"
    if action in {
        "program-lesson-detail",
        "program-lesson-title-edit",
        "program-lesson-title-text",
        "program-lesson-content-edit",
        "program-lesson-content-kind",
        "program-lesson-content-text",
        "program-lesson-move",
        "program-lesson-archive",
        "program-lesson-archive-ok",
    }:
        return "cpm:programs:0"
    if action == "program-lesson" and args:
        return "cpm:programs:0"
    if action == "program-lesson-kind" and args:
        return f"cpm:program-lesson:{args[0]}"
    if action == "program-lesson-text":
        return "cpm:programs:0"
    if action == "offering-new-for":
        return "cpm:offering-new"
    if action in {"offering-new-text", "offering-retire", "offering-retire-ok"}:
        return "cpm:offers"
    if action == "business-retire-ok":
        return None
    return _NATIVE_PARENT_COMMANDS.get(action, "cpm:menu")


_SALES_TEXT_MUTATION_ACTIONS = frozenset(
    {
        "sales-note-text",
        "sales-next-text",
        "sales-close-text",
        "sales-followup-text",
        "sales-followup-optout-text",
    }
)


def _resolved_sales_lead_parent(
    message: CustomerInteractionMessage,
) -> str | None:
    prefix = "cpm:sales-actions:"
    for row in message.rows:
        for button in row:
            if button.command.startswith(prefix):
                lead_id = button.command[len(prefix) :]
                if lead_id:
                    return f"cpm:sales-lead:{lead_id}"
    return None


def _with_parent_navigation(
    message: CustomerInteractionMessage,
    parsed: ParsedMemberInteraction,
) -> CustomerInteractionMessage:
    parent_command = _native_parent_command(parsed)
    if parsed.action in _SALES_TEXT_MUTATION_ACTIONS:
        parent_command = _resolved_sales_lead_parent(message) or parent_command
    if parent_command is None:
        return message

    # Remove only old dedicated navigation rows. Action rows and pagination stay
    # untouched. The final row always means navigation, never a business action.
    rows = [
        row
        for row in message.rows
        if not (
            len(row) == 1
            and row[0].command in {"cpm:menu", parent_command}
        )
    ]
    total = sum(len(row) for row in rows)
    if parsed.action == "events":
        back_label = BACK_TO_GROWTH_LABEL
    elif parsed.action in {
        "event-settings", "event-content", "event-content-followups",
        "event-followup-edit", "event-followup-reset", "event-followup-edit-text",
        "event-followups", "event-segment", "event-channel", "event-new", "event-wizard", "event-wizard-title-text",
        "event-wizard-count-text", "event-wizard-topics-text",
        "event-wizard-timezone-text", "event-wizard-window-text", "event-wizard-room-text",
        "event-wizard-warmup-text", "event-we-text",
        "event-warmup-days-text", "event-create-text", "event-announce", "event-conduct", "event-join", "event-join-text",
    } or (
        parsed.action in {"owner-input-invalid", "owner-input-cancelled"}
        and parsed.args
        and parsed.args[0] in {"online_event", "event_warmup_text", "event_followup_text", "event_warmup_days"}
    ):
        back_label = BACK_TO_EVENTS_LABEL
    else:
        back_label = nav.BACK.label
    navigation = [_button(back_label, parent_command)]
    if parent_command != "cpm:menu" and total + 2 <= 10:
        navigation.append(_button(nav.HOME.label, "cpm:menu"))
    rows.append(tuple(navigation))
    return CustomerInteractionMessage(text=message.text, rows=tuple(rows))

def _permission_message() -> CustomerInteractionMessage:
    return CustomerInteractionMessage(
        text="Для Вашей роли этот раздел недоступен.",
        rows=(_back_row(),),
    )


def _stale_message() -> CustomerInteractionMessage:
    return CustomerInteractionMessage(
        text="Эта кнопка уже неактуальна. Откройте нужный раздел заново.",
        rows=(_back_row(),),
    )



def _work_message(actor: TenantContext) -> CustomerInteractionMessage:
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    items: list[nav.OwnerNavItem] = []
    if actor.role in _SUPPORT_ROLES:
        rows.extend(
            [
                (_button(nav.TODAY.label, "cpm:today"),),
                (_button(nav.CUSTOMERS.label, "cpm:customers:0"),),
                (_button(nav.BOOKINGS.label, "cpm:bookings"),),
            ]
        )
        items.extend((nav.TODAY, nav.CUSTOMERS, nav.BOOKINGS))
    rows.append((_button(nav.PROGRAMS.label, "cpm:programs"),))
    items.append(nav.PROGRAMS)
    if actor.role in _SUPPORT_ROLES:
        rows.append((_button(nav.WORK_MORE.label, "cpm:work-more"),))
        items.append(nav.WORK_MORE)
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text="👥 Клиенты и работа\n\n" + nav.choice_help(*items),
        rows=tuple(rows),
    )

def _work_more_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    items = (nav.SALES, nav.TODAY_FULL, nav.BEHAVIOR, nav.ATTENTION)
    return CustomerInteractionMessage(
        text="⋯ Другие действия по работе\n\n" + nav.choice_help(*items),
        rows=(
            (_button(nav.SALES.label, "cpm:sales"),),
            (_button(nav.TODAY_FULL.label, "cpm:today-full"),),
            (_button(nav.BEHAVIOR.label, "cpm:behavior"),),
            (_button(nav.ATTENTION.label, "cpm:attention"),),
            (_button(nav.WORK.label, "cpm:work"),),
            _back_row(),
        ),
    )

_SALES_STAGE_LABELS = {
    "new": "Новое",
    "contacted": "Связались",
    "qualified": "Подтверждён интерес",
    "checkout": "Оформление",
    "won": "Оплатил / выиграно",
    "lost": "Потеряно",
}
_SALES_SOURCE_LABELS = {
    "telegram": "Telegram",
    "vk": "ВКонтакте",
    "max": "MAX",
    "website": "Сайт",
    "yandex_direct": "Яндекс Директ",
    "referral": "Рекомендации",
    "manual": "Вручную",
}
_HANDOFF_REASON_LABELS = {
    "explicit_request": "Клиент попросил человека",
    "low_confidence": "Нужна ручная проверка",
    "sensitive_context": "Требуется личное внимание",
    "pricing_exception": "Нестандартные условия",
    "negative_sentiment": "Клиент недоволен",
    "repeated_failure": "Автоматический сценарий не справился",
}
_HANDOFF_SEVERITY_LABELS = {"urgent": "🔴 Срочно", "high": "🟠 Важно", "normal": "🟢 Обычно"}
_FOLLOWUP_CHANNELS = frozenset({"telegram", "vk", "max"})


def _sales_reference_item(actor: TenantContext, reference: str) -> dict[str, Any]:
    needle = str(reference or "").strip().casefold()
    if len(needle) < 6:
        raise ValueError("sales lead reference is too short")
    items = [
        *list_sales_workspace(actor=actor, limit=50),
        *list_sales_workspace_recent_closed(actor=actor, limit=50),
    ]
    matches = [
        item
        for item in items
        if str(item.get("id") or "").casefold().startswith(needle)
    ]
    if len(matches) != 1:
        raise ValueError("sales lead reference is missing or ambiguous")
    return matches[0]


def _sales_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    items = list_sales_workspace(actor=actor, limit=6)
    handoffs = list_sales_workspace_handoffs(actor=actor, limit=1)
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    lines = ["Обращения и продажи", ""]
    if handoffs:
        rows.append((_button("🙋 Нужно подключиться", "cpm:sales-handoffs"),))
    for item in items:
        lead_id = str(item.get("id") or "")
        customer = str(item.get("customer_name") or "Клиент")
        stage = _SALES_STAGE_LABELS.get(str(item.get("stage") or ""), "В работе")
        owner = item.get("assigned_user_id")
        owner_text = f"ответственный {owner}" if owner is not None else "без ответственного"
        lines.append(f"• {customer} · {stage} · {owner_text} · {lead_id[:8]}")
        rows.append((_button(customer[:32], f"cpm:sales-lead:{lead_id}"),))
    if not items:
        lines.append("Открытых обращений сейчас нет.")
    lines.extend(["", "Откройте клиента — ClientPlatform покажет главное действие и все дополнительные возможности."])
    rows.append((_button("♻️ Вернуть клиентов", "cpm:reactivate"),))
    rows.append((_button("🗂 Недавно закрытые", "cpm:sales-recent"),))
    rows.append((_button("📊 Работа", "cpm:work"),))
    return CustomerInteractionMessage(text="\n".join(lines), rows=tuple(rows))


def _reactivation_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    opportunities = list_reactivation_opportunities(actor=actor, limit=6)
    lines = [
        "♻️ Вернуть клиентов",
        "",
        "Только подтверждённая история покупок и активности. Ничего не отправляется автоматически.",
    ]
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    for index, item in enumerate(opportunities, start=1):
        candidate = item.candidate
        route = item.route_platform or "ручная работа"
        lines.append(
            f"{index}. {candidate.display_name or 'Клиент'} · без активности {candidate.inactive_days} дн. · канал: {route}"
        )
        rows.append((
            _button(
                f"✅ Взять в работу {index}",
                f"cpm:reactivate-approve:{candidate.customer_id}:{candidate.cohort.value}",
            ),
        ))
    if not opportunities:
        lines.append("Сейчас нет клиентов, которых можно обоснованно предложить для возврата.")
    rows.append((_button("💬 К обращениям", "cpm:sales"),))
    return CustomerInteractionMessage(text="\n".join(lines), rows=tuple(rows))


def _reactivation_approve_message(
    actor: TenantContext,
    customer_id: str,
    cohort: str,
) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    try:
        prepared = prepare_reactivation_sales_lead(
            actor=actor,
            customer_id=customer_id,
            expected_cohort=RetentionCohort(cohort),
        )
    except (RetentionCandidateUnavailable, SalesInvariantViolation, ValueError):
        return _stale_message()
    route = prepared.route_platform or "ручная работа"
    return CustomerInteractionMessage(
        text=(
            "Клиент добавлен в обращения.\n\n"
            f"Канал: {route}. Клиенту ничего не отправлено — отправка по-прежнему требует отдельного подтверждения."
        ),
        rows=(
            (_button("💬 Открыть клиента", f"cpm:sales-lead:{prepared.lead.id}"),),
            (_button("♻️ К возврату клиентов", "cpm:reactivate"),),
        ),
    )


def _native_minor_amount_text(amount_minor: int, currency: str) -> str:
    exponent = settlement_currency_minor_unit_exponent(currency)
    amount = Decimal(int(amount_minor)) / (Decimal(10) ** exponent)
    amount_text = f"{amount:,.{exponent}f}" if exponent else f"{amount:,.0f}"
    return f"{amount_text.replace(',', ' ')} {str(currency).upper()}"


def _ad_spend_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role != PlatformRole.OWNER:
        return _permission_message()
    authorizations = list_ad_spend_authorizations(actor=actor, limit=8)
    launch_enabled = ad_spend_mutations_enabled()
    lines = [
        "💳 Безопасный запуск рекламы",
        "",
        "Здесь видны только уже подтверждённые владельцем лимиты. Новый бюджет или новое согласие не создаются.",
    ]
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    for index, item in enumerate(authorizations[:6], start=1):
        lines.append(
            f"{index}. {item.status.value} · общий "
            f"{_native_minor_amount_text(item.hard_cap_minor, item.currency)} · день "
            f"{_native_minor_amount_text(item.daily_cap_minor, item.currency)}"
        )
        if item.status == AdSpendAuthorizationStatus.AUTHORIZED and launch_enabled:
            rows.append((
                _button(
                    f"🚀 Запустить разрешение {index}",
                    f"cpm:ad-spend-launch:{item.id}",
                ),
            ))
    if not authorizations:
        lines.append("Разрешений на платный запуск сейчас нет.")
    elif not launch_enabled:
        lines.append("Запуск сейчас выключен операторским kill switch; существующие лимиты не расходуются.")
    rows.append((_button("📈 Рост", "cpm:growth"),))
    return CustomerInteractionMessage(text="\n".join(lines), rows=tuple(rows))


def _ad_spend_launch_message(actor: TenantContext, authorization_id: str) -> CustomerInteractionMessage:
    if actor.role != PlatformRole.OWNER:
        return _permission_message()
    try:
        operation = queue_ad_spend_launch(
            actor=actor,
            authorization_id=authorization_id,
        )
    except AdSpendError:
        return CustomerInteractionMessage(
            text="Запуск запрещён, устарел или состояние разрешения изменилось. Ничего не потрачено.",
            rows=((_button("💳 К разрешениям", "cpm:ad-spend"),),),
        )
    except RuntimeError:
        return CustomerInteractionMessage(
            text="Безопасный запуск сейчас недоступен. Ничего не потрачено.",
            rows=((_button("💳 К разрешениям", "cpm:ad-spend"),),),
        )
    except ValueError:
        return _stale_message()
    return CustomerInteractionMessage(
        text=(
            "🚀 Запуск поставлен в защищённую идемпотентную очередь.\n\n"
            "Перед обращением к рекламному провайдеру сервер ещё раз проверит кабинет, "
            "срок, расход и точные подтверждённые лимиты. При расхождении запуск будет заблокирован.\n\n"
            f"Операция: …{operation.id[-12:]}"
        ),
        rows=((_button("💳 К разрешениям", "cpm:ad-spend"),),),
    )


def _sales_recent_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    items = list_sales_workspace_recent_closed(actor=actor, limit=7)
    lines = ["Недавно закрытые обращения", ""]
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    for item in items:
        lead_id = str(item.get("id") or "")
        customer = str(item.get("customer_name") or "Клиент")
        stage = _SALES_STAGE_LABELS.get(str(item.get("stage") or ""), "Закрыто")
        lines.append(f"• {customer} · {stage} · {lead_id[:8]}")
        rows.append((_button(customer[:32], f"cpm:sales-lead:{lead_id}"),))
    if not items:
        lines.append("Недавно закрытых обращений нет.")
    rows.append((_button("💬 К обращениям", "cpm:sales"),))
    rows.append(_back_row())
    return CustomerInteractionMessage(text="\n".join(lines), rows=tuple(rows))


def _sales_handoffs_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    items = list_sales_workspace_handoffs(actor=actor, limit=4)
    lines = ["Нужно подключиться лично", ""]
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    for index, item in enumerate(items, start=1):
        handoff_id = str(item.get("id") or "")
        status = "взято в работу" if str(item.get("status") or "") == "claimed" else "ожидает"
        reason = _HANDOFF_REASON_LABELS.get(str(item.get("reason") or ""), "Нужно личное внимание")
        severity = _HANDOFF_SEVERITY_LABELS.get(str(item.get("severity") or ""), "🟢 Обычно")
        lines.append(f"{index}. {item.get('customer_name') or 'Клиент'} · {severity} · {reason} · {status}")
        actions: list[CustomerInteractionButton] = []
        if str(item.get("status") or "") == "open":
            actions.append(_button(f"✋ Взять {index}", f"cpm:sales-handoff-claim:{handoff_id}"))
        actions.append(_button(f"✅ Готово {index}", f"cpm:sales-handoff-resolve:{handoff_id}"))
        rows.append(tuple(actions))
    if not items:
        lines.append("Сейчас нет обращений, где требуется личное участие.")
    rows.append((_button("💬 К обращениям", "cpm:sales"),))
    rows.append(_back_row())
    return CustomerInteractionMessage(text="\n".join(lines), rows=tuple(rows))


def _sales_primary_button(
    actor: TenantContext,
    item: dict[str, Any],
    lead_id: str,
) -> CustomerInteractionButton | None:
    stage = str(item.get("stage") or "")
    assigned_user = item.get("assigned_user_id")
    if stage == "lost":
        return _button("↩️ Вернуть в работу", f"cpm:sales-reopen:{lead_id}")
    if stage == "won":
        return _button("📝 Добавить заметку", f"cpm:sales-note-help:{lead_id}")
    if assigned_user is None or int(assigned_user) != int(actor.user_id):
        return _button("👤 Взять обращение", f"cpm:sales-assign:{lead_id}")
    if stage == "new":
        return _button("✅ Я связался", f"cpm:sales-stage:{lead_id}:contacted")
    if stage == "contacted":
        return _button("👍 Клиент заинтересован", f"cpm:sales-stage:{lead_id}:qualified")
    if stage == "qualified":
        return _button("🧾 Перейти к оформлению", f"cpm:sales-stage:{lead_id}:checkout")
    if stage == "checkout":
        return _button("🏁 Указать результат", f"cpm:sales-result-menu:{lead_id}")
    return _button("➡️ Указать следующий шаг", f"cpm:sales-next-help:{lead_id}")


def _sales_lead_message(actor: TenantContext, lead_id: str) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    item = get_sales_workspace_item(actor=actor, lead_id=lead_id)
    if item is None:
        return _stale_message()
    stage = str(item.get("stage") or "")
    assigned_user = item.get("assigned_user_id")
    source_key = str(item.get("attribution_source") or item.get("source_kind") or "").strip().lower()
    source = _SALES_SOURCE_LABELS.get(source_key, "Источник не определён")
    if assigned_user is None:
        owner_text = "не назначен"
    elif int(assigned_user) == int(actor.user_id):
        owner_text = "Вы"
    else:
        owner_text = "другой сотрудник"
    lines = [
        f"💬 {item.get('customer_name') or 'Клиент'}", "",
        f"Стадия: {_SALES_STAGE_LABELS.get(stage, stage or 'В работе')}",
        f"Ответственный: {owner_text}",
        f"Следующий шаг: {item.get('next_action') or 'ClientPlatform подскажет по ходу работы'}",
        f"Срок: {item.get('due_at') or 'не задан'}",
        f"Источник: {source}",
    ]
    if item.get("followup_suppressed"):
        lines.append("Напоминания клиенту: отключены по его просьбе")
    elif item.get("active_followup_id"):
        lines.append(
            f"Напоминание клиенту: {item.get('active_followup_scheduled_at') or 'запланировано'}"
        )
    if item.get("closure_reason"):
        lines.append(f"Причина результата: {item['closure_reason']}")
    if item.get("next_plan_id"):
        approval = "нужно Ваше подтверждение" if item.get("next_plan_requires_approval") else "подтверждение не требуется"
        lines.append(f"ClientPlatform подготовил следующий шаг · {approval}")

    rows: list[tuple[CustomerInteractionButton, ...]] = []
    primary = _sales_primary_button(actor, item, lead_id)
    if primary is not None:
        rows.append((primary,))
    rows.append((_button("⋯ Другие действия", f"cpm:sales-actions:{lead_id}"),))
    rows.append((_button("💬 К обращениям", "cpm:sales"),))
    return CustomerInteractionMessage(text="\n".join(lines), rows=tuple(rows))


def _sales_actions_message(actor: TenantContext, lead_id: str) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    item = get_sales_workspace_item(actor=actor, lead_id=lead_id)
    if item is None:
        return _stale_message()
    stage = str(item.get("stage") or "")
    assigned_user = item.get("assigned_user_id")
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if stage not in {"won", "lost"}:
        if assigned_user is not None and int(assigned_user) == int(actor.user_id):
            rows.append((_button("👤 Снять ответственного", f"cpm:sales-unassign:{lead_id}"),))
        else:
            rows.append((_button("👤 Взять себе", f"cpm:sales-assign:{lead_id}"),))
        rows.append((
            _button("Связались", f"cpm:sales-stage:{lead_id}:contacted"),
            _button("Интерес", f"cpm:sales-stage:{lead_id}:qualified"),
            _button("Оформление", f"cpm:sales-stage:{lead_id}:checkout"),
        ))
        rows.append((
            _button("📝 Заметка", f"cpm:sales-note-help:{lead_id}"),
            _button("➡️ Следующее", f"cpm:sales-next-help:{lead_id}"),
        ))
        rows.append((
            _button("✅ Оплатил", f"cpm:sales-close-help:{lead_id}:won"),
            _button("❌ Не состоялось", f"cpm:sales-close-help:{lead_id}:lost"),
        ))
        followup_source = str(item.get("source_kind") or "").strip().lower()
        followup_allowed = (
            followup_source in _FOLLOWUP_CHANNELS
            and str(item.get("contact_basis") or "") != "none"
            and not bool(item.get("followup_suppressed"))
        )
        if item.get("active_followup_id") or followup_allowed:
            rows.append((_button("✉️ Напомнить клиенту", f"cpm:sales-followup-menu:{lead_id}"),))
    else:
        rows.append((_button("📝 Заметка", f"cpm:sales-note-help:{lead_id}"),))
        if stage == "lost":
            rows.append((_button("↩️ Вернуть в работу", f"cpm:sales-reopen:{lead_id}"),))
    rows.append((_button("← К клиенту", f"cpm:sales-lead:{lead_id}"),))
    return CustomerInteractionMessage(
        text="Другие действия\n\nЗдесь сохранены все дополнительные возможности работы с обращением.",
        rows=tuple(rows),
    )


def _sales_result_message(actor: TenantContext, lead_id: str) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    item = get_sales_workspace_item(actor=actor, lead_id=lead_id)
    if item is None or str(item.get("stage") or "") in {"won", "lost"}:
        return _stale_message()
    return CustomerInteractionMessage(
        text="Чем закончилось обращение?\n\nВыберите результат. Затем ClientPlatform попросит коротко указать причину.",
        rows=(
            (
                _button("✅ Клиент оплатил", f"cpm:sales-close-help:{lead_id}:won"),
                _button("❌ Не состоялось", f"cpm:sales-close-help:{lead_id}:lost"),
            ),
            (_button("← К клиенту", f"cpm:sales-lead:{lead_id}"),),
        ),
    )


def _sales_input_help(lead_id: str, kind: str, stage: str | None = None) -> CustomerInteractionMessage:
    short_id = str(lead_id)[:8]
    if kind == "note":
        example = f"заметка {short_id} Клиент попросил связаться утром"
        title = "Добавить заметку"
    elif kind == "next":
        example = f"следующее {short_id} Позвонить и подтвердить время"
        title = "Следующее действие"
    else:
        result = "выиграно" if stage == "won" else "потеряно"
        example = f"результат {short_id} {result} причина результата"
        title = "Закрыть обращение"
    return CustomerInteractionMessage(
        text=f"{title}\n\nОтправьте одним сообщением:\n{example}",
        rows=((_button("💬 К обращениям", "cpm:sales"),), _back_row()),
    )


def _sales_followup_message(actor: TenantContext, lead_id: str) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    item = get_sales_workspace_item(actor=actor, lead_id=lead_id)
    if item is None or str(item.get("stage") or "") in {"won", "lost"}:
        return _stale_message()
    source = str(item.get("source_kind") or "").strip().lower()
    active = bool(item.get("active_followup_id"))
    suppressed = bool(item.get("followup_suppressed"))
    allowed = source in _FOLLOWUP_CHANNELS and str(item.get("contact_basis") or "") != "none" and not suppressed
    lines = ["Напоминание клиенту", ""]
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if suppressed:
        lines.append("Клиент попросил больше не писать по этому каналу.")
    elif active:
        lines.append(f"Напоминание уже запланировано: {item.get('active_followup_scheduled_at') or 'в выбранное время'}.")
        rows.append((_button("✖️ Отменить напоминание", f"cpm:sales-followup-cancel:{lead_id}"),))
    elif allowed:
        lines.append("Сообщение уйдёт только по исходному каналу клиента после явной команды.")
        rows.append((_button("✉️ Запланировать", f"cpm:sales-followup-help:{lead_id}"),))
    else:
        lines.append("Для этого обращения автоматическое напоминание сейчас недоступно.")
    if allowed:
        rows.append((_button("🚫 Больше не писать", f"cpm:sales-followup-optout-help:{lead_id}"),))
    rows.append((_button("↩️ К карточке", f"cpm:sales-lead:{lead_id}"),))
    rows.append((_button("💬 К обращениям", "cpm:sales"),))
    return CustomerInteractionMessage(text="\n".join(lines), rows=tuple(rows))


def _sales_followup_help(lead_id: str) -> CustomerInteractionMessage:
    short_id = str(lead_id)[:8]
    return CustomerInteractionMessage(
        text=(
            "Запланировать напоминание\n\nОтправьте одним сообщением:\n"
            f"напомнить {short_id} 24 Ваш текст клиенту\n\n"
            "Вместо 24 можно указать 1 или 72 часа. Если выбранное время попадёт на ночь, "
            "ClientPlatform перенесёт отправку на ближайшее разрешённое утро."
        ),
        rows=((_button("↩️ К карточке", f"cpm:sales-lead:{lead_id}"),),),
    )


def _sales_followup_optout_help(lead_id: str) -> CustomerInteractionMessage:
    short_id = str(lead_id)[:8]
    return CustomerInteractionMessage(
        text=(
            "Больше не писать клиенту\n\nЕсли клиент действительно попросил больше не писать, "
            "подтвердите отдельным сообщением:\n"
            f"не писать {short_id} подтвердить\n\n"
            "Активное напоминание будет отменено, а новые сообщения по этому каналу будут запрещены."
        ),
        rows=((_button("↩️ К карточке", f"cpm:sales-lead:{lead_id}"),),),
    )


def _sales_mutation_message(
    actor: TenantContext,
    parsed: ParsedMemberInteraction,
    *,
    interaction_key: str,
) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    if parsed.action in {"sales-assign", "sales-unassign"}:
        if len(parsed.args) != 1:
            return _stale_message()
        lead_id = parsed.args[0]
        if parsed.action == "sales-assign":
            assign_sales_workspace_to_actor(actor=actor, lead_id=lead_id)
        else:
            unassign_sales_workspace(actor=actor, lead_id=lead_id)
        return _sales_lead_message(actor, lead_id)
    if parsed.action == "sales-stage":
        if len(parsed.args) != 2 or parsed.args[1] not in {"contacted", "qualified", "checkout"}:
            return _stale_message()
        transition_sales_workspace(
            actor=actor,
            lead_id=parsed.args[0],
            stage=SalesLeadStage(parsed.args[1]),
        )
        return _sales_lead_message(actor, parsed.args[0])
    if parsed.action == "sales-note-text":
        if len(parsed.args) != 2:
            return _stale_message()
        item = _sales_reference_item(actor, parsed.args[0])
        lead_id = str(item["id"])
        add_sales_workspace_note(
            actor=actor,
            lead_id=lead_id,
            note=parsed.args[1],
            interaction_key=interaction_key,
        )
        card = _sales_lead_message(actor, lead_id)
        return CustomerInteractionMessage(text="✅ Заметка сохранена.\n\n" + card.text, rows=card.rows)
    if parsed.action == "sales-next-text":
        if len(parsed.args) != 2:
            return _stale_message()
        item = _sales_reference_item(actor, parsed.args[0])
        lead_id = str(item["id"])
        set_sales_workspace_next_action(
            actor=actor,
            lead_id=lead_id,
            next_action=parsed.args[1],
        )
        return _sales_lead_message(actor, lead_id)
    if parsed.action == "sales-close-text":
        if len(parsed.args) != 3 or parsed.args[1] not in {"won", "lost"}:
            return _stale_message()
        item = _sales_reference_item(actor, parsed.args[0])
        lead_id = str(item["id"])
        transition_sales_workspace(
            actor=actor,
            lead_id=lead_id,
            stage=SalesLeadStage(parsed.args[1]),
            reason=parsed.args[2],
        )
        return _sales_lead_message(actor, lead_id)
    if parsed.action == "sales-reopen":
        if len(parsed.args) != 1:
            return _stale_message()
        reopen_sales_workspace(actor=actor, lead_id=parsed.args[0])
        return _sales_lead_message(actor, parsed.args[0])
    if parsed.action in {"sales-handoff-claim", "sales-handoff-resolve"}:
        if len(parsed.args) != 1:
            return _stale_message()
        if parsed.action == "sales-handoff-claim":
            claim_sales_workspace_handoff(actor=actor, handoff_id=parsed.args[0])
        else:
            resolve_sales_workspace_handoff(actor=actor, handoff_id=parsed.args[0])
        return _sales_handoffs_message(actor)
    if parsed.action == "sales-followup-text":
        if len(parsed.args) != 3 or parsed.args[1] not in {"1", "24", "72"}:
            return _stale_message()
        item = _sales_reference_item(actor, parsed.args[0])
        lead_id = str(item["id"])
        followup = schedule_sales_workspace_followup(
            actor=actor, lead_id=lead_id, message_text=parsed.args[2],
            hours_from_now=int(parsed.args[1]), interaction_key=interaction_key,
        )
        card = _sales_lead_message(actor, lead_id)
        scheduled_at = str(getattr(followup, "scheduled_at", "") or "указанное время")
        return CustomerInteractionMessage(
            text=f"✅ Напоминание клиенту запланировано на {scheduled_at}.\n\n{card.text}", rows=card.rows
        )
    if parsed.action == "sales-followup-cancel":
        if len(parsed.args) != 1:
            return _stale_message()
        cancel_sales_workspace_followup(actor=actor, lead_id=parsed.args[0])
        card = _sales_lead_message(actor, parsed.args[0])
        return CustomerInteractionMessage(text="✅ Напоминание отменено.\n\n" + card.text, rows=card.rows)
    if parsed.action == "sales-followup-optout-text":
        if len(parsed.args) != 1:
            return _stale_message()
        item = _sales_reference_item(actor, parsed.args[0])
        lead_id = str(item["id"])
        suppress_sales_workspace_followup(actor=actor, lead_id=lead_id)
        card = _sales_lead_message(actor, lead_id)
        return CustomerInteractionMessage(
            text="✅ Запрет на сообщения сохранён.\n\n" + card.text, rows=card.rows
        )
    return _stale_message()


def _growth_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in (_MARKETING_ROLES | _CONTENT_ROLES | _AUTOMATION_ROLES):
        return _permission_message()
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    items: list[nav.OwnerNavItem] = []
    if actor.role in _ACQUISITION_ROLES:
        rows.append((_button(nav.ACQUIRE.label, "cpm:acquire"),))
        items.append(nav.ACQUIRE)
    event_visible = any(
        item.id == "events" and item.status == "available"
        for item in cockpit_navigation(actor)
    )
    if actor.role in _CONTENT_ROLES:
        rows.append((_button(nav.PUBLICATIONS.label, "cpm:publications"),))
        items.append(nav.PUBLICATIONS)
    if event_visible:
        rows.append((_button(nav.EVENTS.label, "cpm:events"),))
        items.append(nav.EVENTS)
    if actor.role in _MARKETING_ROLES:
        rows.append((_button(nav.GROWTH_SALES.label, "cpm:growth-sales"),))
        items.append(nav.GROWTH_SALES)
    if actor.role in (_CONTENT_ROLES | _MARKETING_ROLES):
        rows.append((_button(nav.GROWTH_MORE.label, "cpm:growth-more"),))
        items.append(nav.GROWTH_MORE)
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text="📈 Продвижение и продажи\n\n" + nav.choice_help(*items),
        rows=tuple(rows),
    )

def _growth_sales_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _MARKETING_ROLES:
        return _permission_message()
    items = (nav.CUSTOMER_ANALYTICS, nav.MONEY, nav.PAYMENTS, nav.SEGMENTS)
    return CustomerInteractionMessage(
        text="💰 Продажи и деньги\n\n" + nav.choice_help(*items),
        rows=(
            (_button(nav.CUSTOMER_ANALYTICS.label, "cpm:growth-analysis"),),
            (_button(nav.MONEY.label, "cpm:money"),),
            (_button(nav.PAYMENTS.label, "cpm:payments"),),
            (_button(nav.SEGMENTS.label, "cpm:segments"),),
            (_button(nav.GROWTH.label, "cpm:growth"),),
            _back_row(),
        ),
    )

def _growth_analysis_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _MARKETING_ROLES:
        return _permission_message()
    items = (nav.JOURNEY, nav.PROGRAM_PROGRESS)
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (_button(nav.JOURNEY.label, "cpm:funnel2"),),
        (_button(nav.PROGRAM_PROGRESS.label, "cpm:funnel"),),
    ]
    if actor.role in _PROMOTION_ANALYTICS_ROLES:
        rows.append((_button("🧪 A/B креативы", "cpm:experiments:0"),))
    rows.extend(
        [
            (_button(nav.GROWTH_SALES.label, "cpm:growth-sales"),),
            _back_row(),
        ]
    )
    return CustomerInteractionMessage(
        text="📊 Путь клиента и программы\n\n" + nav.choice_help(*items),
        rows=tuple(rows),
    )



def _event_action_command(action: EventHubAction) -> str:
    if action.kind == "create":
        return "cpm:event-new"
    if action.kind == "join" and action.key is not None:
        return f"cpm:event-join:{action.key}"
    if action.kind == "conduct" and action.key is not None:
        return f"cpm:event-conduct:{action.key}"
    if action.kind == "content" and action.key is not None:
        return f"cpm:event-content:{action.key}"
    if action.kind == "edit" and action.key is not None:
        return f"cpm:event-edit:{action.key}"
    if action.kind == "announce" and action.key is not None:
        return f"cpm:event-announce:{action.key}"
    if action.kind == "settings":
        return "cpm:event-settings"
    if action.kind == "followups":
        return f"cpm:event-followups:{'on' if action.enabled else 'off'}"
    if action.kind == "segment" and action.key is not None:
        return f"cpm:event-segment:{action.key}:{'on' if action.enabled else 'off'}"
    if action.kind == "channel" and action.key is not None:
        return f"cpm:event-channel:{action.key}:{'on' if action.enabled else 'off'}"
    raise ValueError("unsupported event hub action")


def _event_projection_fallback(actor: TenantContext) -> CustomerInteractionMessage:
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    try:
        actor.assert_can_manage_business()
    except TenantPermissionDenied:
        pass
    else:
        rows.append((_button("🎥 Создать вебинар", "cpm:event-new"),))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            "🎥 Вебинары\n\n"
            "Раздел открыт, но статистику сейчас не удалось загрузить. "
            "Можно создать новый вебинар или вернуться сюда позже."
        ),
        rows=tuple(rows),
    )


def _events_message(actor: TenantContext) -> CustomerInteractionMessage:
    try:
        snapshot = resolve_events_snapshot(
            actor=actor,
            business_name=_business_name(actor),
            limit=5,
        )
        rows: list[tuple[CustomerInteractionButton, ...]] = []
        for action in event_hub_actions(snapshot):
            rows.append((_button(action.label, _event_action_command(action)),))
        rows.append(_back_row())
        return CustomerInteractionMessage(text=event_hub_text(snapshot), rows=tuple(rows))
    except ValueError:
        log.exception(
            "Native webinar overview projection failed",
            extra={"business_id": actor.business_id, "member_user_id": actor.user_id},
        )
        return _event_projection_fallback(actor)


def _event_edit_message(
    actor: TenantContext,
    event_id: str,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str,
) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    sessions = list_event_sessions(actor=actor, event_id=event_id)
    if not sessions:
        return _stale_message()
    window = get_event_warmup_window(actor=actor, event_id=event_id)
    zone = ZoneInfo(window.timezone_name)
    current_lines = []
    for session in sessions:
        end = session.ends_at or (session.starts_at + timedelta(hours=2))
        current_lines.append(
            session.starts_at.astimezone(zone).strftime("%d.%m.%Y %H:%M")
            + "-"
            + end.astimezone(zone).strftime("%H:%M")
        )
    count = len(sessions)
    return _begin_owner_input_message(
        actor,
        platform=current_platform,
        surface=input_surface,
        action="event_schedule",
        context={"event_id": event_id},
        text=(
            "🕒 Изменить расписание вебинара\n\n"
            f"Эфиров: {count}. Пришлите ровно {count} "
            + ("строку" if count == 1 else "строки" if count in {2, 3, 4} else "строк")
            + " — по одной на каждый эфир.\n"
            "Формат: ДД.ММ.ГГГГ ЧЧ:ММ-ЧЧ:ММ\n\n"
            "Текущее расписание:\n"
            + "\n".join(current_lines)
            + "\n\nКомнаты и площадки сохранятся. Напоминания клиентам будут пересчитаны автоматически."
        ),
        rows=((_button(BACK_TO_EVENTS_LABEL, "cpm:events"),), _back_row()),
    )


def _event_edit_result(
    actor: TenantContext,
    event_id: str,
    raw_schedule: str,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str,
) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    existing = list_event_sessions(actor=actor, event_id=event_id)
    if not existing:
        return _stale_message()
    window = get_event_warmup_window(actor=actor, event_id=event_id)
    lines = [line.strip() for line in str(raw_schedule or "").splitlines() if line.strip()]
    if len(lines) != len(existing):
        return CustomerInteractionMessage(
            text=(
                f"Нужно {len(existing)} строк — по одной на каждый эфир. "
                "Ничего не изменено. Пришлите расписание ещё раз."
            ),
            rows=((_button(BACK_TO_EVENTS_LABEL, "cpm:events"),), _back_row()),
        )
    specs: list[EventSessionSpec] = []
    previous = None
    try:
        for position, (line, current) in enumerate(zip(lines, existing), start=1):
            parsed = parse_session_window(
                line,
                timezone_name=window.timezone_name,
                position=position,
            )
            validate_session_sequence(parsed, previous=previous)
            previous = parsed
            specs.append(
                EventSessionSpec(
                    starts_at=parsed.starts_at,
                    ends_at=parsed.ends_at,
                    join_url=current.join_url,
                    provider_key=current.provider_key,
                    provider_label=current.provider_label,
                )
            )
        updated = configure_event_sessions(
            actor=actor,
            event_id=event_id,
            sessions=tuple(specs),
            reschedule_notifications=True,
        )
    except (TypeError, ValueError):
        return CustomerInteractionMessage(
            text=(
                "Не удалось сохранить расписание. Ничего не изменено. "
                "Проверьте формат, будущие даты и отсутствие пересечений, затем пришлите все строки ещё раз."
            ),
            rows=((_button(BACK_TO_EVENTS_LABEL, "cpm:events"),), _back_row()),
        )
    clear_owner_input(
        user_id=actor.user_id,
        platform=current_platform.value,
        surface=input_surface,
    )
    zone = ZoneInfo(window.timezone_name)
    rendered = "\n".join(
        f"День {session.position}: {session.starts_at.astimezone(zone).strftime('%d.%m.%Y %H:%M')}"
        for session in updated
    )
    return CustomerInteractionMessage(
        text="✅ Расписание вебинара обновлено.\n\n" + rendered,
        rows=((_button(BACK_TO_EVENTS_LABEL, "cpm:events"),), _back_row()),
    )


def _event_conduct_message(
    actor: TenantContext,
    event_id: str,
    *,
    page: int = 0,
) -> CustomerInteractionMessage:
    live = resolve_event_live_snapshot(actor=actor, event_id=event_id)
    ready = tuple(
        session
        for session in live.sessions
        if session.join_ready and session.join_url
    )
    if not ready:
        return CustomerInteractionMessage(
            text=(
                f"▶️ {live.title}\n\n"
                "Ссылка на эфир пока не добавлена. Сначала откройте вебинар и добавьте площадку."
            ),
            rows=((_button(BACK_TO_EVENTS_LABEL, "cpm:events"),), _back_row()),
        )
    if isinstance(page, bool) or not isinstance(page, int) or page < 0 or page >= len(ready):
        return _stale_message()

    session = ready[page]
    provider = str(session.provider_label or session.provider_key or "площадка")
    lines = [
        f"▶️ {live.title}",
        "",
        f"День {session.position} · {session.local_start} · {provider}",
        str(session.join_url),
        "",
        "Откройте ссылку выше — это сохранённая комната вебинара.",
    ]
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    page_row: list[CustomerInteractionButton] = []
    if page > 0:
        page_row.append(
            _button("⬅️ Предыдущий день", f"cpm:event-conduct:{event_id}:{page - 1}")
        )
    if page + 1 < len(ready):
        page_row.append(
            _button("Следующий день ➡️", f"cpm:event-conduct:{event_id}:{page + 1}")
        )
    if page_row:
        rows.append(tuple(page_row))
    rows.extend(((_button(BACK_TO_EVENTS_LABEL, "cpm:events"),), _back_row()))
    return CustomerInteractionMessage(
        text="\n".join(lines),
        rows=tuple(rows),
    )


def _event_content_message(
    actor: TenantContext,
    event_id: str,
) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    snapshot = resolve_events_snapshot(
        actor=actor,
        business_name=_business_name(actor),
        limit=30,
    )
    item = next(
        (row for row in tuple(getattr(snapshot, "items", ())) if str(getattr(row, "id", "")) == event_id),
        None,
    )
    if item is None:
        raise ValueError("webinar was not found")
    warmup = get_saved_event_warmup_plan(actor=actor, event_id=event_id)
    modes = get_event_content_plan(actor=actor, event_id=event_id)
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if warmup.drafts:
        rows.append(
            (
                _button(
                    "📨 Сообщения до вебинара",
                    f"cpm:event-wizard:wp:{event_id}:{warmup.requested_days}:0",
                ),
            )
        )
        rows.append(
            (
                _button(
                    "🗓 Изменить дни сообщений",
                    f"cpm:event-wizard:ws:{event_id}",
                ),
            )
        )
        warmup_text = (
            f"{warmup.requested_days} дн. · по 1 сообщению в день · "
            f"12:00 ({warmup.timezone_name})"
        )
    else:
        rows.append(
            (
                _button(
                    "📨 Настроить сообщения до вебинара",
                    f"cpm:event-wizard:ws:{event_id}",
                ),
            )
        )
        warmup_text = "не настроен"
    rows.extend(
        [
            (_button("✨ Анонс", f"cpm:event-announce:{event_id}"),),
            (_button("💬 Тексты дожима", f"cpm:event-content-followups:{event_id}"),),
            (_button("⚙️ Автосообщения", "cpm:event-settings"),),
            _back_row(),
        ]
    )
    enabled = bool(getattr(snapshot, "commercial_followups_enabled", False))
    effective = bool(getattr(snapshot, "commercial_followups_effective", False))
    autosend = (
        "🟢 включены"
        if enabled and effective
        else "🟡 включены, но ограничены политикой/платформой"
        if enabled
        else "⚪️ выключены"
    )
    return CustomerInteractionMessage(
        text=(
            f"🗓 Контент-план\n\n{item.title}\n\n"
            f"📨 До вебинара: {warmup_text}\n"
            f"Формат: {event_content_mode_label(modes.warmup)}\n\n"
            "✨ Анонс: показывается владельцу до публикации.\n"
            f"Формат: {event_content_mode_label(modes.event_day)}\n\n"
            "🔔 Напоминания: подтверждение регистрации, затем 24 ч, 3 ч и 15 мин "
            "до каждого эфира.\n\n"
            "💬 Дожим: 2 или 3 сообщения в зависимости от поведения участника.\n"
            f"Формат: {event_content_mode_label(modes.post_event)}\n\n"
            f"Автоматическая отправка: {autosend}."
        ),
        rows=tuple(rows),
    )


def _event_followup_content_message(
    actor: TenantContext,
    event_id: str,
    index: int = 0,
) -> CustomerInteractionMessage:
    snapshot = resolve_events_snapshot(
        actor=actor,
        business_name=_business_name(actor),
        limit=30,
    )
    item = next(
        (row for row in tuple(getattr(snapshot, "items", ())) if str(getattr(row, "id", "")) == event_id),
        None,
    )
    if item is None:
        raise ValueError("webinar was not found")
    previews = get_event_followup_content_plan(actor=actor, event_id=event_id)
    if not previews:
        return CustomerInteractionMessage(
            text="Для этого вебинара нет сообщений дожима.",
            rows=((_button("🗓 К контент-плану", f"cpm:event-content:{event_id}"),), _back_row()),
        )
    current = max(0, min(int(index), len(previews) - 1))
    preview = previews[current]
    body = (
        preview.text.replace("{name}", "Имя")
        .replace("{title}", str(item.title))
        .replace("{offer}", "[ссылка на предложение]")
    )
    source = "ваш текст" if preview.source == "owner" else "автотекст"
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    navigation: list[CustomerInteractionButton] = []
    if current > 0:
        navigation.append(
            _button("⬅️ Предыдущее", f"cpm:event-content-followups:{event_id}:{current - 1}")
        )
    if current + 1 < len(previews):
        navigation.append(
            _button("Следующее ➡️", f"cpm:event-content-followups:{event_id}:{current + 1}")
        )
    if navigation:
        rows.append(tuple(navigation))
    rows.append(
        (_button("✏️ Изменить текст", f"cpm:event-followup-edit:{event_id}:{current}"),)
    )
    if preview.source != "template":
        rows.append(
            (_button("↩️ Вернуть автотекст", f"cpm:event-followup-reset:{event_id}:{current}"),)
        )
    rows.extend(
        [
            (_button("⚙️ Автосообщения", "cpm:event-settings"),),
            (_button("🗓 К контент-плану", f"cpm:event-content:{event_id}"),),
            _back_row(),
        ]
    )
    return CustomerInteractionMessage(
        text=(
            f"💬 Дожим {current + 1}/{len(previews)} после «{item.title}»\n\n"
            f"Группа: {preview.segment_label}\n"
            f"Когда: {preview.offset_label}\n"
            f"Источник: {source}\n\n"
            f"{body}\n\n"
            "Можно полностью заменить этот текст своим. Поддерживаются {name}, {title}, {offer}.\n"
            "После оплаты серия прекращается. Без действующего согласия сообщение не отправляется."
        ),
        rows=tuple(rows),
    )


def _event_followup_edit_message(
    actor: TenantContext,
    *,
    event_id: str,
    index: int,
    current_platform: ConnectionPlatform,
    input_surface: str,
) -> CustomerInteractionMessage:
    previews = get_event_followup_content_plan(actor=actor, event_id=event_id)
    if not 0 <= int(index) < len(previews):
        raise ValueError("event followup index is invalid")
    preview = previews[int(index)]
    begin_owner_input(
        actor=actor,
        platform=current_platform.value,
        surface=input_surface,
        action="event_followup_text",
        context={
            "event_id": event_id,
            "index": str(index),
            "segment": preview.segment,
            "stage": str(preview.stage),
        },
    )
    return CustomerInteractionMessage(
        text=(
            f"✏️ Пришлите новый текст дожима ({preview.segment_label}, {preview.offset_label}) "
            "одним сообщением.\n\nМожно написать текст полностью самостоятельно. "
            "Поддерживаются {name}, {title}, {offer}.\n\nДля выхода: Отмена."
        ),
        rows=((_button("🗓 К дожиму", f"cpm:event-content-followups:{event_id}:{index}"),), _back_row()),
    )


def _event_followup_edit_result(
    actor: TenantContext,
    *,
    event_id: str,
    index: int,
    segment: str,
    stage: int,
    text: str,
) -> CustomerInteractionMessage:
    set_event_followup_text(
        actor=actor,
        event_id=event_id,
        segment=segment,
        stage=stage,
        text=text,
    )
    return _event_followup_content_message(actor, event_id, index)


def _event_followup_reset_result(
    actor: TenantContext,
    *,
    event_id: str,
    index: int,
) -> CustomerInteractionMessage:
    previews = get_event_followup_content_plan(actor=actor, event_id=event_id)
    if not 0 <= int(index) < len(previews):
        raise ValueError("event followup index is invalid")
    preview = previews[int(index)]
    reset_event_followup_text(
        actor=actor,
        event_id=event_id,
        segment=preview.segment,
        stage=preview.stage,
    )
    return _event_followup_content_message(actor, event_id, index)


def _event_settings_message(actor: TenantContext) -> CustomerInteractionMessage:
    try:
        snapshot = resolve_events_snapshot(
            actor=actor,
            business_name=_business_name(actor),
            limit=5,
        )
        rows: list[tuple[CustomerInteractionButton, ...]] = []
        channel_row: list[CustomerInteractionButton] = []
        for action in event_settings_actions(snapshot):
            button = _button(action.label, _event_action_command(action))
            if action.kind == "channel":
                channel_row.append(button)
            else:
                rows.append((button,))
        if channel_row:
            rows.append(tuple(channel_row))
        rows.append(_back_row())
        return CustomerInteractionMessage(text=event_settings_text(snapshot), rows=tuple(rows))
    except ValueError:
        log.exception(
            "Native webinar settings projection failed",
            extra={"business_id": actor.business_id, "member_user_id": actor.user_id},
        )
        return CustomerInteractionMessage(
            text=(
                "⚙️ Автосообщения после вебинара\n\n"
                "Настройки сейчас не удалось загрузить. Вернитесь к вебинарам и попробуйте позже."
            ),
            rows=(_back_row(),),
        )

def _event_followups_action(
    actor: TenantContext, args: tuple[str, ...]
) -> CustomerInteractionMessage:
    if len(args) != 1 or args[0] not in {"on", "off"}:
        return _stale_message()
    try:
        set_business_event_followups_enabled(actor=actor, enabled=args[0] == "on")
    except ValueError as exc:
        return CustomerInteractionMessage(
            text=f"Не удалось изменить автосообщения: {exc}",
            rows=((_button(BACK_TO_EVENTS_LABEL, "cpm:events"),), _back_row()),
        )
    return _event_settings_message(actor)


def _event_segment_action(
    actor: TenantContext, args: tuple[str, ...]
) -> CustomerInteractionMessage:
    if len(args) != 2 or args[1] not in {"on", "off"}:
        return _stale_message()
    try:
        set_business_event_followup_segment_enabled(
            actor=actor,
            segment=args[0],
            enabled=args[1] == "on",
        )
    except ValueError as exc:
        return CustomerInteractionMessage(
            text=f"Не удалось изменить группу участников: {exc}",
            rows=((_button(BACK_TO_EVENTS_LABEL, "cpm:events"),), _back_row()),
        )
    return _event_settings_message(actor)


def _event_channel_action(
    actor: TenantContext, args: tuple[str, ...]
) -> CustomerInteractionMessage:
    if len(args) != 2 or args[1] not in {"on", "off"}:
        return _stale_message()
    try:
        set_business_event_followup_channel_enabled(
            actor=actor,
            channel=args[0],
            enabled=args[1] == "on",
        )
    except ValueError as exc:
        return CustomerInteractionMessage(
            text=f"Не удалось изменить канал: {exc}",
            rows=((_button(BACK_TO_EVENTS_LABEL, "cpm:events"),), _back_row()),
        )
    return _event_settings_message(actor)


def _event_announcement_message(
    actor: TenantContext,
    event_id: str,
    *,
    current_platform: ConnectionPlatform,
) -> CustomerInteractionMessage:
    try:
        actor.assert_can_manage_business()
        draft = draft_event_announcement_template(actor=actor, event_id=event_id)
        public_base = str(getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or "").strip()
        registration_url = draft.registration_url(
            public_base_url=public_base,
            source=current_platform.value,
        )
        advertising_url = draft.registration_url(
            public_base_url=public_base,
            source="ads",
        )
    except (TenantPermissionDenied, ValueError, RuntimeError):
        return CustomerInteractionMessage(
            text="Не удалось подготовить анонс. Вернитесь к вебинарам и попробуйте ещё раз.",
            rows=((_button(BACK_TO_EVENTS_LABEL, "cpm:events"),), _back_row()),
        )
    source_note = (
        "Текст подготовлен AI и требует Вашего подтверждения перед публикацией."
        if draft.generated_by.startswith("ai:")
        else "Подготовлен безопасный текст. Перед публикацией его можно отредактировать в выбранном мессенджере."
    )
    return CustomerInteractionMessage(
        text=(
            "✨ Анонс готов\n\n"
            f"{draft.text}\n\n"
            f"{source_note}\n\n"
            f"Регистрация: {registration_url}\n\n"
            f"🔗 Ссылка для рекламы:\n{advertising_url}\n\n"
            "Ссылку для рекламы можно вставить в рекламный кабинет, сайт или пост. "
            "ClientPlatform отдельно сохранит источник ads; обычная регистрационная ссылка помечена текущим каналом."
        ),
        rows=((_button(BACK_TO_EVENTS_LABEL, "cpm:events"),), _back_row()),
    )


def _event_direction_message(
    actor: TenantContext,
    page: int = 0,
) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    directions = list_activity_directions(actor=actor)
    if not directions:
        return _stale_message()
    page = max(0, int(page))
    start = page * _DIRECTION_PAGE_SIZE
    if start >= len(directions) and page:
        return _stale_message()
    shown = directions[start : start + _DIRECTION_PAGE_SIZE]
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (_button(f"🧭 {item.title[:34]}", f"cpm:event-new-dir:{item.id}"),)
        for item in shown
    ]
    rows.append((_button("Без направления", "cpm:event-new-dir:none"),))
    pagination: list[CustomerInteractionButton] = []
    if page:
        pagination.append(_button("⬅️ Назад", f"cpm:event-new-dirs:{page - 1}"))
    if start + _DIRECTION_PAGE_SIZE < len(directions):
        pagination.append(_button("Вперёд ➡️", f"cpm:event-new-dirs:{page + 1}"))
    if pagination:
        rows.append(tuple(pagination))
    rows.append((_button(BACK_TO_EVENTS_LABEL, "cpm:events"),))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            "🎥 Создаём вебинар\n\n"
            "К какому направлению деятельности относится мероприятие? "
            "Выберите направление или «Без направления»."
        ),
        rows=tuple(rows),
    )


def _event_new_entry_message(
    actor: TenantContext,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str,
) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    directions = list_activity_directions(actor=actor)
    if directions:
        return _event_direction_message(actor, 0)
    return begin_native_event_wizard(
        actor,
        platform=current_platform,
        surface=input_surface,
        direction_id=None,
    )


def _event_new_direction_result(
    actor: TenantContext,
    direction_reference: str,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str,
) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    direction_id: str | None = None
    if direction_reference != "none":
        direction = get_activity_direction(actor=actor, direction_id=direction_reference)
        if direction.status != ActivityDirectionStatus.ACTIVE:
            return _stale_message()
        direction_id = direction.id
    return begin_native_event_wizard(
        actor,
        platform=current_platform,
        surface=input_surface,
        direction_id=direction_id,
    )


def _event_new_message(
    actor: TenantContext,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str,
) -> CustomerInteractionMessage:
    try:
        actor.assert_can_manage_business()
    except TenantPermissionDenied:
        return _permission_message()
    profile = get_business_profile(actor=actor)
    return _begin_owner_input_message(
        actor,
        platform=current_platform,
        surface=input_surface,
        action="online_event",
        text=event_creation_prompt(profile.timezone),
        rows=((_button(BACK_TO_EVENTS_LABEL, "cpm:events"),), _back_row()),
        append_exit_hint=False,
    )


def _event_create_result(
    actor: TenantContext,
    title: str,
    local_time: str,
    join_url: str,
    offer_url: str,
) -> CustomerInteractionMessage:
    try:
        actor.assert_can_manage_business()
        profile = get_business_profile(actor=actor)
        starts_at = datetime.fromisoformat(
            parse_local_booking_start(local_time, timezone_name=profile.timezone)
        )
        created = create_and_publish_online_event(
            actor=actor,
            request=OnlineEventCreateRequest(
                title=title,
                starts_at=starts_at,
                timezone_name=profile.timezone,
                join_url=join_url or None,
                offer_url=offer_url or None,
                provider_key=None,
            ),
        )
        public_base = str(getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or "").strip()
        registration_url = created.registration_url(public_base)
    except (TenantPermissionDenied, ValueError, RuntimeError):
        return CustomerInteractionMessage(
            text=event_creation_failure_text(),
            rows=((_button("🎥 Попробовать снова", "cpm:event-new"),), (_button(BACK_TO_EVENTS_LABEL, "cpm:events"),), _back_row()),
        )
    return CustomerInteractionMessage(
        text=event_creation_success_text(
            title=title,
            local_time=local_time,
            provider_key=created.provider_key,
            registration_url=registration_url,
            email_notifications_enabled=created.email_notifications_enabled,
            join_ready=created.join_ready,
        ),
        rows=(
            *(((_button("🔗 Добавить ссылку на эфир", f"cpm:event-join:{created.event_id}"),),) if not created.join_ready else ()),
            (_button(BACK_TO_EVENTS_LABEL, "cpm:events"),),
            (_button("🎥 Создать ещё", "cpm:event-new"),),
            _back_row(),
        ),
    )


def _event_join_message(
    actor: TenantContext,
    event_id: str,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str,
) -> CustomerInteractionMessage:
    try:
        actor.assert_can_manage_business()
    except TenantPermissionDenied:
        return _permission_message()
    return _begin_owner_input_message(
        actor,
        platform=current_platform,
        surface=input_surface,
        action="event_join_url",
        context={"event_id": event_id},
        text=(
            "🔗 Пришлите HTTPS-ссылку на эфир. После сохранения все персональные "
            "ссылки участников начнут вести на неё."
        ),
        rows=((_button(BACK_TO_EVENTS_LABEL, "cpm:events"),), _back_row()),
        append_exit_hint=False,
    )


def _event_join_result(
    actor: TenantContext, event_id: str, join_url: str
) -> CustomerInteractionMessage:
    try:
        updated = set_event_join_target(
            actor=actor, event_id=event_id, join_url=join_url
        )
    except (TenantPermissionDenied, ValueError, RuntimeError):
        return CustomerInteractionMessage(
            text="Не удалось сохранить ссылку на эфир. Проверьте полный HTTPS-адрес.",
            rows=((_button(BACK_TO_EVENTS_LABEL, "cpm:events"),), _back_row()),
        )
    return CustomerInteractionMessage(
        text=(
            f"✅ Ссылка на эфир сохранена. Площадка: {updated.provider_key}. "
            "Персональные ссылки участников уже ведут на неё."
        ),
        rows=((_button(BACK_TO_EVENTS_LABEL, "cpm:events"),), _back_row()),
    )


def _growth_more_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in (_MARKETING_ROLES | _CONTENT_ROLES | _AUTOMATION_ROLES):
        return _permission_message()
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    items: list[nav.OwnerNavItem] = []
    if actor.role in (_CONTENT_ROLES | _MARKETING_ROLES):
        rows.extend(
            [
                (_button(nav.OFFERS.label, "cpm:offers"),),
                (_button(nav.COPY.label, "cpm:copy"),),
            ]
        )
        items.extend((nav.OFFERS, nav.COPY))
    if actor.role in _MARKETING_ROLES:
        rows.append((_button(nav.PRICES.label, "cpm:prices"),))
        items.append(nav.PRICES)
    if actor.role == PlatformRole.CONTENT_MANAGER:
        rows.append((_button("🧪 A/B креативы", "cpm:experiments:0"),))
    if actor.role in _AUTOMATION_ROLES:
        rows.append((_button(nav.AUTOMATION.label, "cpm:autopilot"),))
        items.append(nav.AUTOMATION)
    if actor.role in _CONNECTION_ROLES:
        rows.append((_button(nav.LIFECYCLE.label, "cpm:growth-lifecycle"),))
        items.append(nav.LIFECYCLE)
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text="⋯ Тексты, цены и возврат клиентов\n\n" + nav.choice_help(*items),
        rows=tuple(rows),
    )

def _growth_lifecycle_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _CONNECTION_ROLES:
        return _permission_message()
    items = [nav.INVITES, nav.RETENTION]
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (_button(nav.INVITES.label, "cpm:invites"),),
        (_button(nav.RETENTION.label, "cpm:retention"),),
    ]
    if any(
        item.id == "events" and item.status == "available"
        for item in cockpit_navigation(actor)
    ):
        rows.append((_button(nav.EVENTS.label, "cpm:events"),))
        items.append(nav.EVENTS)
    rows.extend([(_button(nav.GROWTH.label, "cpm:growth"),), _back_row()])
    return CustomerInteractionMessage(
        text="♻️ Вернуть и удержать клиентов\n\n" + nav.choice_help(*items),
        rows=tuple(rows),
    )

def _acquisition_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _ACQUISITION_ROLES:
        return _permission_message()
    public_base = str(getattr(settings, "MESSENGER_PUBLIC_BASE_URL", "") or "").strip()
    if not public_base:
        return CustomerInteractionMessage(
            text=(
                "Найти новых клиентов\n\nПубличная ссылка ClientPlatform пока не настроена. "
                "Ничего не опубликовано и расходы не запущены."
            ),
            rows=((_button("📈 Рост", "cpm:growth"),), _back_row()),
        )
    try:
        prepared = prepare_nearest_acquisition_destination(
            actor=actor,
            public_base_url=public_base,
            attribution_channel=PromotionChannel.WEBSITE,
        )
    except (PromotionError, TenantPermissionDenied, ValueError):
        return CustomerInteractionMessage(
            text=(
                "Найти новых клиентов\n\nНе удалось безопасно подготовить ссылку. "
                "Ничего не опубликовано и расходы не запущены."
            ),
            rows=((_button("🔄 Проверить снова", "cpm:acquire"),), (_button("📈 Рост", "cpm:growth"),), _back_row()),
        )
    if prepared is None:
        first_row = (
            (_button("🕒 Добавить время", "cpm:booking-open"),)
            if actor.role in _BOOKING_MANAGEMENT_ROLES
            else (_button("📅 Проверить расписание", "cpm:bookings"),)
        )
        return CustomerInteractionMessage(
            text=(
                "🚀 Новые клиенты\n\nЧтобы приглашать клиентов на запись, сначала "
                "добавьте хотя бы одно свободное время."
            ),
            rows=(first_row, (_button("📈 Рост", "cpm:growth"),), _back_row()),
        )
    destination = prepared.destination
    if not destination.has_native_messenger_destination:
        return CustomerInteractionMessage(
            text=(
                "Найти новых клиентов\n\nПредложение подготовлено, но у бизнеса пока "
                "нет публичного Telegram, ВКонтакте или MAX. Ссылку не публикую, "
                "чтобы не вести клиента в тупик."
            ),
            rows=((_button("💬 Мессенджеры", "cpm:messengers"),), (_button("📈 Рост", "cpm:growth"),), _back_row()),
        )
    names = {ConnectionPlatform.TELEGRAM: "Telegram", ConnectionPlatform.VK: "ВКонтакте", ConnectionPlatform.MAX: "MAX"}
    channels = ", ".join(names[item.platform] for item in destination.messenger_destinations)
    creative = prepared.promotion.campaign.creative
    slot = prepared.promotion.slot
    return CustomerInteractionMessage(
        text=(
            "🚀 Найти новых клиентов\n\n"
            f"Ближайшее свободное время: {slot.local_start} · {slot.offering_title}.\n"
            f"{creative.headline}\n\n{creative.primary_text}\n\n"
            f"Записаться: {destination.public_url}\n\nДоступно клиенту: {channels}. "
            "Источник сохранится независимо от выбранного мессенджера. Это только "
            "готовый материал и измеряемая ссылка — платная реклама не запускается."
        ),
        rows=((_button("🔄 Обновить", "cpm:acquire"),), (_button("📈 Рост", "cpm:growth"),), _back_row()),
    )


_CREATIVE_TRIAL_STATUS_LABELS = {
    "draft": "черновик",
    "running": "идёт",
    "paused": "на паузе",
    "completed": "завершён",
}


def _creative_metric_from_code(value: str) -> CreativeOptimizationMetric:
    if value == "b":
        return CreativeOptimizationMetric.BOOKINGS
    if value == "w":
        return CreativeOptimizationMetric.WON
    raise ValueError("unknown creative optimization metric")


def _creative_recommendation_text(status: object) -> str:
    value = str(getattr(status, "value", status) or "")
    messages = {
        "ready": (
            "Есть статистически отделившийся лидер. Можно осторожно изменить "
            "распределение, не увеличивая общий рекламный бюджет."
        ),
        "not_running": "Тест сейчас не идёт — распределение не меняется.",
        "attribution_not_ready": (
            "Для части вариантов нет точной атрибуции. Общие данные кампании "
            "не выдаются за победу отдельного креатива."
        ),
        "insufficient_data": "Пока недостаточно точных переходов для безопасного решения.",
        "no_clear_winner": "Разница пока не доказана статистически.",
        "exploration_floor_reached": (
            "Проигрывающие варианты уже достигли минимальной контрольной доли."
        ),
    }
    return messages.get(value, "Автоматическое изменение распределения сейчас не предлагается.")


def _creative_trial_reference(actor: TenantContext, reference: str) -> str:
    return resolve_creative_trial_reference(actor=actor, reference=reference)


def _experiments_message(actor: TenantContext, page: int = 0) -> CustomerInteractionMessage:
    if actor.role not in _PROMOTION_ANALYTICS_ROLES:
        return _permission_message()
    page_size = 4
    current_page = max(int(page), 0)
    offset = current_page * page_size
    visible, has_more = list_creative_trial_page(
        actor=actor,
        limit=page_size,
        offset=offset,
    )
    if not visible:
        empty_rows = (
            (
                (_button(
                    "◀️ Предыдущая страница",
                    f"cpm:experiments:{current_page - 1}",
                ),),
                _back_row(),
            )
            if current_page > 0
            else (_back_row(),)
        )
        return CustomerInteractionMessage(
            text=(
                "🧪 A/B креативы\n\n"
                "Пока нет тестов на этой странице. "
                "Новые A/B тесты появятся здесь после запуска нескольких креативов."
            ),
            rows=empty_rows,
        )

    rows: list[tuple[CustomerInteractionButton, ...]] = []
    for index, plan in enumerate(visible, start=offset + 1):
        token = str(plan.trial_id)[:8]
        status = _CREATIVE_TRIAL_STATUS_LABELS.get(
            str(getattr(plan.status, "value", plan.status)),
            "неизвестно",
        )
        rows.append(
            (
                _button(
                    f"🧪 Тест {index} · {status}",
                    f"cpm:experiment:{token}:b",
                ),
            )
        )
    paging: list[CustomerInteractionButton] = []
    if current_page > 0:
        paging.append(
            _button("◀️ Предыдущая страница", f"cpm:experiments:{current_page - 1}")
        )
    if has_more:
        paging.append(
            _button("Следующая страница ▶️", f"cpm:experiments:{current_page + 1}")
        )
    if paging:
        rows.append(tuple(paging))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            "🧪 A/B креативы\n\n"
            "Сравнение строится только на точной variant-attribution. Слабые или "
            "общие данные кампании не превращаются в ложного победителя.\n\n"
            f"Страница {current_page + 1}"
        ),
        rows=tuple(rows),
    )


def _experiment_message(
    actor: TenantContext,
    reference: str,
    metric_code: str,
) -> CustomerInteractionMessage:
    if actor.role not in _PROMOTION_ANALYTICS_ROLES:
        return _permission_message()
    metric = _creative_metric_from_code(metric_code)
    trial_id = _creative_trial_reference(actor, reference)
    try:
        preview = preview_creative_winner(
            actor=actor,
            trial_id=trial_id,
            metric=metric,
        )
    except LookupError:
        return _stale_message()

    recommendation = preview.recommendation
    plan = preview.plan
    token = str(plan.trial_id)[:8]
    by_variant = {item.variant_id: item for item in preview.variants}
    metric_label = "продаж" if metric == CreativeOptimizationMetric.WON else "записей"
    evidence: list[str] = []
    allocation: list[str] = []
    proposed = {
        item.publication_job_id: item.proposed_allocation_bps
        for item in recommendation.evidence
    }
    for index, arm in enumerate(plan.arms, start=1):
        current = arm.allocation_bps / 100.0
        target = proposed.get(arm.publication_job_id, arm.allocation_bps) / 100.0
        allocation.append(
            f"• Вариант {index}: {current:.0f}%"
            if target == current
            else f"• Вариант {index}: {current:.0f}% → {target:.0f}%"
        )
        item = by_variant.get(arm.variant_id)
        if item is None or str(getattr(item.attribution_scope, "value", "")) != "variant":
            evidence.append(f"• Вариант {index}: нет точной атрибуции")
            continue
        successes = item.won if metric == CreativeOptimizationMetric.WON else item.bookings
        rate = (successes / item.leads * 100.0) if item.leads else 0.0
        marker = " ⭐" if arm.variant_id == recommendation.winner_variant_id else ""
        evidence.append(
            f"• Вариант {index}: {successes}/{item.leads} · {rate:.1f}%{marker}"
        )

    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if recommendation.can_apply and actor.role in _ACQUISITION_ROLES:
        rows.append(
            (
                _button(
                    "✅ Применить осторожный сдвиг",
                    "cpm:experiment-apply:"
                    f"{token}:{recommendation.trial_revision}:{metric_code}:{preview.fingerprint}",
                ),
            )
        )
    rows.append(
        (
            _button("📅 По записям", f"cpm:experiment:{token}:b"),
            _button("💰 По продажам", f"cpm:experiment:{token}:w"),
        )
    )
    rows.append((_button("🔄 Пересчитать", f"cpm:experiment:{token}:{metric_code}"),))
    rows.append(_back_row())
    status = _CREATIVE_TRIAL_STATUS_LABELS.get(
        str(getattr(plan.status, "value", plan.status)),
        "неизвестно",
    )
    return CustomerInteractionMessage(
        text=(
            "🧪 A/B креативы · рекомендация\n\n"
            f"Статус теста: {status} · версия {plan.revision}\n"
            f"Период данных: {preview.date_from} — {preview.date_to}\n"
            f"Метрика: {metric_label}\n\n"
            f"{_creative_recommendation_text(recommendation.status)}\n\n"
            "Распределение:\n"
            + "\n".join(allocation)
            + "\n\nТочные данные:\n"
            + "\n".join(evidence)
            + "\n\nПеред применением ClientPlatform пересчитывает факты и отклоняет "
            "устаревшее подтверждение."
        ),
        rows=tuple(rows),
    )


def _experiment_apply_message(
    actor: TenantContext,
    reference: str,
    revision: str,
    metric_code: str,
    fingerprint: str,
) -> CustomerInteractionMessage:
    if actor.role not in _ACQUISITION_ROLES:
        return _permission_message()
    metric = _creative_metric_from_code(metric_code)
    trial_id = _creative_trial_reference(actor, reference)
    if not str(revision).isdigit() or int(revision) < 1:
        return _stale_message()
    try:
        apply_creative_winner(
            actor=actor,
            trial_id=trial_id,
            expected_revision=int(revision),
            expected_fingerprint=str(fingerprint),
            confirmed=True,
            metric=metric,
        )
    except CreativeWinnerApplyError:
        fresh = _experiment_message(actor, reference, metric_code)
        return CustomerInteractionMessage(
            text=(
                "Данные теста изменились или рекомендация больше не актуальна. "
                "Ничего не применено.\n\n" + fresh.text
            ),
            rows=fresh.rows,
        )
    fresh = _experiment_message(actor, reference, metric_code)
    return CustomerInteractionMessage(
        text="✅ Распределение обновлено в безопасных границах.\n\n" + fresh.text,
        rows=fresh.rows,
    )


_DIRECTION_PAGE_SIZE = 4


def _directions_message(
    actor: TenantContext,
    page: int = 0,
    *,
    archived: bool = False,
) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    all_directions = list_activity_directions(actor=actor, include_archived=True)
    wanted_status = (
        ActivityDirectionStatus.ARCHIVED if archived else ActivityDirectionStatus.ACTIVE
    )
    directions = [item for item in all_directions if item.status == wanted_status]
    page = max(0, int(page))
    start = page * _DIRECTION_PAGE_SIZE
    if start >= len(directions) and page:
        return _stale_message()
    shown = directions[start : start + _DIRECTION_PAGE_SIZE]
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (
            _button(
                f"{'📦' if archived else '🧭'} {item.title[:34]}",
                f"cpm:direction:{item.id}",
            ),
        )
        for item in shown
    ]
    if not archived:
        rows.append((_button("✏️ Описание организации", "cpm:activity-edit-help"),))
        rows.append((_button("➕ Добавить направление", "cpm:direction-new"),))
        if any(item.status == ActivityDirectionStatus.ARCHIVED for item in all_directions):
            rows.append((_button("📦 Архив направлений", "cpm:directions-archived:0"),))
    else:
        rows.append((_button("🧭 Активные направления", "cpm:directions:0"),))
    pagination: list[CustomerInteractionButton] = []
    if page:
        pagination.append(
            _button(
                "⬅️ Назад",
                f"cpm:{'directions-archived' if archived else 'directions'}:{page - 1}",
            )
        )
    if start + _DIRECTION_PAGE_SIZE < len(directions):
        pagination.append(
            _button(
                "Вперёд ➡️",
                f"cpm:{'directions-archived' if archived else 'directions'}:{page + 1}",
            )
        )
    if pagination:
        rows.append(tuple(pagination))
    rows.append(_back_row())
    title = "📦 Архив направлений" if archived else "🧭 Направления деятельности"
    empty = (
        "Архив пуст."
        if archived
        else "Направлений пока нет. Можно работать без них или добавить первое."
    )
    return CustomerInteractionMessage(
        text=(
            f"{title}\n\n"
            "Все каналы ClientPlatform используют один и тот же список для этой организации. "
            "Изменения, сделанные здесь, будут видны в Telegram, ВКонтакте и MAX.\n\n"
            + ("\n".join(f"• {item.title}" for item in shown) if shown else empty)
        ),
        rows=tuple(rows),
    )


def _direction_message(actor: TenantContext, direction_id: str) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    direction = get_activity_direction(actor=actor, direction_id=direction_id)
    bindings = list_activity_direction_bindings(
        actor=actor,
        direction_id=direction.id,
    )
    counts = {"program": 0, "offering": 0, "event": 0}
    for binding in bindings:
        key = str(binding.subject_kind.value)
        if key in counts:
            counts[key] += 1
    active = direction.status == ActivityDirectionStatus.ACTIVE
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if active:
        rows.extend(
            [
                (_button("✏️ Изменить", f"cpm:direction-edit:{direction.id}"),),
                (_button("🗑 Удалить направление", f"cpm:direction-archive:{direction.id}"),),
            ]
        )
        rows.append((_button("🧭 К направлениям", "cpm:directions:0"),))
    else:
        rows.append((_button("↩️ Вернуть в работу", f"cpm:direction-restore:{direction.id}"),))
        rows.append((_button("📦 К архиву", "cpm:directions-archived:0"),))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            f"🧭 {direction.title}\n\n"
            f"{direction.description}\n\n"
            f"Материалы и программы: {counts['program']}\n"
            f"Услуги и предложения: {counts['offering']}\n"
            f"События и вебинары: {counts['event']}\n\n"
            f"Статус: {'в работе' if active else 'в архиве'}.\n\n"
            + ("Нажмите «Удалить направление», чтобы убрать его из активной работы без потери связанных данных." if active else "Направление можно вернуть в работу в любой момент.")
        ),
        rows=tuple(rows),
    )


def _direction_new_message(
    actor: TenantContext,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str,
) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    return _begin_owner_input_message(
        actor,
        platform=current_platform,
        surface=input_surface,
        action="activity_direction_create",
        text=(
            "➕ Новое направление деятельности\n\n"
            "Напишите одним сообщением:\nНазвание | Короткое описание\n\n"
            "Например: Корпоративные клиенты | Услуги и материалы для организаций."
        ),
        rows=((_button("🧭 К направлениям", "cpm:directions:0"),), _back_row()),
    )


def _direction_create_result(
    actor: TenantContext,
    title: str,
    description: str,
) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    direction = create_activity_direction(
        actor=actor,
        title=title,
        description=description,
    )
    return CustomerInteractionMessage(
        text=f"✅ Направление «{direction.title}» создано.",
        rows=(
            (_button("🧭 Открыть направление", f"cpm:direction:{direction.id}"),),
            (_button("🧭 Все направления", "cpm:directions:0"),),
            _back_row(),
        ),
    )


def _direction_edit_message(
    actor: TenantContext,
    direction_id: str,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str,
) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    direction = get_activity_direction(actor=actor, direction_id=direction_id)
    if direction.status != ActivityDirectionStatus.ACTIVE:
        return _stale_message()
    return _begin_owner_input_message(
        actor,
        platform=current_platform,
        surface=input_surface,
        action="activity_direction_edit",
        context={"direction_id": direction.id},
        text=(
            f"✏️ Изменить направление\n\n"
            f"Сейчас: «{direction.title}» — {direction.description}\n\n"
            "Напишите одним сообщением:\nНовое название | Новое описание"
        ),
        rows=((_button("🧭 К направлению", f"cpm:direction:{direction.id}"),), _back_row()),
    )


def _direction_edit_result(
    actor: TenantContext,
    direction_id: str,
    title: str,
    description: str,
) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    direction = update_activity_direction(
        actor=actor,
        direction_id=direction_id,
        title=title,
        description=description,
    )
    return CustomerInteractionMessage(
        text=f"✅ Направление «{direction.title}» обновлено.",
        rows=(
            (_button("🧭 Открыть", f"cpm:direction:{direction.id}"),),
            (_button("🧭 Все направления", "cpm:directions:0"),),
            _back_row(),
        ),
    )


def _direction_archive_confirm(
    actor: TenantContext,
    direction_id: str,
) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    direction = get_activity_direction(actor=actor, direction_id=direction_id)
    if direction.status != ActivityDirectionStatus.ACTIVE:
        return _stale_message()
    return CustomerInteractionMessage(
        text=(
            f"🗑 Удалить направление «{direction.title}»?\n\n"
            "Оно исчезнет из активной работы, но связанные материалы, услуги, "
            "вебинары и история сохранятся в архиве. Направление можно будет восстановить."
        ),
        rows=(
            (
                _button(
                    "🗑 Да, удалить направление",
                    f"cpm:direction-archive-ok:{direction.id}",
                ),
            ),
            (_button("Отмена", f"cpm:direction:{direction.id}"),),
            _back_row(),
        ),
    )


def _direction_archive_result(
    actor: TenantContext,
    direction_id: str,
) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    direction = archive_activity_direction(actor=actor, direction_id=direction_id)
    return CustomerInteractionMessage(
        text=(
            f"✅ Направление «{direction.title}» удалено из активной работы. "
            "Связанные данные сохранены в архиве; направление можно восстановить."
        ),
        rows=(
            (_button("📦 Архив направлений", "cpm:directions-archived:0"),),
            (_button("🧭 Активные направления", "cpm:directions:0"),),
            _back_row(),
        ),
    )


def _direction_restore_result(
    actor: TenantContext,
    direction_id: str,
) -> CustomerInteractionMessage:
    actor.assert_can_manage_business()
    direction = restore_activity_direction(actor=actor, direction_id=direction_id)
    return CustomerInteractionMessage(
        text=f"✅ Направление «{direction.title}» снова активно.",
        rows=(
            (_button("🧭 Открыть", f"cpm:direction:{direction.id}"),),
            (_button("🧭 Все направления", "cpm:directions:0"),),
            _back_row(),
        ),
    )


def _manage_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _CONNECTION_ROLES:
        return _permission_message()
    items = [nav.MESSENGERS, nav.FORMATS]
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (_button("🧭 Направления деятельности", "cpm:directions:0"),),
        (_button(nav.MESSENGERS.label, "cpm:messengers"),),
        (_button(nav.FORMATS.label, "cpm:formats"),),
    ]
    if actor.role == PlatformRole.OWNER:
        rows.append((_button(nav.DELETE_BUSINESS.label, "cpm:business-retire"),))
        items.append(nav.DELETE_BUSINESS)
    rows.append((_button(nav.SETTINGS_MORE.label, "cpm:manage-more"),))
    items.append(nav.SETTINGS_MORE)
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            "⚙️ Настроить бизнес\n\n"
            + nav.choice_help(*items)
            + "\n• изменить описание организации или управлять её направлениями → «🧭 Направления деятельности»"
        ),
        rows=tuple(rows),
    )


def _manage_more_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _CONNECTION_ROLES:
        return _permission_message()
    items = [nav.RECENT, nav.SYSTEM, nav.READINESS]
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (_button(nav.RECENT.label, "cpm:recent"),),
        (_button(nav.SYSTEM.label, "cpm:system"),),
        (_button(nav.READINESS.label, "cpm:release"),),
    ]
    if actor.role in _OWNER_ROLES:
        rows.append((_button(nav.TARIFF.label, "cpm:tariff"),))
        items.append(nav.TARIFF)
    rows.extend(((_button(nav.SETTINGS.label, "cpm:manage"),), _back_row()))
    return CustomerInteractionMessage(
        text="🛠 Состояние и история\n\nОбычно сюда заходить не нужно.\n\n"
        + nav.choice_help(*items),
        rows=tuple(rows),
    )


def _team_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _OWNER_ROLES:
        return _permission_message()
    items = (nav.ADD_MEMBER, nav.MEMBERS, nav.PERMISSIONS)
    return CustomerInteractionMessage(
        text=(
            "👤 Сотрудники и доступы\n\n"
            f"Ваш номер аккаунта ClientPlatform: {actor.user_id}\n"
            "Если другой владелец добавляет Вас в свой бизнес, отправьте ему этот номер.\n\n"
            + nav.choice_help(*items)
        ),
        rows=(
            (_button(nav.ADD_MEMBER.label, "cpm:member-add-help"),),
            (_button(nav.MEMBERS.label, "cpm:members:0"),),
            (_button(nav.PERMISSIONS.label, "cpm:permissions"),),
            _back_row(),
        ),
    )

def _today_full_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    profile = get_business_profile(actor=actor)
    summary = business_delivery_summary(actor=actor)
    capabilities = list_business_capabilities(actor=actor)
    slots = list_booking_slots(actor=actor, include_unavailable=True)
    progress = list_business_program_progress(actor=actor, limit=100)
    active_capabilities = sum(item.status == CapabilityStatus.ACTIVE for item in capabilities)
    open_slots = sum(item.slot.status == BookingSlotStatus.OPEN for item in slots)
    completed = sum(item.completed_lessons for item in progress)
    total = sum(item.total_lessons for item in progress)
    return CustomerInteractionMessage(
        text=(
            "📈 Подробная сводка\n\n"
            f"Бизнес: {_business_name(actor)}\n"
            f"Профиль: {_PROFILE_STATUS_LABELS.get(profile.status.value, profile.status.value)}\n"
            f"Клиентов: {summary.customers}\n"
            f"Программ: {summary.programs}\n"
            f"Форматов работы: {active_capabilities}\n"
            f"Свободных времён: {open_slots}\n\n"
            f"В очереди: {summary.dispatch_pending}\n"
            f"Отправлено: {summary.dispatch_sent}\n"
            f"Требуют внимания: {summary.dispatch_attention}\n"
            f"Прохождение материалов: {completed}/{total}"
        ),
        rows=((_button(nav.TODAY.label, "cpm:today"),), (_button(nav.WORK.label, "cpm:work"),), _back_row()),
    )


def _page_number(args: tuple[str, ...], *, default: int = 0) -> int:
    if not args:
        return default
    raw = args[0]
    if not raw.isdigit():
        raise ValueError("invalid native page")
    page = int(raw)
    if page < 0 or page > 10000:
        raise ValueError("native page is outside range")
    return page


def _customer_message(actor: TenantContext, customer_id: str) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    record = get_customer(actor=actor, customer_id=customer_id)
    timeline = get_customer_timeline(actor=actor, customer_id=customer_id)
    identity_lines = [
        (
            f"• {_CUSTOMER_PLATFORM_LABELS.get(item.platform.value, item.platform.value)}: @{item.username}"
            if item.username
            else f"• {_CUSTOMER_PLATFORM_LABELS.get(item.platform.value, item.platform.value)}: {item.display_name or item.external_subject}"
        )
        for item in record.identities
        if item.platform != CustomerPlatform.INTERNAL
    ]
    return CustomerInteractionMessage(
        text=(
            "Карточка клиента\n\n"
            f"Имя: {record.customer.display_name or 'не указано'}\n"
            f"Статус: {_CUSTOMER_STATUS_LABELS.get(record.customer.status.value, record.customer.status.value)}\n"
            f"Создан: {record.customer.created_at}\n\n"
            "Контакты:\n"
            + ("\n".join(identity_lines) if identity_lines else "• не подключены")
            + "\n\nИстория клиента:\n"
            + "\n".join(format_customer_timeline_lines(timeline))
        ),
        rows=((_button(nav.CUSTOMERS.label, "cpm:customers:0"),), _back_row()),
    )


def _behavior_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    progress = list_business_program_progress(actor=actor, limit=25)
    if not progress:
        text = (
            "🧠 Кто проходит материалы\n\n"
            "Пока никто не начал программу. Когда клиенты получат материалы, здесь будет видно, кто идёт дальше, а кто остановился."
        )
    else:
        lines = []
        for item in progress[:15]:
            name = item.customer_display_name or f"Клиент {item.customer_id[:8]}"
            lines.append(
                f"• {name}: «{item.program_title}» — "
                f"{item.completed_lessons}/{item.total_lessons} ({item.percent_complete}%)"
            )
        text = "🧠 Кто проходит материалы\n\n" + "\n".join(lines)
    return CustomerInteractionMessage(text=text, rows=((_button(nav.WORK.label, "cpm:work"),), _back_row()))

def _attention_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    summary = business_delivery_summary(actor=actor)
    text = (
        "⚠️ Что требует внимания\n\n"
        f"Не удалось отправить: {summary.dispatch_attention}\n"
        f"Ждут отправки: {summary.dispatch_pending}\n\n"
    )
    text += (
        "Проверьте неотправленные материалы и подключения мессенджеров."
        if summary.dispatch_attention or summary.dispatch_pending
        else "Сейчас проблем, требующих Вашего вмешательства, нет."
    )
    return CustomerInteractionMessage(text=text, rows=((_button(nav.WORK.label, "cpm:work"),), _back_row()))

def _publication_schedule_help(
    actor: TenantContext,
    publication_id: str,
) -> CustomerInteractionMessage:
    if actor.role not in _CONTENT_ROLES:
        return _permission_message()
    profile = get_business_profile(actor=actor)
    return CustomerInteractionMessage(
        text=(
            "🗓 Время публикации\n\n"
            f"Часовой пояс бизнеса: {profile.timezone}.\n"
            "Отправьте одной строкой:\n"
            f"публикация {publication_id} 28.08.2026 19:30\n\n"
            "Прошлое, несуществующее и неоднозначное местное время отклоняется."
        ),
        rows=((_button("📣 К публикациям", "cpm:publications"),), _back_row()),
    )


def _publication_schedule_result(
    actor: TenantContext,
    publication_id: str,
    local_time: str,
    *,
    interaction_key: str | None = None,
) -> CustomerInteractionMessage:
    if actor.role not in _CONTENT_ROLES:
        return _permission_message()
    try:
        publication = schedule_publication(
            actor=actor,
            publication_id=publication_id,
            local_time=local_time,
            idempotency_key=interaction_key,
        )
        profile = get_business_profile(actor=actor)
    except ValueError as exc:
        detail = str(exc)
        if "future" in detail:
            reason = "Выберите будущее время."
        elif "ambiguous" in detail:
            reason = "Это местное время неоднозначно. Выберите другое."
        elif "does not exist locally" in detail:
            reason = "Такого местного времени нет. Выберите другое."
        elif "timezone" in detail:
            reason = "Проверьте часовой пояс бизнеса."
        else:
            reason = "Проверьте публикацию и формат 28.08.2026 19:30."
        return CustomerInteractionMessage(
            text=f"Не удалось запланировать публикацию. {reason}",
            rows=((_button("📣 К публикациям", "cpm:publications"),), _back_row()),
        )
    line = format_publication_calendar_lines(
        [publication],
        timezone_name=profile.timezone,
        max_entries=1,
    )[0]
    return CustomerInteractionMessage(
        text=f"✅ Публикация запланирована.\n{line}",
        rows=((_button("📣 К публикациям", "cpm:publications"),), _back_row()),
    )


def _publication_cancel_confirm(
    actor: TenantContext,
    publication_id: str,
    schedule_version: str,
) -> CustomerInteractionMessage:
    if actor.role not in _CONTENT_ROLES:
        return _permission_message()
    try:
        decode_publication_schedule_version(schedule_version)
    except ValueError:
        return _stale_message()
    return CustomerInteractionMessage(
        text=(
            "⛔ Отменить запланированную публикацию?\n\n"
            "Это только изменит канонический статус; отправка не запускается."
        ),
        rows=(
            (
                _button(
                    "⛔ Подтвердить отмену",
                    (
                        f"cpm:publication-cancel-ok:{publication_id}:"
                        f"{schedule_version}"
                    ),
                ),
            ),
            (_button("📣 К публикациям", "cpm:publications"),),
            _back_row(),
        ),
    )


def _publication_cancel_result(
    actor: TenantContext,
    publication_id: str,
    schedule_version: str,
) -> CustomerInteractionMessage:
    if actor.role not in _CONTENT_ROLES:
        return _permission_message()
    try:
        expected_scheduled_at = decode_publication_schedule_version(schedule_version)
        publication = cancel_publication_schedule(
            actor=actor,
            publication_id=publication_id,
            expected_scheduled_at=expected_scheduled_at,
        )
    except ValueError:
        return _stale_message()
    return CustomerInteractionMessage(
        text=(
            f"✅ План публикации «{publication.title}» отменён.\n"
            "Ничего автоматически не отправлено."
        ),
        rows=((_button("📣 К публикациям", "cpm:publications"),), _back_row()),
    )


def _native_amount_minor(raw: str, currency: str) -> int:
    try:
        amount = Decimal(str(raw or "").replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("amount must be numeric") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("amount must be positive")
    exponent = settlement_currency_minor_unit_exponent(currency)
    scale = Decimal(10) ** exponent
    return int((amount * scale).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _native_amount_label(amount_minor: int, currency: str) -> str:
    exponent = settlement_currency_minor_unit_exponent(currency)
    amount = Decimal(int(amount_minor)) / (Decimal(10) ** exponent)
    rendered = f"{amount:,.{exponent}f}" if exponent else f"{amount:,.0f}"
    return f"{rendered.replace(',', ' ')} {str(currency).upper()}"


def _native_all_offerings(actor: TenantContext) -> list[Any]:
    result: list[Any] = []
    for capability in list_business_capabilities(actor=actor):
        result.extend(
            item
            for item in list_business_offerings(actor=actor, capability_id=capability.id)
            if item.status == OfferingStatus.ACTIVE
        )
    result.sort(key=lambda item: (str(item.title).casefold(), str(item.id)))
    return result


def _native_reference(items: list[Any], reference: str, *, field: str = "id") -> str | None:
    raw = str(reference or "").strip().casefold()
    if raw in {"", "-", "none"}:
        return None
    if len(raw) < 6:
        raise ValueError("reference is too short")
    matches = [
        str(getattr(item, field))
        for item in items
        if str(getattr(item, field)).casefold().startswith(raw)
    ]
    if len(matches) != 1:
        raise ValueError("reference is stale or ambiguous")
    return matches[0]


def _native_payment_summary_totals(summary: admin_ops.PaymentSummary) -> str:
    if not summary.by_currency:
        return "0,00 RUB"
    return " · ".join(
        _native_amount_label(item.amount_minor, item.currency)
        for item in summary.by_currency
    )


def _publication_new_help(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _CONTENT_ROLES:
        return _permission_message()
    channels = (
        ("🔵 ВКонтакте", "vk"),
        ("🟣 MAX", "max"),
        ("✈️ Telegram", "telegram"),
        ("📝 Другой канал", "other"),
    )
    return CustomerInteractionMessage(
        text=(
            "➕ Создать публикацию\n\n"
            "Сначала выберите, для какого канала готовим текст. "
            "На следующем шаге нужно будет написать только заголовок и сам текст.\n\n"
            "Ничего автоматически не публикуется."
        ),
        rows=tuple(
            [(_button(label, f"cpm:publication-new-for:{channel}"),) for label, channel in channels]
            + [_back_row()]
        ),
    )


def _publication_new_for_message(
    actor: TenantContext,
    channel: str,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str = "official",
) -> CustomerInteractionMessage:
    if actor.role not in _CONTENT_ROLES:
        return _permission_message()
    labels = {
        "vk": "ВКонтакте",
        "max": "MAX",
        "telegram": "Telegram",
        "other": "другого канала",
    }
    if channel not in labels:
        return _stale_message()
    return _begin_owner_input_message(
        actor,
        platform=current_platform,
        surface=input_surface,
        action="publication_draft",
        context={"channel": channel},
        text=(
            f"📝 Публикация для {labels[channel]}\n\n"
            "Напишите одним сообщением:\n"
            "Заголовок | Полный текст\n\n"
            "Например: Новая услуга | Теперь можно записаться на консультацию по субботам."
        ),
        rows=((_button(nav.PUBLICATIONS.label, "cpm:publications"),), _back_row()),
    )


def _publication_new_result(
    actor: TenantContext,
    channel: str,
    title: str,
    body: str,
    *,
    interaction_key: str,
) -> CustomerInteractionMessage:
    if actor.role not in _CONTENT_ROLES:
        return _permission_message()
    publication = admin_ops.create_publication_draft(
        actor=actor,
        title=title,
        body=body,
        channel=channel,
        idempotency_key=f"{interaction_key}:publication-create",
    )
    return CustomerInteractionMessage(
        text=f"✅ Черновик «{publication.title}» создан. Ничего автоматически не отправлено.",
        rows=((_button("📣 Публикации", "cpm:publications"),), _back_row()),
    )


def _publication_publish_result(actor: TenantContext, publication_id: str) -> CustomerInteractionMessage:
    if actor.role not in _CONTENT_ROLES:
        return _permission_message()
    publication = admin_ops.publish_publication(actor=actor, publication_id=publication_id)
    return CustomerInteractionMessage(
        text=f"✅ Публикация «{publication.title}» отмечена опубликованной.",
        rows=((_button("📣 Публикации", "cpm:publications"),), _back_row()),
    )


def _payment_new_help(
    actor: TenantContext,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str = "official",
) -> CustomerInteractionMessage:
    if actor.role not in admin_ops._FINANCE_WRITE_ROLES:
        return _permission_message()
    customers = list_customers(actor=actor)
    offerings = _native_all_offerings(actor)
    advanced = (
        "Если нужна точная привязка к клиенту или услуге, старый расширенный формат тоже сохранён: "
        "оплата 3500 RUB <код клиента> <код предложения> | комментарий."
        if customers or offerings
        else "При необходимости позже можно привязать оплату к клиенту или услуге через расширенный формат."
    )
    return _begin_owner_input_message(
        actor,
        platform=current_platform,
        surface=input_surface,
        action="payment",
        text=(
            "💳 Зафиксировать оплату\n\n"
            "Напишите сумму и валюту обычным сообщением. Например:\n"
            "3500 RUB\n\n"
            "Можно добавить комментарий через вертикальную черту:\n"
            "3500 RUB | консультация\n\n"
            + advanced
        ),
        rows=((_button(nav.PAYMENTS.label, "cpm:payments"),), _back_row()),
    )


def _payment_new_result(
    actor: TenantContext,
    amount_text: str,
    currency: str,
    customer_reference: str,
    offering_reference: str,
    note: str,
    *,
    interaction_key: str,
) -> CustomerInteractionMessage:
    if actor.role not in admin_ops._FINANCE_WRITE_ROLES:
        return _permission_message()
    customers = list_customers(actor=actor)
    offerings = _native_all_offerings(actor)
    customer_id = _native_reference(customers, customer_reference)
    offering_id = _native_reference(offerings, offering_reference)
    payment = admin_ops.record_payment(
        actor=actor,
        amount_minor=_native_amount_minor(amount_text, currency),
        currency=currency,
        customer_id=customer_id,
        offering_id=offering_id,
        note=note,
        idempotency_key=f"native-payment:{hashlib.sha256(interaction_key.encode()).hexdigest()}",
    )
    evidence = " Канонический факт выручки подтверждён." if payment.outcome_event_id else ""
    return CustomerInteractionMessage(
        text=f"✅ Оплата сохранена: {_native_amount_label(payment.amount_minor, payment.currency)}.{evidence}",
        rows=((_button("💳 Оплаты", "cpm:payments"),), _back_row()),
    )


def _payment_refund_confirm(actor: TenantContext, payment_id: str) -> CustomerInteractionMessage:
    if actor.role not in admin_ops._FINANCE_WRITE_ROLES:
        return _permission_message()
    payment = next((item for item in admin_ops.list_payments(actor=actor, limit=100) if item.id == payment_id), None)
    if payment is None or payment.status != "paid" or payment.outcome_event_id is None:
        return _stale_message()
    return CustomerInteractionMessage(
        text=(
            f"↩️ Полный возврат {_native_amount_label(payment.amount_minor, payment.currency)}?\n\n"
            "Подтверждение изменит статус оплаты и создаст отдельный канонический факт возврата."
        ),
        rows=(
            (_button("↩️ Подтвердить возврат", f"cpm:pay-refund-ok:{payment.id}"),),
            (_button("💳 Оплаты", "cpm:payments"),),
            _back_row(),
        ),
    )


def _payment_refund_result(actor: TenantContext, payment_id: str) -> CustomerInteractionMessage:
    if actor.role not in admin_ops._FINANCE_WRITE_ROLES:
        return _permission_message()
    payment = admin_ops.refund_payment(
        actor=actor,
        payment_id=payment_id,
        idempotency_key=f"native-refund:{payment_id}",
        reason="owner_confirmed_full_refund",
    )
    return CustomerInteractionMessage(
        text=f"✅ Возврат {_native_amount_label(payment.amount_minor, payment.currency)} сохранён.",
        rows=((_button("💳 Оплаты", "cpm:payments"),), _back_row()),
    )


def _price_set_help(
    actor: TenantContext,
    offering_id: str,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str = "official",
) -> CustomerInteractionMessage:
    if actor.role not in admin_ops._FINANCE_WRITE_ROLES:
        return _permission_message()
    offering = next((item for item in _native_all_offerings(actor) if str(item.id) == offering_id), None)
    if offering is None:
        return _stale_message()
    return _begin_owner_input_message(
        actor,
        platform=current_platform,
        surface=input_surface,
        action="price",
        context={"offering_id": str(offering.id)},
        text=(
            f"💵 Цена · {offering.title}\n\n"
            "Напишите только сумму и валюту. Например: 5000 RUB."
        ),
        rows=((_button(nav.PRICES.label, "cpm:prices"),), _back_row()),
    )


def _price_set_result(actor: TenantContext, offering_reference: str, amount: str, currency: str) -> CustomerInteractionMessage:
    if actor.role not in admin_ops._FINANCE_WRITE_ROLES:
        return _permission_message()
    offerings = _native_all_offerings(actor)
    offering_id = _native_reference(offerings, offering_reference)
    if offering_id is None:
        return _stale_message()
    price = admin_ops.set_offering_price(
        actor=actor,
        offering_id=offering_id,
        amount_minor=_native_amount_minor(amount, currency),
        currency=currency,
    )
    return CustomerInteractionMessage(
        text=f"✅ Цена «{price.offering_title}»: {_native_amount_label(price.amount_minor, price.currency)}.",
        rows=((_button("💡 Цены", "cpm:prices"),), _back_row()),
    )


def _invites_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _CONNECTION_ROLES:
        return _permission_message()
    insights = admin_ops.business_admin_insights(actor=actor)
    total = insights.active_invites + insights.claimed_invites
    percent = round(insights.claimed_invites * 100 / total) if total else 0
    return CustomerInteractionMessage(
        text=(
            "🎁 Приглашения и рекомендации\n\n"
            f"Активных кодов: {insights.active_invites}\n"
            f"Использовано: {insights.claimed_invites}\n"
            f"Конверсия в подключение: {percent}%\n"
            f"Клиентов всего: {insights.active_customers}"
        ),
        rows=(
            (_button("➕ Подключить клиента", "cpm:invite-new"),),
            (_button("⋯ Ещё инструменты", "cpm:growth-more"),),
            _back_row(),
        ),
    )


def _invite_new_result(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _CONNECTION_ROLES:
        return _permission_message()
    issued = issue_customer_invite(actor=actor)
    code = f"cpj_{issued.token}"
    return CustomerInteractionMessage(
        text=(
            "✅ Одноразовый код клиента создан. Он действует 7 дней.\n\n"
            f"Код: {code}\n\n"
            "Клиент может отправить его первым сообщением в Telegram, ВКонтакте или MAX:\n"
            f"start {code}\n\n"
            "Один код предназначен для одного клиента."
        ),
        rows=((_button("🎁 Приглашения", "cpm:invites"),), _back_row()),
    )


def _automation_mutation_message(actor: TenantContext, action: str, approval_id: str | None = None) -> CustomerInteractionMessage:
    if actor.role != PlatformRole.OWNER:
        return _permission_message()
    if action in {"autopilot-enable", "autopilot-disable"}:
        enabled = action == "autopilot-enable"
        admin_ops.set_autopilot_enabled(actor=actor, enabled=enabled)
        result = f"Автопилот {'включён' if enabled else 'выключен'}."
    else:
        if not approval_id:
            return _stale_message()
        operation = {
            "automation-approve": admin_ops.approve_pending_automation_action,
            "automation-reject": admin_ops.reject_pending_automation_action,
            "automation-revoke": admin_ops.revoke_approved_automation_action,
        }[action]
        try:
            operation(actor=actor, approval_id=approval_id)
        except AutomationPolicyError:
            return _stale_message()
        result = {
            "automation-approve": "Действие разрешено. Автоматическое выполнение не запущено.",
            "automation-reject": "Действие отклонено.",
            "automation-revoke": "Разрешение отозвано.",
        }[action]
    return CustomerInteractionMessage(
        text=f"✅ {result}",
        rows=((_button("🤖 Автопилот", "cpm:autopilot"),), _back_row()),
    )


def _activity_edit_help(
    actor: TenantContext,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str = "official",
) -> CustomerInteractionMessage:
    if actor.role not in _CONNECTION_ROLES:
        return _permission_message()
    profile = get_business_profile(actor=actor)
    return _begin_owner_input_message(
        actor,
        platform=current_platform,
        surface=input_surface,
        action="activity_description",
        text=(
            "✏️ Изменить направление\n\n"
            f"Сейчас написано:\n{profile.activity_description}\n\n"
            "Напишите новое направление или описание деятельности обычным сообщением — без команды «деятельность»."
        ),
        rows=((_button(nav.COPY.label, "cpm:copy"),), _back_row()),
    )


def _activity_edit_result(actor: TenantContext, description: str) -> CustomerInteractionMessage:
    if actor.role not in _CONNECTION_ROLES:
        return _permission_message()
    profile = get_business_profile(actor=actor)
    updated = save_business_profile(
        actor=actor,
        activity_description=description,
        timezone_name=profile.timezone,
    )
    return CustomerInteractionMessage(
        text=f"✅ Описание деятельности обновлено: {updated.activity_description}",
        rows=((_button("✍️ Тексты", "cpm:copy"),), _back_row()),
    )


def _member_add_help(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role != PlatformRole.OWNER:
        return _permission_message()
    roles = (
        ("💬 Менеджер", "manager"),
        ("✍️ Контент", "content"),
        ("📣 Маркетинг", "marketing"),
        ("📊 Аналитика", "analytics"),
        ("🛟 Поддержка", "support"),
    )
    return CustomerInteractionMessage(
        text=(
            "➕ Добавить сотрудника\n\n"
            "Сначала выберите, что сотрудник будет делать. Затем ClientPlatform попросит номер его аккаунта.\n\n"
            "Где взять номер: сотрудник открывает ClientPlatform → «Сотрудники и доступы» и отправляет Вам свой номер.\n\n"
            "Старый расширенный формат «сотрудник <номер> <роль>» тоже продолжает работать."
        ),
        rows=tuple(
            [(_button(label, f"cpm:member-add-role:{role}"),) for label, role in roles]
            + [_back_row()]
        ),
    )


def _member_add_role_message(
    actor: TenantContext,
    role_code: str,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str = "official",
) -> CustomerInteractionMessage:
    if actor.role != PlatformRole.OWNER:
        return _permission_message()
    role = _MEMBER_ROLE_CODES.get(role_code)
    if role is None:
        return _stale_message()
    commerce = get_commerce_overview(actor=actor)
    if commerce.staff.requires_upgrade:
        return CustomerInteractionMessage(
            text=(
                "👥 Добавить сотрудника\n\n"
                f"Сейчас занято {commerce.staff.used} из {commerce.staff.allowance} мест. "
                "Следующий сотрудник требует расширения тарифа, поэтому ввод данных не начат."
            ),
            rows=((_button(nav.TARIFF.label, "cpm:tariff"),), _back_row()),
        )
    return _begin_owner_input_message(
        actor,
        platform=current_platform,
        surface=input_surface,
        action="member_user",
        context={"role_code": role_code},
        text=(
            f"👤 Новый сотрудник · {_ROLE_LABELS[role]}\n\n"
            "Теперь напишите номер аккаунта ClientPlatform сотрудника — только цифры. "
            "Сотрудник увидит этот номер в своём разделе «Сотрудники и доступы». "
            "Роль можно будет изменить позже."
        ),
        rows=((_button(nav.TEAM.label, "cpm:team"),), _back_row()),
    )


def _member_add_result(actor: TenantContext, user_id: str, role_code: str) -> CustomerInteractionMessage:
    if actor.role != PlatformRole.OWNER:
        return _permission_message()
    role = _MEMBER_ROLE_CODES.get(role_code)
    if role is None:
        return _stale_message()
    commerce = get_commerce_overview(actor=actor)
    if commerce.staff.requires_upgrade:
        return CustomerInteractionMessage(
            text=(
                "Лимит сотрудников изменился до завершения операции. "
                f"Сейчас занято {commerce.staff.used} из {commerce.staff.allowance}; "
                "сотрудник не добавлен."
            ),
            rows=((_button(nav.TARIFF.label, "cpm:tariff"),), _back_row()),
        )
    member = grant_business_member(actor=actor, user_id=int(user_id), role=role)
    return CustomerInteractionMessage(
        text=f"✅ Сотрудник добавлен. Номер аккаунта: {member.user_id}. Роль: {_ROLE_LABELS[member.role]}.",
        rows=((_button("👥 Команда", "cpm:team"),), _back_row()),
    )


def _member_role_result(actor: TenantContext, user_id: int, role_code: str) -> CustomerInteractionMessage:
    if actor.role != PlatformRole.OWNER:
        return _permission_message()
    role = _MEMBER_ROLE_CODES.get(role_code)
    if role is None:
        return _stale_message()
    member = grant_business_member(actor=actor, user_id=user_id, role=role)
    return CustomerInteractionMessage(
        text=f"✅ Роль сотрудника {member.user_id}: {_ROLE_LABELS[member.role]}.",
        rows=((_button("👥 К сотруднику", f"cpm:member:{member.user_id}"),), _back_row()),
    )


def _member_revoke_result(actor: TenantContext, user_id: int) -> CustomerInteractionMessage:
    if actor.role != PlatformRole.OWNER:
        return _permission_message()
    member = revoke_business_member(actor=actor, user_id=user_id)
    return CustomerInteractionMessage(
        text=f"✅ Доступ сотрудника {member.user_id} отозван.",
        rows=((_button("👥 Команда", "cpm:members:0"),), _back_row()),
    )


def _growth_report_message(actor: TenantContext, action: str) -> CustomerInteractionMessage:
    allowed = {
        "autopilot": _AUTOMATION_ROLES,
        "publications": _CONTENT_ROLES,
        "funnel": _MARKETING_ROLES,
        "money": _MARKETING_ROLES,
        "payments": _MARKETING_ROLES,
        "segments": _MARKETING_ROLES,
        "offers": _CONTENT_ROLES | _MARKETING_ROLES,
        "copy": _CONTENT_ROLES | _MARKETING_ROLES,
        "prices": _MARKETING_ROLES,
    }
    if actor.role not in allowed[action]:
        return _permission_message()

    if action == "autopilot":
        profile = get_business_profile(actor=actor)
        enabled = admin_ops.get_autopilot_enabled(actor=actor)
        approvals = sorted(
            admin_ops.get_current_automation_action_approvals(actor=actor),
            key=lambda item: (item.status.value != "pending", item.requested_at, item.id),
        )[:3]
        lines = [
            "🤖 Автоматические действия",
            "",
            f"Статус: {'включён' if enabled else 'выключен'}",
            "Здесь видно, какие действия система может выполнять автоматически. Внешние действия без разрешения не запускаются.",
        ]
        rows: list[tuple[CustomerInteractionButton, ...]] = []
        if approvals:
            lines.extend(["", "Решения владельца:"])
            for index, item in enumerate(approvals, start=1):
                lines.append(
                    f"{index}. {admin_ops.format_automation_action_approval(item, timezone_name=profile.timezone)}"
                )
                if actor.role == PlatformRole.OWNER:
                    if item.status.value == "pending":
                        rows.append(
                            (
                                _button(f"✅ Разрешить #{index}", f"cpm:automation-approve:{item.id}"),
                                _button(f"⛔ Отклонить #{index}", f"cpm:automation-reject:{item.id}"),
                            )
                        )
                    elif item.status.value == "approved":
                        rows.append(
                            (_button(f"↩️ Отозвать #{index}", f"cpm:automation-revoke:{item.id}"),)
                        )
        if actor.role == PlatformRole.OWNER:
            rows.insert(
                0,
                (
                    _button(
                        "⏸ Выключить" if enabled else "▶️ Включить",
                        "cpm:autopilot-disable" if enabled else "cpm:autopilot-enable",
                    ),
                ),
            )
        rows.extend(((_button("📈 Рост", "cpm:growth"),), _back_row()))
        return CustomerInteractionMessage(text="\n".join(lines), rows=tuple(rows))

    if action == "publications":
        profile = get_business_profile(actor=actor)
        projection = get_publication_calendar_projection(actor=actor)
        calendar = "\n".join(
            format_publication_calendar_lines(
                projection.entries,
                timezone_name=profile.timezone,
                max_entries=8,
            )
        )
        publication_rows: list[tuple[CustomerInteractionButton, ...]] = [
            (_button("➕ Создать черновик", "cpm:publication-new"),),
            (_button("🗑 Убрать публикацию", "cpm:publication-retire-list:0"),),
        ]
        for draft_item in projection.actionable_drafts[:2]:
            publication_rows.append(
                (
                    _button("🗓 Запланировать", f"cpm:publication-schedule:{draft_item.id}"),
                    _button("✅ Опубликована", f"cpm:publication-publish:{draft_item.id}"),
                )
            )
        scheduled = [entry for entry in projection.entries if entry.status == "scheduled"]
        for scheduled_item in scheduled[:1]:
            publication_rows.append(
                (
                    _button("🕒 Перенести", f"cpm:publication-schedule:{scheduled_item.id}"),
                    _button(
                        "⛔ Отменить",
                        f"cpm:publication-cancel:{scheduled_item.id}:{encode_publication_schedule_version(scheduled_item.scheduled_at or '')}",
                    ),
                )
            )
        publication_rows.extend(((_button("📈 Рост", "cpm:growth"),), _back_row()))
        return CustomerInteractionMessage(
            text=(
                "📣 Публикации\n\n"
                f"Черновики: {projection.draft_count}\n"
                f"Запланировано: {projection.scheduled_count}\n"
                f"Опубликовано: {projection.published_count}\n"
                f"Ошибки: {projection.failed_count}\n\n"
                f"Ближайшие и последние:\n{calendar}"
            ),
            rows=tuple(publication_rows),
        )

    if action in {"funnel", "segments", "offers", "copy", "money", "payments", "prices"}:
        profile = get_business_profile(actor=actor)
        summary = business_delivery_summary(actor=actor)
        capabilities = list_business_capabilities(actor=actor)
        offerings = _native_all_offerings(actor)
        progress = list_business_program_progress(actor=actor, limit=100)
        enrolled_ids = {item.customer_id for item in progress}
        completed_ids = {
            item.customer_id
            for item in progress
            if item.total_lessons > 0 and item.completed_lessons >= item.total_lessons
        }
        stalled_ids = {
            item.customer_id
            for item in progress
            if item.total_lessons > item.completed_lessons
        }
        insights = admin_ops.business_admin_insights(actor=actor)

        if action == "funnel":
            total_invites = insights.active_invites + insights.claimed_invites
            invite_rate = round(insights.claimed_invites * 100 / total_invites) if total_invites else 0
            return CustomerInteractionMessage(
                text=(
                    f"{nav.PROGRAM_PROGRESS.label}\n\n"
                    f"Создано приглашений: {total_invites}\n"
                    f"Принято: {insights.claimed_invites} ({invite_rate}%)\n"
                    f"Клиентов: {insights.active_customers}\n"
                    f"В программах: {insights.enrollments}\n"
                    f"Завершили: {insights.completed_enrollments}\n"
                    f"Оплат: {insights.paid_payments}"
                ),
                rows=((_button("📈 Рост", "cpm:growth"),), _back_row()),
            )

        payments = admin_ops.list_payments(actor=actor, limit=20) if actor.role in admin_ops._FINANCE_READ_ROLES else []
        payment_facts = (
            admin_ops.payment_summary(actor=actor)
            if actor.role in admin_ops._FINANCE_READ_ROLES
            else admin_ops.PaymentSummary(paid_payments=0, paid_customers=0, by_currency=())
        )
        prices = admin_ops.list_offering_prices(actor=actor) if actor.role in admin_ops._FINANCE_READ_ROLES else []
        price_by_offering = {item.offering_id: item for item in prices}

        if action == "money":
            money_rows: list[tuple[CustomerInteractionButton, ...]] = []
            if actor.role in admin_ops._FINANCE_WRITE_ROLES:
                money_rows.append((_button("➕ Зафиксировать оплату", "cpm:payment-new"),))
            money_rows.extend(((_button("📈 Рост", "cpm:growth"),), _back_row()))
            return CustomerInteractionMessage(
                text=(
                    "💰 Выручка и платящие клиенты\n\n"
                    f"Оплачено: {_native_payment_summary_totals(payment_facts)}\n"
                    f"Успешных оплат: {payment_facts.paid_payments}\n"
                    f"Платящих клиентов: {payment_facts.paid_customers}\n"
                    f"Всего клиентов: {insights.active_customers}"
                ),
                rows=tuple(money_rows),
            )

        if action == "payments":
            recent = "\n".join(
                f"• {_native_amount_label(payment_item.amount_minor, payment_item.currency)} · {payment_item.note[:35] or payment_item.provider} · {payment_item.status}"
                for payment_item in payments[:10]
            ) or "• Оплат пока нет"
            payment_rows: list[tuple[CustomerInteractionButton, ...]] = []
            if actor.role in admin_ops._FINANCE_WRITE_ROLES:
                payment_rows.append((_button("➕ Зафиксировать оплату", "cpm:payment-new"),))
                for refund_candidate in [
                    payment
                    for payment in payments
                    if payment.status == "paid" and payment.outcome_event_id is not None
                ][:4]:
                    payment_rows.append(
                        (_button(f"↩️ Возврат · {_native_amount_label(refund_candidate.amount_minor, refund_candidate.currency)}", f"cpm:pay-refund:{refund_candidate.id}"),)
                    )
            payment_rows.extend(((_button("📈 Рост", "cpm:growth"),), _back_row()))
            return CustomerInteractionMessage(
                text=(
                    "💳 Оплаты\n\n"
                    f"Успешных: {payment_facts.paid_payments}\n"
                    f"Сумма: {_native_payment_summary_totals(payment_facts)}\n\n{recent}"
                ),
                rows=tuple(payment_rows),
            )

        if action == "segments":
            return CustomerInteractionMessage(
                text=(
                    "👥 Группы клиентов\n\n"
                    f"Новые / без программы: {max(0, insights.active_customers - len(enrolled_ids))}\n"
                    f"Проходят программу: {len(enrolled_ids - completed_ids)}\n"
                    f"Завершили: {len(completed_ids)}\n"
                    f"Остановились: {len(stalled_ids)}\n"
                    f"Платящие: {payment_facts.paid_customers}"
                ),
                rows=((_button("📈 Рост", "cpm:growth"),), _back_row()),
            )

        if action == "offers":
            offer_lines = "\n".join(
                f"• {item.title[:40]} — "
                + (
                    _native_amount_label(price_by_offering[item.id].amount_minor, price_by_offering[item.id].currency)
                    if item.id in price_by_offering
                    else "цена не задана"
                )
                for item in offerings[:12]
            ) or "• Предложения ещё не созданы"
            offer_rows: list[tuple[CustomerInteractionButton, ...]] = []
            if actor.role in _PROGRAM_MANAGEMENT_ROLES:
                offer_rows.append((_button("➕ Создать предложение", "cpm:offering-new"),))
                offer_rows.append((_button("🗑 Убрать предложение", "cpm:offering-retire-list:0"),))
            if actor.role in _MARKETING_ROLES:
                offer_rows.append((_button("💡 Настроить цены", "cpm:prices"),))
            offer_rows.extend(((_button("📈 Рост", "cpm:growth"),), _back_row()))
            return CustomerInteractionMessage(
                text=(
                    "🧪 Услуги и предложения\n\n"
                    f"Активных предложений: {len(offerings)}\n"
                    f"С ценой: {len(price_by_offering)}\n"
                    f"Без цены: {max(0, len(offerings) - len(price_by_offering))}\n\n"
                    f"{offer_lines}"
                ),
                rows=tuple(offer_rows),
            )

        if action == "copy":
            copy_rows: list[tuple[CustomerInteractionButton, ...]] = []
            if actor.role in _CONNECTION_ROLES:
                copy_rows.append((_button("✏️ Изменить направление", "cpm:activity-edit-help"),))
            if actor.role in _CONTENT_ROLES:
                copy_rows.append((_button("➕ Создать публикацию", "cpm:publication-new"),))
            copy_rows.extend(((_button("📈 Рост", "cpm:growth"),), _back_row()))
            return CustomerInteractionMessage(
                text=(
                    "✍️ Подготовить текст\n\n"
                    f"Основа бренда:\n{profile.activity_description}\n\n"
                    "Готовая структура:\n"
                    "1. Кому помогает бизнес.\n"
                    "2. Какой конкретный результат получает клиент.\n"
                    "3. Как проходит работа.\n"
                    "4. Один понятный следующий шаг."
                ),
                rows=tuple(copy_rows),
            )

        if action == "prices":
            price_lines = "\n".join(
                f"• {item.title[:36]} — "
                + (
                    _native_amount_label(price_by_offering[item.id].amount_minor, price_by_offering[item.id].currency)
                    if item.id in price_by_offering
                    else "не задана"
                )
                for item in offerings[:12]
            ) or "• Сначала создайте предложение"
            price_rows: list[tuple[CustomerInteractionButton, ...]] = []
            if actor.role in admin_ops._FINANCE_WRITE_ROLES:
                price_rows.extend(
                    (_button(f"💵 Цена · {offering_item.title[:22]}", f"cpm:price-set:{offering_item.id}"),)
                    for offering_item in offerings[:6]
                )
            price_rows.extend(((_button("📈 Рост", "cpm:growth"),), _back_row()))
            return CustomerInteractionMessage(
                text=(
                    "💵 Цены\n\n"
                    f"Предложений: {len(offerings)}\n"
                    f"Цены заполнены: {len(price_by_offering)}/{len(offerings)}\n"
                    f"Зафиксированная выручка: {_native_payment_summary_totals(payment_facts)}\n\n"
                    f"{price_lines}"
                ),
                rows=tuple(price_rows),
            )

    raise ValueError("unknown native growth report")


def _admin_report_message(actor: TenantContext, action: str) -> CustomerInteractionMessage:
    if actor.role not in _CONNECTION_ROLES:
        return _permission_message()
    profile = get_business_profile(actor=actor)
    summary = business_delivery_summary(actor=actor)
    capabilities = list_business_capabilities(actor=actor)
    slots = list_booking_slots(actor=actor, include_unavailable=True)
    customers = list_customers(actor=actor)
    programs = list_programs(actor=actor)
    progress = list_business_program_progress(actor=actor, limit=100)
    active = sum(item.status == CapabilityStatus.ACTIVE for item in capabilities)
    open_slots = sum(item.slot.status == BookingSlotStatus.OPEN for item in slots)
    enrolled = len({item.customer_id for item in progress})
    complete = sum(
        item.total_lessons > 0 and item.completed_lessons >= item.total_lessons
        for item in progress
    )
    incomplete = [item for item in progress if item.completed_lessons < item.total_lessons]
    recent_items = sorted(
        [
            (str(item.created_at), f"Клиент: {item.display_name or item.id[:8]}")
            for item in customers
        ]
        + [(str(item.created_at), f"Программа: {item.title}") for item in programs]
        + [(str(item.updated_at), f"Прогресс: {item.program_title}") for item in progress],
        reverse=True,
    )[:10]
    release_ok = (
        profile.status.value == "ready"
        and active > 0
        and summary.dispatch_attention == 0
    )
    sections = {
        "release": (
            "✅ Проверить готовность\n\n"
            f"Профиль бизнеса: {'✅' if profile.status.value == 'ready' else '❌'}\n"
            f"Форматы работы: {'✅' if active else '❌'}\n"
            f"Ошибки отправки: {'✅' if summary.dispatch_attention == 0 else '❌'}\n\n"
            f"Итог: {'ГОТОВО' if release_ok else 'ТРЕБУЕТ НАСТРОЙКИ'}"
        ),
        "funnel2": (
            "🧭 Путь клиента\n\n"
            f"Клиенты: {summary.customers}\nВ программах: {enrolled}\n"
            f"Завершили: {complete}\nДоступных записей: {open_slots}\n"
            f"Отправлено материалов: {summary.dispatch_sent}"
        ),
        "retention": (
            "♻️ Кого стоит вернуть\n\n"
            f"Клиентов всего: {summary.customers}\n"
            f"Незавершённых прохождений: {len(incomplete)}\n"
            f"Без активной программы: {max(0, summary.customers - enrolled)}"
        ),
        "recent": (
            "🧾 История изменений\n\n"
            + (
                "\n".join(f"• {label} — {stamp}" for stamp, label in recent_items)
                if recent_items
                else "Действий пока нет."
            )
        ),
        "system": (
            "🛠 Проверка системы\n\n"
            "Доступ к бизнесу: ✅\nБаза данных: ✅\n"
            f"Профиль бизнеса: {'✅' if profile.status.value == 'ready' else '⚠️'}\n"
            f"Очередь отправки: {'✅' if summary.dispatch_attention == 0 else '⚠️'}\n"
            f"Программы: {summary.programs}\nКлиенты: {summary.customers}"
        ),
    }
    return CustomerInteractionMessage(
        text=sections[action],
        rows=((_button(nav.SETTINGS.label, "cpm:manage"),), _back_row()),
    )


def _formats_message(actor: TenantContext, page: int = 0) -> CustomerInteractionMessage:
    if actor.role not in _CONNECTION_ROLES:
        return _permission_message()
    capabilities = list_business_capabilities(actor=actor, include_disabled=True)
    page_size = 6
    count = max(1, (len(capabilities) + page_size - 1) // page_size)
    if page >= count:
        raise ValueError("format page is outside result set")
    current = capabilities[page * page_size : (page + 1) * page_size]
    lines = [
        f"{'✅' if item.status == CapabilityStatus.ACTIVE else '➖'} {item.title}"
        for item in current
    ]
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    for item in current:
        if item.status == CapabilityStatus.ACTIVE:
            rows.append(
                (_button(f"➖ Выключить · {item.title[:20]}", f"cpm:format-disable:{item.connector_key}"),)
            )
        else:
            rows.append(
                (_button(f"✅ Включить · {item.title[:20]}", f"cpm:format-enable:{item.connector_key}"),)
            )
    pagination: list[CustomerInteractionButton] = []
    if page > 0:
        pagination.append(_button("◀️ Предыдущая страница", f"cpm:formats:{page - 1}"))
    if page + 1 < count:
        pagination.append(_button("Вперёд ➡️", f"cpm:formats:{page + 1}"))
    if pagination:
        rows.append(tuple(pagination))
    rows.append((_button(nav.SETTINGS.label, "cpm:manage"),))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            "🧰 Услуги и формат работы\n\n"
            "Здесь Вы указываете, что именно предлагаете клиентам. Включите нужные форматы, ненужные можно выключить.\n\n"
            + ("\n".join(lines) if lines else "Форматы ещё не выбраны.")
            + f"\n\nСтраница {page + 1}/{count}"
        ),
        rows=tuple(rows),
    )


def _format_toggle_result(actor: TenantContext, connector_key: str, *, enabled: bool) -> CustomerInteractionMessage:
    if actor.role not in _CONNECTION_ROLES:
        return _permission_message()
    current = next(
        (item for item in list_business_capabilities(actor=actor, include_disabled=True) if item.connector_key == connector_key),
        None,
    )
    if current is None:
        return _stale_message()
    changed = (
        enable_business_capability(actor=actor, connector_key=connector_key, title=current.title)
        if enabled
        else disable_business_capability(actor=actor, connector_key=connector_key)
    )
    return CustomerInteractionMessage(
        text=f"✅ Формат «{changed.title}» {'включён' if enabled else 'выключен'}.",
        rows=((_button(nav.FORMATS.label, "cpm:formats:0"),), _back_row()),
    )


def _tariff_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _OWNER_ROLES:
        return _permission_message()
    subscription = admin_ops.get_subscription_state(actor=actor)
    commerce = get_commerce_overview(actor=actor)
    staff_next = (
        "потребуется расширение"
        if commerce.staff.requires_upgrade
        else f"входит ({commerce.staff.projected_total}/{commerce.staff.allowance})"
    )
    customer_next = (
        "потребуется расширение"
        if commerce.customers.requires_upgrade
        else f"входит ({commerce.customers.projected_total}/{commerce.customers.allowance})"
    )
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if commerce.expansion_recommended:
        rows.append((_button("🛟 Запросить расширение", "cpm:tariff-upgrade"),))
    rows.extend(((_button(nav.SETTINGS.label, "cpm:manage"),), _back_row()))
    return CustomerInteractionMessage(
        text=(
            "💳 Тариф и лимиты\n\n"
            f"Тариф: {subscription.plan_key} · {subscription.status}\n"
            f"Сотрудники: {commerce.staff.used} из {commerce.staff.allowance}\n"
            f"Клиенты: {commerce.customers.used} из {commerce.customers.allowance}\n\n"
            "🧠 Умный контроль тарифа\n"
            f"Следующий сотрудник: {staff_next}\n"
            f"Следующий клиент: {customer_next}\n\n"
            "ClientPlatform не меняет тариф и не списывает деньги автоматически."
        ),
        rows=tuple(rows),
    )


def _tariff_upgrade_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _OWNER_ROLES:
        return _permission_message()
    case = request_subscription_expansion(actor=actor)
    return CustomerInteractionMessage(
        text=(
            "✅ Запрос на расширение тарифа создан.\n\n"
            f"Номер обращения: {case.id}\n"
            "Никаких списаний или изменений тарифа автоматически не выполнено."
        ),
        rows=((_button(nav.TARIFF.label, "cpm:tariff"),), _back_row()),
    )


def _list_members(actor: TenantContext) -> list[dict[str, Any]]:
    if actor.role not in _OWNER_ROLES:
        raise TenantPermissionDenied("team section is owner-only")
    with get_db_ro() as conn:
        current = TenancyRepository(conn).resolve_context(
            user_id=actor.user_id,
            business_id=actor.business_id,
        )
        current.assert_can_manage_business()
        rows = conn.execute(
            """
            SELECT user_id, role, status
            FROM business_members
            WHERE business_id=?
            ORDER BY CASE WHEN role='owner' THEN 0 ELSE 1 END, created_at, id
            """,
            (current.business_id,),
        ).fetchall()
    return [
        {
            "user_id": int(row["user_id"] if hasattr(row, "keys") else row[0]),
            "role": str(row["role"] if hasattr(row, "keys") else row[1]),
            "status": str(row["status"] if hasattr(row, "keys") else row[2]),
        }
        for row in rows
    ]


def _members_message(actor: TenantContext, page: int) -> CustomerInteractionMessage:
    members = _list_members(actor)
    page_size = 6
    count = max(1, (len(members) + page_size - 1) // page_size)
    if page >= count:
        raise ValueError("member page is outside result set")
    current = members[page * page_size : (page + 1) * page_size]
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    lines: list[str] = []
    for item in current:
        role = PlatformRole(item["role"])
        marker = "✅" if item["status"] == "active" else "➖"
        label = f"{marker} Аккаунт {item['user_id']} · {_ROLE_LABELS.get(role, role.value)}"
        lines.append(f"• {label}")
        rows.append((_button(label, f"cpm:member:{item['user_id']}"),))
    navigation: list[CustomerInteractionButton] = []
    if page > 0:
        navigation.append(_button("◀️ Предыдущая страница", f"cpm:members:{page - 1}"))
    if page + 1 < count:
        navigation.append(_button("Вперёд ➡️", f"cpm:members:{page + 1}"))
    if navigation:
        rows.append(tuple(navigation))
    rows.append((_button("👥 Команда", "cpm:team"),))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            "Роли команды\n\n"
            + ("\n".join(lines) if lines else "Сотрудников пока нет.")
            + f"\n\nСтраница {page + 1}/{count}"
        ),
        rows=tuple(rows),
    )


def _member_message(actor: TenantContext, user_id: int) -> CustomerInteractionMessage:
    member = next((item for item in _list_members(actor) if item["user_id"] == user_id), None)
    if member is None:
        raise ValueError("member was not found")
    role = PlatformRole(member["role"])
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if role != PlatformRole.OWNER and member["status"] == "active":
        role_buttons = [
            _button(_ROLE_LABELS[target], f"cpm:member-role:{user_id}:{code}")
            for code, target in _MEMBER_ROLE_CODES.items()
            if target != role
        ]
        for index in range(0, len(role_buttons), 2):
            rows.append(tuple(role_buttons[index : index + 2]))
        rows.append((_button("⛔ Отозвать доступ", f"cpm:member-revoke:{user_id}"),))
    rows.extend(((_button("👥 К команде", "cpm:members:0"),), _back_row()))
    return CustomerInteractionMessage(
        text=(
            "Сотрудник\n\n"
            f"Номер аккаунта ClientPlatform: {user_id}\n"
            f"Роль: {_ROLE_LABELS.get(role, role.value)}\n"
            f"Статус: {member['status']}"
        ),
        rows=tuple(rows),
    )


def _permissions_message(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _OWNER_ROLES:
        return _permission_message()
    permissions = {
        PlatformRole.OWNER: "Все разделы, сотрудники, клиенты, подключения",
        PlatformRole.ADMINISTRATOR: "Бизнес, клиенты, аналитика, подключения",
        PlatformRole.MANAGER: "Клиенты, записи, программы, операционная аналитика",
        PlatformRole.CONTENT_MANAGER: "Программы, материалы, публикации",
        PlatformRole.MARKETER: "Продвижение, группы клиентов, услуги и предложения",
        PlatformRole.ANALYST: "Отчёты, путь клиента, возврат клиентов",
        PlatformRole.SUPPORT: "Клиенты, проблемные отправки, поддержка",
    }
    lines = [
        f"{_ROLE_LABELS[role]}: {text}"
        for role, text in permissions.items()
    ]
    return CustomerInteractionMessage(
        text="Доступы сотрудников\n\n" + "\n\n".join(lines),
        rows=((_button("👥 Команда", "cpm:team"),), _back_row()),
    )


def _native_money_text(rows: Any, *, empty: str) -> str:
    values = tuple(rows or ())
    if not values:
        return empty
    rendered: list[str] = []
    for item in values:
        exponent = settlement_currency_minor_unit_exponent(item.currency)
        amount = Decimal(int(item.amount_minor)) / (Decimal(10) ** exponent)
        amount_text = f"{amount:,.{exponent}f}" if exponent else f"{amount:,.0f}"
        rendered.append(f"{amount_text.replace(',', ' ')} {str(item.currency).upper()}")
    return ", ".join(rendered)


def _native_journey_text(snapshot) -> str:
    journey = snapshot.journey
    best = next((item for item in journey.sources if item.source.value != "unknown"), None)
    if best is None:
        best_text = "пока недостаточно подтверждённых данных"
    else:
        label = acquisition_source_label(best.source)
        revenue = _native_money_text(
            best.revenue_by_currency,
            empty="выручка пока не подтверждена",
        )
        best_text = f"{label} — {revenue} · оплативших: {best.paid_customers}"
    verified = _native_money_text(
        journey.verified_revenue_by_currency,
        empty="пока нет подтверждённой выручки",
    )
    attributed = _native_money_text(
        journey.attributed_revenue_by_currency,
        empty="пока не связана с источниками",
    )
    unattributed = _native_money_text(
        journey.unattributed_revenue_by_currency,
        empty="0",
    )
    completion_text = (
        "нет подтверждённых данных"
        if "booking_completion_unavailable" in getattr(journey, "limitations", ())
        else str(journey.completed_bookings)
    )
    return (
        f"\n\nДеньги и путь клиента · {snapshot.period_days} дней\n"
        f"• Лиды: {journey.leads} → записи: {journey.bookings} → "
        f"пришли: {completion_text} → оплатили: {journey.paid_customers}\n"
        f"• Вернувшиеся клиенты: {journey.reactivated_customers}\n"
        f"• Подтверждённая выручка: {verified}\n"
        f"• Связано с источником: {attributed}\n"
        f"• Без подтверждённого источника: {unattributed}\n"
        f"• Лучший подтверждённый источник: {best_text}"
    )


def _today_message(actor: TenantContext) -> CustomerInteractionMessage:
    summary = business_delivery_summary(actor=actor)
    action_lines: list[str] = []
    primary_action_button: CustomerInteractionButton | None = None
    try:
        snapshot = get_growth_cockpit(
            actor=actor,
            period_days=7,
            advertising_loader=lambda **_kwargs: None,
        )
    except (TenantAccessDenied, TenantPermissionDenied, ValueError):
        snapshot = None
    except OSError:
        snapshot = None
    except RuntimeError:
        snapshot = None
    journey_text = ""
    if snapshot is not None:
        action_lines = [
            f"{index}. {item.title} — {item.reason}"
            for index, item in enumerate(snapshot.actions[:5], start=1)
        ]
        journey_text = _native_journey_text(snapshot)
        primary_action_button = _native_growth_action_button(actor, snapshot.next_action)
        if primary_action_button is not None and primary_action_button.command == "cpm:today":
            primary_action_button = None

    action_text = (
        "\n\nВажные действия\n" + "\n".join(action_lines)
        if action_lines
        else "\n\nВажные действия\n• Срочных действий нет или они недоступны для этой роли."
    )
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if primary_action_button is not None:
        rows.append((primary_action_button,))
    rows.extend(
        [
            (_button(nav.CUSTOMERS.label, "cpm:customers:0"),),
            (_button(nav.BOOKINGS.label, "cpm:bookings"),),
            _back_row(),
        ]
    )
    return CustomerInteractionMessage(
        text=(
            "📊 Что сегодня происходит\n\n"
            + f"Клиентов: {summary.customers}\n"
            + f"Программ: {summary.programs}\n"
            + f"В очереди отправки: {summary.dispatch_pending}\n"
            + f"Отправлено: {summary.dispatch_sent}\n"
            + f"Требуют внимания: {summary.dispatch_attention}"
            + journey_text
            + action_text
        ),
        rows=tuple(rows),
    )


def _customers_message(actor: TenantContext, page: int = 0) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    customers = list_customers(actor=actor)
    page_size = 6
    count = max(1, (len(customers) + page_size - 1) // page_size)
    if page >= count:
        raise ValueError("customer page is outside result set")
    current = customers[page * page_size : (page + 1) * page_size]
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    lines: list[str] = []
    for customer in current:
        name = str(customer.display_name or "").strip() or f"Клиент {customer.id[:8]}"
        lines.append(f"• {name}")
        rows.append((_button(name, f"cpm:customer:{customer.id}"),))
    if not customers and actor.role in _CONNECTION_ROLES:
        rows.append((_button(nav.INVITES.label, "cpm:invites"),))
    navigation: list[CustomerInteractionButton] = []
    if page > 0:
        navigation.append(_button("◀️ Предыдущая страница", f"cpm:customers:{page - 1}"))
    if page + 1 < count:
        navigation.append(_button("Вперёд ➡️", f"cpm:customers:{page + 1}"))
    if navigation:
        rows.append(tuple(navigation))
    rows.append((_button(nav.WORK.label, "cpm:work"),))
    rows.append(_back_row())
    body = (
        "Нажмите имя, чтобы открыть карточку клиента и его историю."
        if customers
        else "Клиентов пока нет. Чтобы добавить первого клиента, создайте персональную ссылку-приглашение."
    )
    return CustomerInteractionMessage(
        text=(
            "👥 Клиенты\n\n"
            + body
            + ("\n\n" + "\n".join(lines) if lines else "")
            + f"\n\nСтраница {page + 1}/{count}"
        ),
        rows=tuple(rows),
    )

def _active_booking_offerings(actor: TenantContext) -> list[Any]:
    offerings: list[Any] = []
    for capability in list_business_capabilities(actor=actor):
        offerings.extend(
            item
            for item in list_business_offerings(
                actor=actor,
                capability_id=capability.id,
            )
            if item.status == OfferingStatus.ACTIVE
        )
    offerings.sort(key=lambda item: (str(item.title).casefold(), str(item.id)))
    return offerings


def _booking_offering_reference(actor: TenantContext, reference: str) -> Any:
    needle = str(reference or "").strip().casefold()
    if len(needle) < 6:
        raise ValueError("booking offering reference is too short")
    matches = [
        item
        for item in _active_booking_offerings(actor)
        if str(item.id).casefold().startswith(needle)
    ]
    if len(matches) != 1:
        raise ValueError("booking offering reference is stale or ambiguous")
    return matches[0]


def _booking_open_message(actor: TenantContext, page: int = 0) -> CustomerInteractionMessage:
    if actor.role not in _BOOKING_MANAGEMENT_ROLES:
        return _permission_message()
    offerings = _active_booking_offerings(actor)
    if not offerings:
        return CustomerInteractionMessage(
            text=(
                "🕒 Добавить свободное время\n\n"
                "Сначала нужна активная услуга. Откройте «Услуги и формат работы», "
                "создайте или включите услугу, затем вернитесь сюда."
            ),
            rows=((_button(nav.FORMATS.label, "cpm:formats"),), _back_row()),
        )
    page_size = 5
    page_count = max(1, (len(offerings) + page_size - 1) // page_size)
    if page < 0 or page >= page_count:
        raise ValueError("booking offering page is outside result set")
    current = offerings[page * page_size : (page + 1) * page_size]
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (_button(f"🕒 {item.title[:30]}", f"cpm:booking-open-for:{item.id}"),)
        for item in current
    ]
    paging: list[CustomerInteractionButton] = []
    if page > 0:
        paging.append(_button("◀️ Предыдущая страница", f"cpm:booking-open:{page - 1}"))
    if page + 1 < page_count:
        paging.append(_button("Дальше ➡️", f"cpm:booking-open:{page + 1}"))
    if paging:
        rows.append(tuple(paging))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            "🕒 Добавить свободное время\n\n"
            "Для какой услуги открываем время? Нажмите нужную услугу. "
            "После этого останется написать дату и время."
            + (f"\n\nСтраница {page + 1}/{page_count}" if page_count > 1 else "")
        ),
        rows=tuple(rows),
    )


_NATIVE_BOOKING_MONTH_PAGE_SIZE = 6
_NATIVE_BOOKING_DATE_PAGE_SIZE = 6
_NATIVE_BOOKING_TIME_PAGE_SIZE = 6


def _booking_offering_exact(actor: TenantContext, offering_id: str) -> Any:
    offering = next(
        (
            item
            for item in _active_booking_offerings(actor)
            if str(item.id) == str(offering_id)
        ),
        None,
    )
    if offering is None:
        raise ValueError("booking offering is stale")
    return offering


def _booking_minimum_date(actor: TenantContext):
    profile = get_business_profile(actor=actor)
    return local_today(profile.timezone)


def _booking_months_message(
    actor: TenantContext,
    offering_id: str,
    page: int = 0,
) -> CustomerInteractionMessage:
    if actor.role not in _BOOKING_MANAGEMENT_ROLES:
        return _permission_message()
    offering = _booking_offering_exact(actor, offering_id)
    minimum = _booking_minimum_date(actor)
    months: list[tuple[int, int]] = []
    year, month = minimum.year, minimum.month
    for offset in range(MAX_CALENDAR_MONTHS + 1):
        if offset:
            year, month = shift_month(year, month, 1)
        months.append((year, month))
    page = max(0, int(page))
    start = page * _NATIVE_BOOKING_MONTH_PAGE_SIZE
    if start >= len(months):
        return _stale_message()
    shown = months[start : start + _NATIVE_BOOKING_MONTH_PAGE_SIZE]
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (
            _button(
                f"{BOOKING_MONTHS_RU_FULL[current_month]} {current_year}",
                f"cpm:booking-dates:{offering.id}:{month_key(current_year, current_month)}:0",
            ),
        )
        for current_year, current_month in shown
    ]
    paging: list[CustomerInteractionButton] = []
    if page:
        paging.append(
            _button(
                "⬅️ Раньше",
                f"cpm:booking-months:{offering.id}:{page - 1}",
            )
        )
    if start + _NATIVE_BOOKING_MONTH_PAGE_SIZE < len(months):
        paging.append(
            _button(
                "Позже ➡️",
                f"cpm:booking-months:{offering.id}:{page + 1}",
            )
        )
    if paging:
        rows.append(tuple(paging))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            f"🕒 Свободное время · {offering.title}\n\n"
            "Выберите месяц. Затем выберите дату, время и длительность кнопками."
        ),
        rows=tuple(rows),
    )


def _booking_dates_message(
    actor: TenantContext,
    offering_id: str,
    selected_month: str,
    page: int = 0,
) -> CustomerInteractionMessage:
    if actor.role not in _BOOKING_MANAGEMENT_ROLES:
        return _permission_message()
    offering = _booking_offering_exact(actor, offering_id)
    minimum = _booking_minimum_date(actor)
    year, month = parse_month_key(selected_month)
    weeks = calendar_days(
        year=year,
        month=month,
        minimum=minimum,
        max_months=MAX_CALENDAR_MONTHS,
    )
    dates = [
        cell.value
        for week in weeks
        for cell in week
        if cell.enabled and cell.value
    ]
    page = max(0, int(page))
    start = page * _NATIVE_BOOKING_DATE_PAGE_SIZE
    if start >= len(dates):
        return _stale_message()
    shown = dates[start : start + _NATIVE_BOOKING_DATE_PAGE_SIZE]
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (
            _button(
                datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m"),
                f"cpm:booking-date:{offering.id}:{value}",
            ),
        )
        for value in shown
    ]
    paging: list[CustomerInteractionButton] = []
    if page:
        paging.append(
            _button(
                "⬅️ Раньше",
                f"cpm:booking-dates:{offering.id}:{selected_month}:{page - 1}",
            )
        )
    if start + _NATIVE_BOOKING_DATE_PAGE_SIZE < len(dates):
        paging.append(
            _button(
                "Позже ➡️",
                f"cpm:booking-dates:{offering.id}:{selected_month}:{page + 1}",
            )
        )
    if paging:
        rows.append(tuple(paging))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            f"📅 {BOOKING_MONTHS_RU_FULL[month]} {year} · {offering.title}\n\n"
            "Выберите дату свободного времени."
        ),
        rows=tuple(rows),
    )


def _booking_times_message(
    actor: TenantContext,
    offering_id: str,
    selected_date: str,
    page: int = 0,
) -> CustomerInteractionMessage:
    if actor.role not in _BOOKING_MANAGEMENT_ROLES:
        return _permission_message()
    offering = _booking_offering_exact(actor, offering_id)
    minimum = _booking_minimum_date(actor)
    selected = parse_calendar_date(
        selected_date,
        minimum=minimum,
        max_months=MAX_CALENDAR_MONTHS,
    )
    page = max(0, int(page))
    start = page * _NATIVE_BOOKING_TIME_PAGE_SIZE
    if start >= len(BOOKING_START_TIMES):
        return _stale_message()
    shown = BOOKING_START_TIMES[start : start + _NATIVE_BOOKING_TIME_PAGE_SIZE]
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (
            _button(
                value,
                f"cpm:booking-time:{offering.id}:{selected.isoformat()}:{value.replace(':', '')}",
            ),
        )
        for value in shown
    ]
    paging: list[CustomerInteractionButton] = []
    if page:
        paging.append(
            _button(
                "⬅️ Раньше",
                f"cpm:booking-times:{offering.id}:{selected.isoformat()}:{page - 1}",
            )
        )
    if start + _NATIVE_BOOKING_TIME_PAGE_SIZE < len(BOOKING_START_TIMES):
        paging.append(
            _button(
                "Позже ➡️",
                f"cpm:booking-times:{offering.id}:{selected.isoformat()}:{page + 1}",
            )
        )
    if paging:
        rows.append(tuple(paging))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            f"🕒 {selected.strftime('%d.%m.%Y')} · {offering.title}\n\n"
            "Во сколько начинается свободное время?"
        ),
        rows=tuple(rows),
    )


def _booking_durations_message(
    actor: TenantContext,
    offering_id: str,
    selected_date: str,
    compact_time: str,
) -> CustomerInteractionMessage:
    if actor.role not in _BOOKING_MANAGEMENT_ROLES:
        return _permission_message()
    offering = _booking_offering_exact(actor, offering_id)
    minimum = _booking_minimum_date(actor)
    selected = parse_calendar_date(
        selected_date,
        minimum=minimum,
        max_months=MAX_CALENDAR_MONTHS,
    )
    if len(compact_time) != 4 or not compact_time.isdigit():
        raise ValueError("booking time is invalid")
    start_time = parse_booking_start_time(
        f"{compact_time[:2]}:{compact_time[2:]}"
    )
    rows = [
        (
            _button(
                BOOKING_DURATION_LABELS[duration],
                f"cpm:booking-duration:{offering.id}:{selected.isoformat()}:{compact_time}:{duration}",
            ),
        )
        for duration in BOOKING_DURATIONS
    ]
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            f"⏱ {selected.strftime('%d.%m.%Y')} {start_time} · {offering.title}\n\n"
            "Выберите длительность."
        ),
        rows=tuple(rows),
    )


def _booking_create_from_selection(
    actor: TenantContext,
    offering_id: str,
    selected_date: str,
    compact_time: str,
    duration_text: str,
) -> CustomerInteractionMessage:
    if actor.role not in _BOOKING_MANAGEMENT_ROLES:
        return _permission_message()
    offering = _booking_offering_exact(actor, offering_id)
    minimum = _booking_minimum_date(actor)
    selected = parse_calendar_date(
        selected_date,
        minimum=minimum,
        max_months=MAX_CALENDAR_MONTHS,
    )
    if len(compact_time) != 4 or not compact_time.isdigit():
        raise ValueError("booking time is invalid")
    start_time = parse_booking_start_time(
        f"{compact_time[:2]}:{compact_time[2:]}"
    )
    duration = parse_booking_duration(duration_text)
    return _booking_open_create_message(
        actor,
        str(offering.id),
        f"{selected.strftime('%d.%m.%Y')} {start_time}",
        str(duration),
    )


def _booking_open_for_message(
    actor: TenantContext,
    offering_id: str,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str = "official",
) -> CustomerInteractionMessage:
    del current_platform, input_surface
    return _booking_months_message(actor, offering_id, 0)


def _booking_open_failure_message() -> CustomerInteractionMessage:
    return CustomerInteractionMessage(
        text=(
            "Не удалось открыть это время. Проверьте код услуги, дату, время и длительность, "
            "затем попробуйте снова. Ничего не было опубликовано автоматически."
        ),
        rows=((_button("🕒 Добавить время", "cpm:booking-open"),), _back_row()),
    )


def _booking_open_create_message(
    actor: TenantContext,
    offering_reference: str,
    local_start: str,
    duration_text: str,
) -> CustomerInteractionMessage:
    if actor.role not in _BOOKING_MANAGEMENT_ROLES:
        return _permission_message()
    try:
        offering = _booking_offering_reference(actor, offering_reference)
        duration = int(duration_text)
        if duration < 5 or duration > 720:
            raise ValueError("booking duration is out of range")
        slot = create_booking_slot(
            actor=actor,
            offering_id=str(offering.id),
            local_start=local_start,
            duration_minutes=duration,
        )
    except BookingError:
        return _booking_open_failure_message()
    except TenantPermissionDenied:
        return _booking_open_failure_message()
    except TypeError:
        return _booking_open_failure_message()
    except ValueError:
        return _booking_open_failure_message()
    return CustomerInteractionMessage(
        text=(
            "✅ Время открыто для записи.\n\n"
            f"{slot.offering_title} — {slot.local_start}.\n"
            "Теперь можно продолжить привлечение клиентов; платная реклама сама не запускается."
        ),
        rows=(
            (_button("🚀 Продолжить привлечение", "cpm:acquire"),),
            (_button("📅 Записи", "cpm:bookings"),),
            _back_row(),
        ),
    )


def _bookings_message(actor: TenantContext) -> CustomerInteractionMessage:
    slots = list_booking_slots(actor=actor, include_unavailable=False)
    if not slots:
        text = (
            "📅 Запись и свободное время\n\n"
            "Сейчас нет открытого времени, на которое клиент может записаться."
        )
    else:
        lines = [
            f"• {item.offering_title} — {item.local_start}"
            for item in slots[:10]
        ]
        suffix = (
            f"\n\nПоказаны первые 10 из {len(slots)}."
            if len(slots) > 10
            else ""
        )
        text = (
            "📅 Запись и свободное время\n\n"
            "Ниже время, которое сейчас открыто для клиентов:\n"
            + "\n".join(lines)
            + suffix
        )
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if actor.role in _BOOKING_MANAGEMENT_ROLES:
        rows.append((_button("🕒 Добавить свободное время", "cpm:booking-open"),))
    rows.append((_button(nav.WORK.label, "cpm:work"),))
    rows.append(_back_row())
    return CustomerInteractionMessage(text=text, rows=tuple(rows))

def _programs_message(actor: TenantContext, page: int = 0) -> CustomerInteractionMessage:
    programs = list_programs(actor=actor)
    page_size = 3
    page_count = max(1, (len(programs) + page_size - 1) // page_size)
    if page >= page_count:
        raise ValueError("program page is outside result set")
    current = programs[page * page_size : (page + 1) * page_size]
    status_labels = {
        "draft": "черновик",
        "active": "готова к выдаче",
        "archived": "архив",
    }
    lines = [
        f"• {item.title} — {status_labels.get(item.status.value, item.status.value)}"
        for item in current
    ] or ["• Материалов и программ пока нет."]
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if actor.role in _PROGRAM_MANAGEMENT_ROLES:
        rows.append((_button("➕ Создать материал или программу", "cpm:program-create"),))
    for item in current:
        if item.status.value == "draft" and actor.role in _PROGRAM_MANAGEMENT_ROLES:
            rows.append(
                (_button("✏️ Открыть черновик", f"cpm:program-draft:{item.id}"),)
            )
        elif item.status.value == "active" and actor.role in _SUPPORT_ROLES:
            rows.append((_button("📤 Выдать клиенту", f"cpm:program-deliver:{item.id}"),))
    pagination: list[CustomerInteractionButton] = []
    if page > 0:
        pagination.append(_button("◀️ Предыдущая страница", f"cpm:programs:{page - 1}"))
    if page + 1 < page_count:
        pagination.append(_button("Вперёд ➡️", f"cpm:programs:{page + 1}"))
    if pagination:
        rows.append(tuple(pagination))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            "📚 Материалы и программы\n\n"
            "Здесь можно подготовить курс, урок, файл или другой материал, а затем выдать его клиенту.\n\n"
            + "\n".join(lines)
            + f"\n\nСтраница {page + 1}/{page_count}"
        ),
        rows=tuple(rows),
    )

def _program_reference(programs: list[Any], reference: str) -> str:
    resolved = _native_reference(programs, reference)
    if resolved is None:
        raise ValueError("program reference is required")
    return resolved


_PROGRAM_LESSON_PAGE_SIZE = 5


def _program_kind_label(value: object) -> str:
    raw = str(getattr(value, "value", value) or "").strip().casefold()
    return {
        "text": "текст",
        "link": "ссылка",
        "audio": "аудио",
        "video": "видео",
        "document": "документ",
        "image": "изображение",
        "task": "задание",
        "mixed": "смешанный материал",
    }.get(raw, raw or "материал")


def _program_draft_message(
    actor: TenantContext,
    program_id: str,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    record = get_program_draft(actor=actor, program_id=program_id)
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if record.lessons:
        rows.append(
            (_button(f"📚 Уроки · {len(record.lessons)}", f"cpm:program-lessons:{program_id}:0"),)
        )
    rows.extend(
        [
            (_button("➕ Добавить урок", f"cpm:program-lesson:{program_id}"),),
            (_button("✅ Опубликовать", f"cpm:program-publish:{program_id}"),),
            (_button("🗑 Удалить черновик", f"cpm:program-draft-archive:{program_id}"),),
            (_button("📚 К программам", "cpm:programs:0"),),
            _back_row(),
        ]
    )
    preview = chr(10).join(
        f"• {lesson.position}. {lesson.title} — {_program_kind_label(lesson.content_kind)}"
        for lesson in record.lessons[:5]
    )
    if not preview:
        preview = "Уроков пока нет."
    elif len(record.lessons) > 5:
        preview += chr(10) + f"…и ещё {len(record.lessons) - 5}."
    return CustomerInteractionMessage(
        text=f"""📝 Черновик «{record.program.title}»

Уроков: {len(record.lessons)}.
{preview}

Черновик сохранён. Его можно продолжить позже из раздела «Программы».""",
        rows=tuple(rows),
    )


def _program_draft_archive_confirm(
    actor: TenantContext,
    program_id: str,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    record = get_program_draft(actor=actor, program_id=program_id)
    return CustomerInteractionMessage(
        text=f"""Удалить черновик «{record.program.title}»?

Опубликованные программы эта операция не затрагивает.""",
        rows=(
            (_button("🗑 Да, удалить черновик", f"cpm:program-draft-archive-ok:{program_id}"),),
            (_button("Нет, вернуться", f"cpm:program-draft:{program_id}"),),
            _back_row(),
        ),
    )


def _program_draft_archive_result(
    actor: TenantContext,
    program_id: str,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    program = archive_program_draft(actor=actor, program_id=program_id)
    return CustomerInteractionMessage(
        text=f"✅ Черновик «{program.title}» удалён.",
        rows=((_button("📚 Программы", "cpm:programs:0"),), _back_row()),
    )


def _program_lessons_message(
    actor: TenantContext,
    program_id: str,
    page: int = 0,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    record = get_program_draft(actor=actor, program_id=program_id)
    page = max(0, int(page))
    page_count = max(
        1,
        (len(record.lessons) + _PROGRAM_LESSON_PAGE_SIZE - 1)
        // _PROGRAM_LESSON_PAGE_SIZE,
    )
    if page >= page_count:
        return _stale_message()
    start = page * _PROGRAM_LESSON_PAGE_SIZE
    visible = record.lessons[start : start + _PROGRAM_LESSON_PAGE_SIZE]
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (
            _button(
                f"{lesson.position}. {lesson.title[:34]}",
                f"cpm:program-lesson-detail:{lesson.id}",
            ),
        )
        for lesson in visible
    ]
    navigation: list[CustomerInteractionButton] = []
    if page:
        navigation.append(
            _button("⬅️ Назад", f"cpm:program-lessons:{program_id}:{page - 1}")
        )
    if page + 1 < page_count:
        navigation.append(
            _button("Вперёд ➡️", f"cpm:program-lessons:{program_id}:{page + 1}")
        )
    if navigation:
        rows.append(tuple(navigation))
    rows.append((_button("➕ Добавить урок", f"cpm:program-lesson:{program_id}"),))
    rows.append((_button("📝 К черновику", f"cpm:program-draft:{program_id}"),))
    rows.append(_back_row())
    suffix = chr(10) + chr(10) + "Уроков пока нет." if not record.lessons else ""
    return CustomerInteractionMessage(
        text=(
            f"📚 Уроки черновика «{record.program.title}»"
            + chr(10) + chr(10)
            + f"Всего: {len(record.lessons)}. Страница {page + 1}/{page_count}."
            + suffix
        ),
        rows=tuple(rows),
    )


def _program_lesson_detail_message(
    actor: TenantContext,
    lesson_id: str,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    record, lesson = get_program_draft_lesson(actor=actor, lesson_id=lesson_id)
    movement: list[CustomerInteractionButton] = []
    if lesson.position > 1:
        movement.append(
            _button("⬆️ Выше", f"cpm:program-lesson-move:{lesson.id}:up")
        )
    if lesson.position < len(record.lessons):
        movement.append(
            _button("⬇️ Ниже", f"cpm:program-lesson-move:{lesson.id}:down")
        )
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (_button("✏️ Переименовать", f"cpm:program-lesson-title-edit:{lesson.id}"),),
        (_button("🔄 Заменить материал", f"cpm:program-lesson-content-edit:{lesson.id}"),),
    ]
    if movement:
        rows.append(tuple(movement))
    rows.extend(
        [
            (_button("🗑 Удалить урок", f"cpm:program-lesson-archive:{lesson.id}"),),
            (
                _button(
                    "📚 К списку уроков",
                    f"cpm:program-lessons:{record.program.id}:{max(0, (lesson.position - 1) // _PROGRAM_LESSON_PAGE_SIZE)}",
                ),
            ),
            (_button("📝 К черновику", f"cpm:program-draft:{record.program.id}"),),
            _back_row(),
        ]
    )
    content_text = str(lesson.content_ref or "")
    preview = ""
    if str(getattr(lesson.content_kind, "value", lesson.content_kind)) in {"text", "task", "link"}:
        visible = content_text[:500]
        if visible:
            preview = chr(10) + chr(10) + "Материал:" + chr(10) + visible
            if len(content_text) > len(visible):
                preview += "…"
    return CustomerInteractionMessage(
        text=(
            f"📝 Черновик «{record.program.title}»"
            + chr(10) + chr(10)
            + f"Урок {lesson.position} из {len(record.lessons)}"
            + chr(10)
            + f"Название: {lesson.title}"
            + chr(10)
            + f"Тип материала: {_program_kind_label(lesson.content_kind)}"
            + preview
        ),
        rows=tuple(rows),
    )


def _program_lesson_title_edit_message(
    actor: TenantContext,
    lesson_id: str,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    _record, lesson = get_program_draft_lesson(actor=actor, lesson_id=lesson_id)
    return _begin_owner_input_message(
        actor,
        platform=current_platform,
        surface=input_surface,
        action="program_lesson_title_edit",
        context={"lesson_id": lesson.id},
        text=f"""✏️ Переименовать урок

Сейчас: «{lesson.title}».

Напишите новое название урока.""",
        rows=((_button("↩️ К уроку", f"cpm:program-lesson-detail:{lesson.id}"),), _back_row()),
    )


def _program_lesson_title_result(
    actor: TenantContext,
    lesson_id: str,
    title: str,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    _record, lesson = update_program_draft_lesson_title(
        actor=actor,
        lesson_id=lesson_id,
        title=title,
    )
    return CustomerInteractionMessage(
        text=f"✅ Название урока сохранено: «{lesson.title}».",
        rows=((_button("📝 Открыть урок", f"cpm:program-lesson-detail:{lesson.id}"),), _back_row()),
    )


def _program_lesson_content_kinds_message(
    actor: TenantContext,
    lesson_id: str,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    _record, lesson = get_program_draft_lesson(actor=actor, lesson_id=lesson_id)
    kinds = (
        ("📝 Текст", "text"),
        ("🔗 Ссылка", "link"),
        ("🎧 Аудио", "audio"),
        ("🎬 Видео", "video"),
        ("📎 Документ", "document"),
        ("🖼 Изображение", "image"),
        ("✅ Задание", "task"),
    )
    return CustomerInteractionMessage(
        text=f"""🔄 Заменить материал · {lesson.title}

Какой тип материала будет использоваться?""",
        rows=tuple(
            [(_button(label, f"cpm:program-lesson-content-kind:{lesson.id}:{kind}"),) for label, kind in kinds]
            + [(_button("↩️ К уроку", f"cpm:program-lesson-detail:{lesson.id}"),), _back_row()]
        ),
    )


def _program_lesson_content_edit_message(
    actor: TenantContext,
    lesson_id: str,
    content_kind: str,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    _record, lesson = get_program_draft_lesson(actor=actor, lesson_id=lesson_id)
    if content_kind not in {"text", "link", "audio", "video", "document", "image", "task"}:
        return _stale_message()
    return _begin_owner_input_message(
        actor,
        platform=current_platform,
        surface=input_surface,
        action="program_lesson_content_edit",
        context={"lesson_id": lesson.id, "content_kind": content_kind},
        text=f"""🔄 Новый материал · {lesson.title}

Пришлите новый материал одним сообщением.
Для текста или задания — сам текст; для ссылки — https-ссылку. Для аудио, видео, документа или изображения пока можно указать сохранённую ссылку или идентификатор материала.""",
        rows=((_button("↩️ К уроку", f"cpm:program-lesson-detail:{lesson.id}"),), _back_row()),
    )


def _program_lesson_content_result(
    actor: TenantContext,
    lesson_id: str,
    content_kind: str,
    content_ref: str,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    _record, lesson = replace_program_draft_lesson_content(
        actor=actor,
        lesson_id=lesson_id,
        content_kind=content_kind,
        content_ref=content_ref,
    )
    return CustomerInteractionMessage(
        text=f"✅ Материал урока «{lesson.title}» заменён.",
        rows=((_button("📝 Открыть урок", f"cpm:program-lesson-detail:{lesson.id}"),), _back_row()),
    )


def _program_lesson_move_result(
    actor: TenantContext,
    lesson_id: str,
    direction: str,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    if direction not in {"up", "down"}:
        return _stale_message()
    record = move_program_draft_lesson(
        actor=actor,
        lesson_id=lesson_id,
        direction=direction,
    )
    lesson = next((item for item in record.lessons if item.id == lesson_id), None)
    if lesson is None:
        return _stale_message()
    return CustomerInteractionMessage(
        text=f"✅ Порядок обновлён. «{lesson.title}» теперь урок {lesson.position}.",
        rows=((_button("📝 Открыть урок", f"cpm:program-lesson-detail:{lesson.id}"),), _back_row()),
    )


def _program_lesson_archive_confirm(
    actor: TenantContext,
    lesson_id: str,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    record, lesson = get_program_draft_lesson(actor=actor, lesson_id=lesson_id)
    return CustomerInteractionMessage(
        text=(
            f"Удалить урок {lesson.position} «{lesson.title}»?"
            + chr(10) + chr(10)
            + "Остальные уроки будут автоматически перенумерованы."
        ),
        rows=(
            (_button("🗑 Да, удалить урок", f"cpm:program-lesson-archive-ok:{lesson.id}"),),
            (_button("Нет, вернуться", f"cpm:program-lesson-detail:{lesson.id}"),),
            (_button("📝 К черновику", f"cpm:program-draft:{record.program.id}"),),
            _back_row(),
        ),
    )


def _program_lesson_archive_result(
    actor: TenantContext,
    lesson_id: str,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    before, lesson = get_program_draft_lesson(actor=actor, lesson_id=lesson_id)
    page = max(0, (lesson.position - 1) // _PROGRAM_LESSON_PAGE_SIZE)
    record = archive_program_draft_lesson(actor=actor, lesson_id=lesson_id)
    if page and page * _PROGRAM_LESSON_PAGE_SIZE >= len(record.lessons):
        page -= 1
    return CustomerInteractionMessage(
        text=f"✅ Урок «{lesson.title}» удалён.",
        rows=(
            (_button("📚 К урокам", f"cpm:program-lessons:{before.program.id}:{page}"),),
            (_button("📝 К черновику", f"cpm:program-draft:{before.program.id}"),),
            _back_row(),
        ),
    )


def _program_direction_message(
    actor: TenantContext,
    page: int = 0,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    directions = list_activity_directions(actor=actor)
    if not directions:
        return _stale_message()
    page = max(0, int(page))
    start = page * _DIRECTION_PAGE_SIZE
    if start >= len(directions) and page:
        return _stale_message()
    shown = directions[start : start + _DIRECTION_PAGE_SIZE]
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (_button(f"🧭 {item.title[:34]}", f"cpm:program-create-dir:{item.id}"),)
        for item in shown
    ]
    rows.append((_button("Без направления", "cpm:program-create-dir:none"),))
    pagination: list[CustomerInteractionButton] = []
    if page:
        pagination.append(_button("⬅️ Назад", f"cpm:program-create-dirs:{page - 1}"))
    if start + _DIRECTION_PAGE_SIZE < len(directions):
        pagination.append(_button("Вперёд ➡️", f"cpm:program-create-dirs:{page + 1}"))
    if pagination:
        rows.append(tuple(pagination))
    rows.append((_button("📚 К программам", "cpm:programs:0"),))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            "➕ Новый материал или программа\n\n"
            "К какому направлению деятельности относится этот материал? "
            "Выберите направление или «Без направления»."
        ),
        rows=tuple(rows),
    )


def _program_title_input(
    actor: TenantContext,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str,
    direction_id: str | None,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    context: dict[str, object] = {}
    if direction_id:
        direction = get_activity_direction(actor=actor, direction_id=direction_id)
        if direction.status != ActivityDirectionStatus.ACTIVE:
            return _stale_message()
        context["direction_id"] = direction.id
    return _begin_owner_input_message(
        actor,
        platform=current_platform,
        surface=input_surface,
        action="program_title",
        context=context,
        text=(
            "➕ Новый материал или программа\n\n"
            "Напишите только название. Например: «Первый урок для новых клиентов».\n\n"
            "После создания можно добавить текст, ссылку, аудио, видео, документ, изображение или задание."
        ),
        rows=((_button(nav.PROGRAMS.label, "cpm:programs:0"),), _back_row()),
    )


def _program_create_help(
    actor: TenantContext,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str = "official",
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    directions = list_activity_directions(actor=actor)
    if directions:
        return _program_direction_message(actor, 0)
    return _program_title_input(
        actor,
        current_platform=current_platform,
        input_surface=input_surface,
        direction_id=None,
    )


def _program_create_result(
    actor: TenantContext,
    title: str,
    *,
    interaction_key: str,
    direction_id: str | None = None,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    if direction_id is None:
        program = create_program(
            actor=actor,
            title=title,
            idempotency_key=f"{interaction_key}:program-create",
        )
    else:
        program = create_program(
            actor=actor,
            title=title,
            idempotency_key=f"{interaction_key}:program-create",
            direction_id=direction_id,
        )
    code = str(program.id)[:8]
    return CustomerInteractionMessage(
        text=(
            f"✅ Черновик «{program.title}» создан.\n\n"
            f"Код программы: {code}\n"
            f"Добавьте урок: урок {code} текст | Название урока | Текст урока"
        ),
        rows=(
            (_button("➕ Добавить урок", f"cpm:program-lesson:{program.id}"),),
            (_button("📚 Программы", "cpm:programs:0"),),
            _back_row(),
        ),
    )


def _program_lesson_help(actor: TenantContext, program_id: str) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    record = get_program_draft(actor=actor, program_id=program_id)
    kinds = (
        ("📝 Текст", "text"),
        ("🔗 Ссылка", "link"),
        ("🎧 Аудио", "audio"),
        ("🎬 Видео", "video"),
        ("📎 Документ", "document"),
        ("🖼 Изображение", "image"),
        ("✅ Задание", "task"),
    )
    return CustomerInteractionMessage(
        text=(
            f"➕ Добавить материал · {record.program.title}\n\n"
            "Что Вы хотите добавить? Выберите тип материала. "
            "На следующем шаге нужно будет написать только название и сам материал."
        ),
        rows=tuple(
            [(_button(label, f"cpm:program-lesson-kind:{program_id}:{kind}"),) for label, kind in kinds]
            + [_back_row()]
        ),
    )


def _program_lesson_kind_message(
    actor: TenantContext,
    program_id: str,
    content_kind: str,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str = "official",
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    record = get_program_draft(actor=actor, program_id=program_id)
    labels = {
        "text": "текст",
        "link": "ссылка",
        "audio": "аудио",
        "video": "видео",
        "document": "документ",
        "image": "изображение",
        "task": "задание",
    }
    if content_kind not in labels:
        return _stale_message()
    return _begin_owner_input_message(
        actor,
        platform=current_platform,
        surface=input_surface,
        action="program_lesson",
        context={"program_id": str(record.program.id), "content_kind": content_kind},
        text=(
            f"➕ {record.program.title} · {labels[content_kind]}\n\n"
            "Напишите одним сообщением:\nНазвание | Материал\n\n"
            "Для текста укажите сам текст. Для ссылки, аудио, видео, документа или изображения — "
            "сохранённую ссылку или идентификатор материала."
        ),
        rows=((_button(nav.PROGRAMS.label, "cpm:programs:0"),), _back_row()),
    )


def _program_lesson_result(
    actor: TenantContext,
    program_reference: str,
    content_kind: str,
    title: str,
    content_ref: str,
    *,
    interaction_key: str,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    drafts = [item for item in list_programs(actor=actor) if item.status.value == "draft"]
    program_id = _program_reference(drafts, program_reference)
    lesson = add_program_lesson(
        actor=actor,
        program_id=program_id,
        title=title,
        content_kind=content_kind,
        content_ref=content_ref,
        idempotency_key=f"{interaction_key}:program-lesson",
    )
    return CustomerInteractionMessage(
        text=f"✅ Урок «{lesson.title}» добавлен.",
        rows=(
            (_button("➕ Ещё урок", f"cpm:program-lesson:{program_id}"),),
            (_button("✅ Опубликовать", f"cpm:program-publish:{program_id}"),),
            (_button("📚 Программы", "cpm:programs:0"),),
            _back_row(),
        ),
    )


def _program_publish_result(
    actor: TenantContext,
    program_id: str,
    *,
    current_platform: ConnectionPlatform,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    record = get_program_draft(actor=actor, program_id=program_id)
    if not record.lessons:
        return CustomerInteractionMessage(
            text="Сначала добавьте хотя бы один урок.",
            rows=((_button("➕ Добавить урок", f"cpm:program-lesson:{program_id}"),), _back_row()),
        )
    program = publish_program(actor=actor, program_id=program_id)
    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if actor.role in _SUPPORT_ROLES and current_platform in {ConnectionPlatform.VK, ConnectionPlatform.MAX}:
        rows.append((_button("📤 Выдать клиенту", f"cpm:program-deliver:{program.id}"),))
    rows.extend(((_button("📚 Программы", "cpm:programs:0"),), _back_row()))
    return CustomerInteractionMessage(
        text=f"✅ Программа «{program.title}» опубликована. Уроков: {len(record.lessons)}.",
        rows=tuple(rows),
    )


def _program_deliver_help(
    actor: TenantContext,
    program_id: str,
    *,
    current_platform: ConnectionPlatform,
) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    if current_platform not in {ConnectionPlatform.VK, ConnectionPlatform.MAX}:
        return _stale_message()
    customers = list_customers_with_active_identity(
        actor=actor,
        platform=current_platform.value,
        limit=8,
    )
    channel = "ВКонтакте" if current_platform == ConnectionPlatform.VK else "MAX"
    if not customers:
        return CustomerInteractionMessage(
            text=(
                f"Сейчас нет клиентов с активным контактом в {channel}. "
                "Подключите клиента или попросите его написать в этот канал."
            ),
            rows=((_button(nav.INVITES.label, "cpm:invites"),), _back_row()),
        )
    rows = [
        (_button(f"👤 {(item.display_name or 'Клиент')[:30]}", f"cpm:program-deliver-to:{program_id}:{item.id}"),)
        for item in customers
    ]
    rows.append(_back_row())
    customer_lines = "\n".join(
        f"• {item.display_name or 'Клиент'}" for item in customers
    )
    return CustomerInteractionMessage(
        text=(
            f"📤 Выдать материал через {channel}\n\n"
            "Кому отправить? Нажмите имя клиента. Никакие коды вводить не нужно.\n\n"
            f"Клиенты, доступные в {channel}:\n{customer_lines}\n\n"
            "Старый формат «выдать <код программы> <код клиента>» также сохранён."
        ),
        rows=tuple(rows),
    )


def _program_deliver_result(
    actor: TenantContext,
    program_reference: str,
    customer_reference: str,
    *,
    current_platform: ConnectionPlatform,
) -> CustomerInteractionMessage:
    if actor.role not in _SUPPORT_ROLES:
        return _permission_message()
    if current_platform not in {ConnectionPlatform.VK, ConnectionPlatform.MAX}:
        return _stale_message()
    programs = [item for item in list_programs(actor=actor) if item.status.value == "active"]
    customers = list_customers_with_active_identity(
        actor=actor,
        platform=current_platform.value,
        limit=100,
    )
    program_id = _program_reference(programs, program_reference)
    customer_id = _native_reference(customers, customer_reference)
    if customer_id is None:
        return _stale_message()
    prepared = prepare_native_program_delivery(
        actor=actor,
        program_id=program_id,
        customer_id=customer_id,
        platform=current_platform,
    )
    channel = "ВКонтакте" if current_platform == ConnectionPlatform.VK else "MAX"
    return CustomerInteractionMessage(
        text=(
            f"✅ Программа «{prepared.program.program.title}» поставлена в очередь "
            f"через {channel}. ClientPlatform сохранит результат доставки."
        ),
        rows=((_button("📚 Программы", "cpm:programs:0"),), _back_row()),
    )


def _offering_new_help(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    capabilities = [
        item
        for item in list_business_capabilities(actor=actor)
        if item.status == CapabilityStatus.ACTIVE
        and resolve_activity_connector(item.connector_key).supports_offerings
    ]
    if not capabilities:
        return CustomerInteractionMessage(
            text="Сначала включите нужный формат работы в разделе «Услуги и формат работы».",
            rows=((_button(nav.FORMATS.label, "cpm:formats:0"),), _back_row()),
        )
    rows = [
        (_button(f"🧰 {item.title[:30]}", f"cpm:offering-new-for:{item.connector_key}"),)
        for item in capabilities[:8]
    ]
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            "➕ Новая услуга или предложение\n\n"
            "К какому формату относится новая услуга? Нажмите подходящий вариант. "
            "Затем напишите название и короткое описание."
        ),
        rows=tuple(rows),
    )


def _offering_capability(actor: TenantContext, connector_key: str):
    return next(
        (
            item
            for item in list_business_capabilities(actor=actor)
            if item.status == CapabilityStatus.ACTIVE
            and item.connector_key == connector_key
            and resolve_activity_connector(item.connector_key).supports_offerings
        ),
        None,
    )


def _offering_direction_message(
    actor: TenantContext,
    connector_key: str,
    page: int = 0,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    capability = _offering_capability(actor, connector_key)
    if capability is None:
        return _stale_message()
    directions = list_activity_directions(actor=actor)
    if not directions:
        return _stale_message()
    page = max(0, int(page))
    start = page * _DIRECTION_PAGE_SIZE
    if start >= len(directions) and page:
        return _stale_message()
    shown = directions[start : start + _DIRECTION_PAGE_SIZE]
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (
            _button(
                f"🧭 {item.title[:34]}",
                f"cpm:offering-new-dir:{connector_key}:{item.id}",
            ),
        )
        for item in shown
    ]
    rows.append(
        (_button("Без направления", f"cpm:offering-new-dir:{connector_key}:none"),)
    )
    pagination: list[CustomerInteractionButton] = []
    if page:
        pagination.append(
            _button("⬅️ Назад", f"cpm:offering-new-dirs:{connector_key}:{page - 1}")
        )
    if start + _DIRECTION_PAGE_SIZE < len(directions):
        pagination.append(
            _button("Вперёд ➡️", f"cpm:offering-new-dirs:{connector_key}:{page + 1}")
        )
    if pagination:
        rows.append(tuple(pagination))
    rows.append((_button("🧪 К предложениям", "cpm:offers"),))
    rows.append(_back_row())
    return CustomerInteractionMessage(
        text=(
            f"🧰 Новая услуга · {capability.title}\n\n"
            "К какому направлению деятельности относится эта услуга? "
            "Выберите направление или «Без направления»."
        ),
        rows=tuple(rows),
    )


def _offering_input_message(
    actor: TenantContext,
    connector_key: str,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str,
    direction_id: str | None,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    capability = _offering_capability(actor, connector_key)
    if capability is None:
        return _stale_message()
    context: dict[str, object] = {"connector_key": connector_key}
    if direction_id:
        direction = get_activity_direction(actor=actor, direction_id=direction_id)
        if direction.status != ActivityDirectionStatus.ACTIVE:
            return _stale_message()
        context["direction_id"] = direction.id
    return _begin_owner_input_message(
        actor,
        platform=current_platform,
        surface=input_surface,
        action="offering",
        context=context,
        text=(
            f"🧰 Новая услуга · {capability.title}\n\n"
            "Напишите одним сообщением:\nНазвание | Короткое описание\n\n"
            "Например: Диагностика | Проверка автомобиля перед покупкой."
        ),
        rows=((_button(nav.OFFERS.label, "cpm:offers"),), _back_row()),
    )


def _offering_new_for_message(
    actor: TenantContext,
    connector_key: str,
    *,
    current_platform: ConnectionPlatform,
    input_surface: str = "official",
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    capability = _offering_capability(actor, connector_key)
    if capability is None:
        return _stale_message()
    directions = list_activity_directions(actor=actor)
    if directions:
        return _offering_direction_message(actor, connector_key, 0)
    return _offering_input_message(
        actor,
        connector_key,
        current_platform=current_platform,
        input_surface=input_surface,
        direction_id=None,
    )


def _offering_new_result(
    actor: TenantContext,
    connector_key: str,
    title: str,
    description: str,
    *,
    interaction_key: str,
    direction_id: str | None = None,
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    capability = _offering_capability(actor, connector_key)
    if capability is None:
        return _stale_message()
    if direction_id is None:
        offering = create_business_offering(
            actor=actor,
            capability_id=capability.id,
            title=title,
            description=description,
            idempotency_key=f"{interaction_key}:offering-create",
        )
    else:
        offering = create_business_offering(
            actor=actor,
            capability_id=capability.id,
            title=title,
            description=description,
            idempotency_key=f"{interaction_key}:offering-create",
            direction_id=direction_id,
        )
    return CustomerInteractionMessage(
        text=f"✅ Предложение «{offering.title}» создано.",
        rows=((_button("🧪 Предложения", "cpm:offers"),), _back_row()),
    )


_RETIRE_PAGE_SIZE = 6


def _offering_retire_list_message(
    actor: TenantContext, page: int = 0
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    offerings = _native_all_offerings(actor)
    page = max(0, int(page))
    start = page * _RETIRE_PAGE_SIZE
    if start >= len(offerings) and page:
        return _stale_message()
    shown = offerings[start : start + _RETIRE_PAGE_SIZE]
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (_button(f"🗑 {item.title[:40]}", f"cpm:offering-retire:{item.id}"),)
        for item in shown
    ]
    pagination: list[CustomerInteractionButton] = []
    if page:
        pagination.append(_button("⬅️ Назад", f"cpm:offering-retire-list:{page - 1}"))
    if start + _RETIRE_PAGE_SIZE < len(offerings):
        pagination.append(_button("Вперёд ➡️", f"cpm:offering-retire-list:{page + 1}"))
    if pagination:
        rows.append(tuple(pagination))
    rows.append((_button("🧪 К предложениям", "cpm:offers"),))
    return CustomerInteractionMessage(
        text=(
            "🗑 Убрать услугу или предложение\n\n"
            "Выберите только то, что больше не должно показываться как активное. "
            "Связанные оплаты и история сохранятся."
            if offerings
            else "🗑 Убрать услугу или предложение\n\nАктивных предложений нет."
        ),
        rows=tuple(rows),
    )


def _offering_retire_confirm(
    actor: TenantContext, offering_id: str
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    offering = next(
        (item for item in _native_all_offerings(actor) if item.id == offering_id),
        None,
    )
    if offering is None:
        return _stale_message()
    return CustomerInteractionMessage(
        text=(
            f"🗑 Убрать «{offering.title}» из активных предложений?\n\n"
            "Это не удалит оплаты, результаты и историю. Предложение будет архивировано."
        ),
        rows=(
            (_button("✅ Да, убрать", f"cpm:offering-retire-ok:{offering.id}"),),
            (_button("Отмена", "cpm:offers"),),
        ),
    )


def _offering_retire_result(
    actor: TenantContext, offering_id: str
) -> CustomerInteractionMessage:
    if actor.role not in _PROGRAM_MANAGEMENT_ROLES:
        return _permission_message()
    offering = archive_business_offering(actor=actor, offering_id=offering_id)
    return CustomerInteractionMessage(
        text=(
            f"✅ «{offering.title}» убрано из активных предложений.\n"
            "История и связанные финансовые данные сохранены."
        ),
        rows=((_button("🧪 К предложениям", "cpm:offers"),),),
    )


def _publication_retire_list_message(
    actor: TenantContext, page: int = 0
) -> CustomerInteractionMessage:
    if actor.role not in _CONTENT_ROLES:
        return _permission_message()
    publications = [
        item
        for item in admin_ops.list_publications(actor=actor, limit=100)
        if item.status != "cancelled"
    ]
    page = max(0, int(page))
    start = page * _RETIRE_PAGE_SIZE
    if start >= len(publications) and page:
        return _stale_message()
    shown = publications[start : start + _RETIRE_PAGE_SIZE]
    rows: list[tuple[CustomerInteractionButton, ...]] = [
        (_button(f"🗑 {item.title[:40]}", f"cpm:publication-retire:{item.id}"),)
        for item in shown
    ]
    pagination: list[CustomerInteractionButton] = []
    if page:
        pagination.append(_button("⬅️ Назад", f"cpm:publication-retire-list:{page - 1}"))
    if start + _RETIRE_PAGE_SIZE < len(publications):
        pagination.append(_button("Вперёд ➡️", f"cpm:publication-retire-list:{page + 1}"))
    if pagination:
        rows.append(tuple(pagination))
    rows.append((_button("📣 К публикациям", "cpm:publications"),))
    return CustomerInteractionMessage(
        text=(
            "🗑 Убрать публикацию\n\nВыберите публикацию. Исторический статус и аудит сохранятся."
            if publications
            else "🗑 Убрать публикацию\n\nАктивных публикаций нет."
        ),
        rows=tuple(rows),
    )


def _publication_retire_confirm(
    actor: TenantContext, publication_id: str
) -> CustomerInteractionMessage:
    if actor.role not in _CONTENT_ROLES:
        return _permission_message()
    publication = next(
        (
            item
            for item in admin_ops.list_publications(actor=actor, limit=100)
            if item.id == publication_id and item.status != "cancelled"
        ),
        None,
    )
    if publication is None:
        return _stale_message()
    warning = (
        "Если она была запланирована, будущая отправка будет отменена. "
        if publication.status == "scheduled"
        else ""
    )
    return CustomerInteractionMessage(
        text=(
            f"🗑 Убрать публикацию «{publication.title}»?\n\n"
            + warning
            + "Запись и её фактическая история останутся в системе."
        ),
        rows=(
            (_button("✅ Да, убрать", f"cpm:publication-retire-ok:{publication.id}"),),
            (_button("Отмена", "cpm:publications"),),
        ),
    )


def _publication_retire_result(
    actor: TenantContext, publication_id: str
) -> CustomerInteractionMessage:
    if actor.role not in _CONTENT_ROLES:
        return _permission_message()
    publication = admin_ops.retire_publication(
        actor=actor, publication_id=publication_id
    )
    return CustomerInteractionMessage(
        text=(
            f"✅ Публикация «{publication.title}» убрана из активной работы.\n"
            "Её история сохранена."
        ),
        rows=((_button("📣 К публикациям", "cpm:publications"),),),
    )


def _business_retire_confirm(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role != PlatformRole.OWNER:
        return _permission_message()
    business_name = _business_name(actor)
    return CustomerInteractionMessage(
        text=(
            f"🗑 Удалить бизнес «{business_name}»?\n\n"
            "Он исчезнет из активной навигации. Оплаты, результаты и аудит не удаляются. "
            "После этого можно подключить другой бизнес. Это действие требует подтверждения."
        ),
        rows=(
            (_button("🗑 Да, удалить бизнес", "cpm:business-retire-ok"),),
            (_button("Отмена", "cpm:manage-more"),),
        ),
    )


def _business_retire_result(actor: TenantContext) -> CustomerInteractionMessage:
    if actor.role != PlatformRole.OWNER:
        return _permission_message()
    business = archive_business(actor=actor)
    return CustomerInteractionMessage(
        text=(
            f"✅ Бизнес «{business.name}» удалён из активных.\n\n"
            "История сохранена. Отправьте «start» или снова откройте ClientPlatform — "
            "появится вход для подключения другого бизнеса."
        ),
        rows=(),
    )


def _messengers_message(
    actor: TenantContext,
    *,
    setup_available: bool,
    current_platform: ConnectionPlatform,
) -> CustomerInteractionMessage:
    connections = list_connections(actor=actor)
    capabilities = project_messenger_capabilities(
        connections,
        setup_available=setup_available,
    )
    labels = {
        ConnectionPlatform.TELEGRAM: ("✈️", "Telegram"),
        ConnectionPlatform.VK: ("🔵", "ВКонтакте"),
        ConnectionPlatform.MAX: ("🟣", "MAX"),
    }
    state_labels = {
        CapabilityAvailability.ACTIVE: "работает",
        CapabilityAvailability.ATTENTION: "требует внимания",
        CapabilityAvailability.CONFIGURING: "настраивается",
        CapabilityAvailability.CONNECTABLE: "можно подключить",
        CapabilityAvailability.CONNECTED_UNAVAILABLE: "подключён, но сейчас выключен",
        CapabilityAvailability.UNAVAILABLE: "сейчас недоступен",
    }
    by_platform = {item.platform: item for item in capabilities}
    lines = [
        "💬 Мессенджеры бизнеса",
        "",
        "Здесь подключаются каналы, через которые клиенты общаются именно с Вашим бизнесом.",
        "Подключайте только те каналы, которыми действительно пользуетесь.",
        "",
        "Состояние каналов:",
    ]
    for platform in (
        ConnectionPlatform.VK,
        ConnectionPlatform.MAX,
        ConnectionPlatform.TELEGRAM,
    ):
        icon, title = labels[platform]
        capability = by_platform[platform]
        current = " · сейчас здесь" if platform == current_platform else ""
        lines.append(
            f"{icon} {title} — {state_labels[capability.availability]}{current}"
        )

    rows: list[tuple[CustomerInteractionButton, ...]] = []
    if actor.role in _CONNECTION_ROLES:
        connect_labels = {
            ConnectionPlatform.TELEGRAM: "✈️ Подключить Telegram",
            ConnectionPlatform.VK: "🔵 Подключить ВКонтакте",
            ConnectionPlatform.MAX: "🟣 Подключить MAX",
        }
        for capability in capabilities:
            if capability.can_connect:
                rows.append(
                    (
                        _button(
                            connect_labels[capability.platform],
                            f"cpm:connect-{capability.platform.value}",
                        ),
                    )
                )

    switch_labels = {
        ConnectionPlatform.TELEGRAM: "✈️ Перейти в Telegram",
        ConnectionPlatform.VK: "🔵 Перейти во ВКонтакте",
        ConnectionPlatform.MAX: "🟣 Перейти в MAX",
    }
    try:
        switchable = available_staff_messenger_switches(actor)
    except (RuntimeError, ValueError):
        switchable = ()
    for platform in switchable:
        capability = by_platform.get(platform)
        if (
            platform == current_platform
            or capability is None
            or capability.availability != CapabilityAvailability.ACTIVE
        ):
            continue
        rows.append(
            (
                _button(
                    switch_labels[platform],
                    build_staff_switch_command(actor, platform),
                ),
            )
        )
    rows.append(_back_row())
    return CustomerInteractionMessage(text="\n".join(lines), rows=tuple(rows))


def _setup_message(
    actor: TenantContext,
    *,
    platform: ConnectionPlatform,
    setup_issuer: NativeSetupCommandIssuer | None,
    setup_key: str,
    setup_available: bool,
) -> CustomerInteractionMessage:
    if actor.role not in _CONNECTION_ROLES:
        return _permission_message()
    capability = next(
        item
        for item in project_messenger_capabilities(
            list_connections(actor=actor),
            setup_available=setup_available,
        )
        if item.platform == platform
    )
    if not capability.can_connect:
        return CustomerInteractionMessage(
            text=(
                "Этот канал сейчас нельзя подключить в данной установке ClientPlatform. "
                "Когда техническая поддержка этого канала будет готова, кнопка подключения появится автоматически."
            ),
            rows=(_back_row(),),
        )
    if setup_issuer is None:
        return CustomerInteractionMessage(
            text="Защищённая настройка мессенджера сейчас недоступна.",
            rows=(_back_row(),),
        )
    try:
        command = setup_issuer(actor, platform, setup_key)
    except (RuntimeError, ValueError):
        # The exception may contain a provider token, secret reference or raw
        # capability. Log only stable context; never serialize exception text
        # or traceback across this credential-adjacent boundary.
        log.error(
            "Native messenger setup command issuance failed",
            extra={
                "business_id": actor.business_id,
                "member_user_id": actor.user_id,
                "platform": platform.value,
            },
        )
        return CustomerInteractionMessage(
            text=(
                "Не удалось подготовить защищённую настройку. "
                "Повторите попытку позже."
            ),
            rows=(_back_row(),),
        )
    channel_name = {
        ConnectionPlatform.TELEGRAM: "Telegram",
        ConnectionPlatform.VK: "ВКонтакте",
        ConnectionPlatform.MAX: "MAX",
    }[platform]
    return CustomerInteractionMessage(
        text=(
            f"Подключение {channel_name}\n\n"
            "Кнопка ниже откроет защищённую HTTPS-страницу ClientPlatform. "
            "Ссылка действует ограниченное время и предназначена только для этого бизнеса.\n\n"
            "Токен провайдера вводите только на этой странице — не отправляйте его сообщением в мессенджере."
        ),
        rows=(
            (_button("🔐 Открыть защищённую настройку", command),),
            _back_row(),
        ),
    )


def _render(
    actor: TenantContext,
    parsed: ParsedMemberInteraction,
    *,
    linked: bool,
    setup_issuer: NativeSetupCommandIssuer | None,
    setup_key: str,
    input_surface: str = "official",
    current_platform: ConnectionPlatform = ConnectionPlatform.TELEGRAM,
) -> CustomerInteractionMessage:
    if parsed.action == "menu":
        return _menu_message(actor, linked=linked)
    if parsed.action == "owner-input-invalid":
        return _owner_input_invalid_message(parsed.args[0] if parsed.args else "")
    if parsed.action == "owner-input-cancelled":
        text = (
            "Создание вебинара отменено. Данные не изменены."
            if parsed.args and parsed.args[0] == "online_event"
            else "Ввод отменён. Данные не изменены."
        )
        return CustomerInteractionMessage(text=text, rows=(_back_row(),))
    try:
        if parsed.action == "menu-all":
            return _menu_all_message(actor)
        if parsed.action == "work":
            return _work_message(actor)
        if parsed.action == "work-more":
            return _work_more_message(actor)
        if parsed.action == "growth":
            return _growth_message(actor)
        if parsed.action == "growth-sales":
            return _growth_sales_message(actor)
        if parsed.action == "growth-analysis":
            return _growth_analysis_message(actor)
        if parsed.action == "growth-more":
            return _growth_more_message(actor)
        if parsed.action == "growth-lifecycle":
            return _growth_lifecycle_message(actor)
        if parsed.action == "events":
            return _events_message(actor)
        if parsed.action == "event-edit":
            if len(parsed.args) != 1:
                return _stale_message()
            return _event_edit_message(
                actor,
                parsed.args[0],
                current_platform=current_platform,
                input_surface=input_surface,
            )
        if parsed.action == "event-edit-text":
            if len(parsed.args) != 2:
                return _stale_message()
            return _event_edit_result(
                actor,
                parsed.args[0],
                parsed.args[1],
                current_platform=current_platform,
                input_surface=input_surface,
            )
        if parsed.action == "event-conduct":
            if len(parsed.args) not in {1, 2}:
                return _stale_message()
            page = 0
            if len(parsed.args) == 2:
                try:
                    page = int(parsed.args[1])
                except (TypeError, ValueError):
                    return _stale_message()
            return _event_conduct_message(actor, parsed.args[0], page=page)
        if parsed.action == "event-settings":
            return _event_settings_message(actor)
        if parsed.action == "event-content":
            if len(parsed.args) != 1:
                return _stale_message()
            return _event_content_message(actor, parsed.args[0])
        if parsed.action == "event-content-followups":
            if len(parsed.args) not in {1, 2}:
                return _stale_message()
            if len(parsed.args) == 2 and not parsed.args[1].isdigit():
                return _stale_message()
            return _event_followup_content_message(
                actor,
                parsed.args[0],
                int(parsed.args[1]) if len(parsed.args) == 2 else 0,
            )
        if parsed.action == "event-followup-edit":
            if len(parsed.args) != 2 or not parsed.args[1].isdigit():
                return _stale_message()
            return _event_followup_edit_message(
                actor,
                event_id=parsed.args[0],
                index=int(parsed.args[1]),
                current_platform=current_platform,
                input_surface=input_surface,
            )
        if parsed.action == "event-followup-reset":
            if len(parsed.args) != 2 or not parsed.args[1].isdigit():
                return _stale_message()
            return _event_followup_reset_result(
                actor,
                event_id=parsed.args[0],
                index=int(parsed.args[1]),
            )
        if parsed.action == "event-followup-edit-text":
            if len(parsed.args) != 5 or not parsed.args[1].isdigit() or not parsed.args[3].isdigit():
                return _stale_message()
            return _event_followup_edit_result(
                actor,
                event_id=parsed.args[0],
                index=int(parsed.args[1]),
                segment=parsed.args[2],
                stage=int(parsed.args[3]),
                text=parsed.args[4],
            )
        if parsed.action == "event-followups":
            return _event_followups_action(actor, parsed.args)
        if parsed.action == "event-segment":
            return _event_segment_action(actor, parsed.args)
        if parsed.action == "event-channel":
            return _event_channel_action(actor, parsed.args)
        if parsed.action == "event-announce":
            if len(parsed.args) != 1:
                return _stale_message()
            return _event_announcement_message(
                actor, parsed.args[0], current_platform=current_platform
            )
        if parsed.action == "event-new":
            return _event_new_entry_message(
                actor,
                current_platform=current_platform,
                input_surface=input_surface,
            )
        if parsed.action == "event-new-dirs":
            return _event_direction_message(actor, _page_number(parsed.args))
        if parsed.action == "event-new-dir":
            if len(parsed.args) != 1:
                return _stale_message()
            return _event_new_direction_result(
                actor,
                parsed.args[0],
                current_platform=current_platform,
                input_surface=input_surface,
            )
        if parsed.action == "event-wizard":
            return handle_native_event_wizard_action(
                actor,
                args=parsed.args,
                platform=current_platform,
                surface=input_surface,
            )
        if parsed.action in {
            "event-wizard-title-text",
            "event-wizard-count-text",
            "event-wizard-topics-text",
            "event-wizard-timezone-text",
            "event-wizard-window-text",
            "event-wizard-room-text",
            "event-wizard-warmup-text",
            "event-we-text",
            "event-warmup-days-text",
        }:
            return handle_native_event_wizard_text(
                actor,
                action=parsed.action,
                args=parsed.args,
                platform=current_platform,
                surface=input_surface,
            )
        if parsed.action == "event-create-text":
            if len(parsed.args) != 4:
                return _stale_message()
            return _event_create_result(actor, *parsed.args)
        if parsed.action == "event-join":
            if len(parsed.args) != 1:
                return _stale_message()
            return _event_join_message(
                actor,
                parsed.args[0],
                current_platform=current_platform,
                input_surface=input_surface,
            )
        if parsed.action == "event-join-text":
            if len(parsed.args) != 2:
                return _stale_message()
            return _event_join_result(actor, parsed.args[0], parsed.args[1])
        if parsed.action == "acquire":
            return _acquisition_message(actor)
        if parsed.action == "experiments":
            return _experiments_message(actor, _page_number(parsed.args))
        if parsed.action == "experiment":
            if len(parsed.args) != 2:
                return _stale_message()
            return _experiment_message(actor, parsed.args[0], parsed.args[1])
        if parsed.action == "experiment-apply":
            if len(parsed.args) != 4:
                return _stale_message()
            return _experiment_apply_message(
                actor,
                parsed.args[0],
                parsed.args[1],
                parsed.args[2],
                parsed.args[3],
            )
        if parsed.action == "manage":
            return _manage_message(actor)
        if parsed.action == "directions":
            return _directions_message(actor, _page_number(parsed.args))
        if parsed.action == "directions-archived":
            return _directions_message(
                actor,
                _page_number(parsed.args),
                archived=True,
            )
        if parsed.action == "direction":
            if len(parsed.args) != 1:
                return _stale_message()
            return _direction_message(actor, parsed.args[0])
        if parsed.action == "direction-new":
            return _direction_new_message(
                actor,
                current_platform=current_platform,
                input_surface=input_surface,
            )
        if parsed.action == "direction-create-text":
            if len(parsed.args) != 2:
                return _stale_message()
            return _direction_create_result(actor, parsed.args[0], parsed.args[1])
        if parsed.action == "direction-edit":
            if len(parsed.args) != 1:
                return _stale_message()
            return _direction_edit_message(
                actor,
                parsed.args[0],
                current_platform=current_platform,
                input_surface=input_surface,
            )
        if parsed.action == "direction-edit-text":
            if len(parsed.args) != 3:
                return _stale_message()
            return _direction_edit_result(
                actor,
                parsed.args[0],
                parsed.args[1],
                parsed.args[2],
            )
        if parsed.action == "direction-archive":
            if len(parsed.args) != 1:
                return _stale_message()
            return _direction_archive_confirm(actor, parsed.args[0])
        if parsed.action == "direction-archive-ok":
            if len(parsed.args) != 1:
                return _stale_message()
            return _direction_archive_result(actor, parsed.args[0])
        if parsed.action == "direction-restore":
            if len(parsed.args) != 1:
                return _stale_message()
            return _direction_restore_result(actor, parsed.args[0])
        if parsed.action == "manage-more":
            return _manage_more_message(actor)
        if parsed.action == "business-retire":
            return _business_retire_confirm(actor)
        if parsed.action == "business-retire-ok":
            return _business_retire_result(actor)
        if parsed.action == "team":
            return _team_message(actor)
        if parsed.action == "today":
            if actor.role not in _SUPPORT_ROLES:
                return _permission_message()
            return _today_message(actor)
        if parsed.action == "today-full":
            return _today_full_message(actor)
        if parsed.action == "customers":
            return _customers_message(actor, _page_number(parsed.args))
        if parsed.action == "customer":
            if len(parsed.args) != 1:
                return _stale_message()
            return _customer_message(actor, parsed.args[0])
        if parsed.action == "booking-open":
            return _booking_open_message(actor, _page_number(parsed.args))
        if parsed.action == "booking-open-for":
            if len(parsed.args) != 1:
                return _stale_message()
            return _booking_open_for_message(
                actor,
                parsed.args[0],
                current_platform=current_platform,
                input_surface=input_surface,
            )
        if parsed.action == "booking-months":
            if len(parsed.args) != 2 or not parsed.args[1].isdigit():
                return _stale_message()
            return _booking_months_message(
                actor,
                parsed.args[0],
                int(parsed.args[1]),
            )
        if parsed.action == "booking-dates":
            if len(parsed.args) != 3 or not parsed.args[2].isdigit():
                return _stale_message()
            return _booking_dates_message(
                actor,
                parsed.args[0],
                parsed.args[1],
                int(parsed.args[2]),
            )
        if parsed.action == "booking-date":
            if len(parsed.args) != 2:
                return _stale_message()
            return _booking_times_message(actor, parsed.args[0], parsed.args[1], 0)
        if parsed.action == "booking-times":
            if len(parsed.args) != 3 or not parsed.args[2].isdigit():
                return _stale_message()
            return _booking_times_message(
                actor,
                parsed.args[0],
                parsed.args[1],
                int(parsed.args[2]),
            )
        if parsed.action == "booking-time":
            if len(parsed.args) != 3:
                return _stale_message()
            return _booking_durations_message(
                actor,
                parsed.args[0],
                parsed.args[1],
                parsed.args[2],
            )
        if parsed.action == "booking-duration":
            if len(parsed.args) != 4:
                return _stale_message()
            return _booking_create_from_selection(
                actor,
                parsed.args[0],
                parsed.args[1],
                parsed.args[2],
                parsed.args[3],
            )
        if parsed.action == "booking-open-text":
            if len(parsed.args) != 3:
                return _stale_message()
            return _booking_open_create_message(
                actor,
                parsed.args[0],
                parsed.args[1],
                parsed.args[2],
            )
        if parsed.action == "bookings":
            if actor.role not in _SUPPORT_ROLES:
                return _permission_message()
            return _bookings_message(actor)
        if parsed.action == "programs":
            return _programs_message(actor, _page_number(parsed.args))
        if parsed.action == "program-draft":
            if len(parsed.args) != 1:
                return _stale_message()
            return _program_draft_message(actor, parsed.args[0])
        if parsed.action == "program-draft-archive":
            if len(parsed.args) != 1:
                return _stale_message()
            return _program_draft_archive_confirm(actor, parsed.args[0])
        if parsed.action == "program-draft-archive-ok":
            if len(parsed.args) != 1:
                return _stale_message()
            return _program_draft_archive_result(actor, parsed.args[0])
        if parsed.action == "program-lessons":
            if len(parsed.args) not in {1, 2}:
                return _stale_message()
            page = int(parsed.args[1]) if len(parsed.args) == 2 and parsed.args[1].isdigit() else 0
            if len(parsed.args) == 2 and not parsed.args[1].isdigit():
                return _stale_message()
            return _program_lessons_message(actor, parsed.args[0], page)
        if parsed.action == "program-lesson-detail":
            if len(parsed.args) != 1:
                return _stale_message()
            return _program_lesson_detail_message(actor, parsed.args[0])
        if parsed.action == "program-lesson-title-edit":
            if len(parsed.args) != 1:
                return _stale_message()
            return _program_lesson_title_edit_message(
                actor,
                parsed.args[0],
                current_platform=current_platform,
                input_surface=input_surface,
            )
        if parsed.action == "program-lesson-title-text":
            if len(parsed.args) != 2:
                return _stale_message()
            return _program_lesson_title_result(actor, parsed.args[0], parsed.args[1])
        if parsed.action == "program-lesson-content-edit":
            if len(parsed.args) != 1:
                return _stale_message()
            return _program_lesson_content_kinds_message(actor, parsed.args[0])
        if parsed.action == "program-lesson-content-kind":
            if len(parsed.args) != 2:
                return _stale_message()
            return _program_lesson_content_edit_message(
                actor,
                parsed.args[0],
                parsed.args[1],
                current_platform=current_platform,
                input_surface=input_surface,
            )
        if parsed.action == "program-lesson-content-text":
            if len(parsed.args) != 3:
                return _stale_message()
            return _program_lesson_content_result(
                actor,
                parsed.args[0],
                parsed.args[1],
                parsed.args[2],
            )
        if parsed.action == "program-lesson-move":
            if len(parsed.args) != 2:
                return _stale_message()
            return _program_lesson_move_result(actor, parsed.args[0], parsed.args[1])
        if parsed.action == "program-lesson-archive":
            if len(parsed.args) != 1:
                return _stale_message()
            return _program_lesson_archive_confirm(actor, parsed.args[0])
        if parsed.action == "program-lesson-archive-ok":
            if len(parsed.args) != 1:
                return _stale_message()
            return _program_lesson_archive_result(actor, parsed.args[0])
        if parsed.action == "program-create":
            return _program_create_help(
                actor, current_platform=current_platform, input_surface=input_surface
            )
        if parsed.action == "program-create-dirs":
            return _program_direction_message(actor, _page_number(parsed.args))
        if parsed.action == "program-create-dir":
            if len(parsed.args) != 1:
                return _stale_message()
            return _program_title_input(
                actor,
                current_platform=current_platform,
                input_surface=input_surface,
                direction_id=None if parsed.args[0] == "none" else parsed.args[0],
            )
        if parsed.action == "program-create-text":
            if len(parsed.args) not in {1, 2}:
                return _stale_message()
            return _program_create_result(
                actor,
                parsed.args[0],
                interaction_key=setup_key,
                direction_id=parsed.args[1] if len(parsed.args) == 2 else None,
            )
        if parsed.action == "program-lesson":
            if len(parsed.args) != 1:
                return _stale_message()
            return _program_lesson_help(actor, parsed.args[0])
        if parsed.action == "program-lesson-kind":
            if len(parsed.args) != 2:
                return _stale_message()
            return _program_lesson_kind_message(
                actor,
                parsed.args[0],
                parsed.args[1],
                current_platform=current_platform,
                input_surface=input_surface,
            )
        if parsed.action == "program-lesson-text":
            if len(parsed.args) != 4:
                return _stale_message()
            return _program_lesson_result(
                actor,
                parsed.args[0],
                parsed.args[1],
                parsed.args[2],
                parsed.args[3],
                interaction_key=setup_key,
            )
        if parsed.action == "program-publish":
            if len(parsed.args) != 1:
                return _stale_message()
            return _program_publish_result(
                actor, parsed.args[0], current_platform=current_platform
            )
        if parsed.action == "program-deliver":
            if len(parsed.args) != 1:
                return _stale_message()
            return _program_deliver_help(
                actor, parsed.args[0], current_platform=current_platform
            )
        if parsed.action in {"program-deliver-text", "program-deliver-to"}:
            if len(parsed.args) != 2:
                return _stale_message()
            return _program_deliver_result(
                actor,
                parsed.args[0],
                parsed.args[1],
                current_platform=current_platform,
            )
        if parsed.action == "offering-new":
            return _offering_new_help(actor)
        if parsed.action == "offering-new-for":
            if len(parsed.args) != 1:
                return _stale_message()
            return _offering_new_for_message(
                actor,
                parsed.args[0],
                current_platform=current_platform,
                input_surface=input_surface,
            )
        if parsed.action == "offering-new-dirs":
            if len(parsed.args) != 2 or not parsed.args[1].isdigit():
                return _stale_message()
            return _offering_direction_message(
                actor,
                parsed.args[0],
                int(parsed.args[1]),
            )
        if parsed.action == "offering-new-dir":
            if len(parsed.args) != 2:
                return _stale_message()
            return _offering_input_message(
                actor,
                parsed.args[0],
                current_platform=current_platform,
                input_surface=input_surface,
                direction_id=None if parsed.args[1] == "none" else parsed.args[1],
            )
        if parsed.action == "offering-new-text":
            if len(parsed.args) not in {3, 4}:
                return _stale_message()
            return _offering_new_result(
                actor,
                parsed.args[0],
                parsed.args[1],
                parsed.args[2],
                interaction_key=setup_key,
                direction_id=parsed.args[3] if len(parsed.args) == 4 else None,
            )
        if parsed.action == "offering-retire-list":
            return _offering_retire_list_message(actor, _page_number(parsed.args))
        if parsed.action == "offering-retire":
            if len(parsed.args) != 1:
                return _stale_message()
            return _offering_retire_confirm(actor, parsed.args[0])
        if parsed.action == "offering-retire-ok":
            if len(parsed.args) != 1:
                return _stale_message()
            return _offering_retire_result(actor, parsed.args[0])
        if parsed.action == "behavior":
            return _behavior_message(actor)
        if parsed.action == "attention":
            return _attention_message(actor)
        if parsed.action == "sales":
            return _sales_message(actor)
        if parsed.action == "reactivate":
            return _reactivation_message(actor)
        if parsed.action == "reactivate-approve":
            if len(parsed.args) != 2:
                return _stale_message()
            return _reactivation_approve_message(actor, parsed.args[0], parsed.args[1])
        if parsed.action == "ad-spend":
            return _ad_spend_message(actor)
        if parsed.action == "ad-spend-launch":
            if len(parsed.args) != 1:
                return _stale_message()
            return _ad_spend_launch_message(actor, parsed.args[0])
        if parsed.action == "sales-recent":
            return _sales_recent_message(actor)
        if parsed.action == "sales-handoffs":
            return _sales_handoffs_message(actor)
        if parsed.action == "sales-lead":
            if len(parsed.args) != 1:
                return _stale_message()
            return _sales_lead_message(actor, parsed.args[0])
        if parsed.action == "sales-actions":
            if len(parsed.args) != 1:
                return _stale_message()
            return _sales_actions_message(actor, parsed.args[0])
        if parsed.action == "sales-result-menu":
            if len(parsed.args) != 1:
                return _stale_message()
            return _sales_result_message(actor, parsed.args[0])
        if parsed.action in {
            "sales-assign",
            "sales-unassign",
            "sales-stage",
            "sales-note-text",
            "sales-next-text",
            "sales-close-text",
            "sales-reopen",
            "sales-handoff-claim",
            "sales-handoff-resolve",
            "sales-followup-text",
            "sales-followup-cancel",
            "sales-followup-optout-text",
        }:
            return _sales_mutation_message(
                actor, parsed, interaction_key=setup_key
            )
        if parsed.action == "sales-note-help":
            if len(parsed.args) != 1:
                return _stale_message()
            return _sales_input_help(parsed.args[0], "note")
        if parsed.action == "sales-next-help":
            if len(parsed.args) != 1:
                return _stale_message()
            return _sales_input_help(parsed.args[0], "next")
        if parsed.action == "sales-close-help":
            if len(parsed.args) != 2 or parsed.args[1] not in {"won", "lost"}:
                return _stale_message()
            return _sales_input_help(parsed.args[0], "close", parsed.args[1])
        if parsed.action == "sales-followup-menu":
            if len(parsed.args) != 1:
                return _stale_message()
            return _sales_followup_message(actor, parsed.args[0])
        if parsed.action == "sales-followup-help":
            if len(parsed.args) != 1:
                return _stale_message()
            return _sales_followup_help(parsed.args[0])
        if parsed.action == "sales-followup-optout-help":
            if len(parsed.args) != 1:
                return _stale_message()
            return _sales_followup_optout_help(parsed.args[0])
        if parsed.action == "publication-new":
            return _publication_new_help(actor)
        if parsed.action == "publication-new-for":
            if len(parsed.args) != 1:
                return _stale_message()
            return _publication_new_for_message(
                actor,
                parsed.args[0],
                current_platform=current_platform,
                input_surface=input_surface,
            )
        if parsed.action == "publication-new-text":
            if len(parsed.args) != 3:
                return _stale_message()
            return _publication_new_result(
                actor,
                parsed.args[0],
                parsed.args[1],
                parsed.args[2],
                interaction_key=setup_key,
            )
        if parsed.action == "publication-publish":
            if len(parsed.args) != 1:
                return _stale_message()
            return _publication_publish_result(actor, parsed.args[0])
        if parsed.action == "publication-schedule":
            if len(parsed.args) != 1:
                return _stale_message()
            return _publication_schedule_help(actor, parsed.args[0])
        if parsed.action == "publication-schedule-text":
            if len(parsed.args) != 2:
                return _stale_message()
            return _publication_schedule_result(
                actor,
                parsed.args[0],
                parsed.args[1],
                interaction_key=setup_key,
            )
        if parsed.action == "publication-cancel":
            if len(parsed.args) != 2:
                return _stale_message()
            return _publication_cancel_confirm(actor, parsed.args[0], parsed.args[1])
        if parsed.action == "publication-cancel-ok":
            if len(parsed.args) != 2:
                return _stale_message()
            return _publication_cancel_result(actor, parsed.args[0], parsed.args[1])
        if parsed.action == "publication-retire-list":
            return _publication_retire_list_message(actor, _page_number(parsed.args))
        if parsed.action == "publication-retire":
            if len(parsed.args) != 1:
                return _stale_message()
            return _publication_retire_confirm(actor, parsed.args[0])
        if parsed.action == "publication-retire-ok":
            if len(parsed.args) != 1:
                return _stale_message()
            return _publication_retire_result(actor, parsed.args[0])
        if parsed.action == "payment-new":
            return _payment_new_help(
                actor, current_platform=current_platform, input_surface=input_surface
            )
        if parsed.action == "payment-new-text":
            if len(parsed.args) != 5:
                return _stale_message()
            return _payment_new_result(
                actor, *parsed.args, interaction_key=setup_key
            )
        if parsed.action == "pay-refund":
            if len(parsed.args) != 1:
                return _stale_message()
            return _payment_refund_confirm(actor, parsed.args[0])
        if parsed.action == "pay-refund-ok":
            if len(parsed.args) != 1:
                return _stale_message()
            return _payment_refund_result(actor, parsed.args[0])
        if parsed.action == "price-set":
            if len(parsed.args) != 1:
                return _stale_message()
            return _price_set_help(
                actor,
                parsed.args[0],
                current_platform=current_platform,
                input_surface=input_surface,
            )
        if parsed.action == "price-set-text":
            if len(parsed.args) != 3:
                return _stale_message()
            return _price_set_result(actor, parsed.args[0], parsed.args[1], parsed.args[2])
        if parsed.action in {"autopilot-enable", "autopilot-disable"}:
            return _automation_mutation_message(actor, parsed.action)
        if parsed.action in {"automation-approve", "automation-reject", "automation-revoke"}:
            if len(parsed.args) != 1:
                return _stale_message()
            return _automation_mutation_message(actor, parsed.action, parsed.args[0])
        if parsed.action == "invites":
            return _invites_message(actor)
        if parsed.action == "invite-new":
            return _invite_new_result(actor)
        if parsed.action == "member-add-help":
            return _member_add_help(actor)
        if parsed.action == "member-add-role":
            if len(parsed.args) != 1:
                return _stale_message()
            return _member_add_role_message(
                actor,
                parsed.args[0],
                current_platform=current_platform,
                input_surface=input_surface,
            )
        if parsed.action == "member-add-text":
            if len(parsed.args) != 2:
                return _stale_message()
            return _member_add_result(actor, parsed.args[0], parsed.args[1])
        if parsed.action == "member-role":
            if len(parsed.args) != 2 or not parsed.args[0].isdigit():
                return _stale_message()
            return _member_role_result(actor, int(parsed.args[0]), parsed.args[1])
        if parsed.action == "member-revoke":
            if len(parsed.args) != 1 or not parsed.args[0].isdigit():
                return _stale_message()
            return _member_revoke_result(actor, int(parsed.args[0]))
        if parsed.action == "activity-edit-help":
            return _activity_edit_help(
                actor, current_platform=current_platform, input_surface=input_surface
            )
        if parsed.action == "activity-edit-text":
            if len(parsed.args) != 1:
                return _stale_message()
            return _activity_edit_result(actor, parsed.args[0])
        if parsed.action == "messengers":
            return _messengers_message(
                actor,
                setup_available=setup_issuer is not None,
                current_platform=current_platform,
            )
        if parsed.action in {
            "autopilot",
            "publications",
            "funnel",
            "money",
            "payments",
            "segments",
            "offers",
            "copy",
            "prices",
        }:
            return _growth_report_message(actor, parsed.action)
        if parsed.action in {"release", "funnel2", "retention", "recent", "system"}:
            return _admin_report_message(actor, parsed.action)
        if parsed.action == "formats":
            return _formats_message(actor, _page_number(parsed.args))
        if parsed.action in {"format-enable", "format-disable"}:
            if len(parsed.args) != 1:
                return _stale_message()
            return _format_toggle_result(
                actor,
                parsed.args[0],
                enabled=parsed.action == "format-enable",
            )
        if parsed.action == "tariff":
            return _tariff_message(actor)
        if parsed.action == "tariff-upgrade":
            return _tariff_upgrade_message(actor)
        if parsed.action == "members":
            return _members_message(actor, _page_number(parsed.args))
        if parsed.action == "member":
            if len(parsed.args) != 1 or not parsed.args[0].isdigit():
                return _stale_message()
            return _member_message(actor, int(parsed.args[0]))
        if parsed.action == "permissions":
            return _permissions_message(actor)
        if parsed.action == "connect-telegram":
            return _setup_message(
                actor,
                platform=ConnectionPlatform.TELEGRAM,
                setup_issuer=setup_issuer,
                setup_key=setup_key,
                setup_available=setup_issuer is not None,
            )
        if parsed.action == "connect-vk":
            return _setup_message(
                actor,
                platform=ConnectionPlatform.VK,
                setup_issuer=setup_issuer,
                setup_key=setup_key,
                setup_available=setup_issuer is not None,
            )
        if parsed.action == "connect-max":
            return _setup_message(
                actor,
                platform=ConnectionPlatform.MAX,
                setup_issuer=setup_issuer,
                setup_key=setup_key,
                setup_available=setup_issuer is not None,
            )
    except TenantPermissionDenied:
        return _permission_message()
    except ActivityDirectionError:
        return _stale_message()
    except (ActivityError, ProgramError, SalesError):
        return _stale_message()
    except ValueError:
        return _stale_message()
    return _menu_message(actor, linked=linked)


def render_native_member_interaction(
    *,
    actor: TenantContext,
    raw_text: object,
    interaction_key: str,
    current_platform: ConnectionPlatform,
    linked: bool = False,
    setup_issuer: NativeSetupCommandIssuer | None = None,
    resolve_pending_input: bool = False,
) -> CustomerInteractionMessage:
    """Render the canonical staff UI without requiring a tenant webhook route.

    This is the channel-neutral control-plane adapter used by official ClientPlatform
    owner entry points. Tenant-scoped provider ingress keeps using
    ``process_native_member_interaction`` so its durable delivery boundary is unchanged.
    """

    current = resolve_tenant_context(
        user_id=actor.user_id,
        business_id=actor.business_id,
    )
    if resolve_pending_input:
        parsed, pending = _pending_owner_input(
            current,
            platform=current_platform,
            surface="official",
            raw_text=raw_text,
        )
    else:
        parsed, pending = parse_native_member_interaction(raw_text), None
    interaction = _render(
        current,
        parsed,
        linked=linked,
        setup_issuer=setup_issuer,
        setup_key=str(interaction_key or "official-owner-entry"),
        input_surface="official",
        current_platform=current_platform,
    )
    interaction = _with_parent_navigation(interaction, parsed)
    if (
        pending is not None
        and pending.action != "online_event"
        and parsed.action != "owner-input-invalid"
    ):
        clear_owner_input(
            user_id=current.user_id,
            platform=current_platform.value,
            surface="official",
        )
    return interaction


def process_native_member_interaction(
    *,
    route: MessengerIngressRoute,
    resolution: NativeMemberResolution,
    external_subject: str,
    raw_text: object,
    provider_event_id: str,
    setup_issuer: NativeSetupCommandIssuer | None = None,
) -> Any:
    actor = resolve_tenant_context(
        user_id=resolution.actor.user_id,
        business_id=route.business_id,
    )
    input_surface = f"route:{route.id}"
    parsed, pending = _pending_owner_input(
        actor,
        platform=route.platform,
        surface=input_surface,
        raw_text=raw_text,
    )
    action_payload = "\x1f".join((parsed.action, *parsed.args))
    action_digest = hashlib.sha256(action_payload.encode("utf-8")).hexdigest()[:20]
    action_key = f"{parsed.action}:{action_digest}"
    interaction_key = (
        f"route:{route.id}:event:{provider_event_id}:"
        + f"member:{actor.user_id}:action:{action_key}"
    )
    interaction = _render(
        actor,
        parsed,
        linked=resolution.linked,
        setup_issuer=setup_issuer,
        setup_key=interaction_key,
        input_surface=input_surface,
        current_platform=route.platform,
    )
    interaction = _with_parent_navigation(interaction, parsed)
    if (
        pending is not None
        and pending.action != "online_event"
        and parsed.action != "owner-input-invalid"
    ):
        clear_owner_input(
            user_id=actor.user_id,
            platform=route.platform.value,
            surface=input_surface,
        )
    with get_db() as conn:
        return DispatchOutboxRepository(conn).materialize_member_interaction(
            business_id=route.business_id,
            connection_id=route.connection_id,
            member_user_id=actor.user_id,
            platform=route.platform.value,
            external_subject=external_subject,
            interaction=interaction,
            interaction_key=interaction_key,
        )


__all__ = [
    "NativeMemberBridgeRejected",
    "NativeMemberResolution",
    "NativeSetupCommandIssuer",
    "SIMPLE_OWNER_NATIVE_INTENT_EQUIVALENTS",
    "TELEGRAM_NATIVE_ACTION_EQUIVALENTS",
    "parse_native_member_interaction",
    "recognizes_native_member_interaction",
    "process_native_member_interaction",
    "render_native_member_interaction",
    "resolve_native_member",
]
