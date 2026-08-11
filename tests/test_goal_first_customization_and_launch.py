from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from handlers import clientplatform_goal_first_autopilot as goal


class FakeState:
    def __init__(self, data=None) -> None:
        self.data = dict(data or {})
        self.state = None
        self.cleared = False

    async def get_data(self):
        return dict(self.data)

    async def set_data(self, data):
        self.data = dict(data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)
        return dict(self.data)

    async def set_state(self, value):
        self.state = value

    async def clear(self):
        self.cleared = True
        self.data.clear()
        self.state = None


async def direct(function, *args, **kwargs):
    return function(*args, **kwargs)


def base_data():
    return {
        "business_id": "11111111-1111-4111-8111-111111111111",
        "business_token": "business-token",
        "job_id": "22222222-2222-4222-8222-222222222222",
        "connection_id": "33333333-3333-4333-8333-333333333333",
        "external_campaign_id": "6001",
        "external_campaign_name": "Campaign",
        "creative_title": "Готовый заголовок",
        "creative_body": "Готовый текст",
        "preview_currency": "RUB",
        "preview_hard_cap_minor": 10_000,
        "preview_daily_cap_minor": 10_000,
    }


def target():
    return SimpleNamespace(answer=AsyncMock(), answer_photo=AsyncMock())


def callback(data: str, out=None):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        message=out or target(),
        answer=AsyncMock(),
    )


def authorization(*, hard=10_000, daily=10_000, currency="RUB"):
    return SimpleNamespace(
        id="44444444-4444-4444-8444-444444444444",
        hard_cap_minor=hard,
        daily_cap_minor=daily,
        currency=currency,
        terms_hash="adterms_" + "a" * 64,
        snapshot=SimpleNamespace(snapshot_hash="adsnap_" + "b" * 64),
    )


class GoalFirstCustomizationAndLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_text_replaces_only_copy(self) -> None:
        state = FakeState(base_data())
        message = SimpleNamespace(
            text="Мой заголовок\nМой рекламный текст",
            from_user=SimpleNamespace(id=101),
            answer=AsyncMock(),
        )
        updated = SimpleNamespace(title="Мой заголовок", text="Мой рекламный текст")
        with (
            patch.object(goal.control, "_user_id", return_value=101),
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal, "update_ad_publication_copy", return_value=updated) as update,
            patch.object(goal.asyncio, "to_thread", new=direct),
        ):
            await goal.receive_custom_text(message, state)
        update.assert_called_once()
        self.assertEqual(state.data["creative_title"], "Мой заголовок")
        self.assertEqual(state.data["creative_body"], "Мой рекламный текст")
        self.assertEqual(state.state, goal.GoalFirstAutopilotState.customizing)
        self.assertIn("Свой текст", message.answer.await_args.args[0])

    async def test_custom_text_requires_title_and_body(self) -> None:
        state = FakeState(base_data())
        message = SimpleNamespace(
            text="Только одна строка",
            from_user=SimpleNamespace(id=101),
            answer=AsyncMock(),
        )
        await goal.receive_custom_text(message, state)
        self.assertIn("две части", message.answer.await_args.args[0])
        self.assertNotEqual(state.state, goal.GoalFirstAutopilotState.customizing)

    async def test_own_image_is_attached_to_persistent_draft(self) -> None:
        state = FakeState(base_data())
        message = SimpleNamespace(
            photo=[SimpleNamespace(file_id="photo-1", file_size=1234)],
            document=None,
            from_user=SimpleNamespace(id=101),
            answer=AsyncMock(),
        )
        attach = Mock()
        with (
            patch.object(goal, "_download_telegram_file", new=AsyncMock(return_value=b"image")),
            patch.object(goal.control, "_user_id", return_value=101),
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal, "attach_image_bytes", new=attach),
            patch.object(goal.asyncio, "to_thread", new=direct),
        ):
            await goal.receive_custom_image(message, state)
        attach.assert_called_once()
        self.assertEqual(state.state, goal.GoalFirstAutopilotState.customizing)
        self.assertIn("в Яндекс вручную не понадобится", message.answer.await_args.args[0])

    async def test_own_video_is_attached_and_waiting_is_hidden_from_owner(self) -> None:
        state = FakeState(base_data())
        message = SimpleNamespace(
            video=SimpleNamespace(
                file_id="video-1",
                file_size=4321,
                mime_type="video/mp4",
                file_name="mine.mp4",
                duration=12,
            ),
            from_user=SimpleNamespace(id=101),
            answer=AsyncMock(),
        )
        attach = Mock()
        with (
            patch.object(goal, "_download_telegram_file", new=AsyncMock(return_value=b"video")),
            patch.object(goal.control, "_user_id", return_value=101),
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal, "attach_video_bytes", new=attach),
            patch.object(goal.asyncio, "to_thread", new=direct),
        ):
            await goal.receive_custom_video(message, state)
        attach.assert_called_once()
        self.assertEqual(state.state, goal.GoalFirstAutopilotState.customizing)
        self.assertIn("дождётся конвертации", message.answer.await_args.args[0])

    async def test_unknown_telegram_file_size_is_allowed_then_verified_after_download(self) -> None:
        class Bot:
            async def get_file(self, _file_id):
                return SimpleNamespace(file_path="remote/file", file_size=0)

            async def download_file(self, _path, *, destination, timeout):
                self.timeout = timeout
                destination.write(b"payload")

        message = SimpleNamespace(bot=Bot())
        payload = await goal._download_telegram_file(
            message,
            file_id="file-1",
            reported_size=0,
        )
        self.assertEqual(payload, b"payload")
        self.assertEqual(message.bot.timeout, 30)

    async def test_generation_requires_separate_explicit_confirmation(self) -> None:
        state = FakeState(base_data())
        out = target()
        cb = callback("cpo:genask:business-token", out)
        with patch.object(goal.control, "_callback_message", return_value=out):
            await goal.ask_generated_image_confirmation(cb, state)
        self.assertEqual(state.state, goal.GoalFirstAutopilotState.confirming_generation)
        labels = [
            button.text
            for row in out.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("✅ Создать 1 картинку", labels)
        self.assertIn("платную квоту", out.answer.await_args.args[0])

    async def test_generated_image_uses_copy_sensitive_idempotency_and_waits_without_duplicate(self) -> None:
        state = FakeState(base_data())
        out = target()
        cb = callback("cpo:gen:business-token", out)
        generated = SimpleNamespace(status="running", asset_ready=False, job_id="visual-1")
        with (
            patch.object(goal, "create_ad_visual", return_value=generated) as create,
            patch.object(goal, "_finish_generated_image", new=AsyncMock(return_value=False)),
            patch.object(goal.control, "_callback_message", return_value=out),
            patch.object(goal.asyncio, "to_thread", new=direct),
        ):
            await goal.generate_custom_image(cb, state)
        create.assert_called_once()
        key = create.call_args.kwargs["idempotency_key"]
        self.assertTrue(key.startswith("clientplatform:"))
        self.assertEqual(state.data["creative_job_id"], "visual-1")
        self.assertEqual(state.state, goal.GoalFirstAutopilotState.generation_pending)
        self.assertIn("Ничего загружать заново не нужно", out.answer.await_args.args[0])

    async def test_generated_image_success_is_persisted_as_ad_asset(self) -> None:
        state = FakeState(base_data())
        out = target()
        cb = callback("cpo:gencheck:business-token", out)
        state.data["creative_job_id"] = "visual-1"
        generated = SimpleNamespace(status="succeeded", asset_ready=True)
        with (
            patch.object(goal, "poll_ad_visual", return_value=generated),
            patch.object(goal, "materialize_ad_visual", return_value=Path("/tmp/generated.jpg")),
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal, "attach_image_file") as attach,
            patch.object(goal.control, "_callback_message", return_value=out),
            patch.object(goal.asyncio, "to_thread", new=direct),
        ):
            await goal.check_generated_image(cb, state)
        attach.assert_called_once()
        self.assertEqual(state.state, goal.GoalFirstAutopilotState.customizing)
        self.assertEqual(state.data["creative_job_id"], "")
        out.answer_photo.assert_awaited_once()

    async def test_launch_click_is_final_spend_confirmation_when_preview_is_unchanged(self) -> None:
        state = FakeState(base_data())
        out = target()
        cb = callback("cpo:launch:business-token", out)
        submitted = SimpleNamespace(
            job=SimpleNamespace(id=base_data()["job_id"]),
            media_pending=False,
        )
        prepared = SimpleNamespace(authorization=authorization())
        granted = SimpleNamespace(authorization=authorization())
        operation = SimpleNamespace(id="operation-123456789012")
        with (
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal, "submit_goal_publication", return_value=submitted),
            patch.object(goal, "ad_spend_mutations_enabled", return_value=True),
            patch.object(goal, "prepare_goal_spend_consent", return_value=prepared),
            patch.object(goal, "grant_ad_spend_consent", return_value=granted) as grant,
            patch.object(goal, "queue_ad_spend_launch", return_value=operation) as queue,
            patch.object(goal.control, "_uuid_token", return_value="auth-token"),
            patch.object(goal.control, "_callback_message", return_value=out),
            patch.object(goal.asyncio, "to_thread", new=direct),
        ):
            await goal.prepare_real_launch(cb, state)
        grant.assert_called_once()
        queue.assert_called_once()
        self.assertTrue(state.cleared)
        self.assertIn("Готово", out.answer.await_args.args[0])
        self.assertIn("Максимальный расход", out.answer.await_args.args[0])

    async def test_changed_provider_cap_requires_new_confirmation_instead_of_silent_launch(self) -> None:
        state = FakeState(base_data())
        out = target()
        cb = callback("cpo:launch:business-token", out)
        changed = authorization(hard=8_000, daily=8_000)
        submitted = SimpleNamespace(
            job=SimpleNamespace(id=base_data()["job_id"]),
            media_pending=False,
        )
        with (
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal, "submit_goal_publication", return_value=submitted),
            patch.object(goal, "ad_spend_mutations_enabled", return_value=True),
            patch.object(
                goal,
                "prepare_goal_spend_consent",
                return_value=SimpleNamespace(authorization=changed),
            ),
            patch.object(goal, "grant_ad_spend_consent") as grant,
            patch.object(goal.control, "_callback_message", return_value=out),
            patch.object(goal.asyncio, "to_thread", new=direct),
        ):
            await goal.prepare_real_launch(cb, state)
        grant.assert_not_called()
        self.assertEqual(state.state, goal.GoalFirstAutopilotState.confirming_launch)
        self.assertEqual(state.data["preview_hard_cap_minor"], 8_000)
        self.assertIn("Сумма изменилась", out.answer.await_args.args[0])

    async def test_pending_custom_video_blocks_spend_until_it_is_attached(self) -> None:
        state = FakeState(base_data())
        out = target()
        cb = callback("cpo:launch:business-token", out)
        submitted = SimpleNamespace(
            job=SimpleNamespace(id=base_data()["job_id"]),
            media_pending=True,
        )
        with (
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal, "submit_goal_publication", return_value=submitted),
            patch.object(goal, "prepare_goal_spend_consent") as prepare,
            patch.object(goal.control, "_callback_message", return_value=out),
            patch.object(goal.asyncio, "to_thread", new=direct),
        ):
            await goal.prepare_real_launch(cb, state)
        prepare.assert_not_called()
        self.assertEqual(state.state, goal.GoalFirstAutopilotState.ready)
        self.assertIn("показы не запускаю", out.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
