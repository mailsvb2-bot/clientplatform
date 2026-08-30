from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clientplatform.domain.automation_policy import AutomationApprovalConflict
from clientplatform.domain.tenancy import (
    PlatformRole,
    TenantPermissionDenied,
    TenancyError,
)
from handlers import clientplatform_admin_extension as extension


class FakeState:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = dict(data or {})
        self.current_state: Any = None
        self.clear_count = 0

    async def clear(self) -> None:
        self.data.clear()
        self.current_state = None
        self.clear_count += 1

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    async def set_state(self, value: Any) -> None:
        self.current_state = value

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=701)
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(
        self,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> None:
        self.answers.append((text, show_alert))


class FakeMessage:
    def __init__(self, text: str | None) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=701)
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs: Any) -> None:
        self.answers.append(text)


class FakeControl:
    @staticmethod
    def _token_uuid(value: str) -> str:
        if value == "bad":
            raise ValueError("bad token")
        return f"uuid:{value}"

    @staticmethod
    def _uuid_token(value: object) -> str:
        return f"token:{value}"

    @staticmethod
    def _user_id(message: FakeMessage) -> int:
        return int(message.from_user.id)


class FakeAdmin:
    control = FakeControl()
    _ADMIN_ROLES = {"owner", "admin"}
    _AUTOMATION_ROLES = {"owner", "administrator", "manager", "marketer"}

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.edits: list[tuple[str, Any]] = []
        self.panels: list[tuple[int, str]] = []

    async def _load_admin_context(self, **_kwargs: Any) -> Any:
        return self.ctx

    async def _safe_edit(
        self,
        _callback: Any,
        text: str,
        reply_markup: Any,
    ) -> None:
        self.edits.append((text, reply_markup))

    async def send_admin_panel(
        self,
        _message: Any,
        *,
        user_id: int,
        business_id: str,
    ) -> None:
        self.panels.append((user_id, business_id))

    @staticmethod
    def _keyboard(rows: Any) -> Any:
        return rows

    @staticmethod
    def _back_keyboard(_ctx: Any, *extra: Any) -> Any:
        return list(extra)

    @staticmethod
    def _callback(_ctx: Any, action: str) -> str:
        return f"cpa:business:{action}"


@pytest.fixture
def ctx() -> Any:
    return SimpleNamespace(
        actor=SimpleNamespace(
            business_id="business-id",
            role=PlatformRole.OWNER,
        ),
        business_id="business-id",
        business_token="business",
        business_name="Business",
        role="owner",
        user_id=701,
    )


@pytest.fixture
def fake_admin(monkeypatch: pytest.MonkeyPatch, ctx: Any) -> FakeAdmin:
    admin = FakeAdmin(ctx)
    monkeypatch.setattr(extension.importlib, "import_module", lambda *_args: admin)
    monkeypatch.setattr(
        extension,
        "telegram_egress_snapshot",
        lambda: SimpleNamespace(egress_redundant=True),
    )
    return admin


def _callback(action: str, *payload: str) -> FakeCallback:
    tail = ":".join(payload)
    data = f"cpao:business:{action}"
    if tail:
        data += f":{tail}"
    return FakeCallback(data)


