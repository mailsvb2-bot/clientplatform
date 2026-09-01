from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, User

from clientplatform.application.admin_ops import PublicationCalendarProjection
from clientplatform.domain.activity import BusinessProfileStatus, CapabilityStatus
from clientplatform.domain.bookings import BookingSlotStatus
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import (
    PlatformRole,
    TenantContext,
    TenantPermissionDenied,
)
from handlers import clientplatform_admin as admin


BUSINESS_ID = str(uuid4())
MEMBERSHIP_ID = str(uuid4())
CUSTOMER_ID = str(uuid4())


def telegram_message(*, user_id: int = 77, text: str = "текст") -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        text=text,
    )


def telegram_callback(*, data: str = "cpa:token:menu", user_id: int = 77) -> CallbackQuery:
    message = telegram_message(user_id=user_id)
    assert message.from_user is not None
    return CallbackQuery(
        id=f"callback-{data}",
        from_user=message.from_user,
        chat_instance="instance",
        message=message,
        data=data,
    )


def fsm_context(*, user_id: int = 77) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


def admin_context(role: PlatformRole = PlatformRole.OWNER) -> admin.AdminContext:
    actor = TenantContext(
        business_id=BUSINESS_ID,
        user_id=77,
        membership_id=MEMBERSHIP_ID,
        role=role,
    )
    return admin.AdminContext(
        user_id=77,
        business_id=BUSINESS_ID,
        business_name="Сантехник",
        actor=actor,
        role=role,
    )


@pytest.fixture(autouse=True)
def stable_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    token_by_uuid = {
        BUSINESS_ID: "business-token",
        CUSTOMER_ID: "customer-token",
    }
    uuid_by_token = {value: key for key, value in token_by_uuid.items()}
    monkeypatch.setattr(
        admin.control,
        "_uuid_token",
        lambda value: token_by_uuid.get(str(value), f"token-{str(value)[:8]}"),
    )
    monkeypatch.setattr(
        admin.control,
        "_token_uuid",
        lambda value: uuid_by_token.get(str(value), BUSINESS_ID),
    )


