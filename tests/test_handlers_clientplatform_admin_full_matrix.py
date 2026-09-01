from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from clientplatform.application.admin_ops import PublicationCalendarProjection, PublicationRecord
from handlers import clientplatform_admin as admin
from handlers import clientplatform_admin_extension as extension


OWNER_ACTIONS = [
    "today",
    "today-full",
    "customers",
    "customer-list",
    "behavior",
    "messengers",
    "attention",
    "autopilot",
    "publications",
    "funnel",
    "money",
    "payments",
    "segments",
    "offers",
    "copy",
    "prices",
    "release",
    "invites",
    "funnel2",
    "retention",
    "recent",
    "system",
    "tariff",
    "add-member",
    "members",
    "permissions",
]


@dataclass
class FakeState:
    data: dict[str, Any] = field(default_factory=dict)
    state: Any = None

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def set_state(self, value: Any) -> None:
        self.state = value

    async def clear(self) -> None:
        self.data.clear()
        self.state = None


def _ctx() -> Any:
    return SimpleNamespace(
        user_id=900001,
        business_id="00000000-0000-0000-0000-000000000001",
        business_name="Тестовый бизнес",
        business_token="business-token",
        role=admin.PlatformRole.OWNER,
        actor=SimpleNamespace(),
    )


def test_owner_menu_groups_all_26_sections_without_surface_sprawl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin.control, "_uuid_token", lambda _value: "business-token")
    markup = admin._menu_keyboard(_ctx())
    labels = [button.text for row in markup.inline_keyboard for button in row]
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]

    assert labels == [
        "👥 Клиенты и работа",
        "📣 Публикации и каналы",
        "📈 Продвижение и продажи",
        "👤 Сотрудники и тариф",
        "🛠 Технические проверки",
        "⬅️ Назад",
    ]
    group_actions = [str(value).split(":")[2] for value in callbacks[:-1]]
    assert group_actions == list(admin._ADMIN_MENU_GROUPS)
    reachable = {
        action
        for group_action in group_actions
        for _title, action in admin._admin_group_items(_ctx(), group_action)
    }
    assert reachable == set(OWNER_ACTIONS)
    assert len(markup.inline_keyboard) == 6
    assert str(markup.inline_keyboard[-1][0].callback_data).endswith(":leave")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", OWNER_ACTIONS)
async def test_every_top_level_section_back_returns_to_admin_menu(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    calls: list[str] = []

    async def render_menu(*_args: Any, **_kwargs: Any) -> None:
        calls.append("menu")

    monkeypatch.setattr(admin, "_render_menu", render_menu)
    state = FakeState(
        {
            "cp_admin_history": ["menu"],
            "cp_admin_section": action,
        }
    )
    await admin._navigate_back(
        SimpleNamespace(),
        state,  # type: ignore[arg-type]
        _ctx(),
    )

    assert calls == ["menu"]
    assert state.data["cp_admin_history"] == []
    assert state.data["cp_admin_section"] == "menu"


@pytest.mark.asyncio
async def test_section_back_returns_to_its_admin_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bool]] = []

    async def render_group(
        _callback: Any,
        _state: Any,
        _ctx: Any,
        group_action: str,
        *,
        push: bool = True,
    ) -> None:
        calls.append((group_action, push))

    monkeypatch.setattr(admin, "_render_admin_group", render_group)
    state = FakeState(
        {
            "cp_admin_history": ["menu", "menu-work"],
            "cp_admin_section": "customer-list",
        }
    )
    await admin._navigate_back(
        SimpleNamespace(),
        state,  # type: ignore[arg-type]
        _ctx(),
    )

    assert calls == [("menu-work", False)]
    assert state.data["cp_admin_history"] == ["menu"]
    assert state.data["cp_admin_section"] == "menu-work"