def test_amount_and_display_helpers_cover_validation_and_formatting(ctx: Any) -> None:
    assert extension._parse_amount("3500 RUB консультация") == (
        350000,
        "RUB",
        "консультация",
    )
    assert extension._parse_amount("12,345 usd") == (1235, "USD", "")
    assert extension._parse_amount("99 комментарий") == (
        9900,
        "RUB",
        "комментарий",
    )
    assert extension._money(123456, "RUB") == "1 234,56 RUB"
    assert extension._parse_amount("3500 JPY") == (3500, "JPY", "")
    assert extension._parse_amount("3500 KWD") == (3_500_000, "KWD", "")
    assert extension._money(3500, "JPY") == "3 500 JPY"
    assert extension._money(3_500_000, "KWD") == "3 500,000 KWD"
    assert extension._percent(1, 4) == "25%"
    assert extension._percent(1, 0) == "0%"
    assert extension._status_icon(True) == "✅"
    assert extension._status_icon(False) == "⚠️"
    assert extension._payment_totals_text(
        extension.admin_ops.PaymentSummary(
            paid_payments=0,
            paid_customers=0,
            by_currency=(),
        )
    ) == "0,00 RUB"
    max_callback = extension._ops_callback(
        SimpleNamespace(business_token="a" * 22),
        "pay-refund-ok",
        "b" * 22,
    )
    assert len(max_callback.encode("utf-8")) == 64
    schedule_version = extension.admin_ops.encode_publication_schedule_version(
        "2026-08-29T09:00:00+00:00"
    )
    cancel_callback = extension._ops_callback(
        SimpleNamespace(business_token="a" * 22),
        "publication-cancel",
        "b" * 22,
        schedule_version,
    )
    assert len(cancel_callback.encode("utf-8")) <= 64
    assert ":pc:" in cancel_callback
    cancel_ok_callback = extension._ops_callback(
        SimpleNamespace(business_token="a" * 22),
        "publication-cancel-ok",
        "b" * 22,
        schedule_version,
    )
    assert len(cancel_ok_callback.encode("utf-8")) <= 64
    assert ":pcx:" in cancel_ok_callback
    publish_callback = extension._ops_callback(
        SimpleNamespace(business_token="a" * 22),
        "publication-publish",
        "b" * 22,
    )
    assert len(publish_callback.encode("utf-8")) <= 64
    assert ":pp:" in publish_callback

    summary = extension.admin_ops.PaymentSummary(
        paid_payments=3,
        paid_customers=2,
        by_currency=(
            extension.admin_ops.PaymentCurrencySummary(
                currency="RUB", amount_minor=30000, paid_payments=2
            ),
            extension.admin_ops.PaymentCurrencySummary(
                currency="USD", amount_minor=500, paid_payments=1
            ),
        ),
    )
    assert extension._payment_totals_text(summary) == "300,00 RUB · 5,00 USD"
    assert extension._payment_average_text(summary) == "150,00 RUB · 5,00 USD"

    for value, message in [
        ("", "Укажите сумму"),
        ("abc", "Сумма должна быть числом"),
        ("0", "Сумма должна быть больше нуля"),
        ("-1", "Сумма должна быть больше нуля"),
        ("NaN", "Сумма должна быть больше нуля"),
    ]:
        with pytest.raises(ValueError, match=message):
            extension._parse_amount(value)

    assert extension._ops_callback(ctx, "run", "x") == "cpao:business:run:x"
    with pytest.raises(ValueError, match="exceeds Telegram limit"):
        extension._ops_callback(ctx, "x" * 80)


@pytest.mark.asyncio
async def test_admin_ops_gate_rejects_malformed_and_unknown(
    fake_admin: FakeAdmin,
) -> None:
    malformed = FakeCallback("cpao:only")
    await extension.admin_ops_gate(malformed, FakeState())
    assert malformed.answers == [("Кнопка устарела", True)]

    unknown = _callback("unknown")
    await extension.admin_ops_gate(unknown, FakeState())
    assert unknown.answers == [("Действие больше недоступно", True)]


