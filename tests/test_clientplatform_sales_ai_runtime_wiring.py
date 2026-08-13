from __future__ import annotations

from pathlib import Path


def test_optional_sales_ai_worker_is_bound_without_owning_core_startup() -> None:
    text = Path("app.py").read_text(encoding="utf-8")
    assert "bind_sales_ai_worker(tm)" in text
    assert "Optional Sales AI worker failed to start; core runtime continues" in text


def test_retention_runtime_survives_disabled_provider_processing() -> None:
    text = Path("clientplatform/runtime/sales_ai.py").read_text(encoding="utf-8")
    assert "if not self.config.enabled or self._running" not in text
    assert "if self.config.enabled:" in text
    assert "purge_sales_ai_retention" in text
    assert text.index("if self.config.enabled:") < text.index("purge_sales_ai_retention")


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


def test_draft_is_revalidated_after_provider_returns() -> None:
    text = Path("clientplatform/application/sales_ai_drafts.py").read_text(encoding="utf-8")
    assert "validate_sales_ai_draft_freshness" in text
    assert text.index("draft = await provider.draft_reply") < text.index(
        "validate_sales_ai_draft_freshness"
    )
    assert "expected_source_order_key" in text
    assert "expected_plan_id" in text
