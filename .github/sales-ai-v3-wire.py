from __future__ import annotations

import base64
from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"SALES_AI_INTEGRATION_FAILED:{path}:marker_count={count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


app_marker = "            bind_task_manager(tm)\n"
app_insert = app_marker + """            # Sales AI is an optional advisory worker owned by the canonical TaskManager.
            # Its configuration/provider failure must never take down core ClientPlatform.
            try:
                from clientplatform.runtime.sales_ai import bind_sales_ai_worker

                bind_sales_ai_worker(tm)
            except Exception:  # validator: allow-wide-except
                log.exception("Optional Sales AI worker failed to start; core runtime continues")
"""
replace_once("app.py", app_marker, app_insert)

bot_old = """            actor = _telegram_actor(payload)
            if actor is not None:
                await asyncio.to_thread(
                    ensure_telegram_customer_link,
                    route=item.route,
                    telegram_user_id=actor[0],
                    username=actor[1],
                    display_name=actor[2],
                )
            bot = await self._bot_for(item.route)
"""
bot_new = """            actor = _telegram_actor(payload)
            if actor is not None:
                customer_link = await asyncio.to_thread(
                    ensure_telegram_customer_link,
                    route=item.route,
                    telegram_user_id=actor[0],
                    username=actor[1],
                    display_name=actor[2],
                )
                from clientplatform.application.sales_intelligence import (
                    extract_customer_message_text,
                    record_managed_bot_customer_message,
                )

                customer_text = extract_customer_message_text(payload)
                if customer_text is not None:
                    ai_enabled = False
                    ai_target = ""
                    try:
                        from clientplatform.runtime.sales_ai_config import SalesAIRuntimeConfig

                        ai_config = SalesAIRuntimeConfig.from_env()
                        ai_enabled = ai_config.enabled
                        ai_target = ai_config.consent_target
                    except (TypeError, ValueError):
                        log.warning(
                            "Sales AI configuration is invalid; continuing without AI",
                            exc_info=True,
                        )
                    try:
                        await asyncio.to_thread(
                            record_managed_bot_customer_message,
                            route=item.route,
                            customer_link=customer_link,
                            telegram_user_id=actor[0],
                            provider_update_id=item.event.provider_update_id,
                            message_text=customer_text,
                            runtime_ai_enabled=ai_enabled,
                            runtime_ai_consent_target=ai_target,
                        )
                    except Exception:  # validator: allow-wide-except
                        log.exception(
                            "Managed bot sales-intelligence side channel failed; dispatch continues",
                            extra={
                                "managed_bot_id": item.route.managed_bot_id,
                                "business_id": item.route.business_id,
                            },
                        )
            bot = await self._bot_for(item.route)
"""
replace_once("clientplatform/runtime/bot_gateway.py", bot_old, bot_new)

handler_path = Path("handlers/clientplatform_sales.py")
handler = handler_path.read_text(encoding="utf-8")
start_marker = "async def _send_sales_work("
end_marker = "async def _send_handoffs("
start = handler.find(start_marker)
end = handler.find(end_marker, start + 1)
if start < 0 or end < 0 or handler.find(start_marker, start + 1) >= 0:
    raise SystemExit(
        "SALES_AI_INTEGRATION_FAILED:handlers/clientplatform_sales.py:section_markers"
    )
encoded = Path(".github/sales-ai-v3-handler-fragment.b64").read_text(encoding="ascii")
fragment = base64.b64decode(encoded, validate=True).decode("utf-8")
fragment_end = fragment.find(end_marker)
if fragment_end < 0:
    raise SystemExit("SALES_AI_INTEGRATION_FAILED:handler_fragment:end_marker")
fragment = fragment[:fragment_end].rstrip() + "\n\n\n"
handler_path.write_text(handler[:start] + fragment + handler[end:], encoding="utf-8")