@pytest.mark.asyncio
async def test_real_admin_group_renders_message_and_pushes_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin.control, "_uuid_token", lambda _value: "business-token")
    answers: list[tuple[str, Any]] = []

    class MessageTarget:
        async def answer(self, text: str, *, reply_markup: Any) -> None:
            answers.append((text, reply_markup))

    state = FakeState({"cp_admin_section": "menu", "cp_admin_history": []})
    await admin._render_admin_group(
        MessageTarget(),  # type: ignore[arg-type]
        state,  # type: ignore[arg-type]
        _ctx(),
        "menu-work",
    )

    text, markup = answers[-1]
    assert text.startswith("👥 Клиенты и работа\n\nЕсли Вам нужно:\n")
    assert "«📊 Что сегодня происходит»" in text
    assert "«👥 Открыть клиентов»" in text
    assert [button.text for row in markup.inline_keyboard for button in row] == [
        "📊 Что сегодня происходит",
        "📈 Подробная сводка",
        "👥 Клиенты за сегодня",
        "👥 Открыть клиентов",
        "⚠️ Что требует внимания",
        "🧠 Кто проходит материалы",
        "⬅️ Назад",
    ]
    assert state.data["cp_admin_section"] == "menu-work"
    assert state.data["cp_admin_history"] == ["menu"]


@pytest.mark.asyncio
async def test_real_admin_group_callback_path_does_not_repush_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CallbackTarget:
        pass

    edits: list[tuple[str, Any]] = []

    async def safe_edit(_target: Any, text: str, markup: Any) -> None:
        edits.append((text, markup))

    monkeypatch.setattr(admin, "CallbackQuery", CallbackTarget)
    monkeypatch.setattr(admin, "_safe_edit", safe_edit)
    monkeypatch.setattr(admin.control, "_uuid_token", lambda _value: "business-token")
    state = FakeState(
        {"cp_admin_section": "menu-work", "cp_admin_history": ["menu"]}
    )
    await admin._render_admin_group(
        CallbackTarget(),  # type: ignore[arg-type]
        state,  # type: ignore[arg-type]
        _ctx(),
        "menu-content",
        push=False,
    )

    assert edits[-1][0].startswith("📣 Публикации и каналы")
    assert state.data == {
        "cp_admin_section": "menu-work",
        "cp_admin_history": ["menu"],
    }


@pytest.mark.asyncio
async def test_admin_group_rejects_role_without_any_visible_action() -> None:
    ctx = _ctx()
    ctx.role = admin.PlatformRole.CONTENT_MANAGER
    with pytest.raises(admin.TenantPermissionDenied):
        await admin._render_admin_group(
            SimpleNamespace(answer=None),  # type: ignore[arg-type]
            FakeState(),  # type: ignore[arg-type]
            ctx,
            "menu-team",
        )


@pytest.mark.asyncio
async def test_render_menu_callback_path_without_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CallbackTarget:
        pass

    edits: list[str] = []

    async def safe_edit(_target: Any, text: str, _markup: Any) -> None:
        edits.append(text)

    monkeypatch.setattr(admin, "CallbackQuery", CallbackTarget)
    monkeypatch.setattr(admin, "_safe_edit", safe_edit)
    monkeypatch.setattr(admin.control, "_uuid_token", lambda _value: "business-token")
    state = FakeState({"cp_admin_section": "menu-content", "cp_admin_history": ["menu"]})
    await admin._render_menu(
        CallbackTarget(),  # type: ignore[arg-type]
        state,  # type: ignore[arg-type]
        _ctx(),
        reset=False,
    )

    assert edits[-1].startswith("⚙️ Управление бизнесом")
    assert state.data["cp_admin_section"] == "menu-content"
    assert state.data["cp_admin_history"] == ["menu"]