def labels(markup: InlineKeyboardMarkup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def callbacks(markup: InlineKeyboardMarkup) -> list[str | None]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def capability_projection(
    *,
    telegram: admin.CapabilityAvailability = admin.CapabilityAvailability.CONNECTABLE,
    vk: admin.CapabilityAvailability = admin.CapabilityAvailability.UNAVAILABLE,
    max_channel: admin.CapabilityAvailability = admin.CapabilityAvailability.UNAVAILABLE,
):
    states = {
        ConnectionPlatform.TELEGRAM: telegram,
        ConnectionPlatform.VK: vk,
        ConnectionPlatform.MAX: max_channel,
    }
    items = tuple(
        SimpleNamespace(
            platform=platform,
            availability=availability,
            can_connect=availability == admin.CapabilityAvailability.CONNECTABLE,
        )
        for platform, availability in states.items()
    )
    return SimpleNamespace(
        messengers=items,
        messenger=lambda platform: next(item for item in items if item.platform == platform),
    )


def test_owner_menu_uses_five_human_groups_instead_of_26_buttons() -> None:
    markup = admin._menu_keyboard(admin_context())

    assert labels(markup) == [
        "👥 Клиенты и работа",
        "📣 Публикации и каналы",
        "📈 Продвижение и продажи",
        "👤 Сотрудники и тариф",
        "🛠 Технические проверки",
        "⬅️ Назад",
    ]
    assert all(
        value is not None and value.startswith("cpa:")
        for value in callbacks(markup)
    )


@pytest.mark.parametrize(
    ("role", "present", "absent"),
    [
        (
            PlatformRole.SUPPORT,
            {"👥 Клиенты и работа", "📣 Публикации и каналы"},
            {"📈 Продвижение и продажи", "👤 Сотрудники и тариф", "🛠 Технические проверки"},
        ),
        (
            PlatformRole.MARKETER,
            {"📣 Публикации и каналы", "📈 Продвижение и продажи"},
            {"👥 Клиенты и работа", "👤 Сотрудники и тариф", "🛠 Технические проверки"},
        ),
        (
            PlatformRole.CONTENT_MANAGER,
            {"📣 Публикации и каналы"},
            {"👥 Клиенты и работа", "📈 Продвижение и продажи", "👤 Сотрудники и тариф", "🛠 Технические проверки"},
        ),
        (
            PlatformRole.ADMINISTRATOR,
            {"👥 Клиенты и работа", "📣 Публикации и каналы", "📈 Продвижение и продажи", "🛠 Технические проверки"},
            {"👤 Сотрудники и тариф"},
        ),
    ],
)
def test_menu_is_filtered_by_live_business_role(
    role: PlatformRole,
    present: set[str],
    absent: set[str],
) -> None:
    visible = set(labels(admin._menu_keyboard(admin_context(role))))

    assert present <= visible
    assert not (absent & visible)


def test_callback_codec_supports_new_and_legacy_keyboards() -> None:
    ctx = admin_context()

    value = admin._callback(ctx, "customer", "customer-token")
    assert value == "cpa:business-token:customer:customer-token"
    assert len(value.encode("utf-8")) <= 64
    assert admin._parse_callback(value) == (
        BUSINESS_ID,
        "customer",
        ("customer-token",),
    )
    assert admin._parse_callback("cpa:home:business-token") == (
        BUSINESS_ID,
        "menu",
        (),
    )
    assert admin._parse_callback("cpa:formats:business-token") == (
        BUSINESS_ID,
        "formats",
        (),
    )
    assert admin._parse_callback("cpa:back:business-token") == (
        BUSINESS_ID,
        "leave",
        (),
    )


def test_callback_codec_rejects_oversized_payload() -> None:
    with pytest.raises(ValueError, match="exceeds Telegram limit"):
        admin._callback(admin_context(), "customer", "x" * 80)


@pytest.mark.asyncio
async def test_load_admin_context_revalidates_actor_and_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = admin_context().actor
    calls: list[tuple[str, Any]] = []

    async def resolve_actor(user_id: int, business_id: str) -> TenantContext:
        calls.append(("actor", (user_id, business_id)))
        return actor

    def accesses(*, user_id: int) -> list[Any]:
        calls.append(("accesses", user_id))
        return [
            SimpleNamespace(
                business=SimpleNamespace(id=BUSINESS_ID, name="Сантехник"),
                membership=SimpleNamespace(role=PlatformRole.OWNER),
            )
        ]

    monkeypatch.setattr(admin.control, "_actor", resolve_actor)
    monkeypatch.setattr(admin, "list_accessible_businesses", accesses)

    ctx = await admin._load_admin_context(user_id=77, business_id=BUSINESS_ID)

    assert ctx.business_name == "Сантехник"
    assert ctx.role == PlatformRole.OWNER
    assert calls == [
        ("actor", (77, BUSINESS_ID)),
        ("accesses", 77),
    ]


@pytest.mark.asyncio
async def test_safe_edit_edits_the_existing_admin_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edits: list[tuple[str, InlineKeyboardMarkup | None]] = []

    async def edit_text(
        _message: Message,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        **_kwargs: Any,
    ) -> None:
        edits.append((text, reply_markup))

    monkeypatch.setattr(Message, "edit_text", edit_text)
    callback = telegram_callback()

    await admin._safe_edit(
        callback,
        "Экран",
        admin._back_keyboard(admin_context()),
    )

    assert edits[-1][0] == "Экран"
    assert edits[-1][1] is not None


@pytest.mark.asyncio
async def test_render_menu_uses_exact_panel_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[str] = []

    async def answer(
        _message: Message,
        text: str,
        **_kwargs: Any,
    ) -> None:
        answers.append(text)

    monkeypatch.setattr(Message, "answer", answer)
    state = fsm_context()

    await admin._render_menu(
        telegram_message(),
        state,
        admin_context(),
        reset=True,
    )

    assert len(answers) == 1
    text = answers[0]
    assert text.startswith("⚙️ Управление бизнесом\n\nСантехник · Владелец\n\nЕсли Вам нужно:\n")
    for label in (
        "👥 Клиенты и работа",
        "📣 Публикации и каналы",
        "📈 Продвижение и продажи",
        "👤 Сотрудники и тариф",
        "🛠 Технические проверки",
    ):
        assert f"«{label}»" in text
    assert "Технические проверки вынесены отдельно" in text
    assert (await state.get_data())["cp_admin_section"] == "menu"


@pytest.mark.asyncio
async def test_open_admin_command_handles_zero_one_and_multiple_businesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[tuple[str, InlineKeyboardMarkup | None]] = []

    async def answer(
        _message: Message,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        **_kwargs: Any,
    ) -> None:
        answers.append((text, reply_markup))

    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(admin.control, "_user_id", lambda _message: 77)

    monkeypatch.setattr(admin, "list_accessible_businesses", lambda **_kwargs: [])
    await admin.open_admin_command(telegram_message(), fsm_context())
    assert answers[-1][0] == "Сначала создайте бизнес через /start."

    access = SimpleNamespace(
        business=SimpleNamespace(id=BUSINESS_ID, name="Сантехник"),
    )
    monkeypatch.setattr(
        admin,
        "list_accessible_businesses",
        lambda **_kwargs: [access],
    )
    monkeypatch.setattr(
        admin,
        "_load_admin_context",
        lambda **_kwargs: _async_value(admin_context()),
    )
    await admin.open_admin_command(telegram_message(), fsm_context())
    assert answers[-1][0].startswith("⚙️ Управление бизнесом")

    second_id = str(uuid4())
    monkeypatch.setattr(
        admin,
        "list_accessible_businesses",
        lambda **_kwargs: [
            access,
            SimpleNamespace(
                business=SimpleNamespace(id=second_id, name="Второй"),
            ),
        ],
    )
    await admin.open_admin_command(telegram_message(), fsm_context())
    assert answers[-1][0] == "Для какого бизнеса открыть админку?"
    assert answers[-1][1] is not None


async def _async_value(value: Any) -> Any:
    return value


def snapshot() -> tuple[Any, Any, list[Any], list[Any], list[Any], list[Any], list[Any]]:
    stamp = datetime.now(timezone.utc).isoformat()
    profile = SimpleNamespace(
        timezone="UTC",
        status=BusinessProfileStatus.READY,
        activity_description="Ремонтирую сантехнику",
    )
    summary = SimpleNamespace(
        customers=3,
        programs=2,
        dispatch_pending=1,
        dispatch_sent=7,
        dispatch_attention=1,
    )
    capabilities = [
        SimpleNamespace(
            status=CapabilityStatus.ACTIVE,
            title="Услуги",
        ),
        SimpleNamespace(
            status=CapabilityStatus.DISABLED,
            title="Консультации",
        ),
    ]
    slots = [
        SimpleNamespace(slot=SimpleNamespace(status=BookingSlotStatus.OPEN)),
        SimpleNamespace(slot=SimpleNamespace(status=BookingSlotStatus.BOOKED)),
    ]
    customers = [
        SimpleNamespace(
            id=CUSTOMER_ID,
            display_name="Иван",
            created_at=stamp,
        )
    ]
    programs = [
        SimpleNamespace(
            id=str(uuid4()),
            title="Первичная программа",
            created_at=stamp,
        )
    ]
    progress = [
        SimpleNamespace(
            customer_id=CUSTOMER_ID,
            customer_display_name="Иван",
            program_title="Первичная программа",
            completed_lessons=1,
            total_lessons=2,
            percent_complete=50,
            updated_at=stamp,
        )
    ]
    return profile, summary, capabilities, slots, customers, programs, progress


@pytest.fixture
def capture_edits(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, InlineKeyboardMarkup]]:
    captured: list[tuple[str, InlineKeyboardMarkup]] = []

    async def safe_edit(
        _callback: CallbackQuery,
        text: str,
        reply_markup: InlineKeyboardMarkup,
    ) -> None:
        captured.append((text, reply_markup))

    monkeypatch.setattr(admin, "_safe_edit", safe_edit)
    return captured


