from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import clientplatform_interaction_safety as safety


class FakeUser:
    def __init__(self, user_id: int = 101) -> None:
        self.id = user_id


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[tuple[str, dict[str, object]]] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        self.answers.append((text, dict(kwargs)))


class FakeCallback:
    def __init__(self, data: str, *, user_id: int = 101) -> None:
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage()
        self.answers: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def answer(self, *args: object, **kwargs: object) -> None:
        self.answers.append((args, dict(kwargs)))


class FakeState:
    def __init__(self) -> None:
        self.clear_count = 0

    async def clear(self) -> None:
        self.clear_count += 1


async def direct_to_thread(func, *args, **kwargs):
    return func(*args, **kwargs)


def access(business_id: str, name: str = "Сантехник"):
    return SimpleNamespace(business=SimpleNamespace(id=business_id, name=name))


class ClientPlatformBusinessArchiveSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.business_id = "business-1"
        self.owner = SimpleNamespace(role="owner")
        self.admin = SimpleNamespace(role="administrator")
        self.patchers = [
            patch.object(safety.asyncio, "to_thread", direct_to_thread),
            patch.object(safety.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(safety.control, "_uuid_token", side_effect=lambda value: value),
            patch.object(safety.control, "_callback_message", side_effect=lambda callback: callback.message),
            patch.object(safety.control, "_keyboard", side_effect=lambda rows: rows),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_owner_prompt_renders_explicit_confirmation_and_one_shot_cancel(self) -> None:
        callback = FakeCallback(f"cps:archive-prompt:{self.business_id}")
        state = FakeState()
        with (
            patch.object(safety.control, "_actor", AsyncMock(return_value=self.owner)),
            patch.object(
                safety,
                "list_accessible_businesses",
                return_value=[access(self.business_id)],
            ),
        ):
            await safety.confirm_business_archive(callback, state)

        self.assertEqual(1, state.clear_count)
        self.assertIn("Удалить бизнес «Сантехник»?", callback.message.answers[-1][0])
        keyboard = callback.message.answers[-1][1]["reply_markup"]
        self.assertEqual(
            [
                ("🗑 Да, удалить бизнес", f"cps:archive-confirm:{self.business_id}"),
                ("Отмена", f"cps:archive-cancel:{self.business_id}"),
            ],
            [button for row in keyboard for button in row],
        )
        self.assertIn("cps:archive-cancel:", safety._ONE_SHOT_PREFIXES)

    async def test_prompt_fails_closed_for_non_owner_stale_and_missing_actor(self) -> None:
        callback = FakeCallback(f"cps:archive-prompt:{self.business_id}")
        with patch.object(safety.control, "_actor", AsyncMock(return_value=self.admin)):
            await safety.confirm_business_archive(callback, FakeState())
        self.assertTrue(callback.answers[-1][1]["show_alert"])
        self.assertIn("только владелец", str(callback.answers[-1][0][0]))

        stale = FakeCallback(f"cps:archive-prompt:{self.business_id}")
        with (
            patch.object(safety.control, "_actor", AsyncMock(return_value=self.owner)),
            patch.object(safety, "list_accessible_businesses", return_value=[]),
        ):
            await safety.confirm_business_archive(stale, FakeState())
        self.assertTrue(stale.answers[-1][1]["show_alert"])
        self.assertIn("уже недоступен", str(stale.answers[-1][0][0]))

        missing = FakeCallback(f"cps:archive-prompt:{self.business_id}")
        with patch.object(
            safety.control,
            "_actor",
            AsyncMock(side_effect=safety.control.TenancyError("gone")),
        ):
            await safety.confirm_business_archive(missing, FakeState())
        self.assertTrue(missing.answers[-1][1]["show_alert"])
        self.assertIn("уже недоступен", str(missing.answers[-1][0][0]))

    async def test_archive_confirm_fails_closed_for_non_owner_and_tenancy_error(self) -> None:
        denied = FakeCallback(f"cps:archive-confirm:{self.business_id}")
        with patch.object(safety.control, "_actor", AsyncMock(return_value=self.admin)):
            await safety.archive_business_from_settings(denied, FakeState())
        self.assertTrue(denied.answers[-1][1]["show_alert"])
        self.assertIn("только владелец", str(denied.answers[-1][0][0]))

        failed = FakeCallback(f"cps:archive-confirm:{self.business_id}")
        with (
            patch.object(safety.control, "_actor", AsyncMock(return_value=self.owner)),
            patch.object(
                safety,
                "archive_business",
                side_effect=safety.control.TenancyError("stale"),
            ),
        ):
            await safety.archive_business_from_settings(failed, FakeState())
        self.assertTrue(failed.answers[-1][1]["show_alert"])
        self.assertIn("Не удалось удалить бизнес", str(failed.answers[-1][0][0]))

    async def test_archive_confirm_success_routes_none_one_and_many_remaining(self) -> None:
        archived = SimpleNamespace(name="Сантехник")

        none_callback = FakeCallback(f"cps:archive-confirm:{self.business_id}")
        with (
            patch.object(safety.control, "_actor", AsyncMock(return_value=self.owner)),
            patch.object(safety, "archive_business", return_value=archived),
            patch.object(safety, "list_accessible_businesses", return_value=[]),
        ):
            await safety.archive_business_from_settings(none_callback, FakeState())
        self.assertIn("История сохранена", none_callback.message.answers[0][0])
        self.assertIn("Активных бизнесов больше нет", none_callback.message.answers[1][0])
        self.assertEqual(
            [[("➕ Создать бизнес", "cps:start")]],
            none_callback.message.answers[1][1]["reply_markup"],
        )

        one_callback = FakeCallback(f"cps:archive-confirm:{self.business_id}")
        remaining = [access("business-2", "Основной бизнес")]
        resume = AsyncMock()
        with (
            patch.object(safety.control, "_actor", AsyncMock(return_value=self.owner)),
            patch.object(safety, "archive_business", return_value=archived),
            patch.object(safety, "list_accessible_businesses", return_value=remaining),
            patch.object(safety.control, "_resume_business", resume),
        ):
            await safety.archive_business_from_settings(one_callback, FakeState())
        resume.assert_awaited_once()
        self.assertEqual("business-2", resume.await_args.kwargs["business_id"])

        many_callback = FakeCallback(f"cps:archive-confirm:{self.business_id}")
        many = [access("business-2", "A"), access("business-3", "B")]
        with (
            patch.object(safety.control, "_actor", AsyncMock(return_value=self.owner)),
            patch.object(safety, "archive_business", return_value=archived),
            patch.object(safety, "list_accessible_businesses", return_value=many),
            patch.object(
                safety.control,
                "_business_choice_keyboard",
                return_value="business-choice",
            ),
        ):
            await safety.archive_business_from_settings(many_callback, FakeState())
        self.assertIn("Выберите бизнес", many_callback.message.answers[-1][0])
        self.assertEqual(
            "business-choice",
            many_callback.message.answers[-1][1]["reply_markup"],
        )

    async def test_cancel_clears_state_and_returns_to_settings(self) -> None:
        callback = FakeCallback(f"cps:archive-cancel:{self.business_id}")
        state = FakeState()
        await safety.cancel_business_archive(callback, state)

        self.assertEqual(1, state.clear_count)
        self.assertIn("Удаление отменено", str(callback.answers[-1][0][0]))
        self.assertEqual("Удаление бизнеса отменено.", callback.message.answers[-1][0])
        self.assertEqual(
            [[("⚙️ Настройки бизнеса", f"cpo:settings:{self.business_id}")]],
            callback.message.answers[-1][1]["reply_markup"],
        )


if __name__ == "__main__":
    unittest.main()
