from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from clientplatform.domain.creative_generation import CreativeGenerationReceiptStatus
from handlers import clientplatform_creative_studio as studio


def _labels_and_callbacks(markup):
    return [
        (button.text, button.callback_data)
        for row in markup.inline_keyboard
        for button in row
    ]


def test_creative_studio_menu_exposes_image_and_video_entry_points() -> None:
    rows = _labels_and_callbacks(studio._menu_rows("business-token", None))
    assert ("✨ Создать картинку", "cpc:new:business-token") in rows
    assert ("🎬 Создать AI-видео", "cpc:video:business-token") in rows


def test_creative_studio_menu_labels_motion_fallback_truthfully() -> None:
    rows = _labels_and_callbacks(
        studio._menu_rows(
            "business-token",
            None,
            video_ready=True,
            video_mode="motion",
        )
    )
    assert ("🎞 Оживить картинку", "cpc:video:business-token") in rows
    assert ("🎬 Создать AI-видео", "cpc:video:business-token") not in rows


def test_prepared_video_keeps_video_edit_path(monkeypatch) -> None:
    active = SimpleNamespace(
        status=CreativeGenerationReceiptStatus.PREPARED,
        delivery_claimed_at=None,
    )
    monkeypatch.setattr(studio, "_receipt_kind", lambda _receipt: "video")
    monkeypatch.setattr(
        studio,
        "_receipt_callback",
        lambda action, token, receipt: f"receipt:{action}:{token}",
    )

    rows = _labels_and_callbacks(studio._menu_rows("business-token", active))
    assert ("✏️ Изменить описание", "cpc:video:business-token") in rows


def test_result_menu_keeps_video_creation_visible() -> None:
    rows = _labels_and_callbacks(studio._result_rows("business-token"))
    assert ("✨ Создать ещё картинку", "cpc:new:business-token") in rows
    assert ("🎬 Создать видео", "cpc:video:business-token") in rows

def test_prepared_image_can_switch_to_video(monkeypatch) -> None:
    active = SimpleNamespace(
        status=CreativeGenerationReceiptStatus.PREPARED,
        delivery_claimed_at=None,
    )
    monkeypatch.setattr(studio, "_receipt_kind", lambda _receipt: "image")
    monkeypatch.setattr(
        studio,
        "_receipt_callback",
        lambda action, token, receipt: f"receipt:{action}:{token}",
    )

    rows = _labels_and_callbacks(studio._menu_rows("business-token", active))
    assert ("🎬 Вместо этого видео", "cpc:video:business-token") in rows


def test_prepared_video_can_switch_to_image(monkeypatch) -> None:
    active = SimpleNamespace(
        status=CreativeGenerationReceiptStatus.PREPARED,
        delivery_claimed_at=None,
    )
    monkeypatch.setattr(studio, "_receipt_kind", lambda _receipt: "video")
    monkeypatch.setattr(
        studio,
        "_receipt_callback",
        lambda action, token, receipt: f"receipt:{action}:{token}",
    )

    rows = _labels_and_callbacks(studio._menu_rows("business-token", active))
    assert ("✨ Вместо этого картинка", "cpc:new:business-token") in rows



def test_creative_menu_shows_unavailable_generation_truthfully() -> None:
    rows = _labels_and_callbacks(
        studio._menu_rows(
            "business-token",
            None,
            image_ready=False,
            video_ready=False,
        )
    )
    assert (
        "⚠️ Картинки недоступны",
        "cpc:status:business-token:image",
    ) in rows
    assert (
        "⚠️ Видео недоступно",
        "cpc:status:business-token:video",
    ) in rows
    assert ("✨ Создать картинку", "cpc:new:business-token") not in rows
    assert ("🎬 Создать AI-видео", "cpc:video:business-token") not in rows
    assert ("🎞 Оживить картинку", "cpc:video:business-token") not in rows

