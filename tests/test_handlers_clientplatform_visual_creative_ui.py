from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.application.visual_creatives import VisualCreativeError
from handlers import clientplatform_ad_connections as ui


async def immediate_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def callback(data: str = "cpa:creative:image") -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def state(data: dict) -> SimpleNamespace:
    return SimpleNamespace(
        get_data=AsyncMock(return_value=dict(data)),
        update_data=AsyncMock(),
        set_state=AsyncMock(),
        clear=AsyncMock(),
    )


def visual_job(*, status: str = "queued", kind: str = "image", ready: bool = False):
    return SimpleNamespace(
        id="gateway-job-1",
        provider="fake",
        scope_id="business-id",
        kind=kind,
        status=status,
        model="model",
        asset_ready=ready,
    )


def base_state() -> dict[str, str]:
    return {
        "business_id": "business-id",
        "business_token": "business-token",
        "creative_title": "Консультация",
        "creative_body": "Свободное время",
        "creative_job_id": "",
        "job_id": "ad-job",
    }


def target_message() -> SimpleNamespace:
    return SimpleNamespace(
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
        answer_video=AsyncMock(),
    )


class ClientPlatformVisualCreativeUiTests(unittest.IsolatedAsyncioTestCase):
    def test_visual_wait_seconds_is_bounded(self) -> None:
        with patch.dict(os.environ, {"VISUAL_TELEGRAM_WAIT_SECONDS": "bad"}):
            self.assertEqual(ui._visual_wait_seconds(), 20)
        with patch.dict(os.environ, {"VISUAL_TELEGRAM_WAIT_SECONDS": "-10"}):
            self.assertEqual(ui._visual_wait_seconds(), 0)
        with patch.dict(os.environ, {"VISUAL_TELEGRAM_WAIT_SECONDS": "999"}):
            self.assertEqual(ui._visual_wait_seconds(), 60)

    async def test_render_pending_is_tenant_scoped_and_idempotent(self) -> None:
        cb = callback()
        st = state(base_state())
        target = target_message()
        with (
            patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(ui, "create_ad_visual", return_value=visual_job()) as create,
            patch.object(ui, "_message", return_value=target),
            patch.object(ui.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            await ui._render_ad_visual(cb, st, kind="image")
        st.update_data.assert_awaited_with(creative_job_id="gateway-job-1")
        rows = target.answer.await_args.kwargs["reply_markup"]
        labels = [label for row in rows for label, _ in row]
        self.assertIn("🔄 Проверить визуал", labels)
        self.assertIn("➡️ Продолжить без визуала", labels)
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["scope_id"], "business-id")
        self.assertTrue(kwargs["idempotency_key"].startswith("clientplatform:"))
        self.assertEqual(
            len(kwargs["idempotency_key"].removeprefix("clientplatform:")),
            64,
        )

    async def test_visual_idempotency_changes_by_kind_not_repeat_click(self) -> None:
        keys: list[str] = []

        async def run(kind: str) -> None:
            cb = callback(f"cpa:creative:{kind}")
            st = state(base_state())
            target = target_message()
            with (
                patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
                patch.object(
                    ui,
                    "create_ad_visual",
                    return_value=visual_job(kind=kind),
                ) as create,
                patch.object(ui, "_message", return_value=target),
                patch.object(ui.control, "_keyboard", side_effect=lambda rows: rows),
            ):
                await ui._render_ad_visual(cb, st, kind=kind)
            keys.append(create.call_args.kwargs["idempotency_key"])

        await run("image")
        await run("image")
        await run("video")
        self.assertEqual(keys[0], keys[1])
        self.assertNotEqual(keys[0], keys[2])

    async def test_stale_generate_button_cannot_overwrite_pending_job(self) -> None:
        cb = callback()
        st = state({**base_state(), "creative_job_id": "already-running"})
        with patch.object(ui, "create_ad_visual") as create:
            await ui._render_ad_visual(cb, st, kind="image")
        create.assert_not_called()
        cb.answer.assert_awaited_once()
        self.assertTrue(cb.answer.await_args.kwargs["show_alert"])

    async def test_expected_gateway_failure_is_visible_without_losing_text_draft(self) -> None:
        cb = callback()
        st = state(base_state())
        with (
            patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(
                ui,
                "create_ad_visual",
                side_effect=VisualCreativeError("visual unavailable"),
            ),
        ):
            await ui._render_ad_visual(cb, st, kind="image")
        cb.answer.assert_awaited_once_with(
            "Не удалось подготовить визуал",
            show_alert=True,
        )
        st.clear.assert_not_awaited()

    async def test_unexpected_runtime_error_is_not_silenced(self) -> None:
        cb = callback()
        st = state(base_state())
        with (
            patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(
                ui,
                "create_ad_visual",
                side_effect=RuntimeError("programming defect"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "programming defect"):
                await ui._render_ad_visual(cb, st, kind="image")

    async def test_render_ready_image_materializes_and_sends(self) -> None:
        cb = callback()
        st = state(base_state())
        target = target_message()
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "creative.png"
            asset.write_bytes(b"png")
            with (
                patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
                patch.object(
                    ui,
                    "create_ad_visual",
                    return_value=visual_job(status="succeeded", ready=True),
                ),
                patch.object(ui, "materialize_ad_visual", return_value=asset),
                patch.object(ui, "_message", return_value=target),
                patch.object(ui.control, "_keyboard", side_effect=lambda rows: rows),
            ):
                await ui._render_ad_visual(cb, st, kind="image")
        target.answer_photo.assert_awaited_once()
        target.answer_video.assert_not_awaited()
        self.assertEqual(
            target.answer_photo.await_args.kwargs["caption"],
            "Готовое рекламное изображение",
        )
        st.update_data.assert_awaited_with(creative_job_id="")
        self.assertIn("текстовый DRAFT", target.answer.await_args.args[0])

    async def test_render_ready_video_materializes_and_sends(self) -> None:
        cb = callback("cpa:creative:video")
        st = state(base_state())
        target = target_message()
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "creative.mp4"
            asset.write_bytes(b"video")
            with (
                patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
                patch.object(
                    ui,
                    "create_ad_visual",
                    return_value=visual_job(
                        status="succeeded",
                        kind="video",
                        ready=True,
                    ),
                ),
                patch.object(ui, "materialize_ad_visual", return_value=asset),
                patch.object(ui, "_message", return_value=target),
                patch.object(ui.control, "_keyboard", side_effect=lambda rows: rows),
            ):
                await ui.generate_ad_visual(cb, st)
        target.answer_video.assert_awaited_once()
        target.answer_photo.assert_not_awaited()
        self.assertEqual(
            target.answer_video.await_args.kwargs["caption"],
            "Готовое рекламное видео",
        )

    async def test_ready_visual_materialization_failure_keeps_text_draft(self) -> None:
        cb = callback()
        st = state(base_state())
        target = target_message()
        with (
            patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(
                ui,
                "create_ad_visual",
                return_value=visual_job(status="succeeded", ready=True),
            ),
            patch.object(
                ui,
                "materialize_ad_visual",
                side_effect=VisualCreativeError("download"),
            ),
            patch.object(ui, "_message", return_value=target),
        ):
            await ui._render_ad_visual(cb, st, kind="image")
        st.update_data.assert_awaited_with(creative_job_id="")
        self.assertIn("Текстовый черновик сохранён", target.answer.await_args.args[0])

    async def test_render_provider_failure_keeps_text_draft(self) -> None:
        cb = callback()
        st = state(base_state())
        target = target_message()
        with (
            patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(ui, "create_ad_visual", return_value=visual_job(status="failed")),
            patch.object(ui, "_message", return_value=target),
        ):
            await ui._render_ad_visual(cb, st, kind="image")
        st.update_data.assert_awaited_with(creative_job_id="")
        message = target.answer.await_args.args[0]
        self.assertIn("Текстовый рекламный черновик", message)
        self.assertNotIn("провайдер", message.casefold())
        self.assertNotIn("конфигурац", message.casefold())

    async def test_refresh_ready_visual_uses_original_business_scope(self) -> None:
        cb = callback("cpa:creative:refresh")
        st = state({**base_state(), "creative_job_id": "gateway-job-1"})
        target = target_message()
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "creative.png"
            asset.write_bytes(b"png")
            with (
                patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
                patch.object(
                    ui,
                    "poll_ad_visual",
                    return_value=visual_job(status="succeeded", ready=True),
                ) as poll_visual,
                patch.object(ui, "materialize_ad_visual", return_value=asset),
                patch.object(ui, "_message", return_value=target),
                patch.object(ui.control, "_keyboard", side_effect=lambda rows: rows),
            ):
                await ui.refresh_ad_visual(cb, st)
        self.assertEqual(poll_visual.call_args.kwargs["scope_id"], "business-id")
        st.update_data.assert_awaited_with(creative_job_id="")
        target.answer_photo.assert_awaited_once()
        self.assertIn("не прикрепляется", target.answer.await_args.args[0])

    async def test_refresh_pending_keeps_explicit_refresh_or_skip(self) -> None:
        cb = callback("cpa:creative:refresh")
        st = state({**base_state(), "creative_job_id": "gateway-job-1"})
        target = target_message()
        with (
            patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(ui, "poll_ad_visual", return_value=visual_job(status="running")),
            patch.object(ui, "_message", return_value=target),
            patch.object(ui.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            await ui.refresh_ad_visual(cb, st)
        rows = target.answer.await_args.kwargs["reply_markup"]
        callbacks = [value for row in rows for _, value in row]
        self.assertIn("cpa:creative:refresh", callbacks)
        self.assertIn("cpa:creative:skip", callbacks)
        st.update_data.assert_not_awaited()

    async def test_refresh_failure_clears_pending_state(self) -> None:
        cb = callback("cpa:creative:refresh")
        st = state({**base_state(), "creative_job_id": "gateway-job-1"})
        target = target_message()
        with (
            patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(ui, "poll_ad_visual", return_value=visual_job(status="failed")),
            patch.object(ui, "_message", return_value=target),
        ):
            await ui.refresh_ad_visual(cb, st)
        st.update_data.assert_awaited_with(creative_job_id="")
        self.assertIn("завершилась ошибкой", target.answer.await_args.args[0])

    async def test_refresh_gateway_failure_preserves_pending_state(self) -> None:
        cb = callback("cpa:creative:refresh")
        st = state({**base_state(), "creative_job_id": "gateway-job-1"})
        with (
            patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(
                ui,
                "poll_ad_visual",
                side_effect=VisualCreativeError("gateway down"),
            ),
        ):
            await ui.refresh_ad_visual(cb, st)
        cb.answer.assert_awaited_once_with(
            "Не удалось проверить визуал",
            show_alert=True,
        )
        st.update_data.assert_not_awaited()

    async def test_confirmation_is_blocked_while_visual_is_pending(self) -> None:
        cb = callback("cpa:confirm")
        st = state({**base_state(), "creative_job_id": "gateway-job-1"})
        with patch.object(ui.control, "_actor", new=AsyncMock()) as actor:
            await ui.confirm_yandex_publication(cb, st)
        actor.assert_not_awaited()
        cb.answer.assert_awaited_once()
        self.assertTrue(cb.answer.await_args.kwargs["show_alert"])

    async def test_skip_visual_explicitly_continues_text_draft(self) -> None:
        cb = callback("cpa:creative:skip")
        st = state({**base_state(), "creative_job_id": "gateway-job-1"})
        with patch.object(ui, "confirm_yandex_publication", new=AsyncMock()) as confirm:
            await ui.skip_ad_visual(cb, st)
        st.update_data.assert_awaited_with(creative_job_id="")
        confirm.assert_awaited_once_with(cb, st)

    async def test_prepare_ad_publication_preserves_canonical_draft_and_adds_visual_choices(self) -> None:
        message = SimpleNamespace(text="47", answer=AsyncMock())
        st = state(
            {
                "business_id": "business-id",
                "business_token": "business-token",
                "promotion_campaign_id": "promotion-id",
                "connection_id": "connection-id",
                "external_campaign_id": "campaign-id",
                "external_campaign_name": "Campaign",
                "source_url": "https://t.me/bot?start=token",
            }
        )
        draft = SimpleNamespace(
            campaign_name="Campaign",
            job=SimpleNamespace(
                id="ad-job",
                region_ids=(47,),
                title="Consultation",
                text="Open slot",
                source_url="https://t.me/bot?start=token",
            ),
        )
        with (
            patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(ui, "normalize_region_ids", return_value=(47,)),
            patch.object(ui.control, "_user_id", return_value=101),
            patch.object(ui.control, "_actor", new=AsyncMock(return_value=object())),
            patch.object(ui, "create_ad_publication_draft", return_value=draft),
            patch.object(ui.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            await ui.prepare_ad_publication(message, st)
        st.update_data.assert_awaited_once_with(
            job_id="ad-job",
            creative_title="Consultation",
            creative_body="Open slot",
            creative_job_id="",
        )
        rows = message.answer.await_args.kwargs["reply_markup"]
        callbacks = [value for row in rows for _, value in row]
        self.assertIn("cpa:creative:image", callbacks)
        self.assertIn("cpa:creative:video", callbacks)
        self.assertIn("cpa:confirm", callbacks)
        self.assertIn("DRAFT", message.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