@pytest.fixture
def render_contract(monkeypatch: pytest.MonkeyPatch):
    rendered: list[tuple[str, Any]] = []

    async def safe_edit(
        _callback: Any,
        text: str,
        markup: Any,
    ) -> None:
        rendered.append((text, markup))

    async def base_snapshot(_ctx: Any):
        profile = SimpleNamespace(
            activity_description="Помогаем клиентам",
            timezone="Europe/Moscow",
            status=SimpleNamespace(value="ready"),
        )
        summary = SimpleNamespace(
            customers=0,
            programs=0,
            dispatch_pending=0,
            dispatch_sent=0,
            dispatch_attention=0,
        )
        return profile, summary, [], [], [], [], []

    insights = SimpleNamespace(
        active_customers=0,
        active_offerings=0,
        active_invites=0,
        claimed_invites=0,
        enrollments=0,
        completed_enrollments=0,
        publication_drafts=0,
        publications_published=0,
        paid_payments=0,
        paid_amount_minor=0,
        payment_currency="RUB",
        priced_offerings=0,
        active_staff=1,
    )
    interaction = SimpleNamespace(
        count=0,
        successes=0,
        failures=0,
        p50_ms=0,
        p95_ms=0,
        max_ms=0,
        ack_p95_ms=0,
        lock_p95_ms=0,
        telegram_p95_ms=0,
    )
    route = SimpleNamespace(
        ui_mode="direct",
        polling_mode="direct",
        ui_route="149.154.167.220",
        polling_route="149.154.167.220",
        route_pool_size=1,
        egress_redundant=False,
        polling_ready=True,
        polling_in_flight=True,
        ui_failures=0,
        polling_failures=0,
    )
    subscription = SimpleNamespace(
        plan_key="base",
        status="active",
        included_staff=5,
        included_customers=500,
        started_at="2026-08-03T00:00:00+00:00",
        renews_at=None,
    )

    monkeypatch.setattr(admin, "_safe_edit", safe_edit)
    monkeypatch.setattr(admin, "_base_snapshot", base_snapshot)
    monkeypatch.setattr(extension, "_all_offerings", lambda *_args: _empty_async())
    monkeypatch.setattr(extension, "telegram_egress_snapshot", lambda: route)
    monkeypatch.setattr(extension.admin_ops, "business_admin_insights", lambda **_kwargs: insights)
    monkeypatch.setattr(extension.admin_ops, "list_payments", lambda **_kwargs: [])
    monkeypatch.setattr(
        extension.admin_ops,
        "payment_summary",
        lambda **_kwargs: extension.admin_ops.PaymentSummary(
            paid_payments=0, paid_customers=0, by_currency=()
        ),
    )
    monkeypatch.setattr(
        extension.admin_ops,
        "get_publication_calendar_projection",
        lambda **_kwargs: PublicationCalendarProjection(
            entries=(), actionable_drafts=(), draft_count=0, scheduled_count=0,
            published_count=0, failed_count=0, cancelled_count=0,
        ),
    )
    monkeypatch.setattr(extension.admin_ops, "list_offering_prices", lambda **_kwargs: [])
    monkeypatch.setattr(extension.admin_ops, "interaction_snapshot", lambda **_kwargs: interaction)
    monkeypatch.setattr(extension.admin_ops, "get_autopilot_enabled", lambda **_kwargs: False)
    monkeypatch.setattr(extension.admin_ops, "get_current_automation_action_approvals", lambda **_kwargs: ())
    monkeypatch.setattr(extension.admin_ops, "get_admin_setting", lambda **_kwargs: "false")
    monkeypatch.setattr(extension.admin_ops, "recent_audit_events", lambda **_kwargs: [])
    monkeypatch.setattr(extension.admin_ops, "refresh_interaction_alerts", lambda **_kwargs: [])
    monkeypatch.setattr(extension.admin_ops, "list_open_alerts", lambda **_kwargs: [])
    monkeypatch.setattr(extension.admin_ops, "get_subscription_state", lambda **_kwargs: subscription)
    monkeypatch.setattr(admin, "business_delivery_summary", lambda **_kwargs: SimpleNamespace(dispatch_attention=0, dispatch_pending=0))
    return rendered


async def _empty_async() -> list[Any]:
    return []