def test_new_visual_button_surfaces_actionable_active_generation(monkeypatch) -> None:
    target = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        data="cpc:new:business-token",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=101),
        message=target,
    )
    state = SimpleNamespace(set_state=AsyncMock(), set_data=AsyncMock())
    active = SimpleNamespace(
        id="receipt-id",
        status=CreativeGenerationReceiptStatus.RUNNING,
        delivery_claimed_at=None,
    )
    monkeypatch.setattr(
        studio,
        "_actor_for_callback",
        AsyncMock(return_value=SimpleNamespace(business_id="business-id")),
    )
    monkeypatch.setattr(studio, "_active", AsyncMock(return_value=active))
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(studio, "_menu_rows", lambda token, receipt: [["continue", token, receipt]])

    asyncio.run(studio._ask_creative_prompt(callback, state, kind="image"))

    callback.answer.assert_awaited_once_with()
    state.set_state.assert_not_awaited()
    state.set_data.assert_not_awaited()
    assert "незавершённая генерация" in target.answer.await_args.args[0]
    assert target.answer.await_args.kwargs["reply_markup"] == [["continue", "business-token", active]]


def test_stale_video_callback_fails_closed_when_provider_is_unavailable(monkeypatch) -> None:
    callback = SimpleNamespace(
        data="cpc:video:business-token",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=101),
    )
    state = SimpleNamespace(set_state=AsyncMock(), set_data=AsyncMock())
    monkeypatch.setattr(
        studio,
        "_actor_for_callback",
        AsyncMock(return_value=SimpleNamespace(business_id="business-id")),
    )
    monkeypatch.setattr(studio, "_active", AsyncMock(return_value=None))
    monkeypatch.setattr(
        studio,
        "visual_video_generation_mode",
        lambda **_kwargs: "unavailable",
    )

    asyncio.run(studio._ask_creative_prompt(callback, state, kind="video"))

    state.set_state.assert_not_awaited()
    state.set_data.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True
    assert "недоступно" in callback.answer.await_args.args[0].casefold()


def test_motion_mode_prompt_does_not_claim_native_video(monkeypatch) -> None:
    target = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        data="cpc:video:business-token",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=101),
        message=target,
    )
    state = SimpleNamespace(set_state=AsyncMock(), set_data=AsyncMock())
    monkeypatch.setattr(
        studio,
        "_actor_for_callback",
        AsyncMock(return_value=SimpleNamespace(business_id="business-id")),
    )
    monkeypatch.setattr(studio, "_active", AsyncMock(return_value=None))
    monkeypatch.setattr(
        studio,
        "visual_video_generation_mode",
        lambda **_kwargs: "motion",
    )
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(studio.control, "_keyboard", lambda rows: rows)

    asyncio.run(studio._ask_creative_prompt(callback, state, kind="video"))

    state.set_state.assert_awaited_once()
    text = target.answer.await_args.args[0]
    assert "Это не генерация движущейся сцены" in text
    assert "AI-кадр" in text


def test_video_status_explains_motion_fallback(monkeypatch) -> None:
    target = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        data="cpc:status:business-token:video",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=101),
        message=target,
    )
    monkeypatch.setattr(studio, "_actor_for_callback", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        studio,
        "visual_video_generation_mode",
        lambda **_kwargs: "motion",
    )
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(studio.control, "_keyboard", lambda rows: rows)

    asyncio.run(studio.creative_provider_status(callback))

    text = target.answer.await_args.args[0]
    assert "оживление AI-кадра" in text
    assert "не генерация движущейся сцены" in text


