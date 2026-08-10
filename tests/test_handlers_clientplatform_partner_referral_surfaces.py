from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from clientplatform.application.partner_attribution import PartnerAttributionWriteError
from clientplatform.domain.partners import PartnerNotFound
from handlers import clientplatform_partner_referral as referral


async def _inline_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def _state() -> SimpleNamespace:
    return SimpleNamespace(clear=AsyncMock())


def _message() -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=101, username="visitor", full_name="Visitor Name"),
        answer=AsyncMock(),
    )


def _callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


class PartnerReferralLinkTests(unittest.IsolatedAsyncioTestCase):
    def test_payload_and_deep_link_validate_opaque_token(self) -> None:
        self.assertEqual(referral.partner_start_payload("opaque_token-1"), "cpg_opaque_token-1")
        self.assertEqual(
            referral.partner_deep_link("@clientplatform_bot", "opaque_token-1"),
            "https://t.me/clientplatform_bot?start=cpg_opaque_token-1",
        )
        for bad in ("", "bad:token", "x" * 129):
            with self.subTest(bad=bad[:10]):
                with self.assertRaises(ValueError):
                    referral.partner_start_payload(bad)
        with self.assertRaises(ValueError):
            referral.partner_deep_link("", "opaque")

    async def test_bot_username_requires_public_username(self) -> None:
        good = SimpleNamespace(
            bot=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="bot_name")))
        )
        self.assertEqual(await referral._bot_username(good), "bot_name")
        bad = SimpleNamespace(
            bot=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="")))
        )
        with self.assertRaises(RuntimeError):
            await referral._bot_username(bad)

    def test_start_payload_parser_distinguishes_foreign_invalid_and_valid(self) -> None:
        message = _message()
        with patch.object(referral.control, "_start_payload", return_value="cp_other"):
            self.assertIsNone(referral._referral_token_from_start(message))
        with patch.object(referral.control, "_start_payload", return_value="cpg_bad:token"):
            self.assertEqual(referral._referral_token_from_start(message), "")
        with patch.object(referral.control, "_start_payload", return_value="cpg_valid_token"):
            self.assertEqual(referral._referral_token_from_start(message), "valid_token")


class PartnerReferralStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_managed_bot_context_and_foreign_start_bypass_partner_route(self) -> None:
        message = _message()
        state = _state()
        self.assertFalse(
            await referral.dispatch_partner_referral_start(
                message,
                state,
                user_id=101,
                managed_bot_business_id="business",
            )
        )
        with patch.object(referral.control, "_start_payload", return_value="other"):
            self.assertFalse(
                await referral.dispatch_partner_referral_start(
                    message,
                    state,
                    user_id=101,
                    managed_bot_business_id=None,
                )
            )
        state.clear.assert_not_awaited()

    async def test_invalid_and_expired_referral_links_are_handled(self) -> None:
        invalid_message = _message()
        invalid_state = _state()
        with patch.object(referral.control, "_start_payload", return_value="cpg_bad:token"):
            handled = await referral.dispatch_partner_referral_start(
                invalid_message,
                invalid_state,
                user_id=101,
                managed_bot_business_id=None,
            )
        self.assertTrue(handled)
        invalid_state.clear.assert_awaited_once()
        self.assertIn("недействительна", invalid_message.answer.await_args.args[0])

        expired_message = _message()
        expired_state = _state()
        with (
            patch.object(referral.asyncio, "to_thread", new=_inline_to_thread),
            patch.object(referral.control, "_start_payload", return_value="cpg_expired"),
            patch.object(referral, "resolve_partner_referral", side_effect=PartnerNotFound("gone")),
        ):
            handled = await referral.dispatch_partner_referral_start(
                expired_message,
                expired_state,
                user_id=101,
                managed_bot_business_id=None,
            )
        self.assertTrue(handled)
        expired_state.clear.assert_awaited_once()
        self.assertIn("больше не активна", expired_message.answer.await_args.args[0])

    async def test_staff_preview_does_not_create_customer_or_attribution(self) -> None:
        message = _message()
        state = _state()
        landing = SimpleNamespace(business_id="business")
        connect = MagicMock()
        record = MagicMock()
        with (
            patch.object(referral.asyncio, "to_thread", new=_inline_to_thread),
            patch.object(referral.control, "_start_payload", return_value="cpg_token"),
            patch.object(referral.control, "_uuid_token", side_effect=lambda value: value),
            patch.object(referral.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(referral, "resolve_partner_referral", return_value=landing),
            patch.object(referral, "is_public_storefront_staff", return_value=True),
            patch.object(referral, "connect_public_storefront_customer", new=connect),
            patch.object(referral, "record_partner_referral_open", new=record),
        ):
            handled = await referral.dispatch_partner_referral_start(
                message,
                state,
                user_id=101,
                managed_bot_business_id=None,
            )
        self.assertTrue(handled)
        connect.assert_not_called()
        record.assert_not_called()
        self.assertIn("не создаёт клиентскую карточку", message.answer.await_args.args[0])

    async def test_customer_without_slots_reaches_storefront_even_if_open_metric_fails(self) -> None:
        message = _message()
        state = _state()
        landing = SimpleNamespace(business_id="business")
        link = SimpleNamespace(business_name="Business")
        with (
            patch.object(referral.asyncio, "to_thread", new=_inline_to_thread),
            patch.object(referral.control, "_start_payload", return_value="cpg_token"),
            patch.object(referral, "resolve_partner_referral", return_value=landing),
            patch.object(referral, "is_public_storefront_staff", return_value=False),
            patch.object(referral, "connect_public_storefront_customer", return_value=link),
            patch.object(
                referral,
                "record_partner_referral_open",
                side_effect=PartnerAttributionWriteError("analytics unavailable"),
            ),
            patch.object(referral.control, "list_customer_booking_slots", return_value=[]),
            patch.object(referral.log, "error") as log_error,
        ):
            handled = await referral.dispatch_partner_referral_start(
                message,
                state,
                user_id=101,
                managed_bot_business_id=None,
            )
        self.assertTrue(handled)
        state.clear.assert_awaited_once()
        log_error.assert_called_once_with("partner_referral_open_record_failed")
        self.assertIn("Свободного времени сейчас нет", message.answer.await_args.args[0])

    async def test_unexpected_open_metric_runtime_error_is_not_silenced(self) -> None:
        message = _message()
        state = _state()
        landing = SimpleNamespace(business_id="business")
        link = SimpleNamespace(business_name="Business")
        with (
            patch.object(referral.asyncio, "to_thread", new=_inline_to_thread),
            patch.object(referral.control, "_start_payload", return_value="cpg_token"),
            patch.object(referral, "resolve_partner_referral", return_value=landing),
            patch.object(referral, "is_public_storefront_staff", return_value=False),
            patch.object(referral, "connect_public_storefront_customer", return_value=link),
            patch.object(
                referral,
                "record_partner_referral_open",
                side_effect=RuntimeError("programming defect"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "programming defect"):
                await referral.dispatch_partner_referral_start(
                    message,
                    state,
                    user_id=101,
                    managed_bot_business_id=None,
                )

    async def test_customer_with_slots_gets_only_bookable_callbacks(self) -> None:
        message = _message()
        state = _state()
        landing = SimpleNamespace(business_id="business")
        link = SimpleNamespace(business_name="Business")
        slot = SimpleNamespace(
            offering_title="Consultation",
            local_start="10.08 12:00",
            slot=SimpleNamespace(id="slot", duration_minutes=60),
        )
        with (
            patch.object(referral.asyncio, "to_thread", new=_inline_to_thread),
            patch.object(referral.control, "_start_payload", return_value="cpg_token"),
            patch.object(referral.control, "_uuid_token", side_effect=lambda value: value),
            patch.object(referral.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(referral, "resolve_partner_referral", return_value=landing),
            patch.object(referral, "is_public_storefront_staff", return_value=False),
            patch.object(referral, "connect_public_storefront_customer", return_value=link),
            patch.object(referral, "record_partner_referral_open", return_value=True),
            patch.object(referral.control, "list_customer_booking_slots", return_value=[slot]),
        ):
            handled = await referral.dispatch_partner_referral_start(
                message,
                state,
                user_id=101,
                managed_bot_business_id=None,
            )
        self.assertTrue(handled)
        text = message.answer.await_args.args[0]
        self.assertIn("Результат партнёрства будет засчитан только после успешной записи", text)
        rows = message.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(rows[0][0][1], "cpg:b:token:slot")


class PartnerReferralMaterialAndBookingTests(unittest.IsolatedAsyncioTestCase):
    async def test_material_builds_individual_share_link(self) -> None:
        callback = _callback("cpg:l:business:candidate")
        output = SimpleNamespace(answer=AsyncMock())
        view = SimpleNamespace(
            candidate=SimpleNamespace(referral_token="opaque_token"),
            content=SimpleNamespace(ready_post="Готовый пост"),
        )
        with (
            patch.object(referral.asyncio, "to_thread", new=_inline_to_thread),
            patch.object(referral.control, "_actor", new=AsyncMock(return_value=object())),
            patch.object(referral.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(referral.control, "_callback_message", return_value=output),
            patch.object(referral, "get_partner_candidate_view", return_value=view),
            patch.object(referral, "_bot_username", new=AsyncMock(return_value="client_bot")),
        ):
            await referral.show_partner_material(callback)
        callback.answer.assert_awaited_once()
        text = output.answer.await_args.args[0]
        self.assertIn("https://t.me/client_bot?start=cpg_opaque_token", text)
        markup = output.answer.await_args.kwargs["reply_markup"]
        self.assertIn("t.me/share/url", markup.inline_keyboard[0][0].url)
        self.assertEqual(
            markup.inline_keyboard[1][0].callback_data,
            "cpg:c:business:candidate",
        )

    async def test_booking_unavailable_returns_alert_without_attribution(self) -> None:
        callback = _callback("cpg:b:token:slot")
        record = MagicMock()
        with (
            patch.object(referral.asyncio, "to_thread", new=_inline_to_thread),
            patch.object(referral, "resolve_partner_referral", side_effect=PartnerNotFound("gone")),
            patch.object(referral, "record_partner_referral_result", new=record),
        ):
            await referral.book_partner_referral(callback)
        callback.answer.assert_awaited_once_with(
            "Это время или партнёрская ссылка больше недоступны",
            show_alert=True,
        )
        record.assert_not_called()

    async def test_successful_booking_survives_attribution_failure(self) -> None:
        callback = _callback("cpg:b:token:slot")
        output = SimpleNamespace(answer=AsyncMock())
        landing = SimpleNamespace(business_id="business")
        slot = SimpleNamespace(id="slot", duration_minutes=60)
        claim = SimpleNamespace(
            slot=SimpleNamespace(
                slot=slot,
                offering_title="Consultation",
                local_start="10.08 12:00",
                business_name="Business",
            )
        )
        with (
            patch.object(referral.asyncio, "to_thread", new=_inline_to_thread),
            patch.object(referral.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(referral.control, "_callback_message", return_value=output),
            patch.object(referral, "resolve_partner_referral", return_value=landing),
            patch.object(referral.control, "book_customer_slot", return_value=claim),
            patch.object(
                referral,
                "record_partner_referral_result",
                side_effect=PartnerAttributionWriteError("analytics unavailable"),
            ),
            patch.object(referral.log, "error") as log_error,
        ):
            await referral.book_partner_referral(callback)
        callback.answer.assert_awaited_once_with("Запись подтверждена")
        log_error.assert_called_once_with("partner_referral_result_record_failed")
        self.assertIn("✅ Вы записаны", output.answer.await_args.args[0])

    async def test_successful_booking_schedules_reminders_and_calendar(self) -> None:
        callback = _callback("cpg:b:token:slot")
        output = SimpleNamespace(answer=AsyncMock(), answer_document=AsyncMock())
        landing = SimpleNamespace(business_id="business")
        slot = SimpleNamespace(
            id="slot",
            business_id="business",
            starts_at="2026-08-10T12:00:00+00:00",
            ends_at="2026-08-10T13:00:00+00:00",
            duration_minutes=60,
        )
        claim = SimpleNamespace(
            slot=SimpleNamespace(
                slot=slot,
                offering_title="Consultation",
                local_start="10.08 12:00",
                business_name="Business",
            )
        )
        schedule = MagicMock()
        with (
            patch.object(referral.asyncio, "to_thread", new=_inline_to_thread),
            patch.object(referral.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(referral.control, "_callback_message", return_value=output),
            patch.object(referral, "resolve_partner_referral", return_value=landing),
            patch.object(referral.control, "book_customer_slot", return_value=claim),
            patch.object(referral, "record_partner_referral_result", return_value=True),
            patch.object(referral.control, "schedule_booking_reminders", new=schedule),
            patch.object(referral.control, "booking_calendar_ics", return_value=b"BEGIN:VCALENDAR"),
            patch.object(referral.control, "booking_calendar_filename", return_value="booking.ics"),
            patch.object(referral.control, "google_calendar_url", return_value="https://calendar.test/event"),
        ):
            await referral.book_partner_referral(callback)
        schedule.assert_called_once()
        output.answer_document.assert_awaited_once()
        markup = output.answer_document.await_args.kwargs["reply_markup"]
        self.assertEqual(
            markup.inline_keyboard[0][0].url,
            "https://calendar.test/event",
        )


if __name__ == "__main__":
    unittest.main()
