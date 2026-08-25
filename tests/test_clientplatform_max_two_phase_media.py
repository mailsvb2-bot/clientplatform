from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from clientplatform.application.dispatch_worker import _effective_max_attempts
from clientplatform.domain.connections import (
    ClaimedDispatch,
    ConnectionPlatform,
    Dispatch,
    DispatchStatus,
)
from clientplatform.domain.programs import ContentKind
from clientplatform.runtime.max_two_phase_media import (
    MaxPreparedMediaNotReadyError,
    MaxPreparedMediaTokenRejectedError,
    PreparedMaxRuntimeMedia,
    TwoPhaseMaxRuntimeClient,
)
from clientplatform.transport.native_messenger import MaxDispatchAdapter
from runtime.messenger_max_sender import MaxBotSender
from runtime.messenger_transport_errors import MessengerTransportError


ROOT = Path(__file__).resolve().parents[1]


class _Resolver:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def resolve(self, reference: str, kind: ContentKind) -> str:
        self.events.append("resolve")
        return f"https://media.example/{kind.value}/{reference}"


class _TwoPhaseClient:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.prepared = object()

    async def prepare_media(self, **kwargs):
        self.events.append("prepare")
        self.asserted_media = kwargs["media"]
        return self.prepared

    async def send_prepared_media(self, **kwargs):
        self.events.append("final-write")
        if kwargs["prepared"] is not self.prepared:
            raise AssertionError("wrong prepared object")
        if kwargs["token"] != "secret-token":
            raise AssertionError("wrong final credential")
        return "provider-media-1"

    async def release_prepared_media(self, prepared):
        if prepared is not self.prepared:
            raise AssertionError("wrong prepared cleanup object")
        self.events.append("release")

    async def send_media(self, **kwargs):
        raise AssertionError("legacy MAX media send must not run in two-phase path")


class MaxTwoPhaseAdapterTests(unittest.TestCase):
    def _claim(self) -> ClaimedDispatch:
        now = "2026-08-25T00:00:00+00:00"
        return ClaimedDispatch(
            dispatch=Dispatch(
                id=str(uuid4()),
                business_id=str(uuid4()),
                platform=ConnectionPlatform.MAX,
                logical_delivery_id=str(uuid4()),
                connection_id=str(uuid4()),
                customer_identity_id=str(uuid4()),
                payload_kind=ContentKind.VIDEO,
                payload_ref="s3://bucket/video.mp4",
                idempotency_key="delivery:max:video:two-phase",
                status=DispatchStatus.SENDING,
                attempts=0,
                available_at=now,
                created_at=now,
                updated_at=now,
                locked_at=now,
                lock_token=str(uuid4()),
            ),
            external_subject="max-user-77",
            credential_reference="secret://env/MAX_TOKEN",
        )

    def test_adapter_prepares_media_before_any_final_write(self) -> None:
        client = _TwoPhaseClient()
        events: list[str] = []
        adapter = MaxDispatchAdapter(client, media_resolver=_Resolver(events))

        prepared = asyncio.run(adapter.prepare(self._claim(), "secret-token"))

        self.assertEqual(["resolve"], events)
        self.assertEqual(["prepare"], client.events)
        self.assertTrue(client.asserted_media.startswith("https://media.example/video/"))
        self.assertNotIn("secret-token", repr(prepared))

        result = asyncio.run(adapter.send_prepared(prepared, "secret-token"))
        self.assertEqual("provider-media-1", result)
        self.assertEqual(["prepare", "final-write"], client.events)

        asyncio.run(adapter.release_prepared(prepared))
        self.assertEqual(["prepare", "final-write", "release"], client.events)