@pytest.mark.asyncio
async def test_fallback_marketing_renderer_covers_publications_and_regular_screen(
    monkeypatch: pytest.MonkeyPatch,
    capture_edits: list[tuple[str, InlineKeyboardMarkup]],
) -> None:
    monkeypatch.setattr(admin, "_base_snapshot", lambda _ctx: _async_value(snapshot()))
    monkeypatch.setattr(
        admin,
        "get_publication_calendar_projection",
        lambda **_kwargs: PublicationCalendarProjection(
            entries=(),
            actionable_drafts=(),
            draft_count=2,
            scheduled_count=21,
            published_count=7,
            failed_count=1,
            cancelled_count=0,
        ),
    )
    state = fsm_context()
    callback = telegram_callback()
    ctx = admin_context()

    await admin._render_marketing_fallback(callback, state, ctx, "publications")
    publication_text = capture_edits[-1][0]
    assert "Черновики: 2" in publication_text
    assert "Запланировано: 21" in publication_text
    assert "• Публикаций пока нет." in publication_text
    assert "ещё не подключ" not in publication_text.casefold()

    await admin._render_marketing_fallback(callback, state, ctx, "autopilot")
    assert capture_edits[-1][0].startswith("🤖 Автоматизация")


@pytest.mark.asyncio
async def test_all_summary_and_marketing_screens_render(
    monkeypatch: pytest.MonkeyPatch,
    capture_edits: list[tuple[str, InlineKeyboardMarkup]],
) -> None:
    monkeypatch.setattr(admin, "_base_snapshot", lambda _ctx: _async_value(snapshot()))
    monkeypatch.setattr(
        admin,
        "business_delivery_summary",
        lambda **_kwargs: snapshot()[1],
    )
    monkeypatch.setattr(
        admin,
        "get_publication_calendar_projection",
        lambda **_kwargs: PublicationCalendarProjection(
            entries=(),
            actionable_drafts=(),
            draft_count=0,
            scheduled_count=0,
            published_count=0,
            failed_count=0,
            cancelled_count=0,
        ),
    )
    state = fsm_context()
    ctx = admin_context()
    callback = telegram_callback()

    await admin._render_today(callback, state, ctx, full=False)
    await admin._render_today(callback, state, ctx, full=True)
    await admin._render_attention(callback, state, ctx)
    for action in [
        "autopilot",
        "publications",
        "funnel",
        "money",
        "payments",
        "segments",
        "offers",
        "copy",
        "prices",
    ]:
        await admin._render_marketing(callback, state, ctx, action)
    for action in [
        "release",
        "invites",
        "funnel2",
        "retention",
        "recent",
        "system",
    ]:
        await admin._render_admin_report(callback, state, ctx, action)
    await admin._render_tariff(callback, state, ctx)

    rendered = "\n".join(item[0] for item in capture_edits)
    for title in [
        "📊 Сегодня (кратко)",
        "📈 Сегодня (подробно)",
        "⚠️ Требуют внимания",
        "🤖 Автоматизация",
        "📣 Публикации",
        "📚 Прохождение программ",
        "💰 Выручка и платящие клиенты",
        "💰 Оплаты",
        "👥 Группы клиентов",
        "🧪 Услуги и предложения",
        "✍️ Подготовить текст",
        "💵 Цены",
        "✅ Проверить готовность",
        "🎁 Приглашения и рекомендации",
        "🧭 Путь клиента",
        "♻️ Кого стоит вернуть",
        "🧾 История изменений",
        "🛠 Проверка системы",
        "💳 Тариф ClientPlatform",
    ]:
        assert title in rendered


