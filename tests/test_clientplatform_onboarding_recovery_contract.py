from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "handlers" / "clientplatform_entry.py"
RECOVERY = ROOT / "handlers" / "clientplatform_onboarding_recovery.py"


def test_recovery_router_is_composed_before_other_clientplatform_subrouters() -> None:
    text = ENTRY.read_text(encoding="utf-8")
    recovery = "router.include_router(onboarding_recovery.router)"
    media = "router.include_router(program_media.router)"
    original = "router.include_router(original_router)"

    assert recovery in text
    assert text.index(recovery) < text.index(media) < text.index(original)
    assert "control._onboarding_recovery_router_composed = True" in text


def test_recovery_module_has_no_syntax_or_import_time_side_effect_contract() -> None:
    tree = ast.parse(RECOVERY.read_text(encoding="utf-8"))
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "recover_activity_description" in function_names
    assert "IncompleteActivityDescriptionFilter" in {
        node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    }
    assert "os" not in {
        alias.name
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
