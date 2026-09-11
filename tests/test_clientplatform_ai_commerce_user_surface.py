from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from clientplatform.application import native_member_interactions as native
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import PlatformRole, TenantContext


def _actor() -> TenantContext:
    return TenantContext(
        business_id=str(uuid4()),
        user_id=940001,
        membership_id=str(uuid4()),
        role=PlatformRole.OWNER,
    )


def _overview(*, staff_upgrade: bool, customer_upgrade: bool = False):
    return SimpleNamespace(
        staff=SimpleNamespace(
            requires_upgrade=staff_upgrade,
            used=5 if staff_upgrade else 2,
            allowance=5,
            projected_total=6 if staff_upgrade else 3,
        ),
        customers=SimpleNamespace(
            requires_upgrade=customer_upgrade,
            used=500 if customer_upgrade else 20,
            allowance=500,
            projected_total=501 if customer_upgrade else 21,
        ),
        expansion_recommended=staff_upgrade or customer_upgrade,
    )


def test_native_tariff_exposes_contextual_commerce_status(monkeypatch) -> None:
    monkeypatch.setattr(
        native.admin_ops,
        "get_subscription_state",
        lambda **_kwargs: SimpleNamespace(plan_key="base", status="active"),
    )
    monkeypatch.setattr(
        native,
        "get_commerce_overview",
        lambda **_kwargs: _overview(staff_upgrade=False, customer_upgrade=True),
    )
    message = native._tariff_message(_actor())
    assert "Умный контроль тарифа" in message.text
    assert "Следующий сотрудник: входит" in message.text
    assert "Следующий клиент: потребуется расширение" in message.text
    assert "не списывает деньги автоматически" in message.text
    assert any(button.command == "cpm:tariff-upgrade" for row in message.rows for button in row)


def test_native_add_member_stops_before_input_when_seat_requires_upgrade(monkeypatch) -> None:
    monkeypatch.setattr(
        native,
        "get_commerce_overview",
        lambda **_kwargs: _overview(staff_upgrade=True),
    )
    message = native._member_add_role_message(
        _actor(),
        "manager",
        current_platform=ConnectionPlatform.VK,
    )
    assert "требует расширения тарифа" in message.text
    assert any(button.command == "cpm:tariff" for row in message.rows for button in row)


def test_native_tariff_upgrade_uses_same_billing_request_use_case(monkeypatch) -> None:
    case = SimpleNamespace(id="case-42")
    captured = []
    monkeypatch.setattr(
        native,
        "request_subscription_expansion",
        lambda **kwargs: captured.append(kwargs["actor"]) or case,
    )
    actor = _actor()
    message = native._tariff_upgrade_message(actor)
    assert captured == [actor]
    assert "case-42" in message.text
    assert "Никаких списаний" in message.text
    assert any(button.command == "cpm:tariff" for row in message.rows for button in row)


def test_native_parser_admits_tariff_upgrade() -> None:
    parsed = native.parse_native_member_interaction("cpm:tariff-upgrade")
    assert parsed.action == "tariff-upgrade"
