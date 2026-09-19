from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from clientplatform.domain.event_content import EventContentMode, EventContentStage
from clientplatform.domain.programs import ContentKind
from handlers import clientplatform_events as events


BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
EVENT_ID = "33333333-3333-4333-8333-333333333333"
BUSINESS_TOKEN = "ERERERERQRGBEREREREREQ"
EVENT_TOKEN = "MzMzMzMzQzODMzMzMzMzMzMzMz"


async def _direct(function, *args, **kwargs):
    return function(*args, **kwargs)


def _decode(value: str) -> str:
    return EVENT_ID if value == EVENT_TOKEN else BUSINESS_ID


def _callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def _message(*, text: str = "", video=None) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        video=video,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def _actor() -> MagicMock:
    actor = MagicMock(unsafe=True)
    actor.business_id = BUSINESS_ID
    return actor


def _video_modes() -> SimpleNamespace:
    return SimpleNamespace(
        warmup=EventContentMode.TEXT_WITH_VIDEO,
        event_day=EventContentMode.TEXT_WITH_VIDEO,
        post_event=EventContentMode.TEXT_WITH_VIDEO,
    )


class WebinarVideoHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_owner_visual_covers_video_image_text_and_failure(self) -> None:
        callback = _callback("cpev:noop")
        target = SimpleNamespace(answer=AsyncMock())
        actor = _actor()
        with (
            patch.object(events.asyncio, "to_thread", new=_direct),
            patch.object(events.control, "_callback_message", return_value=target),
            patch(
                "handlers.clientplatform_creative_studio.send_creative_studio_menu",
                new=AsyncMock(),
            ) as menu,
            patch.object(
                events,
                "prepare_event_stage_visual",
                return_value=SimpleNamespace(mode=EventContentMode.TEXT_WITH_VIDEO),
            ),
        ):
            await events._prepare_event_visual_for_owner(
                callback,
                actor=actor,
                event_id=EVENT_ID,
                stage=EventContentStage.WARMUP,
                message_key="before:2",
                event_title="Вебинар",
                message_text="Текст",
            )
        callback.answer.assert_awaited_once_with("Видео подготовлено к генерации")
        menu.assert_awaited_once_with(target, user_id=101, business_id=BUSINESS_ID)

        callback.answer.reset_mock()
        with (
            patch.object(events.asyncio, "to_thread", new=_direct),
            patch.object(events.control, "_callback_message", return_value=target),
            patch(
                "handlers.clientplatform_creative_studio.send_creative_studio_menu",
                new=AsyncMock(),
            ) as menu,
            patch.object(
                events,
                "prepare_event_stage_visual",
                return_value=SimpleNamespace(mode=EventContentMode.TEXT_WITH_IMAGE),
            ),
        ):
            await events._prepare_event_visual_for_owner(
                callback,
                actor=actor,
                event_id=EVENT_ID,
                stage=EventContentStage.EVENT_DAY,
                message_key="announcement",
                event_title="Вебинар",
                message_text="Текст",
            )
        callback.answer.assert_awaited_once_with("Картинка подготовлена к генерации")
        menu.assert_awaited_once()

        callback.answer.reset_mock()
        with (
            patch.object(events.asyncio, "to_thread", new=_direct),
            patch.object(events, "prepare_event_stage_visual", return_value=None),
        ):
            await events._prepare_event_visual_for_owner(
                callback,
                actor=actor,
                event_id=EVENT_ID,
                stage=EventContentStage.WARMUP,
                message_key="before:1",
                event_title="Вебинар",
                message_text="Текст",
            )
        callback.answer.assert_awaited_once_with(
            "Для этого этапа выбран только текст", show_alert=True
        )

        callback.answer.reset_mock()
        with (
            patch.object(events.asyncio, "to_thread", new=_direct),
            patch.object(events, "prepare_event_stage_visual", side_effect=ValueError("stale")),
        ):
            await events._prepare_event_visual_for_owner(
                callback,
                actor=actor,
                event_id=EVENT_ID,
                stage=EventContentStage.WARMUP,
                message_key="before:1",
                event_title="Вебинар",
                message_text="Текст",
            )
        callback.answer.assert_awaited_once_with(
            "Не удалось безопасно подготовить визуал", show_alert=True
        )

    async def test_prepare_event_visual_routes_warmup_and_followup_and_rejects_stale(self) -> None:
        stale = _callback("cpev:vis:broken")
        await events.prepare_event_visual(stale)
        stale.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        bad_target = _callback(f"cpev:vis:x:{EVENT_TOKEN}:1:{BUSINESS_TOKEN}")
        await events.prepare_event_visual(bad_target)
        bad_target.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        bad_pos = _callback(f"cpev:vis:w:{EVENT_TOKEN}:x:{BUSINESS_TOKEN}")
        with patch.object(events.control, "_token_uuid", side_effect=_decode):
            await events.prepare_event_visual(bad_pos)
        bad_pos.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        actor = _actor()
        snapshot = SimpleNamespace(items=(SimpleNamespace(id=EVENT_ID, title="Вебинар"),))
        warm = SimpleNamespace(position=2, slot_key="before:2", text="Прогрев")
        warm_cb = _callback(f"cpev:vis:w:{EVENT_TOKEN}:2:{BUSINESS_TOKEN}")
        with (
            patch.object(events.asyncio, "to_thread", new=_direct),
            patch.object(events.control, "_token_uuid", side_effect=_decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "resolve_cockpit_events", return_value=snapshot),
            patch.object(
                events,
                "get_saved_event_warmup_plan",
                return_value=SimpleNamespace(drafts=(warm,)),
            ),
            patch.object(events, "_prepare_event_visual_for_owner", new=AsyncMock()) as prepare,
        ):
            await events.prepare_event_visual(warm_cb)
        prepare.assert_awaited_once_with(
            warm_cb,
            actor=actor,
            event_id=EVENT_ID,
            stage=EventContentStage.WARMUP,
            message_key="before:2",
            event_title="Вебинар",
            message_text="Прогрев",
        )

        missing = _callback(f"cpev:vis:w:{EVENT_TOKEN}:3:{BUSINESS_TOKEN}")
        with (
            patch.object(events.asyncio, "to_thread", new=_direct),
            patch.object(events.control, "_token_uuid", side_effect=_decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "resolve_cockpit_events", return_value=snapshot),
            patch.object(
                events,
                "get_saved_event_warmup_plan",
                return_value=SimpleNamespace(drafts=(warm,)),
            ),
        ):
            await events.prepare_event_visual(missing)
        missing.answer.assert_awaited_once_with(
            "Сообщение прогрева уже изменилось", show_alert=True
        )

        preview = SimpleNamespace(slot_key="attended_unpaid:1", text="Дожим")
        follow_cb = _callback(f"cpev:vis:f:{EVENT_TOKEN}:0:{BUSINESS_TOKEN}")
        with (
            patch.object(events.asyncio, "to_thread", new=_direct),
            patch.object(events.control, "_token_uuid", side_effect=_decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "resolve_cockpit_events", return_value=snapshot),
            patch.object(events, "get_event_followup_content_plan", return_value=(preview,)),
            patch.object(events, "_prepare_event_visual_for_owner", new=AsyncMock()) as prepare,
        ):
            await events.prepare_event_visual(follow_cb)
        prepare.assert_awaited_once_with(
            follow_cb,
            actor=actor,
            event_id=EVENT_ID,
            stage=EventContentStage.POST_EVENT,
            message_key="attended_unpaid:1",
            event_title="Вебинар",
            message_text="Дожим",
        )

        stale_follow = _callback(f"cpev:vis:f:{EVENT_TOKEN}:5:{BUSINESS_TOKEN}")
        with (
            patch.object(events.asyncio, "to_thread", new=_direct),
            patch.object(events.control, "_token_uuid", side_effect=_decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "resolve_cockpit_events", return_value=snapshot),
            patch.object(events, "get_event_followup_content_plan", return_value=(preview,)),
        ):
            await events.prepare_event_visual(stale_follow)
        stale_follow.answer.assert_awaited_once_with(
            "Сообщение дожима уже изменилось", show_alert=True
        )

    async def test_begin_video_upload_covers_all_stage_bindings_and_mode_guard(self) -> None:
        invalid = _callback("cpev:vu:broken")
        await events.begin_event_video_upload(invalid, AsyncMock())
        invalid.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        bad_token = _callback("cpev:vu:a:bad:not-int:bad")
        with patch.object(events.control, "_token_uuid", side_effect=ValueError("bad")):
            await events.begin_event_video_upload(bad_token, AsyncMock())
        bad_token.answer.assert_awaited_once_with("Кнопка устарела", show_alert=True)

        actor = _actor()
        reply = SimpleNamespace(answer=AsyncMock())
        for target, position, extra_patch, expected_stage, expected_slot in (
            (
                "a",
                0,
                patch.object(events, "get_saved_event_warmup_plan"),
                EventContentStage.EVENT_DAY,
                "announcement",
            ),
            (
                "w",
                2,
                patch.object(
                    events,
                    "get_saved_event_warmup_plan",
                    return_value=SimpleNamespace(
                        drafts=(SimpleNamespace(position=2, slot_key="before:2"),)
                    ),
                ),
                EventContentStage.WARMUP,
                "before:2",
            ),
            (
                "f",
                0,
                patch.object(
                    events,
                    "get_event_followup_content_plan",
                    return_value=(SimpleNamespace(slot_key="no_show:1"),),
                ),
                EventContentStage.POST_EVENT,
                "no_show:1",
            ),
        ):
            cb = _callback(f"cpev:vu:{target}:{EVENT_TOKEN}:{position}:{BUSINESS_TOKEN}")
            state = AsyncMock()
            with (
                patch.object(events.asyncio, "to_thread", new=_direct),
                patch.object(events.control, "_token_uuid", side_effect=_decode),
                patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
                patch.object(events, "get_event_content_plan", return_value=_video_modes()),
                patch.object(events.control, "_callback_message", return_value=reply),
                patch.object(events, "_cancel_keyboard", return_value="cancel"),
                extra_patch,
            ):
                await events.begin_event_video_upload(cb, state)
            state.set_state.assert_awaited_once_with(
                events.ClientPlatformEventState.waiting_visual_upload
            )
            state.update_data.assert_awaited_once_with(
                event_business_id=BUSINESS_ID,
                event_id=EVENT_ID,
                event_visual_stage=expected_stage.value,
                event_visual_slot_key=expected_slot,
            )
            cb.answer.assert_awaited_once_with()
            self.assertIn("Пришлите видео", reply.answer.await_args.args[0])
            reply.answer.reset_mock()

        mismatch = _callback(f"cpev:vu:a:{EVENT_TOKEN}:0:{BUSINESS_TOKEN}")
        with (
            patch.object(events.asyncio, "to_thread", new=_direct),
            patch.object(events.control, "_token_uuid", side_effect=_decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(
                events,
                "get_event_content_plan",
                return_value=SimpleNamespace(
                    warmup=EventContentMode.TEXT,
                    event_day=EventContentMode.TEXT,
                    post_event=EventContentMode.TEXT,
                ),
            ),
        ):
            await events.begin_event_video_upload(mismatch, AsyncMock())
        mismatch.answer.assert_awaited_once_with(
            "Для этого этапа больше не выбран формат видео", show_alert=True
        )

        stale_warm = _callback(f"cpev:vu:w:{EVENT_TOKEN}:4:{BUSINESS_TOKEN}")
        with (
            patch.object(events.asyncio, "to_thread", new=_direct),
            patch.object(events.control, "_token_uuid", side_effect=_decode),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_event_content_plan", return_value=_video_modes()),
            patch.object(
                events,
                "get_saved_event_warmup_plan",
                return_value=SimpleNamespace(drafts=()),
            ),
        ):
            await events.begin_event_video_upload(stale_warm, AsyncMock())
        stale_warm.answer.assert_awaited_once_with(
            "Не удалось открыть загрузку видео", show_alert=True
        )

    async def test_receive_owner_video_upload_covers_session_validation_and_success(self) -> None:
        missing = _message(video=object())
        missing_state = AsyncMock()
        missing_state.get_data.return_value = {}
        await events.receive_event_video_upload(missing, missing_state)
        missing_state.clear.assert_awaited_once_with()
        self.assertIn("Сессия загрузки устарела", missing.answer.await_args.args[0])

        base_data = {
            "event_business_id": BUSINESS_ID,
            "event_id": EVENT_ID,
            "event_visual_stage": EventContentStage.WARMUP.value,
            "event_visual_slot_key": "before:2",
        }

        cancel = _message(text="Отмена")
        cancel_state = AsyncMock()
        cancel_state.get_data.return_value = dict(base_data)
        with patch.object(events, "_send_event_content_plan", new=AsyncMock()) as send:
            await events.receive_event_video_upload(cancel, cancel_state)
        cancel_state.clear.assert_awaited_once_with()
        send.assert_awaited_once()

        no_video = _message(text="")
        no_video_state = AsyncMock()
        no_video_state.get_data.return_value = dict(base_data)
        await events.receive_event_video_upload(no_video, no_video_state)
        self.assertIn("именно видеофайл", no_video.answer.await_args.args[0])

        actor = _actor()
        success = _message(video=object())
        success_state = AsyncMock()
        success_state.get_data.return_value = dict(base_data)
        with (
            patch.object(events.asyncio, "to_thread", new=_direct),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_event_content_plan", return_value=_video_modes()),
            patch.object(
                events,
                "materialize_program_content",
                new=AsyncMock(return_value=(ContentKind.VIDEO, "s3://bucket/program-media/video.mp4")),
            ) as materialize,
            patch.object(events, "set_event_content_asset_reference") as save,
            patch.object(events, "_send_event_content_plan", new=AsyncMock()) as send,
        ):
            await events.receive_event_video_upload(success, success_state)
        materialize.assert_awaited_once_with(success, business_id=BUSINESS_ID)
        save.assert_called_once()
        success_state.clear.assert_awaited_once_with()
        self.assertIn("Своё видео сохранено", success.answer.await_args.args[0])
        send.assert_awaited_once()

        changed = _message(video=object())
        changed_state = AsyncMock()
        changed_state.get_data.return_value = dict(base_data)
        with (
            patch.object(events.asyncio, "to_thread", new=_direct),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(
                events,
                "get_event_content_plan",
                return_value=SimpleNamespace(
                    warmup=EventContentMode.TEXT,
                    event_day=EventContentMode.TEXT,
                    post_event=EventContentMode.TEXT,
                ),
            ),
        ):
            await events.receive_event_video_upload(changed, changed_state)
        changed_state.clear.assert_awaited_once_with()
        self.assertIn("Формат этапа уже изменён", changed.answer.await_args.args[0])

    async def test_failed_owner_video_binding_queues_cleanup_and_fails_closed(self) -> None:
        actor = _actor()
        message = _message(video=object())
        state = AsyncMock()
        state.get_data.return_value = {
            "event_business_id": BUSINESS_ID,
            "event_id": EVENT_ID,
            "event_visual_stage": EventContentStage.POST_EVENT.value,
            "event_visual_slot_key": "no_show:1",
        }
        reference = "s3://bucket/program-media/video.mp4"
        with (
            patch.object(events.asyncio, "to_thread", new=_direct),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "get_event_content_plan", return_value=_video_modes()),
            patch.object(
                events,
                "materialize_program_content",
                new=AsyncMock(return_value=(ContentKind.VIDEO, reference)),
            ),
            patch.object(
                events,
                "set_event_content_asset_reference",
                side_effect=ValueError("binding changed"),
            ),
            patch.object(events, "queue_program_media_cleanup") as cleanup,
        ):
            await events.receive_event_video_upload(message, state)
        cleanup.assert_called_once_with(
            business_id=BUSINESS_ID,
            media_reference=reference,
            reason="failed_event_owner_video_binding",
        )
        self.assertIn("Не удалось безопасно сохранить видео", message.answer.await_args.args[0])
        state.clear.assert_not_awaited()

    async def test_announcement_video_preparation_and_safe_visual_failure(self) -> None:
        actor = _actor()
        draft = SimpleNamespace(
            title="Вебинар",
            text="Анонс",
            generated_by="ai:test:model",
            registration_url=lambda *, public_base_url, source: (
                f"{public_base_url}/e/demo?source={source}"
            ),
        )
        reply = SimpleNamespace(answer=AsyncMock())
        cb = _callback(f"cpev:announce:{EVENT_TOKEN}:{BUSINESS_TOKEN}")
        prepared = SimpleNamespace(mode=EventContentMode.TEXT_WITH_VIDEO)
        with (
            patch.object(events.asyncio, "to_thread", new=_direct),
            patch.object(events.control, "_token_uuid", side_effect=_decode),
            patch.object(events.control, "_uuid_token", return_value=BUSINESS_TOKEN),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "draft_event_announcement", new=AsyncMock(return_value=draft)),
            patch.object(events, "_public_base_url", return_value="https://example.test"),
            patch.object(events, "get_event_content_plan", return_value=_video_modes()),
            patch.object(events, "prepare_event_stage_visual", return_value=prepared) as prepare,
            patch.object(events.control, "_callback_message", return_value=reply),
            patch.object(events, "_announcement_share_markup", return_value="markup"),
        ):
            await events.create_event_announcement(cb)
        prepare.assert_called_once()
        cb.answer.assert_awaited_once_with("Анонс готов")
        self.assertIn("Видео подготовлено к генерации", reply.answer.await_args.args[0])

        reply.answer.reset_mock()
        cb.answer.reset_mock()
        with (
            patch.object(events.asyncio, "to_thread", new=_direct),
            patch.object(events.control, "_token_uuid", side_effect=_decode),
            patch.object(events.control, "_uuid_token", return_value=BUSINESS_TOKEN),
            patch.object(events.control, "_actor", new=AsyncMock(return_value=actor)),
            patch.object(events, "draft_event_announcement", new=AsyncMock(return_value=draft)),
            patch.object(events, "_public_base_url", return_value="https://example.test"),
            patch.object(events, "get_event_content_plan", return_value=_video_modes()),
            patch.object(events, "prepare_event_stage_visual", side_effect=RuntimeError("down")),
            patch.object(events.control, "_callback_message", return_value=reply),
            patch.object(events, "_announcement_share_markup", return_value="markup"),
        ):
            await events.create_event_announcement(cb)
        self.assertIn("Визуал сейчас не удалось подготовить", reply.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
