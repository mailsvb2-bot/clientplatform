from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clientplatform.domain.tenancy import PlatformRole, TenantPermissionDenied
from handlers import clientplatform_admin_extension as extension


class _State:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = dict(data or {})
        self.current_state: Any = None
        self.clear_count = 0
        self.update_count = 0

    async def clear(self) -> None:
        self.data.clear()
        self.current_state = None
        self.clear_count += 1

    async def update_data(self, **kwargs: Any) -> None:
        self.data.update(kwargs)
        self.update_count += 1

    async def set_state(self, value: Any) -> None:
        self.current_state = value

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)


class _Callback:
    def __init__(self, action: str, *payload: str) -> None:
        suffix = ":".join(payload)
        self.data = f"cpao:business:{action}" + (f":{suffix}" if suffix else "")
        self.from_user = SimpleNamespace(id=701)
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(
        self,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> None:
        self.answers.append((text, show_alert))


class _Message:
    def __init__(self, text: str) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=701)
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs: Any) -> None:
        self.answers.append(text)


class _Control:
    @staticmethod
    def _token_uuid(value: str) -> str:
        return f"uuid:{value}"

    @staticmethod
    def _uuid_token(value: object) -> str:
        return f"token:{value}"


class _Admin:
    control = _Control()

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    async def _load_admin_context(self, **_kwargs: Any) -> Any:
        return self.ctx


def _ctx(role: PlatformRole) -> Any:
    return SimpleNamespace(
        actor=SimpleNamespace(role=role, business_id="business-id"),
        business_id="business-id",
        business_token="business",
        role=role,
        user_id=701,
    )


def test_finance_write_buttons_follow_application_write_roles() -> None:
    admin = _Admin(_ctx(PlatformRole.OWNER))
    offering = SimpleNamespace(id="offering-id", title="Консультация")

    analyst = _ctx(PlatformRole.ANALYST)
    assert extension._can_write_finance(analyst) is False
    assert extension._finance_write_buttons(
        admin,
        analyst,
        action="money",
        offerings=[offering],
    ) == []
    assert extension._finance_write_buttons(
        admin,
        analyst,
        action="payments",
        offerings=[offering],
    ) == []
    assert extension._finance_write_buttons(
        admin,
        analyst,
        action="prices",
        offerings=[offering],
    ) == []

    owner = _ctx(PlatformRole.OWNER)
    assert extension._can_write_finance(owner) is True
    assert extension._finance_write_buttons(
        admin,
        owner,
        action="money",
        offerings=[offering],
    )[0][1] == "cpao:business:payment-new"
    assert "price-set" in extension._finance_write_buttons(
        admin,
        owner,
        action="prices",
        offerings=[offering],
    )[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("payment-new", ()),
        ("payment-customer", ("none",)),
        ("pay-customer", ("none",)),
        ("pay-offer", ("offering",)),
        ("pay-refund", ("payment",)),
        ("pay-refund-ok", ("payment",)),
        ("price-set", ("offering",)),
    ],
)
async def test_analyst_stale_finance_callbacks_never_enter_fsm(
    action: str,
    payload: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx(PlatformRole.ANALYST)
    admin = _Admin(ctx)
    monkeypatch.setattr(extension.importlib, "import_module", lambda *_args: admin)
    state = _State({"existing": "safe"})
    callback = _Callback(action, *payload)

    await extension.admin_ops_gate(callback, state)

    assert callback.answers == [
        ("Для вашей роли финансовые данные доступны только для просмотра.", True)
    ]
    assert state.current_state is None
    assert state.clear_count == 0
    assert state.update_count == 0
    assert state.data == {"existing": "safe"}


@pytest.mark.asyncio
async def test_analyst_old_payment_fsm_is_cleared_on_write_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx(PlatformRole.ANALYST)

    async def context_from_state(_message: Any, _state: Any) -> Any:
        return ctx

    def deny_payment(**_kwargs: Any) -> Any:
        raise TenantPermissionDenied("read only")

    monkeypatch.setattr(extension, "_context_from_state", context_from_state)
    monkeypatch.setattr(extension.admin_ops, "record_payment", deny_payment)
    state = _State(
        {
            "cpao_business_id": "business-id",
            "cpao_payment_customer_id": None,
        }
    )
    message = _Message("3500 RUB консультация")

    await extension.receive_payment_value(message, state)

    assert state.clear_count == 1
    assert state.data == {}
    assert message.answers == [
        "Для вашей роли финансовые данные доступны только для просмотра."
    ]


@pytest.mark.asyncio
async def test_analyst_old_price_fsm_is_cleared_on_write_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx(PlatformRole.ANALYST)

    async def context_from_state(_message: Any, _state: Any) -> Any:
        return ctx

    def deny_price(**_kwargs: Any) -> Any:
        raise TenantPermissionDenied("read only")

    monkeypatch.setattr(extension, "_context_from_state", context_from_state)
    monkeypatch.setattr(extension.admin_ops, "set_offering_price", deny_price)
    state = _State(
        {
            "cpao_business_id": "business-id",
            "cpao_offering_id": "offering-id",
        }
    )
    message = _Message("5000 RUB")

    await extension.receive_price_value(message, state)

    assert state.clear_count == 1
    assert state.data == {}
    assert message.answers == [
        "Для вашей роли финансовые данные доступны только для просмотра."
    ]
