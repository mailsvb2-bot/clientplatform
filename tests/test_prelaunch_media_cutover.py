from __future__ import annotations

from pathlib import Path

import pytest

from clientplatform.runtime import prelaunch_media_cutover as cutover


def test_prelaunch_cutover_removes_only_legacy_ad_media(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "clientplatform"
    legacy = root / "ad-assets"
    keep = root / "state"
    legacy.mkdir(parents=True)
    keep.mkdir(parents=True)
    (legacy / "old.jpg").write_bytes(b"user-media")
    (keep / "keep.txt").write_text("metadata", encoding="utf-8")

    monkeypatch.setattr(cutover, "_PRODUCTION_MEDIA_ROOT", root.resolve())
    monkeypatch.setattr(cutover, "_LEGACY_AD_ASSET_DIR", legacy.resolve())
    monkeypatch.delenv("CLIENTPLATFORM_AD_ASSET_DIR", raising=False)

    assert cutover.purge_prelaunch_legacy_ad_media() is True
    assert not legacy.exists()
    assert (keep / "keep.txt").read_text(encoding="utf-8") == "metadata"
    assert cutover.purge_prelaunch_legacy_ad_media() is False


def test_prelaunch_cutover_refuses_root_and_escape(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "clientplatform"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    monkeypatch.setattr(cutover, "_PRODUCTION_MEDIA_ROOT", root.resolve())
    monkeypatch.setenv("CLIENTPLATFORM_AD_ASSET_DIR", str(root))
    with pytest.raises(RuntimeError, match="refusing to purge"):
        cutover.purge_prelaunch_legacy_ad_media()

    monkeypatch.setenv("CLIENTPLATFORM_AD_ASSET_DIR", str(outside))
    with pytest.raises(RuntimeError, match="escapes ClientPlatform root"):
        cutover.purge_prelaunch_legacy_ad_media()