@pytest.mark.asyncio
async def test_admin_ops_gate_autopilot_and_alert_routes(
    monkeypatch: pytest.MonkeyPatch,
    fake_admin: FakeAdmin,
    ctx: Any,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        extension.admin_ops,
        "toggle_autopilot",
        lambda **_kwargs: calls.append("toggle"),
    )
    monkeypatch.setattr(
        extension.admin_ops,
        "refresh_interaction_alerts",
        lambda **_kwargs: calls.append("refresh") or [],
    )

    async def marketing(_callback: Any, _state: Any, _ctx: Any, action: str) -> None:
        calls.append(f"marketing:{action}")

    async def report(_callback: Any, _state: Any, _ctx: Any, action: str) -> None:
        calls.append(f"report:{action}")

    async def attention(_callback: Any, _state: Any, _ctx: Any) -> None:
        calls.append("attention")

    monkeypatch.setattr(extension, "_enhanced_marketing", marketing)
    monkeypatch.setattr(extension, "_enhanced_admin_report", report)
    monkeypatch.setattr(extension, "_enhanced_attention", attention)

    await extension.admin_ops_gate(_callback("autopilot-toggle"), FakeState())
    assert calls[:3] == ["toggle", "refresh", "marketing:autopilot"]

    calls.clear()
    ctx.role = "content_manager"
    with pytest.raises(TenantPermissionDenied, match="automation controls"):
        await extension.admin_ops_gate(_callback("autopilot-toggle"), FakeState())
    assert calls == []

    ctx.role = PlatformRole.ADMINISTRATOR
    non_owner = _callback("autopilot-toggle")
    await extension.admin_ops_gate(non_owner, FakeState())
    assert non_owner.answers == [
        ("Изменить effective AutomationPolicy может только владелец бизнеса.", True)
    ]
    assert calls == []

    ctx.role = "owner"
    await extension.admin_ops_gate(_callback("alerts-refresh"), FakeState())
    assert calls == ["refresh", "report:system"]

    calls.clear()
    ctx.role = "operator"
    await extension.admin_ops_gate(_callback("alerts-refresh"), FakeState())
    assert calls == ["refresh", "attention"]


@pytest.mark.asyncio
async def test_admin_ops_gate_automation_action_decisions(
    monkeypatch: pytest.MonkeyPatch,
    fake_admin: FakeAdmin,
    ctx: Any,
) -> None:
    calls: list[str] = []

    async def marketing(_callback: Any, _state: Any, _ctx: Any, action: str) -> None:
        calls.append(f"marketing:{action}")

    monkeypatch.setattr(extension, "_enhanced_marketing", marketing)
    monkeypatch.setattr(
        extension.admin_ops,
        "approve_pending_automation_action",
        lambda **kwargs: calls.append(f"approve:{kwargs['approval_id']}"),
    )
    monkeypatch.setattr(
        extension.admin_ops,
        "reject_pending_automation_action",
        lambda **kwargs: calls.append(f"reject:{kwargs['approval_id']}"),
    )
    monkeypatch.setattr(
        extension.admin_ops,
        "revoke_approved_automation_action",
        lambda **kwargs: calls.append(f"revoke:{kwargs['approval_id']}"),
    )

    for token, prefix, message in (
        ("aa", "approve", "Действие разрешено"),
        ("ar", "reject", "Действие отклонено"),
        ("av", "revoke", "Разрешение отозвано"),
    ):
        calls.clear()
        callback = _callback(token, "approval")
        await extension.admin_ops_gate(callback, FakeState())
        assert calls == [f"{prefix}:uuid:approval", "marketing:autopilot"]
        assert callback.answers and callback.answers[0][0].startswith(message)

    calls.clear()
    ctx.role = PlatformRole.ADMINISTRATOR
    non_owner = _callback("aa", "approval")
    await extension.admin_ops_gate(non_owner, FakeState())
    assert non_owner.answers == [
        ("Решение по автоматическому действию может принять только владелец бизнеса.", True)
    ]
    assert calls == []

    ctx.role = PlatformRole.OWNER
    malformed = _callback("aa")
    await extension.admin_ops_gate(malformed, FakeState())
    assert malformed.answers == [("Кнопка устарела", True)]

    def stale(**_kwargs: Any) -> None:
        raise AutomationApprovalConflict("automation_action_policy_changed")

    monkeypatch.setattr(extension.admin_ops, "approve_pending_automation_action", stale)
    calls.clear()
    stale_callback = _callback("aa", "approval")
    await extension.admin_ops_gate(stale_callback, FakeState())
    assert stale_callback.answers == [
        ("Это решение уже изменилось или устарело. Экран обновлён без выполнения действия.", True)
    ]
    assert calls == ["marketing:autopilot"]


