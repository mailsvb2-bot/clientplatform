from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.domain.creative_generation import (
    CreativeGenerationReceipt,
    CreativeGenerationReceiptStatus,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied
from handlers import clientplatform_creative_studio as creative
from handlers import clientplatform_one_click_experience as one_click

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_MEMBER = "22222222-2222-4222-8222-222222222222"
_TOKEN = "ERERERERQRGBEREREREREQ"
_RECEIPT = "66666666-6666-4666-8666-666666666666"
_RECEIPT_TOKEN = creative.control._uuid_token(_RECEIPT)


def actor(role: PlatformRole = PlatformRole.OWNER) -> TenantContext:
    return TenantContext(
        business_id=_BUSINESS,
        user_id=101,
        membership_id=_MEMBER,
        role=role,
    )


def labels(rows):
    return [label for row in rows for label, _callback in row]


def receipt(
    *,
    status: CreativeGenerationReceiptStatus,
    source_job_id: str = "",
    request_text: str = "calm office",
    delivery_claimed_at: str = "",
):
    return CreativeGenerationReceipt(
        id=_RECEIPT,
        business_id=_BUSINESS,
        created_by_member_id=_MEMBER,
        request_text=request_text,
        brand_context="Tone: calm, human.",
        country_code="RU",
        provider_payload_json='{"version":1,"brief":"frozen"}',
        idempotency_key="clientplatform:owner-image:stable",
        source_job_id=source_job_id,
        status=status,
        created_at="2026-09-09T20:00:00+00:00",
        updated_at="2026-09-09T20:00:00+00:00",
        delivery_claimed_at=delivery_claimed_at,
    )


class FakeState:
    def __init__(self, data=None) -> None:
        self.data = dict(data or {})
        self.state = None
        self.clear_count = 0

    async def get_data(self):
        return dict(self.data)

    async def set_data(self, data):
        self.data = dict(data)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.clear_count += 1
        self.data.clear()


def outbound():
    return SimpleNamespace(text=None, answer=AsyncMock(), answer_photo=AsyncMock())


def callback(data: str, target=None):
    target = target or outbound()
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
        message=target,
    )


def job(*, status: str = "running", asset_ready: bool = False, job_id: str = "job-1"):
    return SimpleNamespace(id=job_id, status=status, asset_ready=asset_ready)


async def direct(function, *args, **kwargs):
    return function(*args, **kwargs)


class CreativeDiscoverabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_creative_is_second_level_owner_action_not_extra_home_primary(self) -> None:
        owner_rows = one_click._more_rows(_TOKEN, actor())
        self.assertIn("🎨 Создать картинку", labels(owner_rows))
        home_rows = one_click._home_keyboard(_BUSINESS).inline_keyboard
        self.assertNotIn("🎨 Создать картинку", [b.text for row in home_rows for b in row])

    def test_content_surface_exposes_same_creative_entry(self) -> None:
        rows, help_lines = one_click._content_tools_rows(_TOKEN, actor())
        self.assertIn("🎨 Создать картинку", labels(rows))
        self.assertTrue(any("изображение" in line for line in help_lines))

    def test_roles_without_promotion_management_do_not_get_paid_creative_entry(self) -> None:
        rows = one_click._more_rows(_TOKEN, actor(PlatformRole.ANALYST))
        self.assertNotIn("🎨 Создать картинку", labels(rows))
        with self.assertRaises(TenantPermissionDenied):
            actor(PlatformRole.ANALYST).assert_can_manage_promotions()

    async def test_existing_provider_job_is_polled_not_resubmitted_after_restart(self) -> None:
        current = receipt(
            status=CreativeGenerationReceiptStatus.RUNNING,
            source_job_id="provider-job-1",
        )
        job = SimpleNamespace(id="provider-job-1", status="running", asset_ready=False)
        with (
            patch.object(
                creative,
                "begin_creative_generation_submission",
                return_value=current,
            ),
            patch.object(creative, "poll_ad_visual", return_value=job) as poll,
            patch.object(
                creative,
                "remember_creative_generation_job",
                return_value=current,
            ),
            patch.object(creative, "create_business_image_from_frozen_payload") as create,
        ):
            recovered, provider_job = await creative._submit_or_recover(actor(), current)
        self.assertIs(recovered, current)
        self.assertIs(provider_job, job)
        poll.assert_called_once_with(job_id="provider-job-1", scope_id=_BUSINESS)
        create.assert_not_called()

    async def test_prepared_receipt_reuses_durable_idempotency_key(self) -> None:
        prepared = receipt(status=CreativeGenerationReceiptStatus.PREPARED)
        submitting = receipt(status=CreativeGenerationReceiptStatus.SUBMITTING)
        job = SimpleNamespace(id="provider-job-2", status="queued", asset_ready=False)
        queued = receipt(
            status=CreativeGenerationReceiptStatus.QUEUED,
            source_job_id="provider-job-2",
        )
        with (
            patch.object(
                creative,
                "begin_creative_generation_submission",
                return_value=submitting,
            ),
            patch.object(creative, "create_business_image_from_frozen_payload", return_value=job) as create,
            patch.object(
                creative,
                "remember_creative_generation_job",
                return_value=queued,
            ),
        ):
            saved, provider_job = await creative._submit_or_recover(actor(), prepared)
        self.assertIs(saved, queued)
        self.assertIs(provider_job, job)
        self.assertEqual(
            create.call_args.kwargs["idempotency_key"], submitting.idempotency_key
        )
        self.assertEqual(
            create.call_args.kwargs["provider_payload_json"],
            submitting.provider_payload_json,
        )

    def test_delivery_materializes_into_temporary_directory(self) -> None:
        source = Path("handlers/clientplatform_creative_studio.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("TemporaryDirectory", source)
        self.assertIn("output_dir=directory", source)
        self.assertIn("mark_creative_generation_delivered", source)

    def test_mini_app_navigation_explains_images_only_to_authorized_roles(self) -> None:
        from clientplatform.application.cockpit import cockpit_navigation

        owner_navigation = {item.id: item for item in cockpit_navigation(actor())}
        self.assertIn("визуалов", owner_navigation["growth"].summary)
        self.assertIn("создание картинок", owner_navigation["content"].summary)
        restricted = {
            item.id: item
            for item in cockpit_navigation(actor(PlatformRole.ANALYST))
        }
        self.assertNotIn("картин", restricted["growth"].summary.casefold())
        self.assertNotIn("картин", restricted["content"].summary.casefold())

    def test_creative_menu_covers_new_prepared_running_and_succeeded_states(self) -> None:
        self.assertIn(
            "✨ Создать картинку",
            [b.text for r in creative._menu_rows(_TOKEN).inline_keyboard for b in r],
        )
        prepared = creative._menu_rows(
            _TOKEN, receipt(status=CreativeGenerationReceiptStatus.PREPARED)
        )
        prepared_labels = [b.text for r in prepared.inline_keyboard for b in r]
        self.assertIn("🔄 Продолжить создание", prepared_labels)
        self.assertIn("✏️ Изменить описание", prepared_labels)
        succeeded = creative._menu_rows(
            _TOKEN,
            receipt(
                status=CreativeGenerationReceiptStatus.SUCCEEDED,
                source_job_id="provider-job-1",
            ),
        )
        self.assertIn(
            "✅ Получить готовую картинку",
            [b.text for r in succeeded.inline_keyboard for b in r],
        )
        running = creative._menu_rows(
            _TOKEN,
            receipt(
                status=CreativeGenerationReceiptStatus.RUNNING,
                source_job_id="provider-job-1",
            ),
        )
        callbacks = [b.callback_data for r in running.inline_keyboard for b in r]
        self.assertIn(f"cpc:check:{_TOKEN}:{_RECEIPT_TOKEN}", callbacks)

    async def test_active_receipt_uses_canonical_application_boundary(self) -> None:
        expected = receipt(status=CreativeGenerationReceiptStatus.PREPARED)
        to_thread = AsyncMock(return_value=expected)
        with patch.object(creative.asyncio, "to_thread", new=to_thread):
            self.assertIs(await creative._active(actor()), expected)
        self.assertIs(to_thread.await_args.args[0], creative.get_active_creative_generation)

    async def test_menu_copy_reflects_absent_prepared_and_running_generation(self) -> None:
        target = outbound()
        cases = [
            (None, "Опишите картинку"),
            (
                receipt(status=CreativeGenerationReceiptStatus.PREPARED),
                "Платный AI-вызов ещё не начинался",
            ),
            (
                receipt(
                    status=CreativeGenerationReceiptStatus.RUNNING,
                    source_job_id="provider-job-1",
                ),
                "незавершённая генерация",
            ),
        ]
        for current, phrase in cases:
            target.answer.reset_mock()
            with (
                patch.object(creative.control, "_actor", new=AsyncMock(return_value=actor())),
                patch.object(creative, "_active", new=AsyncMock(return_value=current)),
                patch.object(creative.control, "_uuid_token", return_value=_TOKEN),
            ):
                await creative.send_creative_studio_menu(
                    target,
                    user_id=101,
                    business_id=_BUSINESS,
                )
            self.assertIn(phrase, target.answer.await_args.args[0])

    async def test_open_creative_studio_authorizes_and_denies_role_failures(self) -> None:
        target = outbound()
        cb = callback(f"cpc:open:{_TOKEN}", target)
        state = FakeState({"old": "wizard"})
        with (
            patch.object(creative, "_actor_for_callback", new=AsyncMock(return_value=actor())),
            patch.object(creative.control, "_callback_message", return_value=target),
            patch.object(creative, "send_creative_studio_menu", new=AsyncMock()) as send,
        ):
            await creative.open_creative_studio(cb, state)
        self.assertEqual(state.clear_count, 1)
        send.assert_awaited_once_with(target, user_id=101, business_id=_BUSINESS)
        cb.answer.assert_awaited_once_with()

        denied = callback(f"cpc:open:{_TOKEN}", target)
        with patch.object(
            creative,
            "_actor_for_callback",
            new=AsyncMock(side_effect=TenantPermissionDenied("denied")),
        ):
            await creative.open_creative_studio(denied, FakeState())
        self.assertTrue(denied.answer.await_args.kwargs["show_alert"])

    async def test_new_prompt_blocks_running_generation_and_opens_when_safe(self) -> None:
        target = outbound()
        running = receipt(
            status=CreativeGenerationReceiptStatus.RUNNING,
            source_job_id="provider-job-1",
        )
        blocked = callback(f"cpc:new:{_TOKEN}", target)
        with (
            patch.object(creative, "_actor_for_callback", new=AsyncMock(return_value=actor())),
            patch.object(creative, "_active", new=AsyncMock(return_value=running)),
        ):
            await creative.ask_creative_prompt(blocked, FakeState())
        self.assertTrue(blocked.answer.await_args.kwargs["show_alert"])

        allowed = callback(f"cpc:new:{_TOKEN}", target)
        state = FakeState()
        target.answer.reset_mock()
        with (
            patch.object(creative, "_actor_for_callback", new=AsyncMock(return_value=actor())),
            patch.object(creative, "_active", new=AsyncMock(return_value=None)),
            patch.object(creative.control, "_callback_message", return_value=target),
        ):
            await creative.ask_creative_prompt(allowed, state)
        self.assertEqual(state.state, creative.ClientPlatformCreativeStudioState.waiting_prompt)
        self.assertEqual(state.data["creative_business_id"], _BUSINESS)
        self.assertIn("Какую картинку создать", target.answer.await_args.args[0])

    async def test_receive_prompt_rejects_stale_invalid_and_prepare_failure(self) -> None:
        target = outbound()
        stale = FakeState()
        await creative.receive_creative_prompt(target, stale)
        self.assertEqual(stale.clear_count, 1)
        self.assertIn("Сессия устарела", target.answer.await_args.args[0])

        state = FakeState(
            {"creative_business_id": _BUSINESS, "creative_business_token": _TOKEN}
        )
        target.answer.reset_mock()
        with (
            patch.object(creative.control, "_actor", new=AsyncMock(return_value=actor())),
            patch.object(creative.control, "_user_id", return_value=101),
            patch.object(
                creative,
                "normalize_business_image_request",
                side_effect=ValueError("bad"),
            ),
        ):
            await creative.receive_creative_prompt(target, state)
        self.assertIn("до 1500 символов", target.answer.await_args.args[0])

        state = FakeState(
            {"creative_business_id": _BUSINESS, "creative_business_token": _TOKEN}
        )
        target.answer.reset_mock()
        brand = SimpleNamespace(prompt_context=lambda: "Tone: calm")
        with (
            patch.object(creative.asyncio, "to_thread", new=direct),
            patch.object(creative.control, "_actor", new=AsyncMock(return_value=actor())),
            patch.object(creative.control, "_user_id", return_value=101),
            patch.object(creative, "load_goal_visual_brand", return_value=brand),
            patch.object(creative, "prepare_creative_generation", side_effect=OSError("db")),
        ):
            target.text = "calm office"
            await creative.receive_creative_prompt(target, state)
        self.assertIn("безопасно подготовить", target.answer.await_args.args[0])

    async def test_receive_prompt_confirms_paid_call_and_preserves_existing_request(self) -> None:
        target = outbound()
        target.text = " calm   office "
        brand = SimpleNamespace(prompt_context=lambda: "Tone: calm")
        state = FakeState(
            {"creative_business_id": _BUSINESS, "creative_business_token": _TOKEN}
        )
        prepared = receipt(status=CreativeGenerationReceiptStatus.PREPARED)
        with (
            patch.object(creative.asyncio, "to_thread", new=direct),
            patch.object(creative.control, "_actor", new=AsyncMock(return_value=actor())),
            patch.object(creative.control, "_user_id", return_value=101),
            patch.object(creative, "load_goal_visual_brand", return_value=brand),
            patch.object(creative, "prepare_creative_generation", return_value=prepared),
        ):
            await creative.receive_creative_prompt(target, state)
        self.assertEqual(state.clear_count, 1)
        self.assertIn("Платный вызов начнётся", target.answer.await_args.args[0])
        labels_ = [
            b.text
            for row in target.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("✅ Создать 1 картинку", labels_)

        target.answer.reset_mock()
        state = FakeState(
            {"creative_business_id": _BUSINESS, "creative_business_token": _TOKEN}
        )
        existing = receipt(
            status=CreativeGenerationReceiptStatus.RUNNING,
            source_job_id="provider-job-1",
            request_text="previous request",
        )
        with (
            patch.object(creative.asyncio, "to_thread", new=direct),
            patch.object(creative.control, "_actor", new=AsyncMock(return_value=actor())),
            patch.object(creative.control, "_user_id", return_value=101),
            patch.object(creative, "load_goal_visual_brand", return_value=brand),
            patch.object(creative, "prepare_creative_generation", return_value=existing),
        ):
            await creative.receive_creative_prompt(target, state)
        self.assertIn("уже есть незавершённая генерация", target.answer.await_args.args[0])

    async def test_finish_image_requires_ready_asset_and_cleans_temporary_materialization(
        self,
    ) -> None:
        target = outbound()
        cb = callback(f"cpc:check:{_TOKEN}", target)
        current = receipt(
            status=CreativeGenerationReceiptStatus.SUCCEEDED,
            source_job_id="provider-job-1",
        )
        self.assertFalse(
            await creative._finish_image(
                cb,
                actor=actor(),
                receipt=current,
                job=job(status="running"),
            )
        )

        materialized_dirs: list[str] = []

        def materialize(_job, *, output_dir=None):
            assert output_dir is not None
            materialized_dirs.append(output_dir)
            path = Path(output_dir) / "creative.jpg"
            path.write_bytes(b"image")
            return path

        with (
            patch.object(creative.asyncio, "to_thread", new=direct),
            patch.object(creative.control, "_callback_message", return_value=target),
            patch.object(creative, "materialize_ad_visual", side_effect=materialize),
            patch.object(
                creative,
                "claim_creative_generation_delivery",
                return_value=True,
            ) as claim,
            patch.object(
                creative,
                "mark_creative_generation_delivered",
                return_value=current,
            ) as delivered,
            patch.object(creative.control, "_uuid_token", return_value=_TOKEN),
        ):
            self.assertTrue(
                await creative._finish_image(
                    cb,
                    actor=actor(),
                    receipt=current,
                    job=job(status="succeeded", asset_ready=True),
                )
            )
        target.answer_photo.assert_awaited_once()
        claim.assert_called_once()
        delivered.assert_called_once()
        self.assertFalse(Path(materialized_dirs[0]).exists())
        self.assertIn("Можно сохранить", target.answer.await_args.args[0])

    async def test_finish_image_keeps_receipt_undelivered_when_materialization_fails(self) -> None:
        target = outbound()
        current = receipt(
            status=CreativeGenerationReceiptStatus.SUCCEEDED,
            source_job_id="provider-job-1",
        )
        with (
            patch.object(creative.asyncio, "to_thread", new=direct),
            patch.object(creative.control, "_callback_message", return_value=target),
            patch.object(
                creative,
                "materialize_ad_visual",
                side_effect=creative.VisualCreativeError("download"),
            ),
            patch.object(creative, "claim_creative_generation_delivery") as claim,
            patch.object(creative, "mark_creative_generation_delivered") as delivered,
            patch.object(creative.control, "_uuid_token", return_value=_TOKEN),
        ):
            self.assertTrue(
                await creative._finish_image(
                    callback(f"cpc:check:{_TOKEN}:{_RECEIPT_TOKEN}", target),
                    actor=actor(),
                    receipt=current,
                    job=job(status="succeeded", asset_ready=True),
                )
            )
        claim.assert_not_called()
        delivered.assert_not_called()
        self.assertIn("файл сейчас не удалось получить", target.answer.await_args.args[0])
        recovery = target.answer.await_args.kwargs["reply_markup"]
        recovery_labels = [b.text for row in recovery.inline_keyboard for b in row]
        self.assertIn("🔄 Проверить файл ещё раз", recovery_labels)
        self.assertIn("🗑 Завершить и создать новую", recovery_labels)

    async def test_ambiguous_telegram_delivery_is_fail_closed_until_explicit_redelivery(self) -> None:
        target = outbound()
        current = receipt(
            status=CreativeGenerationReceiptStatus.SUCCEEDED,
            source_job_id="provider-job-1",
        )

        def materialize(_job, *, output_dir=None):
            assert output_dir is not None
            path = Path(output_dir) / "creative.jpg"
            path.write_bytes(b"image")
            return path

        target.answer_photo.side_effect = creative.TelegramAPIError(
            method=SimpleNamespace(),
            message="ambiguous transport failure",
        )
        with (
            patch.object(creative.asyncio, "to_thread", new=direct),
            patch.object(creative.control, "_callback_message", return_value=target),
            patch.object(creative, "materialize_ad_visual", side_effect=materialize),
            patch.object(
                creative,
                "claim_creative_generation_delivery",
                return_value=True,
            ) as claim,
            patch.object(creative, "mark_creative_generation_delivered") as delivered,
            patch.object(creative.control, "_uuid_token", return_value=_TOKEN),
        ):
            self.assertTrue(
                await creative._finish_image(
                    callback(f"cpc:check:{_TOKEN}:{_RECEIPT_TOKEN}", target),
                    actor=actor(),
                    receipt=current,
                    job=job(status="succeeded", asset_ready=True),
                )
            )
        claim.assert_called_once()
        delivered.assert_not_called()
        self.assertIn("Автоматический повтор заблокирован", target.answer.await_args.args[0])
        recovery = target.answer.await_args.kwargs["reply_markup"]
        recovery_labels = [b.text for row in recovery.inline_keyboard for b in row]
        self.assertIn("📤 Отправить ещё раз (возможен дубль)", recovery_labels)

    async def test_claimed_delivery_is_not_automatically_resent_after_restart(self) -> None:
        target = outbound()
        claimed = receipt(
            status=CreativeGenerationReceiptStatus.SUCCEEDED,
            source_job_id="provider-job-1",
            delivery_claimed_at="2026-09-10T09:00:00+00:00",
        )
        with (
            patch.object(creative, "_poll_existing", new=AsyncMock()) as poll,
            patch.object(creative.control, "_callback_message", return_value=target),
            patch.object(creative.control, "_uuid_token", return_value=_TOKEN),
        ):
            await creative._continue_generation(
                callback(f"cpc:check:{_TOKEN}:{_RECEIPT_TOKEN}", target),
                actor=actor(),
                receipt=claimed,
            )
        poll.assert_not_awaited()
        self.assertIn("Автоматический повтор", target.answer.await_args.args[0])

    async def test_abandon_and_explicit_redelivery_are_exact_receipt_actions(self) -> None:
        current = receipt(
            status=CreativeGenerationReceiptStatus.SUCCEEDED,
            source_job_id="provider-job-1",
            delivery_claimed_at="2026-09-10T09:00:00+00:00",
        )
        target = outbound()
        abandon_cb = callback(f"cpc:abandon:{_TOKEN}:{_RECEIPT_TOKEN}", target)
        with (
            patch.object(creative.asyncio, "to_thread", new=direct),
            patch.object(creative, "_actor_for_callback", new=AsyncMock(return_value=actor())),
            patch.object(creative, "_receipt_for_callback", new=AsyncMock(return_value=current)),
            patch.object(creative, "abandon_creative_generation", return_value=True) as abandon,
            patch.object(creative.control, "_callback_message", return_value=target),
        ):
            await creative.abandon_creative_image(abandon_cb, FakeState())
        abandon.assert_called_once_with(actor=actor(), receipt_id=_RECEIPT)
        self.assertIn("можно создать новую картинку", target.answer.await_args.args[0])

        target.answer.reset_mock()
        unclaimed = receipt(
            status=CreativeGenerationReceiptStatus.SUCCEEDED,
            source_job_id="provider-job-1",
        )
        redeliver_cb = callback(f"cpc:redeliver:{_TOKEN}:{_RECEIPT_TOKEN}", target)
        exact = AsyncMock(side_effect=[current, unclaimed])
        with (
            patch.object(creative.asyncio, "to_thread", new=direct),
            patch.object(creative, "_actor_for_callback", new=AsyncMock(return_value=actor())),
            patch.object(creative, "_receipt_for_callback", new=exact),
            patch.object(
                creative,
                "authorize_creative_generation_redelivery",
                return_value=True,
            ) as authorize,
            patch.object(creative, "_continue_generation", new=AsyncMock()) as resume,
        ):
            await creative.redeliver_creative_image(redeliver_cb, FakeState())
        authorize.assert_called_once_with(actor=actor(), receipt_id=_RECEIPT)
        resume.assert_awaited_once_with(redeliver_cb, actor=actor(), receipt=unclaimed)

    async def test_continue_generation_handles_provider_state_failed_pending_and_ready(
        self,
    ) -> None:
        target = outbound()
        cb = callback(f"cpc:generate:{_TOKEN}", target)
        prepared = receipt(status=CreativeGenerationReceiptStatus.PREPARED)
        with (
            patch.object(
                creative,
                "_submit_or_recover",
                new=AsyncMock(side_effect=creative.VisualCreativeError("down")),
            ),
            patch.object(creative.control, "_callback_message", return_value=target),
            patch.object(creative.control, "_uuid_token", return_value=_TOKEN),
        ):
            await creative._continue_generation(cb, actor=actor(), receipt=prepared)
        self.assertIn("Запрос сохранён", target.answer.await_args.args[0])

        target.answer.reset_mock()
        with (
            patch.object(
                creative,
                "_submit_or_recover",
                new=AsyncMock(side_effect=LookupError("gone")),
            ),
            patch.object(creative.control, "_callback_message", return_value=target),
            patch.object(creative.control, "_uuid_token", return_value=_TOKEN),
        ):
            await creative._continue_generation(cb, actor=actor(), receipt=prepared)
        self.assertIn("Состояние генерации изменилось", target.answer.await_args.args[0])

        failed = receipt(
            status=CreativeGenerationReceiptStatus.FAILED,
            source_job_id="provider-job-1",
        )
        target.answer.reset_mock()
        with (
            patch.object(
                creative,
                "_poll_existing",
                new=AsyncMock(return_value=(failed, job(status="failed"))),
            ),
            patch.object(creative.control, "_callback_message", return_value=target),
            patch.object(creative.control, "_uuid_token", return_value=_TOKEN),
        ):
            await creative._continue_generation(cb, actor=actor(), receipt=failed)
        self.assertIn("завершилась ошибкой", target.answer.await_args.args[0])

        running = receipt(
            status=CreativeGenerationReceiptStatus.RUNNING,
            source_job_id="provider-job-1",
        )
        target.answer.reset_mock()
        with (
            patch.object(
                creative,
                "_poll_existing",
                new=AsyncMock(return_value=(running, job(status="running"))),
            ),
            patch.object(creative, "_finish_image", new=AsyncMock(return_value=False)),
            patch.object(creative.control, "_callback_message", return_value=target),
            patch.object(creative.control, "_uuid_token", return_value=_TOKEN),
        ):
            await creative._continue_generation(cb, actor=actor(), receipt=running)
        self.assertIn("ещё создаётся", target.answer.await_args.args[0])

        target.answer.reset_mock()
        with (
            patch.object(
                creative,
                "_poll_existing",
                new=AsyncMock(return_value=(running, job(status="succeeded", asset_ready=True))),
            ),
            patch.object(creative, "_finish_image", new=AsyncMock(return_value=True)) as finish,
            patch.object(creative.control, "_uuid_token", return_value=_TOKEN),
        ):
            await creative._continue_generation(cb, actor=actor(), receipt=running)
        finish.assert_awaited_once()
        target.answer.assert_not_awaited()

    async def test_succeeded_job_without_ready_asset_offers_retry_or_abandon(self) -> None:
        target = outbound()
        succeeded = receipt(
            status=CreativeGenerationReceiptStatus.SUCCEEDED,
            source_job_id="provider-job-1",
        )
        with (
            patch.object(
                creative,
                "_poll_existing",
                new=AsyncMock(return_value=(succeeded, job(status="succeeded", asset_ready=False))),
            ),
            patch.object(creative.control, "_callback_message", return_value=target),
            patch.object(creative.control, "_uuid_token", return_value=_TOKEN),
        ):
            await creative._continue_generation(
                callback(f"cpc:check:{_TOKEN}:{_RECEIPT_TOKEN}", target),
                actor=actor(),
                receipt=succeeded,
            )
        self.assertIn("готовый файл пока недоступен", target.answer.await_args.args[0])
        recovery = target.answer.await_args.kwargs["reply_markup"]
        recovery_labels = [b.text for row in recovery.inline_keyboard for b in row]
        self.assertIn("🔄 Проверить файл ещё раз", recovery_labels)
        self.assertIn("🗑 Завершить и создать новую", recovery_labels)

    async def test_generate_and_check_callbacks_bind_exact_receipt_and_resume_safely(self) -> None:
        current = receipt(status=CreativeGenerationReceiptStatus.PREPARED)
        for handler, action, missing_text in (
            (creative.generate_creative_image, "generate", "устаревшему запросу"),
            (creative.check_creative_image, "check", "не найдена"),
        ):
            cb = callback(f"cpc:{action}:{_TOKEN}:{_RECEIPT_TOKEN}")
            with (
                patch.object(creative, "_actor_for_callback", new=AsyncMock(return_value=actor())),
                patch.object(creative, "_receipt_for_callback", new=AsyncMock(side_effect=LookupError("gone"))),
            ):
                await handler(cb, FakeState())
            self.assertIn(missing_text, cb.answer.await_args.args[0])
            self.assertTrue(cb.answer.await_args.kwargs["show_alert"])

            cb = callback(f"cpc:{action}:{_TOKEN}:{_RECEIPT_TOKEN}")
            with (
                patch.object(creative, "_actor_for_callback", new=AsyncMock(return_value=actor())),
                patch.object(creative, "_receipt_for_callback", new=AsyncMock(return_value=current)) as exact,
                patch.object(creative, "_continue_generation", new=AsyncMock()) as resume,
            ):
                await handler(cb, FakeState())
            exact.assert_awaited_once_with(actor(), _RECEIPT_TOKEN)
            resume.assert_awaited_once_with(cb, actor=actor(), receipt=current)

        stale_shape = callback(f"cpc:generate:{_TOKEN}")
        await creative.generate_creative_image(stale_shape, FakeState())
        self.assertIn("устарело", stale_shape.answer.await_args.args[0])

        denied = callback(f"cpc:generate:{_TOKEN}:{_RECEIPT_TOKEN}")
        with patch.object(
            creative,
            "_actor_for_callback",
            new=AsyncMock(side_effect=ValueError("bad token")),
        ):
            await creative.generate_creative_image(denied, FakeState())
        self.assertTrue(denied.answer.await_args.kwargs["show_alert"])

    def test_visibility_and_safety_installers_are_idempotent_and_role_aware(self) -> None:
        fake = SimpleNamespace(
            _more_rows=lambda token, _actor: [
                [("📈 Продвижение и контент", f"cpo:content:{token}")],
                [("⬅️ Назад", f"cpj:home:{token}")],
            ],
            _content_tools_rows=lambda token, _actor: (
                [[("📣 Публикации", f"cp:x:{token}")]],
                ["base"],
            ),
            _creative_studio_visibility_installed=False,
        )
        creative.install_creative_studio_visibility(fake)
        owner_labels = labels(fake._more_rows(_TOKEN, actor()))
        self.assertIn("🎨 Создать картинку", owner_labels)
        analyst_labels = labels(fake._more_rows(_TOKEN, actor(PlatformRole.ANALYST)))
        self.assertNotIn("🎨 Создать картинку", analyst_labels)
        content_rows, help_lines = fake._content_tools_rows(_TOKEN, actor())
        self.assertEqual(labels(content_rows)[0], "🎨 Создать картинку")
        self.assertTrue(any("создать изображение" in line for line in help_lines))
        creative.install_creative_studio_visibility(fake)

        safety = SimpleNamespace(
            _creative_studio_safety_installed=False,
            _CLIENTPLATFORM_CALLBACK_PREFIXES=("cp:",),
            _STATE_ESCAPE_PREFIXES=("cp:entry:",),
            _REPEATABLE_NAVIGATION_PREFIXES=("cp:entry:",),
            _ONE_SHOT_PREFIXES=("cp:mutate:",),
            _callback_can_escape_state=lambda current, data: data == "legacy",
        )
        creative.install_creative_studio_safety(safety)
        self.assertIn("cpc:", safety._CLIENTPLATFORM_CALLBACK_PREFIXES)
        self.assertIn("cpc:generate:", safety._ONE_SHOT_PREFIXES)
        self.assertIn("cpc:abandon:", safety._ONE_SHOT_PREFIXES)
        self.assertIn("cpc:redeliver:", safety._ONE_SHOT_PREFIXES)
        self.assertTrue(
            safety._callback_can_escape_state(
                "ClientPlatformCreativeStudioState:waiting_prompt",
                f"cpc:new:{_TOKEN}",
            )
        )
        self.assertTrue(safety._callback_can_escape_state("OtherState:x", "legacy"))
        self.assertFalse(safety._callback_can_escape_state("OtherState:x", "cpc:new:x"))
        creative.install_creative_studio_safety(safety)


if __name__ == "__main__":
    unittest.main()
