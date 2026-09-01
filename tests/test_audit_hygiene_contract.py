from __future__ import annotations

from pathlib import Path


def test_no_engine_backup_artifacts_are_tracked_or_present():
    assert not list(Path("core").glob("*.bak*"))


def test_text_ui_is_thin_clientplatform_compatibility_surface():
    source = Path("services/messenger/text_ui.py").read_text(encoding="utf-8")
    assert "services.messenger.text_ui_router" in source
    assert "MessengerReply" in source
    assert len(source.splitlines()) < 40