@pytest.mark.asyncio
async def test_admin_ops_gate_publication_flow(
    monkeypatch: pytest.MonkeyPatch,
    fake_admin: FakeAdmin,
) -> None:
    state = FakeState({"old": "value"})
    await extension.admin_ops_gate(_callback("publication-new"), state)
    assert state.clear_count == 1
    assert state.data["cpao_business_id"] == "business-id"
    assert "Выберите канал" in fake_admin.edits[-1][0]

    state = FakeState()
    await extension.admin_ops_gate(
        _callback("publication-channel", "telegram"),
        state,
    )
    assert state.current_state == extension.ClientPlatformAdminOpsState.publication_title
    assert state.data["cpao_publication_channel"] == "telegram"

    with pytest.raises(ValueError, match="unsupported publication channel"):
        await extension.admin_ops_gate(
            _callback("publication-channel", "email"),
            FakeState(),
        )

    published: list[str] = []
    monkeypatch.setattr(
        extension.admin_ops,
        "publish_publication",
        lambda *, publication_id, **_kwargs: published.append(publication_id),
    )

    async def marketing(_callback: Any, _state: Any, _ctx: Any, action: str) -> None:
        published.append(action)

    monkeypatch.setattr(extension, "_enhanced_marketing", marketing)
    await extension.admin_ops_gate(
        _callback("publication-publish", "publication"),
        FakeState(),
    )
    assert published == ["uuid:publication", "publications"]


@pytest.mark.asyncio
async def test_admin_ops_gate_publication_schedule_and_cancel_controls(
    monkeypatch: pytest.MonkeyPatch,
    fake_admin: FakeAdmin,
) -> None:
    monkeypatch.setattr(
        extension,
        "get_business_profile",
        lambda **_kwargs: SimpleNamespace(timezone="Europe/Moscow"),
    )
    state = FakeState({"dirty": True})
    await extension.admin_ops_gate(
        _callback("publication-schedule", "publication"),
        state,
    )
    assert state.current_state == extension.ClientPlatformAdminOpsState.publication_schedule_time
    assert state.data["cpao_business_id"] == "business-id"
    assert state.data["cpao_publication_id"] == "uuid:publication"
    assert "Europe/Moscow" in fake_admin.edits[-1][0]
    assert "28.08.2026 19:30" in fake_admin.edits[-1][0]

    schedule_version = extension.admin_ops.encode_publication_schedule_version(
        "2026-08-29T09:00:00+00:00"
    )
    await extension.admin_ops_gate(
        _callback("publication-cancel", "publication", schedule_version),
        FakeState(),
    )
    assert "Подтвердить отмену" in str(fake_admin.edits[-1][1])
    assert schedule_version in str(fake_admin.edits[-1][1])

    calls: list[tuple[str, str | None] | str] = []
    monkeypatch.setattr(
        extension.admin_ops,
        "cancel_publication_schedule",
        lambda *, publication_id, expected_scheduled_at, **_kwargs: calls.append(
            (publication_id, expected_scheduled_at)
        ),
    )

    async def marketing(_callback: Any, _state: Any, _ctx: Any, action: str) -> None:
        calls.append(action)

    monkeypatch.setattr(extension, "_enhanced_marketing", marketing)
    await extension.admin_ops_gate(
        _callback("publication-cancel-ok", "publication", schedule_version),
        FakeState(),
    )
    assert calls == [
        ("uuid:publication", "2026-08-29T09:00:00+00:00"),
        "publications",
    ]

    stale_callback = _callback(
        "publication-cancel-ok",
        "publication",
        extension.admin_ops.encode_publication_schedule_version(
            "2026-08-30T09:00:00+00:00"
        ),
    )

    def stale_cancel(**_kwargs: Any) -> None:
        raise ValueError("publication schedule changed; refresh and retry")

    monkeypatch.setattr(
        extension.admin_ops,
        "cancel_publication_schedule",
        stale_cancel,
    )
    await extension.admin_ops_gate(stale_callback, FakeState())
    assert stale_callback.answers == [
        (
            "Расписание уже изменилось. Обновите публикации и повторите действие.",
            True,
        )
    ]

    for action in (
        "publication-schedule",
        "publication-cancel",
        "publication-cancel-ok",
    ):
        malformed = _callback(action)
        await extension.admin_ops_gate(malformed, FakeState())
        assert malformed.answers == [("Кнопка устарела", True)]


