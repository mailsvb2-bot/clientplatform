"""Fast guardrail for git/pre-commit/CI.

Fails if the working tree contains artifacts that must never be shipped:
- __pycache__ directories
- *.pyc/*.pyo files
- runtime SQLite DB files (data.db, data/data.db)
- test/lint caches or runtime logs
- suspicious temporary root-level packaging fragments

The optional ``--clean-generated`` mode removes only deterministic Python and
analysis caches created by the gate itself. It deliberately does not remove
runtime databases, logs or unknown packaging fragments: those remain hard
release failures and require an explicit owner decision.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

FORBIDDEN_DB = {
    Path("data.db"),
    Path("data") / "data.db",
}
FORBIDDEN_DB_GLOBS = ["*.sqlite", "*.db-wal", "*.db-shm", "*.db-journal"]

FORBIDDEN_DIRS = {
    Path(".pytest_cache"),
    Path(".mypy_cache"),
    Path(".idea"),
    Path(".vscode"),
}

IGNORED_ROOT_NAMES = {
    ".git",
    ".venv",
    "venv",
    "env",
    ".envrc",
    ".ruff_cache",
}

GENERATED_CACHE_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
GENERATED_FILE_SUFFIXES = {".pyc", ".pyo"}
VIRTUALENV_DIR_NAMES = {".venv", "venv", "env"}
VIRTUALENV_DIR_PREFIXES = (".venv-", "venv-", "env-")
CLEANUP_SKIP_DIR_NAMES = {".git", *VIRTUALENV_DIR_NAMES}

FORBIDDEN_LOG_DIR = Path("logs")
FORBIDDEN_LOG_GLOBS = ["*.log"]

ALLOWED_ROOT_FILES = {
    ".audio-assets.json",
    ".dockerignore",
    ".env.example",
    ".gitignore",
    ".pre-commit-config.yaml",
    ".release.json",
    "README.md",
    "VERSION",
    "SOVEREIGNTY_BUILD_MANIFEST.json",
    "app.py",
    "main.py",
    "check_db.py",
    "pyproject.toml",
    "requirements.in",
    "requirements-dev.in",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-py313.txt",
    "pytest.ini",
    "release.sh",
    "release.ps1",
}

ALLOWED_ROOT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".yml",
    ".yaml",
    ".service",
    ".sql",
    ".sh",
    ".ps1",
    ".bat",
    ".example",
}


def _is_ignored(rel: Path) -> bool:
    parts = rel.parts
    return bool(parts) and parts[0] in IGNORED_ROOT_NAMES


def _is_virtualenv_dir(name: str) -> bool:
    return name in VIRTUALENV_DIR_NAMES or name.startswith(VIRTUALENV_DIR_PREFIXES)


def clean_generated_artifacts(root: Path) -> None:
    """Remove only deterministic cache/bytecode products created by checks."""

    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        dirs[:] = [
            name
            for name in dirs
            if name not in CLEANUP_SKIP_DIR_NAMES and not _is_virtualenv_dir(name)
        ]
        for dirname in list(dirs):
            if dirname in GENERATED_CACHE_DIR_NAMES:
                shutil.rmtree(current_path / dirname, ignore_errors=True)
                dirs.remove(dirname)
        for filename in files:
            if Path(filename).suffix not in GENERATED_FILE_SUFFIXES:
                continue
            try:
                (current_path / filename).unlink()
            except FileNotFoundError:
                pass


def find_forbidden_artifacts(root: Path) -> list[str]:
    bad: list[str] = []

    for rel in FORBIDDEN_DB:
        if _is_ignored(rel):
            continue
        p = root / rel
        if p.exists():
            bad.append(str(rel).replace("\\", "/"))

    for rel in FORBIDDEN_DIRS:
        if _is_ignored(rel):
            continue
        p = root / rel
        if p.exists():
            bad.append(str(rel).replace("\\", "/"))

    log_dir = root / FORBIDDEN_LOG_DIR
    if log_dir.exists():
        for pattern in FORBIDDEN_LOG_GLOBS:
            for p in log_dir.rglob(pattern):
                rel_path = p.relative_to(root)
                if p.is_file() and not _is_ignored(rel_path):
                    bad.append(str(rel_path).replace("\\", "/"))

    for p in root.iterdir():
        rel_path = p.relative_to(root)
        if _is_ignored(rel_path):
            continue
        if not p.is_file():
            continue
        if p.name.startswith(".") and p.name not in ALLOWED_ROOT_FILES:
            bad.append(str(rel_path).replace("\\", "/"))
            continue
        if p.name in ALLOWED_ROOT_FILES:
            continue
        if p.suffix in ALLOWED_ROOT_SUFFIXES:
            continue
        bad.append(str(rel_path).replace("\\", "/"))

    for glob_name in FORBIDDEN_DB_GLOBS:
        for p in root.rglob(glob_name):
            rel_path = p.relative_to(root)
            rel = str(rel_path).replace("\\", "/")
            if p.is_file() and not rel.startswith("dist/") and not _is_ignored(rel_path):
                bad.append(rel)

    for p in root.rglob("__pycache__"):
        rel_path = p.relative_to(root)
        if p.is_dir() and not _is_ignored(rel_path):
            bad.append(str(rel_path).replace("\\", "/"))

    for ext in ("*.pyc", "*.pyo"):
        for p in root.rglob(ext):
            rel_path = p.relative_to(root)
            if p.is_file() and not _is_ignored(rel_path):
                bad.append(str(rel_path).replace("\\", "/"))

    return [item for item in bad if not item.startswith("dist/")]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    unknown = [arg for arg in args if arg != "--clean-generated"]
    if unknown:
        print(f"Unknown arguments: {' '.join(unknown)}")
        return 2

    root = Path(__file__).resolve().parents[1]
    if "--clean-generated" in args:
        clean_generated_artifacts(root)

    bad = find_forbidden_artifacts(root)
    if bad:
        print("❌ Release hygiene failed. Remove forbidden artifacts:")
        unique = sorted(set(bad))
        for item in unique[:200]:
            print(f"  - {item}")
        if len(unique) > 200:
            print(f"  ... and {len(unique) - 200} more")
        return 2

    print("✅ Release hygiene OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