def test_generate_video_status_does_not_promise_unbounded_auto_delivery(monkeypatch) -> None:
    target = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        data="cpc:generate:business-token:receipt-token",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=101),
        message=target,
    )
    state = SimpleNamespace()
    actor = SimpleNamespace(business_id="business-id")
    receipt = SimpleNamespace(
        id="receipt-id",
        provider_payload_json="payload",
    )
    continue_generation = AsyncMock()

    monkeypatch.setattr(studio, "_actor_for_callback", AsyncMock(return_value=actor))
    monkeypatch.setattr(
        studio,
        "_receipt_for_callback",
        AsyncMock(return_value=receipt),
    )
    monkeypatch.setattr(studio, "_receipt_kind", lambda _receipt: "video")
    monkeypatch.setattr(studio, "_continue_generation", continue_generation)
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)

    asyncio.run(studio.generate_creative_image(callback, state))

    status = target.answer.await_args.args[0]
    assert "в ближайшую минуту" in status
    assert "кнопку проверки готовности" in status
    assert "повторный платный job не запускается" in status
    assert "отправлю сюда автоматически" not in status
    continue_generation.assert_awaited_once_with(
        callback,
        actor=actor,
        receipt=receipt,
        auto_wait=True,
    )


def test_active_video_delivery_recovery_names_video_not_picture(monkeypatch) -> None:
    active = SimpleNamespace(
        status=CreativeGenerationReceiptStatus.SUCCEEDED,
        delivery_claimed_at="2026-10-02T00:00:00+00:00",
    )
    monkeypatch.setattr(studio, "_receipt_kind", lambda _receipt: "video")
    text_target = SimpleNamespace(answer=AsyncMock())
    actor = SimpleNamespace(
        user_id=101,
        business_id="business-id",
        assert_can_manage_promotions=lambda: None,
    )

    monkeypatch.setattr(studio.control, "_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(studio, "_active", AsyncMock(return_value=active))
    monkeypatch.setattr(
        studio,
        "_retire_unavailable_completed_receipt",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        studio,
        "visual_generation_ready",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        studio,
        "visual_video_generation_mode",
        lambda **_kwargs: "motion",
    )
    monkeypatch.setattr(studio.control, "_uuid_token", lambda _value: "business-token")
    monkeypatch.setattr(studio, "_menu_rows", lambda *_args, **_kwargs: None)

    asyncio.run(
        studio.send_creative_studio_menu(
            text_target,
            user_id=101,
            business_id="business-id",
        )
    )

    body = text_target.answer.await_args.args[0]
    assert "Отправка готового видео" in body
    assert "готовой картинки" not in body


def test_result_menu_exposes_download_for_completed_receipt(monkeypatch) -> None:
    receipt = SimpleNamespace(source_job_id="job-123")
    monkeypatch.setattr(
        studio,
        "_receipt_callback",
        lambda action, token, _receipt: f"receipt:{action}:{token}",
    )

    rows = _labels_and_callbacks(studio._result_rows("business-token", receipt))

    assert ("📥 Скачать файл", "receipt:download:business-token") in rows


def test_owner_delivery_survives_secondary_event_asset_failure(monkeypatch, tmp_path) -> None:
    asset = tmp_path / "creative.jpg"
    asset.write_bytes(b"jpeg")
    target = SimpleNamespace(
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
        answer_video=AsyncMock(),
        answer_document=AsyncMock(),
    )
    callback = SimpleNamespace(from_user=SimpleNamespace(id=101))
    actor = SimpleNamespace(business_id="business-id")
    receipt = SimpleNamespace(
        id="receipt-id",
        request_text="ёж слушает метро",
        provider_payload_json="payload",
        source_job_id="job-123",
        delivery_claimed_at=None,
    )
    job = SimpleNamespace(
        status="succeeded",
        asset_ready=True,
        kind="image",
        provider="yandexart",
        mime_type="image/jpeg",
    )

    async def immediate_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(studio.asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(studio.control, "_uuid_token", lambda _value: "business-token")
    monkeypatch.setattr(studio, "materialize_ad_visual", lambda *_args, **_kwargs: asset)
    monkeypatch.setattr(studio, "claim_creative_generation_delivery", lambda **_kwargs: True)
    mark = Mock()
    monkeypatch.setattr(studio, "mark_creative_generation_delivered", mark)
    monkeypatch.setattr(
        studio,
        "frozen_business_visual_binding",
        lambda _payload: {
            "type": "event_content",
            "event_id": "event-id",
            "stage": "warmup",
            "slot_key": "hero",
            "kind": "image",
        },
    )
    monkeypatch.setattr(
        studio,
        "store_generated_event_content_asset",
        Mock(side_effect=studio.EventContentAssetError("storage failed")),
    )

    result = asyncio.run(
        studio._finish_visual(
            callback,
            actor=actor,
            receipt=receipt,
            job=job,
        )
    )

    assert result is True
    target.answer_photo.assert_awaited_once()
    mark.assert_called_once()
    assert "📥 Скачать файл" in [
        button.text
        for row in target.answer.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]


def test_video_delivery_enables_streaming(monkeypatch, tmp_path) -> None:
    asset = tmp_path / "creative.mp4"
    asset.write_bytes(b"video")
    target = SimpleNamespace(
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
        answer_video=AsyncMock(),
        answer_document=AsyncMock(),
    )
    callback = SimpleNamespace(from_user=SimpleNamespace(id=101))
    actor = SimpleNamespace(business_id="business-id")
    receipt = SimpleNamespace(
        id="receipt-id",
        request_text="короткое видео",
        provider_payload_json="payload",
        source_job_id="job-123",
    )
    job = SimpleNamespace(
        status="succeeded",
        asset_ready=True,
        kind="video",
        provider="yandexart_motion",
        mime_type="video/mp4",
    )

    async def immediate_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(studio.asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(studio.control, "_uuid_token", lambda _value: "business-token")
    monkeypatch.setattr(studio, "materialize_ad_visual", lambda *_args, **_kwargs: asset)
    monkeypatch.setattr(studio, "claim_creative_generation_delivery", lambda **_kwargs: True)
    monkeypatch.setattr(studio, "mark_creative_generation_delivered", lambda **_kwargs: None)
    monkeypatch.setattr(studio, "frozen_business_visual_binding", lambda _payload: None)

    result = asyncio.run(
        studio._finish_visual(
            callback,
            actor=actor,
            receipt=receipt,
            job=job,
        )
    )

    assert result is True
    target.answer_video.assert_awaited_once()
    assert target.answer_video.await_args.kwargs["supports_streaming"] is True


def test_download_creative_file_sends_document(monkeypatch, tmp_path) -> None:
    asset = tmp_path / "creative.jpg"
    asset.write_bytes(b"jpeg")
    target = SimpleNamespace(answer_document=AsyncMock())
    callback = SimpleNamespace(
        data="cpc:download:business-token:receipt-token",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=101),
    )
    state = SimpleNamespace()
    actor = SimpleNamespace(business_id="business-id")
    receipt = SimpleNamespace(source_job_id="job-123")
    job = SimpleNamespace(
        id="job-123",
        status="succeeded",
        asset_ready=True,
        kind="image",
    )

    async def immediate_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(studio.asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(studio, "_actor_for_callback", AsyncMock(return_value=actor))
    monkeypatch.setattr(studio, "_receipt_for_callback", AsyncMock(return_value=receipt))
    monkeypatch.setattr(studio, "poll_ad_visual", lambda **_kwargs: job)
    monkeypatch.setattr(
        studio,
        "materialize_ad_visual",
        lambda *_args, **_kwargs: asset,
    )
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)

    asyncio.run(studio.download_creative_file(callback, state))

    callback.answer.assert_awaited_once_with("Готовлю файл…")
    target.answer_document.assert_awaited_once()
    document = target.answer_document.await_args.args[0]
    assert document.filename == "clientplatform-image.jpg"
    assert target.answer_document.await_args.kwargs["caption"] == "📥 Файл для сохранения"


def test_download_creative_file_reports_expired_asset(monkeypatch) -> None:
    callback = SimpleNamespace(
        data="cpc:download:business-token:receipt-token",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=101),
    )
    state = SimpleNamespace()
    actor = SimpleNamespace(business_id="business-id")
    receipt = SimpleNamespace(source_job_id="job-123")
    job = SimpleNamespace(status="succeeded", asset_ready=False, kind="image")

    async def immediate_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(studio.asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(studio, "_actor_for_callback", AsyncMock(return_value=actor))
    monkeypatch.setattr(studio, "_receipt_for_callback", AsyncMock(return_value=receipt))
    monkeypatch.setattr(studio, "poll_ad_visual", lambda **_kwargs: job)

    asyncio.run(studio.download_creative_file(callback, state))

    callback.answer.assert_awaited_once_with(
        "Файл уже недоступен. Создайте визуал заново.",
        show_alert=True,
    )


def test_stale_succeeded_receipt_is_retired_only_after_authoritative_asset_loss(
    monkeypatch,
) -> None:
    actor = SimpleNamespace(business_id="business-id")
    receipt = SimpleNamespace(
        id="receipt-id",
        status=CreativeGenerationReceiptStatus.SUCCEEDED,
        source_job_id="job-123",
    )
    job = SimpleNamespace(status="succeeded", asset_ready=False)
    abandon = Mock(return_value=True)
    monkeypatch.setattr(studio, "poll_ad_visual", lambda **_kwargs: job)
    monkeypatch.setattr(studio, "abandon_creative_generation", abandon)

    retired = asyncio.run(
        studio._retire_unavailable_completed_receipt(actor, receipt)
    )

    assert retired is True
    abandon.assert_called_once_with(actor=actor, receipt_id="receipt-id")


def test_stale_succeeded_receipt_is_preserved_when_provider_state_is_ambiguous(
    monkeypatch,
) -> None:
    actor = SimpleNamespace(business_id="business-id")
    receipt = SimpleNamespace(
        id="receipt-id",
        status=CreativeGenerationReceiptStatus.SUCCEEDED,
        source_job_id="job-123",
    )
    abandon = Mock()

    def unavailable_poll(**_kwargs):
        raise studio.VisualCreativeError("temporary provider failure")

    monkeypatch.setattr(studio, "poll_ad_visual", unavailable_poll)
    monkeypatch.setattr(studio, "abandon_creative_generation", abandon)

    retired = asyncio.run(
        studio._retire_unavailable_completed_receipt(actor, receipt)
    )

    assert retired is False
    abandon.assert_not_called()


def test_video_button_recovers_from_stale_completed_image_and_keeps_video_flow(
    monkeypatch,
) -> None:
    target = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        data="cpc:video:business-token",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=101),
        message=target,
    )
    state = SimpleNamespace(set_state=AsyncMock(), set_data=AsyncMock())
    actor = SimpleNamespace(business_id="business-id")
    stale = SimpleNamespace(
        id="receipt-id",
        status=CreativeGenerationReceiptStatus.SUCCEEDED,
        source_job_id="job-123",
    )
    gone = SimpleNamespace(status="succeeded", asset_ready=False)
    abandon = Mock(return_value=True)

    monkeypatch.setattr(
        studio,
        "_actor_for_callback",
        AsyncMock(return_value=actor),
    )
    monkeypatch.setattr(studio, "_active", AsyncMock(side_effect=[stale, None]))
    monkeypatch.setattr(studio, "poll_ad_visual", lambda **_kwargs: gone)
    monkeypatch.setattr(studio, "abandon_creative_generation", abandon)
    monkeypatch.setattr(
        studio,
        "visual_video_generation_mode",
        lambda **_kwargs: "motion",
    )
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(studio.control, "_keyboard", lambda rows: rows)

    asyncio.run(studio._ask_creative_prompt(callback, state, kind="video"))

    abandon.assert_called_once_with(actor=actor, receipt_id="receipt-id")
    state.set_state.assert_awaited_once()
    state.set_data.assert_awaited_once()
    callback.answer.assert_awaited_once_with()
    assert "Какое видео создать?" in target.answer.await_args.args[0]
    assert "AI-кадр" in target.answer.await_args.args[0]