@pytest.mark.asyncio
async def test_publication_mutation_callbacks_fail_closed_for_read_only_role(
    fake_admin: FakeAdmin,
    ctx: Any,
) -> None:
    ctx.actor.role = PlatformRole.ANALYST
    callback = _callback("publication-schedule", "publication")
    await extension.admin_ops_gate(callback, FakeState())
    assert callback.answers == [
        ("Для вашей роли публикации доступны только для просмотра.", True)
    ]
    assert fake_admin.edits == []


@pytest.mark.asyncio
async def test_admin_ops_gate_payment_and_price_flows(
    monkeypatch: pytest.MonkeyPatch,
    fake_admin: FakeAdmin,
) -> None:
    customer = SimpleNamespace(id="customer-id", display_name="Иван")
    monkeypatch.setattr(extension, "list_customers", lambda **_kwargs: [customer])
    monkeypatch.setattr(
        extension.admin_ops,
        "list_offering_prices",
        lambda **_kwargs: [],
    )

    state = FakeState()
    await extension.admin_ops_gate(_callback("payment-new"), state)
    assert "Выберите клиента" in fake_admin.edits[-1][0]
    assert state.data["cpao_business_id"] == "business-id"

    state = FakeState()
    await extension.admin_ops_gate(
        _callback("payment-customer", "none"),
        state,
    )
    assert state.data["cpao_payment_customer_id"] is None
    assert state.current_state == extension.ClientPlatformAdminOpsState.payment_value

    state = FakeState()
    await extension.admin_ops_gate(
        _callback("payment-customer", "customer"),
        state,
    )
    assert state.data["cpao_payment_customer_id"] == "uuid:customer"

    state = FakeState()
    await extension.admin_ops_gate(_callback("price-set", "offering"), state)
    assert state.data["cpao_offering_id"] == "uuid:offering"
    assert state.current_state == extension.ClientPlatformAdminOpsState.price_value


@pytest.mark.asyncio
async def test_payment_flow_uses_active_offering_price_when_available(
    monkeypatch: pytest.MonkeyPatch,
    fake_admin: FakeAdmin,
) -> None:
    price = SimpleNamespace(
        offering_id="offering-id",
        offering_title="Консультация",
        amount_minor=50_000,
        currency="RUB",
        status="active",
    )
    monkeypatch.setattr(
        extension.admin_ops,
        "list_offering_prices",
        lambda **_kwargs: [price],
    )
    state = FakeState()

    await extension.admin_ops_gate(
        _callback("pay-customer", "customer"),
        state,
    )
    assert "Выберите предложение" in fake_admin.edits[-1][0]
    assert state.data["cpao_payment_customer_id"] == "uuid:customer"

    await extension.admin_ops_gate(
        _callback("pay-offer", "offering"),
        state,
    )
    assert state.current_state == extension.ClientPlatformAdminOpsState.payment_value
    assert state.data["cpao_payment_offering_id"] == "uuid:offering"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "section"),
    [
        ("return-publications", "publications"),
        ("return-payments", "payments"),
        ("return-prices", "prices"),
    ],
)
async def test_admin_ops_gate_return_routes(
    action: str,
    section: str,
    monkeypatch: pytest.MonkeyPatch,
    fake_admin: FakeAdmin,
) -> None:
    rendered: list[str] = []

    async def marketing(_callback: Any, _state: Any, _ctx: Any, value: str) -> None:
        rendered.append(value)

    monkeypatch.setattr(extension, "_enhanced_marketing", marketing)
    state = FakeState({"dirty": True})
    await extension.admin_ops_gate(_callback(action), state)
    assert state.clear_count == 1
    assert rendered == [section]


