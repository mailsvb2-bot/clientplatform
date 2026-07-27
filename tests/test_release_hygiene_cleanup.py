from __future__ import annotations

from pathlib import Path

from scripts.check_release_hygiene import clean_generated_artifacts, find_forbidden_artifacts


def test_cleanup_removes_only_deterministic_analysis_artifacts(tmp_path: Path) -> None:
    (tmp_path / ".mypy_cache").mkdir()
    (tmp_path / ".mypy_cache" / "state.json").write_text("{}", encoding="utf-8")
    package_cache = tmp_path / "services" / "__pycache__"
    package_cache.mkdir(parents=True)
    (package_cache / "module.cpython-312.pyc").write_bytes(b"cache")

    runtime_db = tmp_path / "data.db"
    runtime_db.write_bytes(b"runtime")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    runtime_log = log_dir / "app.log"
    runtime_log.write_text("runtime", encoding="utf-8")

    clean_generated_artifacts(tmp_path)

    assert not (tmp_path / ".mypy_cache").exists()
    assert not package_cache.exists()
    assert runtime_db.exists()
    assert runtime_log.exists()

    forbidden = set(find_forbidden_artifacts(tmp_path))
    assert "data.db" in forbidden
    assert "logs/app.log" in forbidden
