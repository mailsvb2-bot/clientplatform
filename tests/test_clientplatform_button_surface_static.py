from __future__ import annotations

import ast
import re
from pathlib import Path

from handlers.clientplatform_interaction_safety import (
    _CLIENTPLATFORM_CALLBACK_PREFIXES,
)


ROOT = Path(__file__).resolve().parents[1]
_CALLBACK_ROOT = re.compile(r"^(cp[a-z]*):")
_AD_ACTION_FIRST = {
    "home",
    "connect",
    "yandex-cancel",
    "promote",
    "slot",
    "conn",
    "campaign",
    "confirm",
    "disconnects",
    "disconnect",
    "revoke",
}


def _surface_files() -> list[Path]:
    paths = sorted((ROOT / "handlers").glob("clientplatform_*.py"))
    paths.append(ROOT / "clientplatform" / "presentation" / "ad_spend_telegram.py")
    return [path for path in paths if path.is_file()]


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _static_prefix(node: ast.AST) -> tuple[str, bool]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, True
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
                continue
            return "".join(parts), False
        return "".join(parts), True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, left_complete = _static_prefix(node.left)
        if not left_complete:
            return left, False
        right, right_complete = _static_prefix(node.right)
        return left + right, right_complete
    return "", False


def _callback_prefix(node: ast.AST) -> str | None:
    prefix, _complete = _static_prefix(node)
    return prefix if _CALLBACK_ROOT.match(prefix) else None


def _keyboard_callbacks(tree: ast.AST) -> set[str]:
    emitted: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node) == "InlineKeyboardButton":
            for keyword in node.keywords:
                if keyword.arg == "callback_data":
                    prefix = _callback_prefix(keyword.value)
                    if prefix:
                        emitted.add(prefix)
        if not _call_name(node).endswith("keyboard"):
            continue
        for descendant in ast.walk(node):
            if not isinstance(descendant, ast.Tuple) or len(descendant.elts) != 2:
                continue
            prefix = _callback_prefix(descendant.elts[1])
            if prefix:
                emitted.add(prefix)
    return emitted


def _decorator_callback_patterns(tree: ast.AST) -> set[str]:
    accepted: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or _call_name(decorator) != "callback_query":
                continue
            for part in ast.walk(decorator):
                if isinstance(part, ast.Call) and _call_name(part) == "startswith":
                    for argument in part.args[:1]:
                        if isinstance(argument, ast.Tuple):
                            candidates = argument.elts
                        else:
                            candidates = [argument]
                        for candidate in candidates:
                            prefix, complete = _static_prefix(candidate)
                            if complete and _CALLBACK_ROOT.match(prefix):
                                accepted.add(prefix)
                if isinstance(part, ast.Compare) and len(part.comparators) == 1:
                    prefix, complete = _static_prefix(part.comparators[0])
                    if complete and _CALLBACK_ROOT.match(prefix):
                        accepted.add(prefix)
    return accepted


def _specific_handler_exists(emitted: str, accepted: set[str]) -> bool:
    parts = emitted.split(":")
    action_first_ad = (
        len(parts) >= 2
        and parts[0] == "cpa"
        and parts[1] in _AD_ACTION_FIRST
    )
    for pattern in accepted:
        if action_first_ad and pattern == "cpa:":
            continue
        if emitted.startswith(pattern) or pattern.startswith(emitted):
            return True
    return False


def test_every_rendered_callback_namespace_is_known_to_interaction_safety() -> None:
    roots: set[str] = set()
    for path in _surface_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for emitted in _keyboard_callbacks(tree):
            match = _CALLBACK_ROOT.match(emitted)
            assert match is not None
            roots.add(f"{match.group(1)}:")

    missing = sorted(roots.difference(_CLIENTPLATFORM_CALLBACK_PREFIXES))
    assert not missing, f"callback namespaces missing from safety contract: {missing}"


def test_every_statically_rendered_callback_has_a_registered_handler() -> None:
    emitted: set[str] = set()
    accepted: set[str] = set()
    origins: dict[str, set[str]] = {}

    for path in _surface_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for prefix in _keyboard_callbacks(tree):
            emitted.add(prefix)
            origins.setdefault(prefix, set()).add(str(path.relative_to(ROOT)))
        accepted.update(_decorator_callback_patterns(tree))

    missing = sorted(
        prefix
        for prefix in emitted
        if not _specific_handler_exists(prefix, accepted)
    )
    detail = {
        prefix: sorted(origins[prefix])
        for prefix in missing
    }
    assert not missing, f"rendered callbacks without handler contract: {detail}"