@pytest.mark.asyncio
async def test_publication_input_handlers_cover_invalid_and_success(
    monkeypatch: pytest.MonkeyPatch,
    fake_admin: FakeAdmin,
    ctx: Any,
) -> None:
    state = FakeState()
    invalid = FakeMessage("/start")
    await extension.receive_publication_title(invalid, state)
    assert "обычный заголовок" in invalid.answers[-1]

    title = FakeMessage("Новый заголовок")
    await extension.receive_publication_title(title, state)
    assert state.data["cpao_publication_title"] == "Новый заголовок"
    assert state.current_state == extension.ClientPlatformAdminOpsState.publication_body

    invalid_body = FakeMessage(" ")
    await extension.receive_publication_body(invalid_body, state)
    assert "Текст пустой" in invalid_body.answers[-1]

    state.data.update(
        cpao_publication_channel="vk",
        cpao_business_id="business-id",
    )
    monkeypatch.setattr(extension, "_context_from_state", lambda *_args: None)

    async def context_from_state(*_args: Any) -> Any:
        return ctx

    monkeypatch.setattr(extension, "_context_from_state", context_from_state)
    monkeypatch.setattr(
        extension.admin_ops,
        "create_publication_draft",
        lambda **kwargs: SimpleNamespace(title=kwargs["title"]),
    )
    body = FakeMessage("Полный текст")
    await extension.receive_publication_body(body, state)
    assert body.answers[-1] == "✅ Черновик «Новый заголовок» создан."
    assert fake_admin.panels == [(701, "business-id")]


@pytest.mark.asyncio
async def test_publication_schedule_input_handler_retries_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    fake_admin: FakeAdmin,
    ctx: Any,
) -> None:
    async def context_from_state(*_args: Any) -> Any:
        return ctx

    monkeypatch.setattr(extension, "_context_from_state", context_from_state)
    monkeypatch.setattr(
        extension,
        "get_business_profile",
        lambda **_kwargs: SimpleNamespace(timezone="Europe/Moscow"),
    )
    state = FakeState(
        {
            "cpao_business_id": "business-id",
            "cpao_publication_id": "publication-id",
        }
    )

    invalid = FakeMessage("/cancel")
    await extension.receive_publication_schedule_time(invalid, state)
    assert "28.08.2026 19:30" in invalid.answers[-1]

    def reject_past(**_kwargs: Any) -> None:
        raise ValueError("publication time must be in the future")

    def reject_role(**_kwargs: Any) -> None:
        raise TenantPermissionDenied("operation is not allowed for this business role")

    monkeypatch.setattr(extension.admin_ops, "schedule_publication", reject_role)
    revoked = FakeMessage("29.08.2026 12:00")
    await extension.receive_publication_schedule_time(revoked, state)
    assert revoked.answers == [
        "Для вашей роли публикации доступны только для просмотра."
    ]
    assert state.clear_count == 1
    state.data.update(
        cpao_business_id="business-id",
        cpao_publication_id="publication-id",
    )
    state.clear_count = 0

    monkeypatch.setattr(extension.admin_ops, "schedule_publication", reject_past)
    past = FakeMessage("27.08.2026 10:00")
    await extension.receive_publication_schedule_time(past, state)
    assert past.answers[-1] == "Выберите будущее время публикации."
    assert state.clear_count == 0

    for error, expected in (
        (
            "publication time is ambiguous because of a timezone transition",
            "Это местное время неоднозначно. Выберите другое время.",
        ),
        (
            "publication time does not exist locally because of a timezone transition",
            "Такого местного времени нет. Выберите другое время.",
        ),
        (
            "business timezone is invalid",
            "Не удалось проверить часовой пояс бизнеса. Проверьте настройки.",
        ),
        (
            "publication changed concurrently; refresh and retry",
            "Публикация уже изменилась. Откройте раздел публикаций заново.",
        ),
        (
            "publication time must look like 28.08.2026 19:30",
            "Введите дату и время в формате 28.08.2026 19:30.",
        ),
    ):
        def reject(**_kwargs: Any) -> None:
            raise ValueError(error)

        monkeypatch.setattr(extension.admin_ops, "schedule_publication", reject)
        invalid_time = FakeMessage("29.08.2026 12:00")
        await extension.receive_publication_schedule_time(invalid_time, state)
        assert invalid_time.answers[-1] == expected
        assert state.clear_count == 0

    publication = SimpleNamespace(
        status="scheduled",
        channel="vk",
        title="План",
        scheduled_at="2026-08-29T09:00:00+00:00",
        published_at=None,
        failed_at=None,
        updated_at="2026-08-28T08:00:00+00:00",
    )
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        extension.admin_ops,
        "schedule_publication",
        lambda **kwargs: calls.append(kwargs) or publication,
    )
    success = FakeMessage("29.08.2026 12:00")
    await extension.receive_publication_schedule_time(success, state)
    assert calls == [
        {
            "actor": ctx.actor,
            "publication_id": "publication-id",
            "local_time": "29.08.2026 12:00",
        }
    ]
    assert "✅ Публикация запланирована" in success.answers[-1]
    assert "29.08.2026 12:00 · ВКонтакте · Запланировано" in success.answers[-1]
    assert state.clear_count == 1
    assert fake_admin.panels == [(701, "business-id")]


