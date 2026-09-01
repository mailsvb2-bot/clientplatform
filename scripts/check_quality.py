from __future__ import annotations

import compileall
import os
# Reviewed: operator quality gate invokes fixed local checks without shell.
import subprocess  # nosec B404
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "audio",
    "backups",
    "build",
    "data",
    "dist",
    "logs",
    "tmp",
}


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env


def _run(title: str, cmd: list[str]) -> int:
    print(f"\n== {title} ==")
    print("cmd:", " ".join(cmd))
    # Reviewed: command lists are defined in this file and executed without shell.
    completed = subprocess.run(cmd, cwd=ROOT, env=_env(), check=False)  # nosec B603
    return int(completed.returncode)


def _compile_project() -> int:
    print("\n== Python compile check ==")
    ok = True
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if not compileall.compile_file(str(path), quiet=1, force=False):
            print(f"compile failed: {rel.as_posix()}")
            ok = False
    return 0 if ok else 1


def main() -> int:
    checks = [
        ("compile", _compile_project),
        ("ruff", lambda: _run("Ruff P0 gate", [sys.executable, "scripts/check_ruff.py"])),
    ]

    failed: list[str] = []
    for name, check in checks:
        rc = check()
        if rc != 0:
            failed.append(name)
            print(f"❌ {name} failed with exit code {rc}")
        else:
            print(f"✅ {name} OK")

    if failed:
        print("\n❌ Quality gate failed:", ", ".join(failed))
        return 1

    print("\n✅ Quality gate OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
