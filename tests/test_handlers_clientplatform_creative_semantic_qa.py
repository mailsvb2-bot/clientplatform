from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import handlers.clientplatform_creative_studio as studio


def _immediate_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def _receipt() -> SimpleNamespace:
    return SimpleNamespace(
        id="receipt-1",
        request_text="предмет постепенно меняется",
        provider_payload_json="frozen-payload",
        source_job_id="job-1",
    )


def _job() -> SimpleNamespace:
    return SimpleNamespace(
        id="job-1",
        provider="fake",
        kind="image",
        status="succeeded",
        asset_ready=True,
        mime_type="image/png",
    )


def _target() -> SimpleNamespace:
    return SimpleNamespace(
        answer=AsyncMock(),
        answer_photo=AsyncMock(),
        answer_video=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_semantic_qa_labels_questionable_image_before_success_caption() -> None:
    target = _target()
    review = MagicMock(
        return_value=SimpleNamespace(
            status="needs_review",
            issues=("не видно запрошенного изменения",),
            summary="",
        )
    )

    def materialize(_job, *, output_dir, **_kwargs):
        path = Path(output_dir) / "result.png"
        path.write_bytes(b"png")
        return path

    with (
        patch.object(studio.asyncio, "to_thread", new=_immediate_to_thread),
        patch.object(studio.control, "_callback_message", return_value=target),
        patch.object(studio.control, "_uuid_token", return_value="token"),
        patch.object(studio.control, "_keyboard", side_effect=lambda rows: rows),
        patch.object(studio, "_receipt_kind", return_value="image"),
        patch.object(studio, "materialize_ad_visual", side_effect=materialize),
        patch.object(studio, "frozen_business_visual_binding", return_value=None),
        patch.object(studio, "claim_creative_generation_delivery", return_value=True),
        patch.object(studio, "mark_creative_generation_delivered"),
        patch.object(
            studio,
            "review_business_image_semantics_from_frozen_payload",
            review,
        ),
    ):
        completed = await studio._finish_visual(
            SimpleNamespace(),
            actor=SimpleNamespace(business_id="business-1"),
            receipt=_receipt(),
            job=_job(),
        )

    assert completed is True
    assert review.call_count == 1
    target.answer_photo.assert_awaited_once()
    caption = target.answer_photo.await_args.kwargs["caption"]
    assert caption.startswith("⚠️ Картинка сгенерирована")
    assert "проверить соответствие исходному запросу" in caption
    messages = [call.args[0] for call in target.answer.await_args_list]
    assert any("не видно запрошенного изменения" in item for item in messages)
    assert any("Новую генерацию я не запускала" in item for item in messages)


@pytest.mark.asyncio
async def test_semantic_qa_pass_keeps_normal_ready_caption() -> None:
    target = _target()
    review = MagicMock(
        return_value=SimpleNamespace(status="pass", issues=(), summary="")
    )

    def materialize(_job, *, output_dir, **_kwargs):
        path = Path(output_dir) / "result.png"
        path.write_bytes(b"png")
        return path

    with (
        patch.object(studio.asyncio, "to_thread", new=_immediate_to_thread),
        patch.object(studio.control, "_callback_message", return_value=target),
        patch.object(studio.control, "_uuid_token", return_value="token"),
        patch.object(studio.control, "_keyboard", side_effect=lambda rows: rows),
        patch.object(studio, "_receipt_kind", return_value="image"),
        patch.object(studio, "materialize_ad_visual", side_effect=materialize),
        patch.object(studio, "frozen_business_visual_binding", return_value=None),
        patch.object(studio, "claim_creative_generation_delivery", return_value=True),
        patch.object(studio, "mark_creative_generation_delivered"),
        patch.object(
            studio,
            "review_business_image_semantics_from_frozen_payload",
            review,
        ),
    ):
        completed = await studio._finish_visual(
            SimpleNamespace(),
            actor=SimpleNamespace(business_id="business-1"),
            receipt=_receipt(),
            job=_job(),
        )

    assert completed is True
    target.answer_photo.assert_awaited_once()
    assert target.answer_photo.await_args.kwargs["caption"] == "✅ Картинка готова"
    messages = [call.args[0] for call in target.answer.await_args_list]
    assert all("Автопроверка смысла" not in item for item in messages)