@pytest.mark.asyncio
async def test_payment_and_price_input_handlers_cover_invalid_and_success(
    monkeypatch: pytest.MonkeyPatch,
    fake_admin: FakeAdmin,
    ctx: Any,
) -> None:
    async def context_from_state(*_args: Any) -> Any:
        return ctx

    monkeypatch.setattr(extension, "_context_from_state", context_from_state)

    invalid_payment = FakeMessage("bad")
    await extension.receive_payment_value(invalid_payment, FakeState())
    assert "Сумма должна быть числом" in invalid_payment.answers[-1]

    monkeypatch.setattr(
        extension.admin_ops,
        "record_payment",
        lambda **kwargs: (
            recorded_payments.append(kwargs)
            or SimpleNamespace(
                amount_minor=kwargs["amount_minor"],
                currency=kwargs["currency"],
                outcome_event_id="outcome-id",
            )
        ),
    )
    recorded_payments: list[dict[str, Any]] = []
    payment_state = FakeState(
        {
            "cpao_business_id": "business-id",
            "cpao_payment_customer_id": "customer-id",
        }
    )
    payment = FakeMessage("3500 RUB консультация")
    await extension.receive_payment_value(payment, payment_state)
    assert "3 500,00 RUB" in payment.answers[-1]
    assert "Канонический факт выручки подтверждён" in payment.answers[-1]
    assert recorded_payments[0]["idempotency_key"] == "telegram-payment:701:0"

    invalid_price = FakeMessage("0")
    await extension.receive_price_value(invalid_price, FakeState())
    assert "больше нуля" in invalid_price.answers[-1]

    monkeypatch.setattr(
        extension.admin_ops,
        "set_offering_price",
        lambda **kwargs: SimpleNamespace(
            offering_title="Консультация",
            amount_minor=kwargs["amount_minor"],
            currency=kwargs["currency"],
        ),
    )
    price_state = FakeState(
        {
            "cpao_business_id": "business-id",
            "cpao_offering_id": "offering-id",
        }
    )
    price = FakeMessage("5000 USD")
    await extension.receive_price_value(price, price_state)
    assert "Консультация" in price.answers[-1]
    assert "5 000,00 USD" in price.answers[-1]


@pytest.mark.asyncio
async def test_payment_and_price_input_handlers_explain_domain_validation(
    monkeypatch: pytest.MonkeyPatch,
    fake_admin: FakeAdmin,
    ctx: Any,
) -> None:
    async def context_from_state(*_args: Any) -> Any:
        return ctx

    def reject_payment(**_kwargs: Any) -> None:
        raise ValueError("currency must be a known ISO 4217 code")

    def reject_price(**_kwargs: Any) -> None:
        raise ValueError("offering is archived")

    monkeypatch.setattr(extension, "_context_from_state", context_from_state)
    monkeypatch.setattr(extension.admin_ops, "record_payment", reject_payment)
    payment_state = FakeState({"cpao_business_id": "business-id"})
    payment = FakeMessage("3500 XXX")
    await extension.receive_payment_value(payment, payment_state)
    assert payment.answers == [
        "currency must be a known ISO 4217 code. Пример: 3500 RUB консультация."
    ]
    assert payment_state.clear_count == 0

    monkeypatch.setattr(extension.admin_ops, "set_offering_price", reject_price)
    price_state = FakeState(
        {
            "cpao_business_id": "business-id",
            "cpao_offering_id": "offering-id",
        }
    )
    price = FakeMessage("5000 RUB")
    await extension.receive_price_value(price, price_state)
    assert price.answers == ["Цена не сохранена: offering is archived."]
    assert price_state.clear_count == 0