def test_ambiguous_submit_blocks_automatic_retry_and_requires_explicit_resolution(
    monkeypatch,
) -> None:
    target = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(message=target)
    actor = SimpleNamespace(business_id="business-id")
    receipt = SimpleNamespace(
        id="receipt-id",
        status=CreativeGenerationReceiptStatus.RUNNING,
        source_job_id="gateway-job-1",
        delivery_claimed_at=None,
    )
    job = SimpleNamespace(
        status="failed",
        asset_ready=False,
        error_code="visual_gateway_submit_ambiguous",
    )

    monkeypatch.setattr(
        studio,
        "_poll_existing",
        AsyncMock(return_value=(receipt, job)),
    )
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(
        studio.control,
        "_uuid_token",
        lambda value: "receipt-token" if value == "receipt-id" else "business-token",
    )

    asyncio.run(
        studio._continue_generation(
            callback,
            actor=actor,
            receipt=receipt,
        )
    )

    text = target.answer.await_args.args[0]
    rows = _labels_and_callbacks(target.answer.await_args.kwargs["reply_markup"])
    assert "не запускает новый платный job автоматически" in text
    assert "мог уже быть принят провайдером" in text
    assert (
        "⚠️ Завершить неопределённый запрос",
        "cpc:resolve:business-token:receipt-token",
    ) in rows


