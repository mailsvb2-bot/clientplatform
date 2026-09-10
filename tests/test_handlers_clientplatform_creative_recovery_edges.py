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

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_MEMBER = "22222222-2222-4222-8222-222222222222"
_TOKEN = "ERERERERQRGBEREREREREQ"
_RECEIPT = "66666666-6666-4666-8666-666666666666"
_RECEIPT_TOKEN = creative.control._uuid_token(_RECEIPT)


def actor() -> TenantContext:
    return TenantContext(
        business_id=_BUSINESS,
        user_id=101,
        membership_id=_MEMBER,
        role=PlatformRole.OWNER,
    )


def receipt(*, delivery_claimed_at: str = "") -> CreativeGenerationReceipt:
    return CreativeGenerationReceipt(
        id=_RECEIPT,
        business_id=_BUSINESS,
        created_by_member_id=_MEMBER,
        request_text="calm office",
        brand_context="Tone: calm, human.",
        country_code="RU",
        provider_payload_json='{"version":1,"brief":"frozen"}',
        idempotency_key="clientplatform:owner-image:stable",
        source_job_id="provider-job-1",
        status=CreativeGenerationReceiptStatus.SUCCEEDED,
        created_at="2026-09-09T20:00:00+00:00",
        updated_at="2026-09-09T20:00:00+00:00",
        delivery_claimed_at=delivery_claimed_at,
    )


class FakeState:
    async def clear(self):
        return None


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


def job():
    return SimpleNamespace(id="provider-job-1", status="succeeded", asset_ready=True)


async def direct(function, *args, **kwargs):
    return function(*args, **kwargs)


class CreativeRecoveryEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_helpers_authorize_exact_business_and_reject_oversize(self) -> None:
        cb = callback(f"cpc:open:{_TOKEN}")
        expected = actor()
        with (
            patch.object(creative.control, "_token_uuid", return_value=_BUSINESS),
            patch.object(creative.control, "_actor", new=AsyncMock(return_value=expected)) as resolve,
        ):
            self.assertIs(await creative._actor_for_callback(cb, _TOKEN), expected)
        resolve.assert_awaited_once_with(101, _BUSINESS)
        with self.assertRaises(ValueError, msg="oversize callback must fail closed"):
            creative._receipt_callback("x" * 48, _TOKEN, receipt())

    async def test_claimed_menu_and_new_prompt_denial_are_explicit(self) -> None:
        target = outbound()
        claimed = receipt(delivery_claimed_at="2026-09-10T09:00:00+00:00")
        with (
            patch.object(creative.control, "_actor", new=AsyncMock(return_value=actor())),
            patch.object(creative, "_active", new=AsyncMock(return_value=claimed)),
            patch.object(creative.control, "_uuid_token", return_value=_TOKEN),
        ):
            await creative.send_creative_studio_menu(target, user_id=101, business_id=_BUSINESS)
        self.assertIn("могла завершиться", target.answer.await_args.args[0])

        denied = callback(f"cpc:new:{_TOKEN}", target)
        with patch.object(
            creative, "_actor_for_callback", new=AsyncMock(side_effect=ValueError("bad token"))
        ):
            await creative.ask_creative_prompt(denied, FakeState())
        self.assertTrue(denied.answer.await_args.kwargs["show_alert"])

    async def test_finish_image_claim_conflicts_never_duplicate_delivery(self) -> None:
        target = outbound()
        current = receipt()

        def materialize(_job, *, output_dir=None):
            path = Path(output_dir) / "creative.jpg"
            path.write_bytes(b"image")
            return path

        for latest, phrase in (
            (current, "могла завершиться"),
            (LookupError("gone"), "уже была отправлена"),
        ):
            target.answer.reset_mock()
            target.answer_photo.reset_mock()
            with (
                patch.object(creative.asyncio, "to_thread", new=direct),
                patch.object(creative.control, "_callback_message", return_value=target),
                patch.object(creative.control, "_uuid_token", return_value=_TOKEN),
                patch.object(creative, "materialize_ad_visual", side_effect=materialize),
                patch.object(creative, "claim_creative_generation_delivery", return_value=False),
                patch.object(
                    creative,
                    "get_creative_generation",
                    side_effect=latest if isinstance(latest, BaseException) else None,
                    return_value=None if isinstance(latest, BaseException) else latest,
                ),
            ):
                self.assertTrue(
                    await creative._finish_image(
                        callback(f"cpc:check:{_TOKEN}:{_RECEIPT_TOKEN}", target),
                        actor=actor(),
                        receipt=current,
                        job=job(),
                    )
                )
            target.answer_photo.assert_not_awaited()
            self.assertIn(phrase, target.answer.await_args.args[0])

    async def test_abandon_guards_cover_stale_denied_missing_and_changed(self) -> None:
        stale = callback(f"cpc:abandon:{_TOKEN}")
        await creative.abandon_creative_image(stale, FakeState())
        self.assertTrue(stale.answer.await_args.kwargs["show_alert"])

        for failure in (TenantPermissionDenied("denied"), LookupError("gone")):
            cb = callback(f"cpc:abandon:{_TOKEN}:{_RECEIPT_TOKEN}")
            with patch.object(creative, "_actor_for_callback", new=AsyncMock(side_effect=failure)):
                await creative.abandon_creative_image(cb, FakeState())
            self.assertTrue(cb.answer.await_args.kwargs["show_alert"])

        cb = callback(f"cpc:abandon:{_TOKEN}:{_RECEIPT_TOKEN}")
        with (
            patch.object(creative.asyncio, "to_thread", new=direct),
            patch.object(creative, "_actor_for_callback", new=AsyncMock(return_value=actor())),
            patch.object(creative, "_receipt_for_callback", new=AsyncMock(return_value=receipt())),
            patch.object(creative, "abandon_creative_generation", return_value=False),
        ):
            await creative.abandon_creative_image(cb, FakeState())
        self.assertIn("уже изменилось", cb.answer.await_args.args[0])

    async def test_redelivery_and_check_callbacks_fail_closed_on_stale_state(self) -> None:
        stale = callback(f"cpc:redeliver:{_TOKEN}")
        await creative.redeliver_creative_image(stale, FakeState())
        self.assertTrue(stale.answer.await_args.kwargs["show_alert"])

        for failure in (TenantPermissionDenied("denied"), LookupError("gone")):
            cb = callback(f"cpc:redeliver:{_TOKEN}:{_RECEIPT_TOKEN}")
            with patch.object(creative, "_actor_for_callback", new=AsyncMock(side_effect=failure)):
                await creative.redeliver_creative_image(cb, FakeState())
            self.assertTrue(cb.answer.await_args.kwargs["show_alert"])

        current = receipt()
        cb = callback(f"cpc:redeliver:{_TOKEN}:{_RECEIPT_TOKEN}")
        with (
            patch.object(creative.asyncio, "to_thread", new=direct),
            patch.object(creative, "_actor_for_callback", new=AsyncMock(return_value=actor())),
            patch.object(creative, "_receipt_for_callback", new=AsyncMock(return_value=current)),
            patch.object(creative, "authorize_creative_generation_redelivery", return_value=False),
        ):
            await creative.redeliver_creative_image(cb, FakeState())
        self.assertIn("больше не нужна", cb.answer.await_args.args[0])

        cb = callback(f"cpc:redeliver:{_TOKEN}:{_RECEIPT_TOKEN}")
        with (
            patch.object(creative.asyncio, "to_thread", new=direct),
            patch.object(creative, "_actor_for_callback", new=AsyncMock(return_value=actor())),
            patch.object(
                creative,
                "_receipt_for_callback",
                new=AsyncMock(side_effect=[current, LookupError("gone")]),
            ),
            patch.object(creative, "authorize_creative_generation_redelivery", return_value=True),
        ):
            await creative.redeliver_creative_image(cb, FakeState())
        self.assertIn("уже завершён", cb.answer.await_args.args[0])

        stale_check = callback(f"cpc:check:{_TOKEN}")
        await creative.check_creative_image(stale_check, FakeState())
        self.assertTrue(stale_check.answer.await_args.kwargs["show_alert"])
        denied_check = callback(f"cpc:check:{_TOKEN}:{_RECEIPT_TOKEN}")
        with patch.object(
            creative, "_actor_for_callback", new=AsyncMock(side_effect=ValueError("bad"))
        ):
            await creative.check_creative_image(denied_check, FakeState())
        self.assertTrue(denied_check.answer.await_args.kwargs["show_alert"])


if __name__ == "__main__":
    unittest.main()