@pytest.mark.asyncio
async def test_payment_refund_requires_confirmation_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    fake_admin: FakeAdmin,
) -> None:
    state = FakeState()
    await extension.admin_ops_gate(_callback("pay-refund", "payment"), state)
    assert "Подтвердите возврат" in fake_admin.edits[-1][0]

    calls: list[dict[str, Any]] = []
    rendered: list[str] = []
    monkeypatch.setattr(
        extension.admin_ops,
        "refund_payment",
        lambda **kwargs: (
            calls.append(kwargs)
            or SimpleNamespace(amount_minor=35_000, currency="RUB")
        ),
    )

    async def render(_callback: Any, _state: Any, _ctx: Any, action: str) -> None:
        rendered.append(action)

    monkeypatch.setattr(extension, "_enhanced_marketing", render)
    callback = _callback("pay-refund-ok", "payment")
    await extension.admin_ops_gate(callback, state)

    assert calls == [
        {
            "actor": fake_admin.ctx.actor,
            "payment_id": "uuid:payment",
            "idempotency_key": "telegram-refund:uuid:payment",
            "reason": "owner_confirmed_full_refund",
        }
    ]
    assert callback.answers == [("Возврат 350,00 RUB сохранён", False)]
    assert rendered == ["payments"]

    def reject_second_refund(**_kwargs: Any) -> None:
        raise extension.admin_ops.PaymentStateConflict("payment is not refundable")

    monkeypatch.setattr(
        extension.admin_ops,
        "refund_payment",
        reject_second_refund,
    )
    second_callback = _callback("pay-refund-ok", "payment")
    await extension.admin_ops_gate(second_callback, state)
    assert second_callback.answers == [
        ("Возврат уже выполнен или больше недоступен", True)
    ]
    assert rendered == ["payments"]


@pytest.mark.asyncio
async def test_record_trace_covers_filters_success_and_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
    fake_admin: FakeAdmin,
) -> None:
    trace = extension._InteractionTrace(
        started=0.0,
        ack_ms=2,
        lock_wait_ms=3,
        telegram_ms=5,
    )
    invalid = SimpleNamespace(
        data="other:value",
        from_user=SimpleNamespace(id=701),
    )
    await extension._record_trace(
        event=invalid,
        trace=trace,
        success=True,
        error_code=None,
        total_ms=20,
        data={},
    )

    bad_token = SimpleNamespace(
        data="cpa:bad:run",
        from_user=SimpleNamespace(id=701),
    )
    await extension._record_trace(
        event=bad_token,
        trace=trace,
        success=True,
        error_code=None,
        total_ms=20,
        data={},
    )

    recorded: list[Any] = []
    monkeypatch.setattr(
        extension.admin_ops,
        "record_interaction_metric",
        lambda metric: recorded.append(metric),
    )
    event = SimpleNamespace(
        data="cpao:business:run",
        from_user=SimpleNamespace(id=701),
        bot=None,
    )
    bot = SimpleNamespace(
        session=SimpleNamespace(
            transport_role="ui",
            active_route="direct",
            transport_generation=4,
        )
    )
    await extension._record_trace(
        event=event,
        trace=trace,
        success=True,
        error_code=None,
        total_ms=20,
        data={"bot": bot},
    )
    assert len(recorded) == 1
    assert recorded[0].app_ms == 10

    for exc in [TenancyError("tenant"), ValueError("bad")]:
        def raise_known(_metric: Any, error: Exception = exc) -> None:
            raise error

        monkeypatch.setattr(
            extension.admin_ops,
            "record_interaction_metric",
            raise_known,
        )
        await extension._record_trace(
            event=event,
            trace=trace,
            success=False,
            error_code="Failure",
            total_ms=1,
            data={},
        )

    for exc in [RuntimeError("db"), OSError("io")]:
        def raise_logged(_metric: Any, error: Exception = exc) -> None:
            raise error

        monkeypatch.setattr(
            extension.admin_ops,
            "record_interaction_metric",
            raise_logged,
        )
        await extension._record_trace(
            event=event,
            trace=trace,
            success=False,
            error_code="Failure",
            total_ms=1,
            data={},
        )
