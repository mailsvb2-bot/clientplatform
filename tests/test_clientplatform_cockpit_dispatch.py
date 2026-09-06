from __future__ import annotations

import importlib.util
import unittest
from unittest.mock import AsyncMock, patch

_RUNTIME_AVAILABLE = importlib.util.find_spec("aiogram") is not None

if _RUNTIME_AVAILABLE:
    from clientplatform.application.cockpit_action_routing import (
        build_cockpit_action_start_payload,
        build_cockpit_section_start_payload,
        parse_cockpit_action_start_payload,
    )
    from handlers import clientplatform_cockpit_dispatch as dispatch

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_LEAD = "44444444-4444-4444-8444-444444444444"


@unittest.skipUnless(_RUNTIME_AVAILABLE, "aiogram runtime dependency is not installed")
class CockpitDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_reuses_existing_section_sender(self) -> None:
        sender = AsyncMock()
        route = parse_cockpit_action_start_payload(
            build_cockpit_section_start_payload(business_id=_BUSINESS, section="sales")
        )
        self.assertIsNotNone(route)
        target = AsyncMock()
        with patch.object(dispatch.one_click, "send_one_click_section", sender):
            await dispatch.send_cockpit_action_route(target, user_id=202, route=route)
        sender.assert_awaited_once_with(
            target, user_id=202, business_id=_BUSINESS, section="sales"
        )

    async def test_dispatch_reuses_existing_sales_surfaces(self) -> None:
        cases = (
            ("sales_handoff", dispatch.sales, "send_sales_handoff_view"),
            (
                "sales_plan:55555555-5555-4555-8555-555555555555",
                dispatch.sales,
                "send_sales_work_view",
            ),
            (f"sales_lead:{_LEAD}", dispatch.sales_operations, "send_sales_lead_view"),
        )
        for action_key, owner, attr in cases:
            with self.subTest(action_key=action_key):
                route = parse_cockpit_action_start_payload(
                    build_cockpit_action_start_payload(
                        business_id=_BUSINESS, action_key=action_key
                    )
                )
                self.assertIsNotNone(route)
                target = AsyncMock()
                sender = AsyncMock()
                with patch.object(owner, attr, sender):
                    await dispatch.send_cockpit_action_route(
                        target, user_id=303, route=route
                    )
                kwargs = {"user_id": 303, "business_id": _BUSINESS}
                if route.lead_id is not None:
                    kwargs["lead_id"] = route.lead_id
                sender.assert_awaited_once_with(target, **kwargs)


if __name__ == "__main__":
    unittest.main()
