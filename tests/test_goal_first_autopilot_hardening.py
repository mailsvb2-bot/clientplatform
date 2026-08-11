from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from clientplatform.domain.ad_connections import AdConnectionError
from clientplatform.domain.ad_publication_assets import AdPublicationAssetError
from handlers import clientplatform_goal_dashboard as dashboard
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

    async def update_data(self, **values):
        self.data.update(values)
        return dict(self.data)

    async def set_state(self, value):
        self.state = value

    async def clear(self):
        self.cleared = True
        self.data.clear()
        self.state = None


async def direct(function, *args, **kwargs):
    return function(*args, **kwargs)


def target():
    return SimpleNamespace(answer=AsyncMock(), answer_photo=AsyncMock())


def callback(data: str, out=None):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        message=out or target(),
        answer=AsyncMock(),
    )


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
    }


class GoalFirstAutopilotHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_preserves_status_when_no_time_is_open(self) -> None:
        out = target()
        snapshot = (
            "actor",
            SimpleNamespace(business=SimpleNamespace(name="Мой бизнес")),
            SimpleNamespace(activity_description="Помогаю клиентам"),
            [],
            [object()],
            [object()],
            [],
        )
        with (
            patch.object(
                dashboard.one_click.simple,
                "_business_snapshot",
                new=AsyncMock(return_value=snapshot),
            ),
            patch.object(dashboard.owner, "_all_offerings", new=AsyncMock(return_value=[])),
            patch.object(dashboard.control, "list_booking_slots", return_value=[]),
            patch.object(dashboard.asyncio, "to_thread", new=direct),
            patch.object(dashboard.control, "_uuid_token", side_effect=lambda value: value),
        ):
            await dashboard.send_goal_dashboard(out, user_id=101, business_id="business-1")
        text = out.answer.await_args.args[0]
        self.assertIn("Ближайшее время: пока не опубликовано", text)
        self.assertIn("Свободных времён пока нет", text)
        self.assertIn("Клиентов: 1", text)

    async def test_launch_label_without_known_cap_requires_fresh_check(self) -> None:
        with patch.object(goal, "ad_spend_mutations_enabled", return_value=True):
            self.assertEqual(goal._launch_label({}), "🚀 Проверить и запустить")

    async def test_region_lookup_fails_closed_to_first_region_question(self) -> None:
        out = target()
        cb = callback("unused", out)
        state = FakeState()
        data = {
            "business_id": "business-1",
            "business_token": "business-token",
            "slot_id": "slot-1",
            "connection_id": "connection-1",
        }
        with (
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal.one_click, "list_ad_publications", side_effect=AdConnectionError("down")),
            patch.object(goal.control, "_callback_message", return_value=out),
            patch.object(goal.asyncio, "to_thread", new=direct),
        ):
            await goal._choose_goal_region(
                cb,
                state,
                data=data,
                campaign_id="6001",
                campaign_name="Campaign",
            )
        self.assertEqual(state.state, goal.one_click.OneClickOwnerState.waiting_region)
        self.assertIn("где искать клиентов", out.answer.await_args.args[0].lower())

    async def test_stale_customization_callbacks_fail_closed(self) -> None:
        cases = (
            (goal.open_customization, "cpo:custom:business-token"),
            (goal.ask_custom_text, "cpo:custom-text:business-token"),
            (goal.ask_custom_image, "cpo:custom-image:business-token"),
            (goal.ask_custom_video, "cpo:custom-video:business-token"),
            (goal.clear_custom_media, "cpo:custom-clear:business-token"),
            (goal.ask_generated_image_confirmation, "cpo:genask:business-token"),
            (goal.generate_custom_image, "cpo:gen:business-token"),
            (goal.check_generated_image, "cpo:gencheck:business-token"),
            (goal.finish_customization, "cpo:custom-done:business-token"),
        )
        for function, payload in cases:
            cb = callback(payload)
            state = FakeState({"business_token": "other"})
            await function(cb, state)
            cb.answer.assert_awaited_once_with("Этот черновик уже устарел", show_alert=True)

    async def test_custom_text_prompt_and_save_failure_are_human_readable(self) -> None:
        out = target()
        cb = callback("cpo:custom-text:business-token", out)
        state = FakeState(base_data())
        with patch.object(goal.control, "_callback_message", return_value=out):
            await goal.ask_custom_text(cb, state)
        self.assertEqual(state.state, goal.GoalFirstAutopilotState.waiting_text)
        self.assertIn("первая строка", out.answer.await_args.args[0])

        message = SimpleNamespace(
            text="Заголовок\nТекст",
            from_user=SimpleNamespace(id=101),
            answer=AsyncMock(),
        )
        with patch.object(
            goal.control,
            "_actor",
            new=AsyncMock(side_effect=AdConnectionError("cannot save")),
        ):
            await goal.receive_custom_text(message, FakeState(base_data()))
        self.assertIn("Не получилось сохранить текст", message.answer.await_args.args[0])

    async def test_image_prompt_document_input_and_invalid_input(self) -> None:
        out = target()
        cb = callback("cpo:custom-image:business-token", out)
        state = FakeState(base_data())
        with patch.object(goal.control, "_callback_message", return_value=out):
            await goal.ask_custom_image(cb, state)
        self.assertEqual(state.state, goal.GoalFirstAutopilotState.waiting_image)

        message = SimpleNamespace(
            photo=[],
            document=SimpleNamespace(
                mime_type="image/png",
                file_id="doc-1",
                file_size=7,
                file_name="mine.png",
            ),
            from_user=SimpleNamespace(id=101),
            answer=AsyncMock(),
        )
        attach = Mock()
        with (
            patch.object(goal, "_download_telegram_file", new=AsyncMock(return_value=b"payload")),
            patch.object(goal.control, "_user_id", return_value=101),
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal, "attach_image_bytes", new=attach),
            patch.object(goal.asyncio, "to_thread", new=direct),
        ):
            await goal.receive_custom_image(message, FakeState(base_data()))
        self.assertEqual(attach.call_args.kwargs["original_name"], "mine.png")

        invalid = SimpleNamespace(photo=[], document=None, answer=AsyncMock())
        await goal.receive_custom_image(invalid, FakeState(base_data()))
        self.assertIn("именно изображение", invalid.answer.await_args.args[0])

    async def test_image_upload_failure_is_contained(self) -> None:
        message = SimpleNamespace(
            photo=[SimpleNamespace(file_id="photo-1", file_size=7)],
            document=None,
            answer=AsyncMock(),
        )
        with patch.object(
            goal,
            "_download_telegram_file",
            new=AsyncMock(side_effect=AdPublicationAssetError("bad")),
        ):
            await goal.receive_custom_image(message, FakeState(base_data()))
        self.assertIn("Не удалось принять картинку", message.answer.await_args.args[0])

    async def test_telegram_download_rejects_all_size_and_path_integrity_failures(self) -> None:
        class Bot:
            def __init__(self, *, remote_size=0, path="remote/file", payload=b"payload") -> None:
                self.remote_size = remote_size
                self.path = path
                self.payload = payload

            async def get_file(self, _file_id):
                return SimpleNamespace(file_path=self.path, file_size=self.remote_size)

            async def download_file(self, _path, *, destination, timeout):
                self.timeout = timeout
                destination.write(self.payload)

        with self.assertRaises(AdPublicationAssetError):
            await goal._download_telegram_file(
                SimpleNamespace(bot=Bot()),
                file_id="f",
                reported_size=goal._MAX_TELEGRAM_MEDIA_BYTES + 1,
            )
        with self.assertRaises(AdPublicationAssetError):
            await goal._download_telegram_file(
                SimpleNamespace(bot=Bot(remote_size=goal._MAX_TELEGRAM_MEDIA_BYTES + 1)),
                file_id="f",
                reported_size=0,
            )
        with self.assertRaises(AdPublicationAssetError):
            await goal._download_telegram_file(
                SimpleNamespace(bot=Bot(path="")),
                file_id="f",
                reported_size=0,
            )
        with self.assertRaises(AdPublicationAssetError):
            await goal._download_telegram_file(
                SimpleNamespace(bot=Bot(payload=b"")),
                file_id="f",
                reported_size=0,
            )
        with self.assertRaises(AdPublicationAssetError):
            await goal._download_telegram_file(
                SimpleNamespace(bot=Bot(payload=b"changed")),
                file_id="f",
                reported_size=3,
            )

    async def test_video_prompt_missing_video_and_upload_failure_are_contained(self) -> None:
        out = target()
        cb = callback("cpo:custom-video:business-token", out)
        state = FakeState(base_data())
        with patch.object(goal.control, "_callback_message", return_value=out):
            await goal.ask_custom_video(cb, state)
        self.assertEqual(state.state, goal.GoalFirstAutopilotState.waiting_video)

        missing = SimpleNamespace(video=None, answer=AsyncMock())
        await goal.receive_custom_video(missing, FakeState(base_data()))
        self.assertIn("именно как видео", missing.answer.await_args.args[0])

        message = SimpleNamespace(
            video=SimpleNamespace(
                file_id="video-1",
                file_size=7,
                mime_type="video/mp4",
                file_name="mine.mp4",
                duration=12,
            ),
            answer=AsyncMock(),
        )
        with patch.object(
            goal,
            "_download_telegram_file",
            new=AsyncMock(side_effect=AdPublicationAssetError("bad")),
        ):
            await goal.receive_custom_video(message, FakeState(base_data()))
        self.assertIn("Не удалось принять видео", message.answer.await_args.args[0])

    async def test_clear_media_success_and_failure_are_explicit(self) -> None:
        out = target()
        cb = callback("cpo:custom-clear:business-token", out)
        with (
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(goal.control, "_callback_message", return_value=out),
            patch.object(goal, "remove_asset", new=Mock()),
            patch.object(goal.asyncio, "to_thread", new=direct),
        ):
            state = FakeState(base_data())
            await goal.clear_custom_media(cb, state)
        self.assertEqual(state.state, goal.GoalFirstAutopilotState.customizing)
        cb.answer.assert_awaited_with("Медиа убрано")

        failed = callback("cpo:custom-clear:business-token", target())
        with (
            patch.object(goal.control, "_actor", new=AsyncMock(return_value="actor")),
            patch.object(
                goal,
                "remove_asset",
                side_effect=AdPublicationAssetError("cannot remove"),
            ),
            patch.object(goal.asyncio, "to_thread", new=direct),
        ):
            await goal.clear_custom_media(failed, FakeState(base_data()))
        failed.answer.assert_awaited_with("Не удалось убрать медиа", show_alert=True)

    async def test_generated_image_finish_rejects_not_ready_and_materialization_failure(self) -> None:
        cb = callback("cpo:gen:business-token", target())
        self.assertFalse(
            await goal._finish_generated_image(
                cb,
                FakeState(base_data()),
                job=SimpleNamespace(status="running", asset_ready=False),
                data=base_data(),
            )
        )
        with (
            patch.object(goal, "materialize_ad_visual", side_effect=OSError("missing")),
            patch.object(goal.asyncio, "to_thread", new=direct),
        ):
            self.assertFalse(
                await goal._finish_generated_image(
                    cb,
                    FakeState(base_data()),
                    job=SimpleNamespace(status="succeeded", asset_ready=True),
                    data=base_data(),
                )
            )

    async def test_generated_image_creation_handles_bad_state_and_missing_job_id(self) -> None:
        out = target()
        cb = callback("cpo:gen:business-token", out)
        broken = base_data()
        broken.pop("business_id")
        with patch.object(goal.control, "_callback_message", return_value=out):
            state = FakeState(broken)
            await goal.generate_custom_image(cb, state)
        self.assertEqual(state.state, goal.GoalFirstAutopilotState.customizing)
        self.assertIn("Не удалось создать картинку", out.answer.await_args.args[0])

        out2 = target()
        cb2 = callback("cpo:gen:business-token", out2)
        with (
            patch.object(
                goal,
                "create_ad_visual",
                return_value=SimpleNamespace(status="running", asset_ready=False),
            ),
            patch.object(goal, "_finish_generated_image", new=AsyncMock(return_value=False)),
            patch.object(goal.control, "_callback_message", return_value=out2),
            patch.object(goal.asyncio, "to_thread", new=direct),
        ):
            state2 = FakeState(base_data())
            await goal.generate_custom_image(cb2, state2)
        self.assertEqual(state2.state, goal.GoalFirstAutopilotState.customizing)
        self.assertIn("не вернул результат", out2.answer.await_args.args[0])

    async def test_generated_image_poll_error_pending_and_terminal_failure(self) -> None:
        base = base_data()
        base["creative_job_id"] = "visual-1"

        failed_check = callback("cpo:gencheck:business-token", target())
        with (
            patch.object(goal, "poll_ad_visual", side_effect=AdPublicationAssetError("bad")),
            patch.object(goal.asyncio, "to_thread", new=direct),
        ):
            with self.assertRaises(AdPublicationAssetError):
                await goal.check_generated_image(failed_check, FakeState(base))

        error_check = callback("cpo:gencheck:business-token", target())
        missing_job = dict(base_data())
        with patch.object(goal.control, "_callback_message", return_value=error_check.message):
            await goal.check_generated_image(error_check, FakeState(missing_job))
        error_check.answer.assert_awaited_with(
            "Пока не удалось проверить картинку",
            show_alert=True,
        )

        pending_out = target()
        pending = callback("cpo:gencheck:business-token", pending_out)
        with (
            patch.object(goal, "poll_ad_visual", return_value=SimpleNamespace(status="running")),
            patch.object(goal, "_finish_generated_image", new=AsyncMock(return_value=False)),
            patch.object(goal.control, "_callback_message", return_value=pending_out),
            patch.object(goal.asyncio, "to_thread", new=direct),
        ):
            await goal.check_generated_image(pending, FakeState(base))
        self.assertIn("Ещё создаётся", pending_out.answer.await_args.args[0])

        terminal_out = target()
        terminal = callback("cpo:gencheck:business-token", terminal_out)
        with (
            patch.object(goal, "poll_ad_visual", return_value=SimpleNamespace(status="failed")),
            patch.object(goal, "_finish_generated_image", new=AsyncMock(return_value=False)),
            patch.object(goal.control, "_callback_message", return_value=terminal_out),
            patch.object(goal.asyncio, "to_thread", new=direct),
        ):
            terminal_state = FakeState(base)
            await goal.check_generated_image(terminal, terminal_state)
        self.assertEqual(terminal_state.state, goal.GoalFirstAutopilotState.customizing)
        self.assertEqual(terminal_state.data["creative_job_id"], "")

    async def test_finish_customization_returns_to_ready_result(self) -> None:
        out = target()
        cb = callback("cpo:custom-done:business-token", out)
        with (
            patch.object(goal, "ad_spend_mutations_enabled", return_value=False),
            patch.object(goal.control, "_callback_message", return_value=out),
        ):
            state = FakeState(base_data())
            await goal.finish_customization(cb, state)
        self.assertEqual(state.state, goal.GoalFirstAutopilotState.ready)
        cb.answer.assert_awaited_with("Готово")
        self.assertIn("Изменения сохранены", out.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
