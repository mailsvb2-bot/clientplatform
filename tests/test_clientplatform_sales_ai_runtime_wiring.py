from __future__ import annotations

from pathlib import Path


def test_optional_sales_ai_worker_is_bound_without_owning_core_startup() -> None:
    text = Path("app.py").read_text(encoding="utf-8")
    assert "bind_sales_ai_worker(tm)" in text
    assert "Optional Sales AI worker failed to start; core runtime continues" in text


def test_managed_bot_capture_is_a_fail_open_side_channel() -> None:
    text = Path("clientplatform/runtime/bot_gateway.py").read_text(encoding="utf-8")
    assert "record_managed_bot_customer_message" in text
    assert "Managed bot sales-intelligence side channel failed; dispatch continues" in text
    assert text.index("record_managed_bot_customer_message") < text.index("await dp.feed_update(bot, update)")


def test_owner_flow_exposes_opt_in_and_review_draft_but_no_send_callback() -> None:
    text = Path("handlers/clientplatform_sales.py").read_text(encoding="utf-8")
    assert "cps:sat:" in text
    assert "cps:sae:" in text
    assert "cps:sad:" in text
    assert "ClientPlatform ничего не отправил клиенту автоматически" in text
    assert "send_sales_ai" not in text
