from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.domain.partners import (
    ContactBasis,
    PartnerCandidateStatus,
    PartnerChannel,
    PartnerInvariantViolation,
)
from clientplatform.integrations.partner_discovery import PartnerDiscoveryUnavailable
from handlers import clientplatform_partner_growth as growth
from handlers import clientplatform_partner_materials as materials


async def _inline_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def _callback(data: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def _message(text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def _stats() -> SimpleNamespace:
    return SimpleNamespace(
        campaigns=2,
        candidates=4,
        contacted=3,
        replies=2,
        accepted=1,
    )


class PartnerGrowthSurfaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.actor = SimpleNamespace(user_id=101, business_id="business")
        self.output = SimpleNamespace(answer=AsyncMock())

    def _base_patches(self):
        return (
            patch.object(growth.asyncio, "to_thread", new=_inline_to_thread),
            patch.object(growth, "_actor", new=AsyncMock(return_value=self.actor)),
            patch.object(growth.control, "_callback_message", return_value=self.output),
            patch.object(growth.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(growth.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(growth.control, "_uuid_token", side_effect=lambda value: value),
        )

    async def test_home_is_fail_closed_without_discovery_connection(self) -> None:
        callback = _callback()
        campaign = SimpleNamespace(id="campaign", name="Психологи")
        patches = self._base_patches()
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(growth, "list_partner_campaigns", return_value=[campaign]),
            patch.object(growth, "partner_stats", return_value=_stats()),
            patch.object(
                growth,
                "build_connected_partner_discovery",
                return_value=SimpleNamespace(configured=False),
            ),
        ):
            await growth._render_home(callback, "business")

        callback.answer.assert_awaited_once()
        text = self.output.answer.await_args.args[0]
        self.assertIn("Live-поиск не настроен", text)
        rows = self.output.answer.await_args.kwargs["reply_markup"]
        callbacks = [value for row in rows for _label, value in row]
        self.assertNotIn("cpg:start:business", callbacks)
        self.assertIn("cpg:p:business:campaign", callbacks)

    async def test_home_exposes_discovery_only_when_configured(self) -> None:
        callback = _callback()
        patches = self._base_patches()
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(growth, "list_partner_campaigns", return_value=[]),
            patch.object(growth, "partner_stats", return_value=_stats()),
            patch.object(
                growth,
                "build_connected_partner_discovery",
                return_value=SimpleNamespace(configured=True),
            ),
        ):
            await growth._render_home(callback, "business")

        rows = self.output.answer.await_args.kwargs["reply_markup"]
        callbacks = [value for row in rows for _label, value in row]
        self.assertIn("cpg:start:business", callbacks)
        self.assertNotIn("Live-поиск не настроен", self.output.answer.await_args.args[0])

    async def test_campaign_renders_statuses_and_optional_callback_ack(self) -> None:
        callback = _callback()
        candidate = SimpleNamespace(
            id="candidate",
            name="Хороший партнёр",
            status=PartnerCandidateStatus.READY,
        )
        patches = self._base_patches()
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(growth, "list_partner_candidates", return_value=[candidate]),
            patch.object(growth, "partner_stats", return_value=_stats()),
        ):
            await growth._render_campaign(
                callback,
                business_token="business",
                campaign_token="campaign",
                answer_callback=False,
            )
        callback.answer.assert_not_awaited()
        text = self.output.answer.await_args.args[0]
        self.assertIn("Написали: 3", text)
        rows = self.output.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(rows[0][0][1], "cpg:c:business:candidate")

    async def test_candidate_with_permission_exposes_send_and_reply(self) -> None:
        callback = _callback()
        candidate = SimpleNamespace(
            id="candidate",
            campaign_id="campaign",
            name="Partner",
            status=PartnerCandidateStatus.REPLIED,
            source_url="https://example.test",
            first_contact_permitted=True,
        )
        view = SimpleNamespace(
            candidate=candidate,
            fit_total=91.5,
            latest_reply="Да, интересно",
            content=SimpleNamespace(outreach_message="Предложение"),
        )
        connection = SimpleNamespace(id="connection", label="Bot")
        patches = self._base_patches()
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(growth, "get_partner_candidate_view", return_value=view),
            patch.object(growth, "list_partner_send_connections", return_value=[connection]),
        ):
            await growth._render_candidate(
                callback,
                business_token="business",
                candidate_token="candidate",
            )
        text = self.output.answer.await_args.args[0]
        self.assertIn("Последний ответ", text)
        rows = self.output.answer.await_args.kwargs["reply_markup"]
        callbacks = [value for row in rows for _label, value in row]
        self.assertIn("cpg:s:business:candidate", callbacks)

    async def test_email_candidate_with_permission_uses_email_send_route(self) -> None:
        callback = _callback()
        candidate = SimpleNamespace(
            id="candidate",
            campaign_id="campaign",
            name="Partner",
            status=PartnerCandidateStatus.READY,
            source_url="https://example.test",
            channel=PartnerChannel.EMAIL,
            contact_basis=ContactBasis.OPTED_IN,
            first_contact_permitted=True,
        )
        view = SimpleNamespace(
            candidate=candidate,
            fit_total=88.0,
            latest_reply="",
            content=SimpleNamespace(outreach_message="Предложение"),
        )
        connection = SimpleNamespace(id="connection", label="SMTP")
        patches = self._base_patches()
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(growth, "get_partner_candidate_view", return_value=view),
            patch.object(growth, "list_partner_send_connections", return_value=[connection]),
        ):
            await growth._render_candidate(
                callback,
                business_token="business",
                candidate_token="candidate",
            )
        rows = self.output.answer.await_args.kwargs["reply_markup"]
        callbacks = [value for row in rows for _label, value in row]
        self.assertIn("cpg:se:business:candidate", callbacks)
        self.assertNotIn("cpg:s:business:candidate", callbacks)

    async def test_candidate_without_permission_requires_explicit_basis(self) -> None:
        callback = _callback()
        candidate = SimpleNamespace(
            id="candidate",
            campaign_id="campaign",
            name="Partner",
            status=PartnerCandidateStatus.READY,
            source_url="",
            first_contact_permitted=False,
        )
        view = SimpleNamespace(
            candidate=candidate,
            fit_total=50.0,
            latest_reply="",
            content=SimpleNamespace(outreach_message="Предложение"),
        )
        patches = self._base_patches()
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(growth, "get_partner_candidate_view", return_value=view),
            patch.object(growth, "list_partner_send_connections", return_value=[]),
        ):
            await growth._render_candidate(
                callback,
                business_token="business",
                candidate_token="candidate",
            )
        rows = self.output.answer.await_args.kwargs["reply_markup"]
        callbacks = [value for row in rows for _label, value in row]
        self.assertIn("cpg:a:business:candidate:o", callbacks)
        self.assertIn("cpg:a:business:candidate:r", callbacks)

    async def test_terminal_candidate_does_not_offer_contact_authorization(self) -> None:
        callback = _callback()
        candidate = SimpleNamespace(
            id="candidate",
            campaign_id="campaign",
            name="Partner",
            status=PartnerCandidateStatus.DO_NOT_CONTACT,
            source_url="",
            first_contact_permitted=False,
        )
        view = SimpleNamespace(
            candidate=candidate,
            fit_total=10.0,
            latest_reply="",
            content=SimpleNamespace(outreach_message="Предложение"),
        )
        patches = self._base_patches()
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(growth, "get_partner_candidate_view", return_value=view),
            patch.object(growth, "list_partner_send_connections", return_value=[]),
        ):
            await growth._render_candidate(
                callback,
                business_token="business",
                candidate_token="candidate",
            )
        rows = self.output.answer.await_args.kwargs["reply_markup"]
        callbacks = [value for row in rows for _label, value in row]
        self.assertFalse(any(value.startswith("cpg:a:") for value in callbacks))

    async def test_queue_selected_connection_surfaces_domain_rejection(self) -> None:
        callback = _callback()
        patches = self._base_patches()
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(
                growth,
                "queue_partner_outreach",
                side_effect=PartnerInvariantViolation("consent required"),
            ),
        ):
            await growth._queue_selected_connection(
                callback,
                business_token="business",
                candidate_token="candidate",
                connection_id="connection",
            )
        callback.answer.assert_awaited_once_with("consent required", show_alert=True)
        self.output.answer.assert_not_awaited()

    async def test_queue_selected_connection_reports_idempotent_dispatch(self) -> None:
        callback = _callback()
        dispatch = SimpleNamespace(status=SimpleNamespace(value="pending"))
        patches = self._base_patches()
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(growth, "queue_partner_outreach", return_value=dispatch),
        ):
            await growth._queue_selected_connection(
                callback,
                business_token="business",
                candidate_token="candidate",
                connection_id="connection",
            )
        callback.answer.assert_awaited_once_with("Поставлено в очередь")
        self.assertIn("Повторное нажатие не создаст дубль", self.output.answer.await_args.args[0])

    async def test_start_discovery_failure_is_fail_closed(self) -> None:
        callback = _callback("cpg:start:business")
        patches = self._base_patches()
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(
                growth,
                "start_connected_partner_campaign",
                side_effect=PartnerDiscoveryUnavailable("vk"),
            ),
        ):
            await growth.start_partner_growth(callback)
        callback.answer.assert_awaited_once_with("Ищу и оцениваю…")
        self.assertIn("фиктивным нулевым результатом", self.output.answer.await_args.args[0])

    async def test_start_discovery_success_does_not_send_contacts(self) -> None:
        callback = _callback("cpg:start:business")
        run = SimpleNamespace(
            discovered=7,
            prepared=3,
            campaign=SimpleNamespace(id="campaign"),
        )
        patches = self._base_patches()
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(growth, "start_connected_partner_campaign", return_value=run),
        ):
            await growth.start_partner_growth(callback)
        text = self.output.answer.await_args.args[0]
        self.assertIn("Найдено публичных источников: 7", text)
        self.assertIn("Контакты не отправлялись автоматически", text)

    async def test_rerun_failure_preserves_existing_candidates(self) -> None:
        callback = _callback("cpg:r:business:campaign")
        patches = self._base_patches()
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(
                growth,
                "rerun_connected_partner_campaign",
                side_effect=PartnerDiscoveryUnavailable("vk"),
            ),
            patch.object(growth, "_render_campaign", new=AsyncMock()) as render,
        ):
            await growth.rerun_partner_growth(callback)
        render.assert_not_awaited()
        self.assertIn("прежние кандидаты сохранены", self.output.answer.await_args.args[0])

    async def test_contact_authorization_records_explicit_basis_in_state(self) -> None:
        callback = _callback("cpg:a:business:candidate:o")
        state = SimpleNamespace(
            clear=AsyncMock(),
            update_data=AsyncMock(),
            set_state=AsyncMock(),
        )
        patches = self._base_patches()
        with patches[2], patches[3]:
            await growth.begin_partner_contact_authorization(callback, state)
        state.clear.assert_awaited_once()
        state.update_data.assert_awaited_once_with(
            partner_business_token="business",
            partner_candidate_token="candidate",
            partner_contact_basis="opted_in",
        )
        callback.answer.assert_awaited_once()

    async def test_save_contact_rejects_stale_state(self) -> None:
        message = _message("70001")
        state = SimpleNamespace(
            get_data=AsyncMock(return_value={}),
            clear=AsyncMock(),
        )
        await growth.save_partner_contact(message, state)
        state.clear.assert_awaited_once()
        self.assertIn("устарел", message.answer.await_args.args[0])

    async def test_save_contact_rejects_invalid_chat_without_clearing_state(self) -> None:
        message = _message("@username")
        state = SimpleNamespace(
            get_data=AsyncMock(
                return_value={
                    "partner_business_token": "business",
                    "partner_candidate_token": "candidate",
                    "partner_contact_basis": "opted_in",
                }
            ),
            clear=AsyncMock(),
        )
        with (
            patch.object(growth.asyncio, "to_thread", new=_inline_to_thread),
            patch.object(growth.control, "_actor", new=AsyncMock(return_value=self.actor)),
            patch.object(growth.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(
                growth,
                "authorize_partner_telegram_contact",
                side_effect=PartnerInvariantViolation("numeric chat id"),
            ),
        ):
            await growth.save_partner_contact(message, state)
        state.clear.assert_not_awaited()
        self.assertIn("numeric Telegram chat ID", message.answer.await_args.args[0])

    async def test_save_contact_success_clears_state(self) -> None:
        message = _message("70001")
        state = SimpleNamespace(
            get_data=AsyncMock(
                return_value={
                    "partner_business_token": "business",
                    "partner_candidate_token": "candidate",
                    "partner_contact_basis": "existing_relationship",
                }
            ),
            clear=AsyncMock(),
        )
        with (
            patch.object(growth.asyncio, "to_thread", new=_inline_to_thread),
            patch.object(growth.control, "_actor", new=AsyncMock(return_value=self.actor)),
            patch.object(growth.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(growth.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(growth, "authorize_partner_telegram_contact", return_value=None),
        ):
            await growth.save_partner_contact(message, state)
        state.clear.assert_awaited_once()
        self.assertIn("Контакт подтверждён", message.answer.await_args.args[0])

    async def test_send_requires_active_connection(self) -> None:
        callback = _callback("cpg:s:business:candidate")
        patches = self._base_patches()
        with (
            patches[0], patches[1],
            patch.object(growth, "list_partner_send_connections", return_value=[]),
        ):
            await growth.send_partner_outreach(callback)
        callback.answer.assert_awaited_once_with(
            "Нет активного Telegram bot connection",
            show_alert=True,
        )

    async def test_send_one_connection_queues_directly(self) -> None:
        callback = _callback("cpg:s:business:candidate")
        connection = SimpleNamespace(id="connection", label="Bot")
        queue = AsyncMock()
        patches = self._base_patches()
        with (
            patches[0], patches[1],
            patch.object(growth, "list_partner_send_connections", return_value=[connection]),
            patch.object(growth, "_queue_selected_connection", new=queue),
        ):
            await growth.send_partner_outreach(callback)
        queue.assert_awaited_once_with(
            callback,
            business_token="business",
            candidate_token="candidate",
            connection_id="connection",
        )

    async def test_email_send_one_connection_queues_directly(self) -> None:
        callback = _callback("cpg:se:business:candidate")
        connection = SimpleNamespace(id="connection", label="SMTP")
        queue = AsyncMock()
        patches = self._base_patches()
        with (
            patches[0], patches[1],
            patch.object(growth, "list_partner_send_connections", return_value=[connection]),
            patch.object(growth, "_queue_selected_connection", new=queue),
        ):
            await growth.send_partner_email_outreach(callback)
        queue.assert_awaited_once_with(
            callback,
            business_token="business",
            candidate_token="candidate",
            connection_id="connection",
        )

    async def test_send_multiple_connections_requires_explicit_selection(self) -> None:
        callback = _callback("cpg:s:business:candidate")
        connections = [
            SimpleNamespace(id="connection-a", label="Bot A"),
            SimpleNamespace(id="connection-b", label="Bot B"),
        ]
        patches = self._base_patches()
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch.object(growth, "list_partner_send_connections", return_value=connections),
        ):
            await growth.send_partner_outreach(callback)
        text = self.output.answer.await_args.args[0]
        self.assertIn("не выбирает его автоматически", text)
        rows = self.output.answer.await_args.kwargs["reply_markup"]
        callbacks = [value for row in rows for _label, value in row]
        self.assertIn("cpg:sc:business:candidate:connection-a", callbacks)
        self.assertIn("cpg:sc:business:candidate:connection-b", callbacks)

    async def test_explicit_connection_callback_uses_selected_id(self) -> None:
        callback = _callback("cpg:sc:business:candidate:connection")
        queue = AsyncMock()
        with (
            patch.object(growth.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(growth, "_queue_selected_connection", new=queue),
        ):
            await growth.send_partner_outreach_via_connection(callback)
        queue.assert_awaited_once_with(
            callback,
            business_token="business",
            candidate_token="candidate",
            connection_id="connection",
        )

    async def test_accept_and_do_not_contact_use_explicit_statuses(self) -> None:
        set_status = unittest.mock.MagicMock()
        render = AsyncMock()
        patches = self._base_patches()
        with (
            patches[0], patches[1], patches[4],
            patch.object(growth, "set_partner_candidate_status", new=set_status),
            patch.object(growth, "_render_candidate", new=render),
        ):
            await growth.accept_partner(_callback("cpg:ok:business:candidate"))
            await growth.do_not_contact_partner(_callback("cpg:no:business:candidate"))
        statuses = [call.kwargs["status"] for call in set_status.call_args_list]
        self.assertEqual(
            statuses,
            [PartnerCandidateStatus.ACCEPTED, PartnerCandidateStatus.DO_NOT_CONTACT],
        )
        self.assertEqual(render.await_count, 2)


class PartnerMaterialsSurfaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.actor = SimpleNamespace(user_id=101, business_id="business")
        self.output = SimpleNamespace(answer=AsyncMock())

    async def test_materials_lists_campaigns_and_navigation(self) -> None:
        callback = _callback("cpg:materials:business")
        campaigns = [SimpleNamespace(id="campaign", name="Партнёрская кампания")]
        with (
            patch.object(materials.asyncio, "to_thread", new=_inline_to_thread),
            patch.object(materials.control, "_actor", new=AsyncMock(return_value=self.actor)),
            patch.object(materials.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(materials.control, "_uuid_token", side_effect=lambda value: value),
            patch.object(materials.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(materials.control, "_callback_message", return_value=self.output),
            patch.object(materials, "list_partner_campaigns", return_value=campaigns),
        ):
            await materials.open_partner_materials(callback)
        callback.answer.assert_awaited_once()
        rows = self.output.answer.await_args.kwargs["reply_markup"]
        callbacks = [value for row in rows for _label, value in row]
        self.assertIn("cpg:mc:business:campaign", callbacks)
        self.assertIn("cpj:home:business", callbacks)

    async def test_campaign_materials_lists_candidate_links(self) -> None:
        callback = _callback("cpg:mc:business:campaign")
        candidates = [SimpleNamespace(id="candidate", name="Партнёр")]
        with (
            patch.object(materials.asyncio, "to_thread", new=_inline_to_thread),
            patch.object(materials.control, "_actor", new=AsyncMock(return_value=self.actor)),
            patch.object(materials.control, "_token_uuid", side_effect=lambda value: value),
            patch.object(materials.control, "_uuid_token", side_effect=lambda value: value),
            patch.object(materials.control, "_keyboard", side_effect=lambda rows: rows),
            patch.object(materials.control, "_callback_message", return_value=self.output),
            patch.object(materials, "list_partner_candidates", return_value=candidates),
        ):
            await materials.open_partner_campaign_materials(callback)
        callback.answer.assert_awaited_once()
        rows = self.output.answer.await_args.kwargs["reply_markup"]
        callbacks = [value for row in rows for _label, value in row]
        self.assertIn("cpg:l:business:candidate", callbacks)
        self.assertIn("cpg:materials:business", callbacks)


if __name__ == "__main__":
    unittest.main()
