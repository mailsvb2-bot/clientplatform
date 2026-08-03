from __future__ import annotations

import ast
from pathlib import Path


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
    for name in ("record_payment", "set_offering_price"):
        assert "allowed_roles=_FINANCE_WRITE_ROLES" in _function(value, name)
    for name in ("list_payments", "list_offering_prices"):
        assert "allowed_roles=_FINANCE_READ_ROLES" in _function(value, name)


def test_autopilot_direct_callback_rechecks_role() -> None:
    value = _function(_source(HANDLER), "admin_ops_gate")
    branch = value[
        value.index('if action == "autopilot-toggle"') : value.index(
            'if action == "alerts-refresh"'
        )
    ]
    assert "ctx.role not in admin._AUTOMATION_ROLES" in branch
    assert branch.index("_AUTOMATION_ROLES") < branch.index("toggle_autopilot")


def test_suppressed_callback_is_not_success() -> None:
    value = _source(HANDLER)
    start = value.index("    async def traced_call(")
    traced = value[start : value.index("\n    safety._answer_callback", start)]
    assert "handler_invoked = False" in traced
    assert "nonlocal handler_invoked" in traced
    assert "success = handler_invoked" in traced
    assert 'error_code = "suppressed_callback"' in traced
    assert "success = True" not in traced