class MaxTwoPhaseRuntimeTests(unittest.TestCase):
    def _prepared(self, *, temporary: bool = False) -> PreparedMaxRuntimeMedia:
        return PreparedMaxRuntimeMedia(
            external_subject="max-user-88",
            media_type="video",
            media_token="upload-token-1",
            source_path=Path("/tmp/clientplatform-max-media-test.mp4"),
            temporary=temporary,
        )

    def test_prepare_uploads_token_but_never_creates_message(self) -> None:
        client = TwoPhaseMaxRuntimeClient()
        with (
            patch(
                "clientplatform.runtime.max_two_phase_media._materialize_media",
                new=AsyncMock(return_value=(Path("/tmp/source.mp4"), False)),
            ),
            patch.object(
                MaxBotSender,
                "_ensure_media_token",
                new=AsyncMock(return_value="upload-token-prepared"),
            ) as upload,
            patch.object(
                MaxBotSender,
                "send_text",
                new=AsyncMock(return_value={"message_id": "must-not-run"}),
            ) as final_write,
        ):
            prepared = asyncio.run(
                client.prepare_media(
                    token="secret-token",
                    external_subject="max-user-88",
                    kind=ContentKind.VIDEO,
                    media="https://media.example/video.mp4",
                    idempotency_key="delivery:max:video:prepare",
                )
            )

        self.assertEqual("upload-token-prepared", prepared.media_token)
        self.assertEqual(1, upload.await_count)
        self.assertEqual(0, final_write.await_count)
        self.assertNotIn("secret-token", repr(prepared))
        self.assertNotIn("upload-token-prepared", repr(prepared))

    def test_final_phase_is_exactly_one_message_write(self) -> None:
        client = TwoPhaseMaxRuntimeClient()
        send = AsyncMock(return_value={"message_id": "max-message-1"})
        with patch.object(MaxBotSender, "send_text", new=send):
            result = asyncio.run(
                client.send_prepared_media(
                    token="secret-token",
                    prepared=self._prepared(),
                )
            )

        self.assertEqual("max-message-1", result)
        self.assertEqual(1, send.await_count)
        args, kwargs = send.await_args
        self.assertEqual("max-user-88", args[0])
        self.assertEqual("", args[1])
        self.assertFalse(kwargs["legacy_ui"])
        self.assertEqual(
            [{"type": "video", "payload": {"token": "upload-token-1"}}],
            kwargs["attachments"],
        )

    def test_explicit_attachment_not_ready_keeps_durable_retry_budget(self) -> None:
        client = TwoPhaseMaxRuntimeClient()
        provider_error = MessengerTransportError(
            "not ready",
            code="max.send_text.attachment.not.ready",
        )
        with patch.object(
            MaxBotSender,
            "send_text",
            new=AsyncMock(side_effect=provider_error),
        ):
            with self.assertRaises(MaxPreparedMediaNotReadyError) as raised:
                asyncio.run(
                    client.send_prepared_media(
                        token="secret-token",
                        prepared=self._prepared(),
                    )
                )

        self.assertTrue(raised.exception.provider_write_definitely_rejected)
        self.assertEqual(
            8,
            _effective_max_attempts(
                raised.exception,
                8,
                non_replay_boundary_crossed=True,
            ),
        )

    def test_top_level_attachment_not_ready_response_is_explicit_rejection(self) -> None:
        client = TwoPhaseMaxRuntimeClient()
        send = AsyncMock(return_value={"code": "attachment.not.ready"})
        with patch.object(MaxBotSender, "send_text", new=send):
            with self.assertRaises(MaxPreparedMediaNotReadyError) as raised:
                asyncio.run(
                    client.send_prepared_media(
                        token="secret-token",
                        prepared=self._prepared(),
                    )
                )

        self.assertEqual(1, send.await_count)
        self.assertTrue(raised.exception.provider_write_definitely_rejected)
        self.assertEqual(
            8,
            _effective_max_attempts(
                raised.exception,
                8,
                non_replay_boundary_crossed=True,
            ),
        )

    def test_explicit_token_rejection_invalidates_cache_and_retries_durably(self) -> None:
        client = TwoPhaseMaxRuntimeClient()
        provider_error = MessengerTransportError(
            "invalid token",
            code="max.send_text.invalid_token",
        )
        with (
            patch.object(
                MaxBotSender,
                "send_text",
                new=AsyncMock(side_effect=provider_error),
            ),
            patch(
                "clientplatform.runtime.max_two_phase_media.invalidate_media_token",
                return_value=True,
            ) as invalidate,
        ):
            with self.assertRaises(MaxPreparedMediaTokenRejectedError) as raised:
                asyncio.run(
                    client.send_prepared_media(
                        token="secret-token",
                        prepared=self._prepared(),
                    )
                )

        self.assertTrue(raised.exception.provider_write_definitely_rejected)
        self.assertEqual(1, invalidate.call_count)
        self.assertEqual(
            8,
            _effective_max_attempts(
                raised.exception,
                8,
                non_replay_boundary_crossed=True,
            ),
        )

    def test_unknown_connection_loss_after_final_write_stays_ambiguous(self) -> None:
        client = TwoPhaseMaxRuntimeClient()
        failure = OSError("connection lost after POST")
        with patch.object(
            MaxBotSender,
            "send_text",
            new=AsyncMock(side_effect=failure),
        ):
            with self.assertRaises(OSError) as raised:
                asyncio.run(
                    client.send_prepared_media(
                        token="secret-token",
                        prepared=self._prepared(),
                    )
                )

        self.assertEqual(
            1,
            _effective_max_attempts(
                raised.exception,
                8,
                non_replay_boundary_crossed=True,
            ),
        )

    def test_temporary_preparation_is_cleaned_after_final_phase(self) -> None:
        client = TwoPhaseMaxRuntimeClient()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            path = Path(tmp.name)
        prepared = PreparedMaxRuntimeMedia(
            external_subject="max-user-88",
            media_type="video",
            media_token="upload-token-1",
            source_path=path,
            temporary=True,
        )

        asyncio.run(client.release_prepared_media(prepared))

        self.assertFalse(path.exists())


class MaxTwoPhaseCompositionTests(unittest.TestCase):
    def test_production_runtime_uses_two_phase_max_client(self) -> None:
        source = (
            ROOT / "clientplatform" / "runtime" / "dispatch_runtime.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "MaxDispatchAdapter(\n                TwoPhaseMaxRuntimeClient(),",
            source,
        )
        self.assertNotIn("MaxDispatchAdapter(\n                MaxRuntimeClient(),", source)

    def test_two_phase_boundary_is_mandatory_static_surface(self) -> None:
        source = (ROOT / "scripts" / "critical_static_gate.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('"clientplatform/runtime/max_two_phase_media.py"', source)
        self.assertIn('"clientplatform/transport/base.py"', source)
        self.assertIn('"clientplatform/transport/native_messenger.py"', source)


if __name__ == "__main__":
    unittest.main()