def test_explicit_ambiguous_resolution_warns_about_possible_prior_spend(
    monkeypatch,
) -> None:
    target = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        data="cpc:resolve:business-token:receipt-token",
        answer=AsyncMock(),
        from_user=SimpleNamespace(id=101),
        message=target,
    )
    state = SimpleNamespace()
    actor = SimpleNamespace(business_id="business-id")
    receipt = SimpleNamespace(id="receipt-id")

    async def immediate_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(studio.asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(studio, "_actor_for_callback", AsyncMock(return_value=actor))
    monkeypatch.setattr(
        studio,
        "_receipt_for_callback",
        AsyncMock(return_value=receipt),
    )
    monkeypatch.setattr(
        studio,
        "abandon_ambiguous_creative_generation",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(studio, "_result_rows", lambda token: [["result", token]])

    asyncio.run(studio.resolve_ambiguous_creative_generation(callback, state))

    callback.answer.assert_awaited_once_with()
    text = target.answer.await_args.args[0]
    assert "предыдущий расход или результат нельзя полностью исключить" in text
    assert "отдельного подтверждения" in text
    assert target.answer.await_args.kwargs["reply_markup"] == [
        ["result", "business-token"]
    ]


def test_checking_expired_completed_asset_ends_deadlock_and_restores_both_entries(
    monkeypatch,
) -> None:
    target = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(message=target)
    actor = SimpleNamespace(business_id="business-id")
    receipt = SimpleNamespace(
        id="receipt-id",
        status=CreativeGenerationReceiptStatus.SUCCEEDED,
        source_job_id="job-123",
        delivery_claimed_at=None,
    )
    job = SimpleNamespace(status="succeeded", asset_ready=False)

    monkeypatch.setattr(
        studio,
        "_poll_existing",
        AsyncMock(return_value=(receipt, job)),
    )
    monkeypatch.setattr(
        studio,
        "_retire_unavailable_completed_receipt",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(studio.control, "_callback_message", lambda _callback: target)
    monkeypatch.setattr(studio.control, "_uuid_token", lambda _value: "business-token")

    asyncio.run(
        studio._continue_generation(
            callback,
            actor=actor,
            receipt=receipt,
        )
    )

    text = target.answer.await_args.args[0]
    rows = _labels_and_callbacks(target.answer.await_args.kwargs["reply_markup"])
    assert "Старый результат завершён" in text
    assert ("✨ Создать ещё картинку", "cpc:new:business-token") in rows
    assert ("🎬 Создать видео", "cpc:video:business-token") in rows


def test_transient_receipt_reconciliation_covers_safe_noop_states(monkeypatch) -> None:
    actor = SimpleNamespace(business_id="business-id")
    abandon = Mock()
    poll = Mock()
    monkeypatch.setattr(studio, "abandon_creative_generation", abandon)
    monkeypatch.setattr(studio, "poll_ad_visual", poll)

    assert asyncio.run(
        studio._retire_unavailable_completed_receipt(actor, None)
    ) is False
    assert asyncio.run(
        studio._retire_unavailable_completed_receipt(
            actor,
            SimpleNamespace(
                id="running-receipt",
                status=CreativeGenerationReceiptStatus.RUNNING,
                source_job_id="job-running",
            ),
        )
    ) is False
    assert asyncio.run(
        studio._retire_unavailable_completed_receipt(
            actor,
            SimpleNamespace(
                id="missing-job-receipt",
                status=CreativeGenerationReceiptStatus.SUCCEEDED,
                source_job_id="",
            ),
        )
    ) is False

    succeeded = SimpleNamespace(
        id="receipt-id",
        status=CreativeGenerationReceiptStatus.SUCCEEDED,
        source_job_id="job-123",
    )
    assert asyncio.run(
        studio._retire_unavailable_completed_receipt(
            actor,
            succeeded,
            job=SimpleNamespace(status="running", asset_ready=False),
        )
    ) is False
    assert asyncio.run(
        studio._retire_unavailable_completed_receipt(
            actor,
            succeeded,
            job=SimpleNamespace(status="succeeded", asset_ready=True),
        )
    ) is False

    poll.assert_not_called()
    abandon.assert_not_called()


def test_transient_receipt_reconciliation_treats_concurrent_cleanup_as_terminal(
    monkeypatch,
) -> None:
    actor = SimpleNamespace(business_id="business-id")
    receipt = SimpleNamespace(
        id="receipt-id",
        status=CreativeGenerationReceiptStatus.SUCCEEDED,
        source_job_id="job-123",
    )

    def already_gone(**_kwargs):
        raise LookupError("already retired")

    monkeypatch.setattr(studio, "abandon_creative_generation", already_gone)

    retired = asyncio.run(
        studio._retire_unavailable_completed_receipt(
            actor,
            receipt,
            job=SimpleNamespace(status="succeeded", asset_ready=False),
        )
    )

    assert retired is True


def test_opening_studio_reconciles_expired_success_before_rendering_menu(
    monkeypatch,
) -> None:
    message = SimpleNamespace(answer=AsyncMock())
    actor = SimpleNamespace(
        business_id="business-id",
        assert_can_manage_promotions=Mock(),
    )
    stale = SimpleNamespace(
        id="receipt-id",
        status=CreativeGenerationReceiptStatus.SUCCEEDED,
        source_job_id="job-123",
        delivery_claimed_at=None,
    )

    monkeypatch.setattr(studio.control, "_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(studio.control, "_uuid_token", lambda _value: "business-token")
    monkeypatch.setattr(studio, "_active", AsyncMock(side_effect=[stale, None]))
    monkeypatch.setattr(
        studio,
        "_retire_unavailable_completed_receipt",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        studio,
        "visual_generation_ready",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        studio,
        "visual_video_generation_mode",
        lambda **_kwargs: "motion",
    )

    asyncio.run(
        studio.send_creative_studio_menu(
            message,
            user_id=101,
            business_id="business-id",
        )
    )

    text = message.answer.await_args.args[0]
    rows = _labels_and_callbacks(message.answer.await_args.kwargs["reply_markup"])
    assert "Выберите, что хотите создать" in text
    assert ("✨ Создать картинку", "cpc:new:business-token") in rows
    assert ("🎞 Оживить картинку", "cpc:video:business-token") in rows
