from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clientplatform.application import admin_ops
from clientplatform.domain.tenancy import PlatformRole, TenantPermissionDenied
from handlers import clientplatform_admin_extension as extension


ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "clientplatform/application/admin_ops.py"
HANDLER = ROOT / "handlers/clientplatform_admin_extension.py"


def _source(path: Path) -> str:
    value = path.read_text(encoding="utf-8")
    ast.parse(value)
    return value


def _function(value: str, name: str) -> str:
    start = value.index(f"def {name}(")
    end = value.find("\n\ndef ", start + 10)
    return value[start:] if end < 0 else value[start:end]


def test_finance_rbac_is_read_write_separated() -> None:
    value = _source(OPS)
    read = value[
        value.index("_FINANCE_READ_ROLES") : value.index("_FINANCE_WRITE_ROLES")
    ]
    write = value[
        value.index("_FINANCE_WRITE_ROLES") : value.index("_OBSERVABILITY_ROLES")
    ]
    assert "PlatformRole.ANALYST" in read
    assert "PlatformRole.ANALYST" not in write
    for name in ("record_payment", "refund_payment", "set_offering_price"):
        assert "allowed_roles=_FINANCE_WRITE_ROLES" in _function(value, name)
    for name in ("list_payments", "list_offering_prices"):
        assert "allowed_roles=_FINANCE_READ_ROLES" in _function(value, name)


def test_finance_rbac_enforces_analyst_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(user_id=701, business_id="business-id")
    resolved = SimpleNamespace(role=PlatformRole.ANALYST)

    class FakeRepository:
        def __init__(self, _conn: Any) -> None:
            pass

        def resolve_context(self, *, user_id: int, business_id: str) -> Any:
            assert user_id == actor.user_id
            assert business_id == actor.business_id
            return resolved

    monkeypatch.setattr(admin_ops, "TenancyRepository", FakeRepository)

    assert (
        admin_ops._resolve(
            object(),
            actor,
            allowed_roles=admin_ops._FINANCE_READ_ROLES,
        )
        is resolved
    )
    with pytest.raises(TenantPermissionDenied, match="operation is not allowed"):
        admin_ops._resolve(
            object(),
            actor,
            allowed_roles=admin_ops._FINANCE_WRITE_ROLES,
        )


def test_autopilot_direct_callback_rechecks_role() -> None:
    value = _function(_source(HANDLER), "admin_ops_gate")
    branch = value[
        value.index('if action == "autopilot-toggle"') : value.index(
            'if action == "alerts-refresh"'
        )
    ]
    assert "ctx.role not in admin._AUTOMATION_ROLES" in branch
    assert branch.index("_AUTOMATION_ROLES") < branch.index("toggle_autopilot")


@pytest.mark.asyncio
async def test_suppressed_callback_is_recorded_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCallback:
        def __init__(self) -> None:
            self.data = "cpao:business:run"

    class SuppressingMiddleware:
        async def __call__(
            self,
            _handler: Any,
            _event: Any,
            _data: dict[str, Any],
        ) -> str:
            return "suppressed"

    async def answer_callback(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def safe_edit(*_args: Any, **_kwargs: Any) -> None:
        return None

    recorded: list[dict[str, Any]] = []

    async def record_trace(**kwargs: Any) -> None:
        recorded.append(kwargs)

    admin = SimpleNamespace(_safe_edit=safe_edit)
    safety = SimpleNamespace(
        _answer_callback=answer_callback,
        ClientPlatformInteractionSafetyMiddleware=SuppressingMiddleware,
        _clientplatform_admin_trace_installed=False,
    )

    monkeypatch.setattr(extension, "CallbackQuery", FakeCallback)
    monkeypatch.setattr(extension, "_record_trace", record_trace)
    extension._install_trace_hooks(admin, safety)

    handler_called = False

    async def handler(_event: Any, _data: dict[str, Any]) -> None:
        nonlocal handler_called
        handler_called = True

    result = await safety.ClientPlatformInteractionSafetyMiddleware()(
        handler,
        FakeCallback(),
        {},
    )

    assert result == "suppressed"
    assert handler_called is False
    assert len(recorded) == 1
    assert recorded[0]["success"] is False
    assert recorded[0]["error_code"] == "suppressed_callback"
