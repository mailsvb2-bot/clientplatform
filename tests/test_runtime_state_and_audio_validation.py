from __future__ import annotations

from pathlib import Path

import pytest

from core import runtime_paths


def test_production_runtime_paths_are_outside_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "runtime" / "current"
    source.mkdir(parents=True)
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("CLIENTPLATFORM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.delenv("CLIENTPLATFORM_WRITABLE_ROOT", raising=False)
    monkeypatch.delenv("MPLCONFIGDIR", raising=False)

    writable = runtime_paths.writable_root()
    mpl = runtime_paths.matplotlib_cache_dir()

    assert writable == (tmp_path / "state").resolve()
    assert mpl.is_relative_to(writable)
    assert not writable.is_relative_to(source)
