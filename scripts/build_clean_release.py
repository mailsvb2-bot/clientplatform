from __future__ import annotations

"""Build a clean release archive from a staging tree.

Why this exists:
- raw zip/glob exclusions are easy to bypass or misconfigure;
- users were still ending up with dirty archives containing __pycache__, logs,
  pytest caches and local SQLite data;
- staging gives us a deterministic, inspectable packaging path.
"""

import shutil
import sys
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

EXCLUDED_DIR_NAMES = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "dist",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".idea",
    ".vscode",
}

EXCLUDED_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.prod",
    ".env.production",
    "data.db",
    "data.db-journal",
    "data.db-wal",
    "data.db-shm",
    "=3.9,",
    ".coverage",
}

EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".sqlite", ".db-wal", ".db-shm", ".db-journal"}
EXCLUDED_TOP_LEVEL_FILES = {".DS_Store", "Thumbs.db", ".coverage"}


def _should_skip(path: Path, project_root: Path) -> bool:
    rel = path.relative_to(project_root)
    parts = rel.parts
    if any(part in EXCLUDED_DIR_NAMES for part in parts):
        return True
    if path.name in EXCLUDED_FILE_NAMES or path.name in EXCLUDED_TOP_LEVEL_FILES:
        return True
    if path.suffix in EXCLUDED_SUFFIXES:
        return True
    if path.name.startswith('.env'):
        return True
    if path.suffix == '.db' and path.name != '.keep':
        return True
    if len(parts) >= 2 and parts[0] == "logs" and path.is_file() and path.suffix == ".log":
        return True
    return False


def _copy_clean_tree(project_root: Path, stage_project: Path) -> None:
    for src in project_root.rglob("*"):
        if src == stage_project or stage_project in src.parents:
            continue
        if _should_skip(src, project_root):
            continue
        rel = src.relative_to(project_root)
        dst = stage_project / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _assert_stage_clean(stage_project: Path) -> None:
    forbidden: list[str] = []
    for p in stage_project.rglob("*"):
        if p.name in EXCLUDED_FILE_NAMES or p.suffix in EXCLUDED_SUFFIXES:
            forbidden.append(str(p.relative_to(stage_project)))
        elif p.name in EXCLUDED_DIR_NAMES:
            forbidden.append(str(p.relative_to(stage_project)))
        elif len(p.relative_to(stage_project).parts) >= 2 and p.relative_to(stage_project).parts[0] == "logs" and p.is_file() and p.suffix == ".log":
            forbidden.append(str(p.relative_to(stage_project)))
    if forbidden:
        sample = "\n  - ".join(sorted(set(forbidden))[:50])
        raise SystemExit(f"Staging tree is still dirty:\n  - {sample}")


def _zip_tree(stage_root: Path, project_name: str, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as zf:
        base = stage_root / project_name
        for path in sorted(base.rglob("*")):
            arcname = path.relative_to(stage_root)
            if path.is_dir():
                continue
            zf.write(path, arcname)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("Usage: python scripts/build_clean_release.py <project_dir> <output_zip>", file=sys.stderr)
        return 2

    project_root = Path(argv[1]).resolve()
    zip_path = Path(argv[2]).resolve()
    if not project_root.is_dir():
        print(f"Project dir not found: {project_root}", file=sys.stderr)
        return 2

    zip_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="clientplatform_release_stage_") as tmp:
        stage_root = Path(tmp)
        stage_project = stage_root / project_root.name
        stage_project.mkdir(parents=True, exist_ok=True)
        _copy_clean_tree(project_root, stage_project)
        _assert_stage_clean(stage_project)
        _zip_tree(stage_root, project_root.name, zip_path)

    print(f"✅ Clean archive built: {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
