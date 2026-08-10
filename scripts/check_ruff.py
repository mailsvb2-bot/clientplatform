from __future__ import annotations

import os
# Reviewed: operator-only quality gate invokes fixed local Python quality tools.
import subprocess  # nosec B404
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUFF_TARGETS = (
    "services",
    "scripts",
    "handlers",
    "core",
    "runtime",
    "config",
    "tests",
    "app.py",
    "main.py",
)
HANDLER_BOUNDARY_AUDITS = (
    "scripts/handler_exception_boundary_audit.py",
    "scripts/handler_db_boundary_audit.py",
)
VENV_PREFIXES = (".venv", "venv", "env")


def _existing_project_targets() -> list[str]:
    targets: list[str] = []
    for target in RUFF_TARGETS:
        path = ROOT / target
        if path.exists():
            targets.append(target)
    return targets


def _run_fixed(command: list[str], *, env: dict[str, str]) -> int:
    # Reviewed: fixed local command, no shell, repository-owned paths only.
    return int(subprocess.call(command, cwd=ROOT, env=env))  # nosec B603


def main() -> int:
    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    targets = _existing_project_targets()
    if not targets:
        print("No project targets found for Ruff quality gate")
        return 2

    ruff_cmd = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        *targets,
        "--exclude",
        ".venv*",
        "--exclude",
        "venv*",
        "--exclude",
        "env*",
    ]
    print("== Ruff quality gate ==")
    print("cwd:", ROOT)
    print("cmd:", " ".join(ruff_cmd))
    code = _run_fixed(ruff_cmd, env=env)
    if code:
        return code

    print("== Handler architecture boundary audits ==")
    for relative in HANDLER_BOUNDARY_AUDITS:
        path = ROOT / relative
        if not path.is_file():
            print(f"Missing required handler boundary audit: {relative}")
            return 2
        command = [sys.executable, relative]
        print("cmd:", " ".join(command))
        code = _run_fixed(command, env=env)
        if code:
            return code

    print("HANDLER_ARCHITECTURE_BOUNDARIES_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
