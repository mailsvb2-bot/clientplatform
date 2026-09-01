from __future__ import annotations

import importlib
from pathlib import Path


def test_db_core_does_not_import_writer_eagerly():
    import services.db.core as core
    assert not hasattr(core, "_enqueue")


def test_architecture_contract_validator_imports_and_passes():
    mod = importlib.import_module("services.validators.architecture")
    assert callable(mod.validate_architecture_contracts)
    mod.validate_architecture_contracts(strict=True)


def test_retired_second_brain_files_are_absent():
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "core/engine.py",
        "core/ai/decision_core.py",
        "core/ai/action_gateway.py",
        "core/runtime/self_healing.py",
        "services/scheduler.py",
    ):
        assert not (root / relative).exists(), relative


def test_app_registers_only_canonical_clientplatform_router():
    app = Path("app.py").read_text(encoding="utf-8")
    assert app.count("dp.include_router(clientplatform_entry.router)") == 1
    assert app.count("dp.include_router(") == 1
