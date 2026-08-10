from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clientplatform.application.visual_creatives import VisualCreativeError
from handlers import clientplatform_ad_connections as ui


async def immediate_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def callback(data: str = "cpa:creative:image"):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )


def state(data: dict):
    return SimpleNamespace(
        get_data=AsyncMock(return_value=dict(data)),
        update_data=AsyncMock(),
        set_state=AsyncMock(),
        clear=AsyncMock(),
    )


def visual_job(*, status="queued", kind="image", ready=False):
    return SimpleNamespace(
        id="gateway-job-1",
        provider="fake",
        scope_id="business-id",
        kind=kind,
        status=status,
        model="model",
        asset_ready=ready,
    )


def base_state():
    return {
        "business_id": "business-id",
        "business_token": "business-token",
        "creative_title": "Консультация",
        "creative_body": "Свободное время",
        "creative_job_id": "",
        "job_id": "ad-job",
    }


def target_message():
    return SimpleNamespace(
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
        answer_video=AsyncMock(),
    )


def test_visual_wait_seconds_is_bounded(monkeypatch):
    monkeypatch.setenv("VISUAL_TELEGRAM_WAIT_SECONDS", "bad")
    assert ui._visual_wait_seconds() == 20
    monkeypatch.setenv("VISUAL_TELEGRAM_WAIT_SECONDS", "-10")
    assert ui._visual_wait_seconds() == 0
    monkeypatch.setenv("VISUAL_TELEGRAM_WAIT_SECONDS", "999")
    assert ui._visual_wait_seconds() == 60


@pytest.mark.asyncio
async def test_render_pending_is_tenant_scoped_and_idempotent():
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
    assert "🔄 Проверить визуал" in labels
    assert "➡️ Продолжить без визуала" in labels
    kwargs = create.call_args.kwargs
    assert kwargs["scope_id"] == "business-id"
    assert kwargs["idempotency_key"].startswith("clientplatform:")
    assert len(kwargs["idempotency_key"].removeprefix("clientplatform:")) == 64


@pytest.mark.asyncio
async def test_visual_idempotency_changes_by_kind_not_repeat_click():
    keys = []

    async def run(kind: str):
        cb = callback(f"cpa:creative:{kind}")
        st = state(base_state())
        target = target_message()
        with (
            patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
            patch.object(ui, "create_ad_visual", return_value=visual_job(kind=kind)) as create,
            patch.object(ui, "_message", return_value=target),
            patch.object(ui.control, "_keyboard", side_effect=lambda rows: rows),
        ):
            await ui._render_ad_visual(cb, st, kind=kind)
        keys.append(create.call_args.kwargs["idempotency_key"])

    await run("image")
    await run("image")
    await run("video")
    assert keys[0] == keys[1]
    assert keys[0] != keys[2]


@pytest.mark.asyncio
async def test_stale_generate_button_cannot_overwrite_pending_job():
    cb = callback()
    st = state({**base_state(), "creative_job_id": "already-running"})
    with patch.object(ui, "create_ad_visual") as create:
        await ui._render_ad_visual(cb, st, kind="image")
    create.assert_not_called()
    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_expected_gateway_failure_is_visible_without_losing_text_draft():
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


@pytest.mark.asyncio
async def test_unexpected_runtime_error_is_not_silenced():
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
        with pytest.raisesRegex(RuntimeError, "programming defect"):
            await ui._render_ad_visual(cb, st, kind="image")


@pytest.mark.asyncio
async def test_render_ready_image_materializes_and_sends(tmp_path):
    cb = callback()
    st = state(base_state())
    target = target_message()
    asset = tmp_path / "creative.png"
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
    st.update_data.assert_awaited_with(creative_job_id="")
    assert "текстовый DRAFT" in target.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_render_ready_video_materializes_and_sends(tmp_path):
    cb = callback("cpa:creative:video")
    st = state(base_state())
    target = target_message()
    asset = tmp_path / "creative.mp4"
    asset.write_bytes(b"video")
    with (
        patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
        patch.object(
            ui,
            "create_ad_visual",
            return_value=visual_job(status="succeeded", kind="video", ready=True),
        ),
        patch.object(ui, "materialize_ad_visual", return_value=asset),
        patch.object(ui, "_message", return_value=target),
        patch.object(ui.control, "_keyboard", side_effect=lambda rows: rows),
    ):
        await ui.generate_ad_visual(cb, st)
    target.answer_video.assert_awaited_once()
    target.answer_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_visual_materialization_failure_keeps_text_draft():
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
    assert "Текстовый черновик сохранён" in target.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_render_provider_failure_keeps_text_draft():
    cb = callback()
    st = state(base_state())
    target = target_message()
    with (
        patch.object(ui.asyncio, "to_thread", new=immediate_to_thread),
        patch.object(
            ui,
            "create_ad_visual",
            return_value=visual_job(status="failed"),
        ),
        patch.object(ui, "_message", return_value=target),
    ):
        await ui._render_ad_visual(cb, st, kind="image")
    st.update_data.assert_awaited_with(creative_job_id="")
    assert "Текстовый рекламный черновик" in target.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_refresh_ready_visual_uses_original_business_scope(tmp_path):
    cb = callback("cpa:creative:refresh")
    st = state({**base_state(), "creative_job_id": "gateway-job-1"})
    target = target_message()
    asset = tmp_path / "creative.png"
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
    assert poll_visual.call_args.kwargs["scope_id"] == "business-id"
    st.update_data.assert_awaited_with(creative_job_id="")
    target.answer_photo.assert_awaited_once()
    assert "не прикрепляется" in target.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_refresh_pending_keeps_explicit_refresh_or_skip():
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
    assert "cpa:creative:refresh" in callbacks
    assert "cpa:creative:skip" in callbacks
    st.update_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_failure_clears_pending_state():
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
    assert "завершилась ошибкой" in target.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_refresh_gateway_failure_is_visible_and_pending_state_is_preserved():
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


@pytest.mark.asyncio
async def test_confirmation_is_blocked_while_visual_is_pending():
    cb = callback("cpa:confirm")
    st = state({**base_state(), "creative_job_id": "gateway-job-1"})
    with patch.object(ui.control, "_actor", new=AsyncMock()) as actor:
        await ui.confirm_yandex_publication(cb, st)
    actor.assert_not_awaited()
    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_skip_visual_explicitly_continues_text_draft():
    cb = callback("cpa:creative:skip")
    st = state({**base_state(), "creative_job_id": "gateway-job-1"})
    with patch.object(ui, "confirm_yandex_publication", new=AsyncMock()) as confirm:
        await ui.skip_ad_visual(cb, st)
    st.update_data.assert_awaited_with(creative_job_id="")
    confirm.assert_awaited_once_with(cb, st)


@pytest.mark.asyncio
async def test_prepare_ad_publication_preserves_canonical_draft_and_adds_visual_choices():
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
    assert "cpa:creative:image" in callbacks
    assert "cpa:creative:video" in callbacks
    assert "cpa:confirm" in callbacks
    assert "DRAFT" in message.answer.await_args.args[0]