@pytest.mark.asyncio
async def test_autopilot_screen_is_read_only_for_non_owner_role(
    render_contract: list[tuple[str, Any]],
) -> None:
    ctx = _ctx()
    ctx.role = admin.PlatformRole.ADMINISTRATOR
    state = FakeState({"cp_admin_section": "menu", "cp_admin_history": []})

    await extension._enhanced_marketing(
        SimpleNamespace(),  # type: ignore[arg-type]
        state,  # type: ignore[arg-type]
        ctx,
        "autopilot",
    )

    text, markup = render_contract[-1]
    assert "Изменить режим может только владелец бизнеса." in text
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "▶️ Включить" not in labels
    assert "⏸ Выключить" not in labels



@pytest.mark.asyncio
async def test_autopilot_screen_renders_owner_action_approval_controls(
    render_contract: list[tuple[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000201",
        status=SimpleNamespace(value="pending"),
        requested_at="2026-08-29T15:05:00+00:00",
    )
    approved = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000202",
        status=SimpleNamespace(value="approved"),
        requested_at="2026-08-29T15:00:00+00:00",
    )
    monkeypatch.setattr(
        extension.admin_ops,
        "get_current_automation_action_approvals",
        lambda **_kwargs: (approved, pending),
    )
    monkeypatch.setattr(
        extension.admin_ops,
        "format_automation_action_approval",
        lambda item, **_kwargs: (
            "Нужно Ваше подтверждение: Отправить follow-up"
            if item.status.value == "pending"
            else "Разрешено владельцем · ещё не исполняется автоматически"
        ),
    )
    state = FakeState({"cp_admin_section": "menu", "cp_admin_history": []})
    await extension._enhanced_marketing(
        SimpleNamespace(),  # type: ignore[arg-type]
        state,  # type: ignore[arg-type]
        _ctx(),
        "autopilot",
    )

    text, markup = render_contract[-1]
    assert "Решения, которые ждут владельца или могут быть отозваны" in text
    assert "Нужно Ваше подтверждение" in text
    assert "Разрешено владельцем" in text
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "✅ Разрешить действие #1" in labels
    assert "⛔ Отклонить действие #1" in labels
    assert "↩️ Отозвать разрешение #2" in labels
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]
    assert any(":aa:" in value for value in callbacks)
    assert any(":ar:" in value for value in callbacks)
    assert any(":av:" in value for value in callbacks)
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "title"),
    [
        ("autopilot", "🤖 Автоматизация"),
        ("publications", "📣 Публикации"),
        ("funnel", "📚 Прохождение программ"),
        ("money", "💰 Выручка и платящие клиенты"),
        ("payments", "💰 Оплаты"),
        ("segments", "👥 Группы клиентов"),
        ("offers", "🧪 Услуги и предложения"),
        ("copy", "✍️ Подготовить текст"),
        ("prices", "💵 Цены"),
    ],
)
async def test_all_growth_sections_render_real_screen_and_back(
    render_contract: list[tuple[str, Any]],
    action: str,
    title: str,
) -> None:
    state = FakeState({"cp_admin_section": "menu", "cp_admin_history": []})
    await extension._enhanced_marketing(
        SimpleNamespace(),  # type: ignore[arg-type]
        state,  # type: ignore[arg-type]
        _ctx(),
        action,
    )
    text, markup = render_contract[-1]
    assert text.startswith(title)
    assert "ещё не подключ" not in text.casefold()
    assert markup.inline_keyboard[-1][0].text == "⬅️ Назад"
    assert state.data["cp_admin_section"] == action


