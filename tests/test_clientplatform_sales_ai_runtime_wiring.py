from __future__ import annotations

from pathlib import Path


def _function_body(text: str, marker: str, next_marker: str | None = None) -> str:
    start = text.index(marker)
    if next_marker is None:
        return text[start:]
    end = text.index(next_marker, start + len(marker))
    return text[start:end]


def test_optional_sales_ai_worker_is_bound_without_owning_core_startup() -> None:
    text = Path("app.py").read_text(encoding="utf-8")
    assert "bind_sales_ai_worker(tm)" in text
    assert "Optional Sales AI worker failed to start; core runtime continues" in text


def test_retention_runtime_survives_disabled_provider_processing() -> None:
    text = Path("clientplatform/runtime/sales_ai.py").read_text(encoding="utf-8")
    run_body = _function_body(text, "    async def _run(self) -> None:", "    async def _process")
    start_body = _function_body(text, "    def start(self) -> bool:", "    async def _run")
    assert "if not self.config.enabled or self._running" not in start_body
    assert "if self.config.enabled:" in run_body
    assert "purge_sales_ai_retention" in run_body
    assert run_body.index("if self.config.enabled:") < run_body.index(
        "purge_sales_ai_retention"
    )


def test_managed_bot_capture_is_a_fail_open_side_channel() -> None:
    text = Path("clientplatform/runtime/bot_gateway.py").read_text(encoding="utf-8")
    process_body = _function_body(text, "    async def _process(self, item", "    async def _bot_for")
    assert "record_managed_bot_customer_message" in process_body
    assert "Managed bot sales-intelligence side channel failed; dispatch continues" in process_body
    assert process_body.index("record_managed_bot_customer_message") < process_body.index(
        "await self._dispatcher.feed_webhook_update"
    )


def test_owner_flow_exposes_opt_in_and_review_draft_but_no_send_callback() -> None:
    text = Path("handlers/clientplatform_sales.py").read_text(encoding="utf-8")
    assert "cps:sat:" in text
    assert "cps:sae:" in text
    assert "cps:sad:" in text
    assert "ClientPlatform ничего не отправил клиенту автоматически" in text
    assert "send_sales_ai" not in text


def test_draft_is_revalidated_after_provider_returns() -> None:
    text = Path("clientplatform/application/sales_ai_drafts.py").read_text(encoding="utf-8")
    body = _function_body(text, "async def draft_sales_reply", "\n\n__all__")
    assert "validate_sales_ai_draft_freshness" in body
    assert body.index("draft = await provider.draft_reply") < body.index(
        "validate_sales_ai_draft_freshness"
    )
    assert "expected_source_order_key" in body
    assert "expected_plan_id" in body
