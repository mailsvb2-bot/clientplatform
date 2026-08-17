from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.domain.ad_connections import normalize_region_ids
from handlers import clientplatform_ad_connections as ad_handlers


class _State:
    def __init__(self, data: dict[str, object]) -> None:
        self.data = dict(data)
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **values):
        self.data.update(values)

    async def set_state(self, state):
        self.state = state


async def _direct_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


class AdvertisingRegionInputTests(unittest.TestCase):
    def test_common_city_names_and_goal_first_tokens_share_region_ids(self) -> None:
        cases = {
            "Москва": (213,),
            "  г.   Москва ": (213,),
            "moscow": (213,),
            "Нижний Новгород": (47,),
            "н. новгород": (47,),
            "nn": (47,),
            "Санкт-Петербург": (2,),
            "Санкт Петербург": (2,),
            "СПб": (2,),
            "spb": (2,),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_region_ids(value), expected)

    def test_names_numeric_ids_and_duplicates_can_be_combined(self) -> None:
        self.assertEqual(
            normalize_region_ids("Москва, Санкт-Петербург, 47, Москва"),
            (2, 47, 213),
        )

    def test_unknown_city_stays_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "supported region name"):
            normalize_region_ids("Неизвестный город")


class ManagedAdvertisingTelegramRegionTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_managed_flow_accepts_moscow_without_yandex_region_id(self) -> None:
        state = _State(
            {
                "business_id": "business-1",
                "business_token": "business-1",
                "promotion_campaign_id": "promotion-1",
                "connection_id": "connection-1",
                "source_url": "https://t.me/clientplatform_bot?start=safe",
            }
        )
        incoming = SimpleNamespace(
            text="Москва",
            from_user=SimpleNamespace(id=101),
            answer=AsyncMock(),
        )
        job = SimpleNamespace(
            id="job-1",
            region_ids=(213,),
            title="Свободное время",
            text="Запишитесь онлайн",
            source_url="https://t.me/clientplatform_bot?start=safe",
        )
        draft = SimpleNamespace(
            job=job,
            campaign_name="ClientPlatform · promotion-1",
        )

        with (
            patch.object(ad_handlers.asyncio, "to_thread", new=_direct_to_thread),
            patch.object(
                ad_handlers.control,
                "_actor",
                new=AsyncMock(return_value="actor"),
            ),
            patch.object(ad_handlers.control, "_user_id", return_value=101),
            patch.object(ad_handlers.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(
                ad_handlers,
                "create_managed_ad_publication_draft",
                return_value=draft,
            ) as managed_create,
        ):
            await ad_handlers.prepare_ad_publication(incoming, state)

        managed_create.assert_called_once_with(
            actor="actor",
            promotion_campaign_id="promotion-1",
            connection_id="connection-1",
            region_ids=(213,),
            source_url="https://t.me/clientplatform_bot?start=safe",
        )
        self.assertEqual(
            state.state,
            ad_handlers.AdConnectionState.confirming_publication,
        )
        self.assertIn("Регионы: 213", incoming.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