@pytest.mark.asyncio
async def test_customer_behavior_messenger_and_format_screens_render(
    monkeypatch: pytest.MonkeyPatch,
    capture_edits: list[tuple[str, InlineKeyboardMarkup]],
) -> None:
    profile, _summary, capabilities, _slots, customers, _programs, progress = snapshot()
    monkeypatch.setattr(admin, "get_business_profile", lambda **_kwargs: profile)
    monkeypatch.setattr(admin, "list_customers", lambda **_kwargs: customers)
    monkeypatch.setattr(admin, "list_business_program_progress", lambda **_kwargs: progress)
    monkeypatch.setattr(admin, "list_business_capabilities", lambda **_kwargs: capabilities)
    monkeypatch.setattr(
        admin,
        "get_business_capability_projection",
        lambda **_kwargs: capability_projection(
            vk=admin.CapabilityAvailability.ACTIVE,
            max_channel=admin.CapabilityAvailability.ATTENTION,
        ),
    )
    monkeypatch.setattr(
        admin,
        "get_customer",
        lambda **_kwargs: SimpleNamespace(
            customer=SimpleNamespace(
                display_name="Иван",
                status=SimpleNamespace(value="active"),
                created_at=datetime.now(timezone.utc).isoformat(),
            ),
            identities=(
                SimpleNamespace(
                    platform=SimpleNamespace(value="telegram"),
                    username="ivan",
                    display_name="Иван",
                    external_subject="77",
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        admin,
        "get_customer_timeline",
        lambda **_kwargs: SimpleNamespace(entries=()),
    )
    monkeypatch.setattr(
        admin,
        "format_customer_timeline_lines",
        lambda _timeline: ("• 27.08.2026 · Получена оплата · 500,00 RUB",),
    )
    state = fsm_context()
    ctx = admin_context()
    callback = telegram_callback()

    await admin._render_customer_list(callback, state, ctx, today_only=True)
    await admin._render_customer_list(callback, state, ctx, today_only=False)
    await admin._render_customer_card(
        callback,
        state,
        ctx,
        "customer-token",
    )
    await admin._render_behavior(callback, state, ctx)
    await admin._render_messengers(callback, state, ctx)
    await admin._render_formats(callback, state, ctx)

    rendered = "\n".join(item[0] for item in capture_edits)
    assert "👥 Клиенты сегодня" in rendered
    assert "🔎 Карточка клиента" in rendered
    assert "История клиента" in rendered
    assert "Получена оплата" in rendered
    assert "🧠 Поведение" in rendered
    assert "💬 Мессенджеры бизнеса" in rendered
    assert "ВКонтакте: ✅ работает" in rendered
    assert "MAX: ⚠️ требует внимания" in rendered
    assert "Telegram: ○ можно подключить" in rendered
    assert "🧩 Форматы работы" in rendered


@pytest.mark.asyncio
async def test_team_and_permission_screens_render(
    monkeypatch: pytest.MonkeyPatch,
    capture_edits: list[tuple[str, InlineKeyboardMarkup]],
) -> None:
    members = [
        {
            "id": str(uuid4()),
            "user_id": 77,
            "role": "owner",
            "status": "active",
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
            "revoked_at": None,
        },
        {
            "id": str(uuid4()),
            "user_id": 88,
            "role": "support",
            "status": "active",
            "created_at": "2026-01-02",
            "updated_at": "2026-01-02",
            "revoked_at": None,
        },
    ]
    monkeypatch.setattr(admin, "_list_members_sync", lambda _actor: members)
    state = fsm_context()
    ctx = admin_context()
    callback = telegram_callback()

    await admin._render_members(callback, state, ctx)
    await admin._render_member_card(callback, state, ctx, 88)
    await admin._render_permissions(callback, state, ctx)
    await admin._begin_add_member(callback, state, ctx)

    rendered = "\n".join(item[0] for item in capture_edits)
    assert "👥 Роли команды" in rendered
    assert "👤 Сотрудник" in rendered
    assert "🔐 Доступы сотрудников" in rendered
    assert "👥 Добавить сотрудника" in rendered


@pytest.mark.asyncio
async def test_add_member_input_is_validated_and_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[str] = []

    async def answer(
        _message: Message,
        text: str,
        **_kwargs: Any,
    ) -> None:
        answers.append(text)

    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(admin.control, "_user_id", lambda _message: 77)
    monkeypatch.setattr(
        admin,
        "_load_admin_context",
        lambda **_kwargs: _async_value(admin_context()),
    )
    granted: list[tuple[int, PlatformRole]] = []

    def grant(*, actor: TenantContext, user_id: int, role: PlatformRole) -> Any:
        granted.append((user_id, role))
        return SimpleNamespace(user_id=user_id, role=role)

    monkeypatch.setattr(admin, "grant_business_member", grant)
    state = fsm_context()
    await state.set_state(admin.ClientPlatformAdminState.waiting_member_user)
    await state.update_data(
        cp_admin_business_id=BUSINESS_ID,
        cp_admin_member_role=PlatformRole.SUPPORT.value,
    )

    await admin.receive_member_user(
        telegram_message(text="не пользователь"),
        state,
    )
    assert "Не понял" in answers[-1]

    await admin.receive_member_user(
        telegram_message(text="88"),
        state,
    )
    assert granted == [(88, PlatformRole.SUPPORT)]
    assert await state.get_state() is None
    assert any("Сотрудник добавлен" in item for item in answers)


@pytest.mark.asyncio
async def test_optional_thread_call_does_not_bypass_permissions() -> None:
    def denied(**_kwargs: Any) -> None:
        raise TenantPermissionDenied("denied")

    result = await admin._optional_thread_call(
        denied,
        default=["restricted"],
    )

    assert result == ["restricted"]


@pytest.mark.asyncio
async def test_admin_gate_routes_every_section_through_live_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = admin_context()
    calls: list[str] = []

    monkeypatch.setattr(
        admin,
        "_load_admin_context",
        lambda **_kwargs: _async_value(ctx),
    )
    monkeypatch.setattr(admin, "_assert_section_allowed", lambda *_args: None)

    async def mark(name: str, *_args: Any, **_kwargs: Any) -> None:
        calls.append(name)

    monkeypatch.setattr(admin, "_render_menu", lambda *a, **k: mark("menu"))
    monkeypatch.setattr(admin, "_navigate_back", lambda *a, **k: mark("back"))
    monkeypatch.setattr(admin.control, "_send_dashboard", lambda *a, **k: mark("leave"))
    monkeypatch.setattr(admin, "_render_today", lambda *a, full, **k: mark(f"today:{full}"))
    monkeypatch.setattr(admin, "_render_customer_list", lambda *a, today_only, **k: mark(f"customers:{today_only}"))
    monkeypatch.setattr(admin, "_render_customer_card", lambda *a, **k: mark("customer"))
    monkeypatch.setattr(admin, "_render_behavior", lambda *a, **k: mark("behavior"))
    monkeypatch.setattr(admin, "_render_messengers", lambda *a, **k: mark("messengers"))
    monkeypatch.setattr(admin, "_render_attention", lambda *a, **k: mark("attention"))
    monkeypatch.setattr(admin, "_render_marketing", lambda *a, **k: mark(str(a[3])))
    monkeypatch.setattr(admin, "_render_admin_report", lambda *a, **k: mark(str(a[3])))
    monkeypatch.setattr(admin, "_render_formats", lambda *a, **k: mark("formats"))
    monkeypatch.setattr(admin.control, "_send_capability_setup", lambda *a, **k: mark("formats-edit"))
    monkeypatch.setattr(admin, "_render_tariff", lambda *a, **k: mark("tariff"))
    monkeypatch.setattr(admin, "_begin_add_member", lambda *a, **k: mark("add-member"))
    monkeypatch.setattr(admin, "_select_add_member_role", lambda *a, **k: mark("add-role"))
    monkeypatch.setattr(admin, "_render_members", lambda *a, **k: mark("members"))
    monkeypatch.setattr(admin, "_render_member_card", lambda *a, **k: mark("member"))
    monkeypatch.setattr(admin, "_render_permissions", lambda *a, **k: mark("permissions"))
    monkeypatch.setattr(
        admin,
        "grant_business_member",
        lambda **_kwargs: SimpleNamespace(user_id=88),
    )
    monkeypatch.setattr(
        admin,
        "revoke_business_member",
        lambda **_kwargs: SimpleNamespace(user_id=88),
    )
    monkeypatch.setattr(admin, "_safe_edit", lambda *a, **k: mark("safe-edit"))

    actions = [
        ("menu", ()),
        ("back", ()),
        ("leave", ()),
        ("today", ()),
        ("today-full", ()),
        ("customers", ()),
        ("customer-list", ()),
        ("customer", ("customer-token",)),
        ("behavior", ()),
        ("messengers", ()),
        ("attention", ()),
        ("autopilot", ()),
        ("publications", ()),
        ("funnel", ()),
        ("money", ()),
        ("payments", ()),
        ("segments", ()),
        ("offers", ()),
        ("copy", ()),
        ("prices", ()),
        ("release", ()),
        ("invites", ()),
        ("funnel2", ()),
        ("retention", ()),
        ("recent", ()),
        ("system", ()),
        ("formats", ()),
        ("formats-edit", ()),
        ("tariff", ()),
        ("add-member", ()),
        ("add-role", ("support",)),
        ("members", ()),
        ("member", ("88",)),
        ("member-role", ("88", "support")),
        ("member-revoke", ("88",)),
        ("permissions", ()),
    ]
    state = fsm_context()
    for action, payload in actions:
        monkeypatch.setattr(
            admin,
            "_parse_callback",
            lambda _data, action=action, payload=payload: (
                BUSINESS_ID,
                action,
                payload,
            ),
        )
        await admin.admin_gate(
            telegram_callback(data=f"cpa:business-token:{action}"),
            state,
        )

    assert len(calls) >= len(actions)


@pytest.mark.asyncio
async def test_admin_gate_fails_closed_after_role_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alerts: list[tuple[str | None, bool]] = []

    async def answer_callback(
        _callback: CallbackQuery,
        text: str | None = None,
        *,
        show_alert: bool = False,
        **_kwargs: Any,
    ) -> None:
        alerts.append((text, show_alert))

    monkeypatch.setattr(CallbackQuery, "answer", answer_callback)
    monkeypatch.setattr(
        admin,
        "_load_admin_context",
        lambda **_kwargs: _async_value(admin_context(PlatformRole.SUPPORT)),
    )
    monkeypatch.setattr(
        admin,
        "_parse_callback",
        lambda _data: (BUSINESS_ID, "tariff", ()),
    )

    await admin.admin_gate(telegram_callback(), fsm_context())

    assert alerts[-1] == (
        "Доступ к этому разделу отозван или не назначен.",
        True,
    )


def test_dashboard_button_uses_same_panel_entry_as_admin_command() -> None:
    module = SimpleNamespace(
        _admin_dashboard_installed=False,
        _dashboard_keyboard=lambda *_args: InlineKeyboardMarkup(inline_keyboard=[]),
        _uuid_token=lambda _value: "business-token",
    )

    admin.install_admin_dashboard_button(module)

    markup = module._dashboard_keyboard(BUSINESS_ID, [])
    assert labels(markup) == ["🛠 Панель"]
    assert callbacks(markup) == ["cpa:business-token:menu"]
    assert module._admin_dashboard_installed is True


@pytest.mark.asyncio
async def test_messenger_owner_gets_secure_telegram_vk_max_setup_links(
    monkeypatch: pytest.MonkeyPatch,
    capture_edits: list[tuple[str, InlineKeyboardMarkup]],
) -> None:
    monkeypatch.setenv("CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED", "1")
    monkeypatch.setattr(
        admin,
        "get_business_capability_projection",
        lambda **_kwargs: capability_projection(
            telegram=admin.CapabilityAvailability.CONNECTABLE,
            vk=admin.CapabilityAvailability.CONNECTABLE,
            max_channel=admin.CapabilityAvailability.CONNECTABLE,
        ),
    )
    monkeypatch.setattr(
        admin,
        "issue_native_messenger_setup",
        lambda **_kwargs: SimpleNamespace(token="S" * 43),
    )
    monkeypatch.setattr(
        admin.settings,
        "MESSENGER_PUBLIC_BASE_URL",
        "https://client.example.test",
    )
    ctx = admin_context(PlatformRole.OWNER)
    state = fsm_context()
    callback = telegram_callback()

    await admin._render_messengers(callback, state, ctx)
    messenger_markup = capture_edits[-1][1]
    assert "✈️ Подключить Telegram" in labels(messenger_markup)
    assert "🔵 Подключить ВКонтакте" in labels(messenger_markup)
    assert "🟣 Подключить MAX" in labels(messenger_markup)

    await admin._render_messenger_connect(callback, state, ctx, "vk")
    text, markup = capture_edits[-1]
    assert "не отправляется сообщением в Telegram" in text
    url_buttons = [
        button
        for row in markup.inline_keyboard
        for button in row
        if button.url
    ]
    assert len(url_buttons) == 1
    assert url_buttons[0].url == (
        "https://client.example.test/clientplatform/connect/" + "S" * 43
    )

    await admin._render_messenger_connect(callback, state, ctx, "telegram")
    text, markup = capture_edits[-1]
    assert "Подключение Telegram" in text
    assert any(button.url for row in markup.inline_keyboard for button in row)


@pytest.mark.asyncio
async def test_messenger_setup_actions_are_hidden_when_omnichannel_ingress_is_off(
    monkeypatch: pytest.MonkeyPatch,
    capture_edits: list[tuple[str, InlineKeyboardMarkup]],
) -> None:
    monkeypatch.setenv("CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED", "0")
    monkeypatch.setattr(
        admin,
        "get_business_capability_projection",
        lambda **_kwargs: capability_projection(
            telegram=admin.CapabilityAvailability.UNAVAILABLE,
            vk=admin.CapabilityAvailability.UNAVAILABLE,
            max_channel=admin.CapabilityAvailability.UNAVAILABLE,
        ),
    )
    issued: list[object] = []
    monkeypatch.setattr(
        admin,
        "issue_native_messenger_setup",
        lambda **kwargs: issued.append(kwargs),
    )
    monkeypatch.setattr(
        admin.settings,
        "MESSENGER_PUBLIC_BASE_URL",
        "https://client.example.test",
    )
    ctx = admin_context(PlatformRole.OWNER)

    await admin._render_messengers(telegram_callback(), fsm_context(), ctx)
    visible = set(labels(capture_edits[-1][1]))
    assert "✈️ Подключить Telegram" not in visible
    assert "🔵 Подключить ВКонтакте" not in visible
    assert "🟣 Подключить MAX" not in visible

    await admin._render_messenger_connect(
        telegram_callback(),
        fsm_context(),
        ctx,
        "vk",
    )
    text, _markup = capture_edits[-1]
    assert "сейчас нельзя подключить" in text
    assert issued == []


@pytest.mark.asyncio
async def test_support_can_view_messengers_but_cannot_create_setup_link(
    monkeypatch: pytest.MonkeyPatch,
    capture_edits: list[tuple[str, InlineKeyboardMarkup]],
) -> None:
    monkeypatch.setattr(
        admin,
        "get_business_capability_projection",
        lambda **_kwargs: capability_projection(
            telegram=admin.CapabilityAvailability.CONNECTABLE,
            vk=admin.CapabilityAvailability.CONNECTABLE,
            max_channel=admin.CapabilityAvailability.CONNECTABLE,
        ),
    )
    ctx = admin_context(PlatformRole.SUPPORT)
    await admin._render_messengers(telegram_callback(), fsm_context(), ctx)
    visible = set(labels(capture_edits[-1][1]))
    assert "✈️ Подключить Telegram" not in visible
    assert "🔵 Подключить ВКонтакте" not in visible
    assert "🟣 Подключить MAX" not in visible