@pytest.mark.asyncio
async def test_publications_use_shared_calendar_projection(
    render_contract: list[tuple[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = PublicationRecord(
        id="00000000-0000-0000-0000-000000000099",
        business_id="00000000-0000-0000-0000-000000000001",
        channel="max",
        title="Вечерняя публикация",
        body="Текст",
        status="scheduled",
        created_at="2026-08-27T08:00:00+00:00",
        updated_at="2026-08-27T08:00:00+00:00",
        scheduled_at="2026-08-28T18:00:00+00:00",
        published_at=None,
        failed_at=None,
        failure_reason=None,
    )
    draft = PublicationRecord(
        id="00000000-0000-0000-0000-000000000100",
        business_id=publication.business_id,
        channel="telegram",
        title="Готовый черновик",
        body="Текст",
        status="draft",
        created_at="2026-08-27T09:00:00+00:00",
        updated_at="2026-08-27T09:00:00+00:00",
        scheduled_at=None,
        published_at=None,
        failed_at=None,
        failure_reason=None,
    )
    monkeypatch.setattr(
        extension.admin_ops,
        "get_publication_calendar_projection",
        lambda **_kwargs: PublicationCalendarProjection(
            entries=(publication,),
            actionable_drafts=(draft,),
            draft_count=1,
            scheduled_count=21,
            published_count=4,
            failed_count=2,
            cancelled_count=0,
        ),
    )
    state = FakeState({"cp_admin_section": "menu", "cp_admin_history": []})
    await extension._enhanced_marketing(
        SimpleNamespace(),  # type: ignore[arg-type]
        state,  # type: ignore[arg-type]
        _ctx(),
        "publications",
    )
    text, markup = render_contract[-1]
    assert "Запланировано: 21" in text
    assert "Черновики: 1" in text
    assert "28.08.2026 21:00 · MAX · Запланировано · Вечерняя публикация" in text
    assert "ещё не подключ" not in text.casefold()
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert any("🗓 Запланировать · Готовый черновик" in label for label in labels)
    assert any("✅ Отметить опубликованной · Готовый черновик" in label for label in labels)
    assert any("🕒 Перенести · Вечерняя публикаци" in label for label in labels)
    assert any("⛔ Отменить · Вечерняя публикаци" in label for label in labels)
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]
    assert any(":ps:" in value for value in callbacks)
    assert any(":pc:" in value for value in callbacks)
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)
    assert markup.inline_keyboard[-1][0].text == "⬅️ Назад"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "title"),
    [
        ("release", "✅ Проверить готовность"),
        ("invites", "🎁 Приглашения и рекомендации"),
        ("funnel2", "🧭 Путь клиента"),
        ("retention", "♻️ Кого стоит вернуть"),
        ("recent", "🧾 История изменений"),
        ("system", "🛠 Проверка системы"),
    ],
)
async def test_all_admin_reports_render_real_screen_and_back(
    render_contract: list[tuple[str, Any]],
    action: str,
    title: str,
) -> None:
    state = FakeState({"cp_admin_section": "menu", "cp_admin_history": []})
    await extension._enhanced_admin_report(
        SimpleNamespace(),  # type: ignore[arg-type]
        state,  # type: ignore[arg-type]
        _ctx(),
        action,
    )
    text, markup = render_contract[-1]
    assert text.startswith(title)
    assert markup.inline_keyboard[-1][0].text == "⬅️ Назад"
    assert state.data["cp_admin_section"] == action


@pytest.mark.asyncio
async def test_attention_and_tariff_render_real_state(
    render_contract: list[tuple[str, Any]],
) -> None:
    state = FakeState({"cp_admin_section": "menu", "cp_admin_history": []})
    await extension._enhanced_attention(
        SimpleNamespace(),  # type: ignore[arg-type]
        state,  # type: ignore[arg-type]
        _ctx(),
    )
    assert render_contract[-1][0].startswith("⚠️ Требуют внимания")
    assert render_contract[-1][1].inline_keyboard[-1][0].text == "⬅️ Назад"

    await extension._enhanced_tariff(
        SimpleNamespace(),  # type: ignore[arg-type]
        state,  # type: ignore[arg-type]
        _ctx(),
    )
    assert render_contract[-1][0].startswith("💳 Тариф ClientPlatform")
    assert "пока не активирован" not in render_contract[-1][0].casefold()
    assert render_contract[-1][1].inline_keyboard[-1][0].text == "⬅️ Назад"


def test_all_operation_subflows_have_explicit_back_buttons() -> None:
    ctx = _ctx()
    for return_action in (
        "return-publications",
        "return-payments",
        "return-prices",
    ):
        markup = extension._flow_keyboard(
            admin,
            ctx,
            return_action=return_action,
        )
        button = markup.inline_keyboard[-1][0]
        assert button.text == "⬅️ Назад"
        assert button.callback_data == f"cpao:business-token:{return_action}"
