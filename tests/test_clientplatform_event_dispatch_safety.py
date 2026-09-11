from __future__ import annotations

from pathlib import Path


def test_event_messages_use_the_canonical_provider_outbox() -> None:
    source = Path("clientplatform/application/event_notifications.py").read_text(encoding="utf-8")
    assert "provider_dispatch_outbox" in source
    assert "services.jobs" not in source
    assert "smtplib" not in source
    assert "event_message_outbox" not in source


def test_owner_flow_has_no_zoom_default() -> None:
    source = Path("clientplatform/application/event_owner_flow.py").read_text(encoding="utf-8")
    assert "EventProvider" not in source
    assert "provider_key: str | None = None" in source
    assert "ZOOM" not in source
