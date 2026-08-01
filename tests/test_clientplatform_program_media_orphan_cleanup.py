from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import clientplatform.application.program_media as program_media_app
from clientplatform.domain.programs import ContentKind
from clientplatform.infrastructure.program_media_store import (
    ProgramMediaStore,
    ProgramMediaStoreConfig,
    ProgramMediaStoreError,
)


class FakeResponse:
    def __init__(self, *, status: int, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = dict(headers or {})

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return b""


class VerificationMismatchOpener:
    def __init__(self) -> None:
        self.upload_headers: dict[str, str] = {}

    def __call__(self, request: Any, *, timeout: float) -> FakeResponse:
        if timeout != 12.0:
            raise AssertionError("unexpected timeout")
        headers = {name.lower(): value for name, value in request.header_items()}
        method = request.get_method()
        if method == "PUT":
            uploaded = b"".join(iter(request.data))
            if hashlib.sha256(uploaded).hexdigest() != headers[
                "x-amz-content-sha256"
            ]:
                raise AssertionError("payload digest mismatch")
            self.upload_headers = headers
            return FakeResponse(status=200)
        if method == "HEAD":
            return FakeResponse(
                status=200,
                headers={
                    "Content-Length": self.upload_headers["content-length"],
                    "X-Amz-Meta-Clientplatform-Sha256": "0" * 64,
                    "X-Amz-Meta-Clientplatform-Size": self.upload_headers[
                        "x-amz-meta-clientplatform-size"
                    ],
                    "X-Amz-Meta-Clientplatform-Kind": self.upload_headers[
                        "x-amz-meta-clientplatform-kind"
                    ],
                },
            )
        raise AssertionError(f"unexpected request method: {method}")


def enabled_config() -> ProgramMediaStoreConfig:
    return ProgramMediaStoreConfig(
        enabled=True,
        endpoint_host="s3.example.test",
        endpoint_path="",
        region="test-1",
        bucket="clientplatform-production",
        access_key="access-key",
        secret_key="secret-key",
        session_token="",
        timeout_seconds=12.0,
        max_bytes=20_000_000,
    )


class ProgramMediaOrphanReferenceTests(unittest.TestCase):
    def test_failed_post_upload_verification_preserves_cleanup_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "lesson.mp3"
            source.write_bytes(b"program-media")
            store = ProgramMediaStore(
                enabled_config(),
                opener=VerificationMismatchOpener(),
            )

            with self.assertRaises(ProgramMediaStoreError) as raised:
                store.put_file(
                    source,
                    business_id=str(uuid4()),
                    content_kind=ContentKind.AUDIO,
                    content_type="audio/mpeg",
                    extension="mp3",
                )

        self.assertEqual(
            raised.exception.code,
            "program_media_upload_verification_failed",
        )
        self.assertTrue(
            raised.exception.cleanup_reference.startswith(
                "s3://clientplatform-production/program-media/"
            )
        )
        self.assertEqual(str(raised.exception), raised.exception.code)

    def test_application_queues_uncertain_object_and_preserves_original_error(
        self,
    ) -> None:
        business_id = str(uuid4())
        cleanup_reference = (
            "s3://clientplatform-production/program-media/scope/audio/aa/orphan.mp3"
        )
        original = ProgramMediaStoreError(
            "program_media_verify_transport_failure",
            retryable=True,
            cleanup_reference=cleanup_reference,
        )

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "lesson.mp3"
            source.write_bytes(b"program-media")
            with (
                patch.object(
                    program_media_app,
                    "program_media_store_config",
                    return_value=enabled_config(),
                ),
                patch.object(program_media_app, "ProgramMediaStore") as store_type,
                patch.object(
                    program_media_app,
                    "queue_program_media_cleanup",
                    return_value=True,
                ) as queue_cleanup,
            ):
                store_type.return_value.put_file.side_effect = original
                with self.assertRaises(ProgramMediaStoreError) as raised:
                    program_media_app.store_program_media(
                        source,
                        business_id=business_id,
                        content_kind=ContentKind.AUDIO,
                        content_type="audio/mpeg",
                        extension="mp3",
                    )

        self.assertIs(raised.exception, original)
        queue_cleanup.assert_called_once_with(
            business_id=business_id,
            media_reference=cleanup_reference,
            reason="failed_program_media_ingest",
        )

    def test_cleanup_queue_failure_is_sanitized_and_keeps_reference(self) -> None:
        business_id = str(uuid4())
        cleanup_reference = (
            "s3://clientplatform-production/program-media/scope/video/bb/orphan.mp4"
        )
        original = ProgramMediaStoreError(
            "program_media_upload_transport_failure",
            retryable=True,
            cleanup_reference=cleanup_reference,
        )

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "lesson.mp4"
            source.write_bytes(b"program-media")
            with (
                patch.object(
                    program_media_app,
                    "program_media_store_config",
                    return_value=enabled_config(),
                ),
                patch.object(program_media_app, "ProgramMediaStore") as store_type,
                patch.object(
                    program_media_app,
                    "queue_program_media_cleanup",
                    side_effect=RuntimeError("postgresql://secret@database"),
                ),
            ):
                store_type.return_value.put_file.side_effect = original
                with self.assertRaises(ProgramMediaStoreError) as raised:
                    program_media_app.store_program_media(
                        source,
                        business_id=business_id,
                        content_kind=ContentKind.VIDEO,
                        content_type="video/mp4",
                        extension="mp4",
                    )

        self.assertEqual(
            raised.exception.code,
            "program_media_cleanup_enqueue_failed",
        )
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.cleanup_reference, cleanup_reference)
        self.assertNotIn("secret", str(raised.exception))

    def test_disabled_cleanup_queue_fails_closed(self) -> None:
        business_id = str(uuid4())
        cleanup_reference = (
            "s3://clientplatform-production/program-media/scope/image/cc/orphan.jpg"
        )
        original = ProgramMediaStoreError(
            "program_media_upload_transport_failure",
            retryable=True,
            cleanup_reference=cleanup_reference,
        )

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "lesson.jpg"
            source.write_bytes(b"program-media")
            with (
                patch.object(
                    program_media_app,
                    "program_media_store_config",
                    return_value=enabled_config(),
                ),
                patch.object(program_media_app, "ProgramMediaStore") as store_type,
                patch.object(
                    program_media_app,
                    "queue_program_media_cleanup",
                    return_value=False,
                ),
            ):
                store_type.return_value.put_file.side_effect = original
                with self.assertRaises(ProgramMediaStoreError) as raised:
                    program_media_app.store_program_media(
                        source,
                        business_id=business_id,
                        content_kind=ContentKind.IMAGE,
                        content_type="image/jpeg",
                        extension="jpg",
                    )

        self.assertEqual(
            raised.exception.code,
            "program_media_cleanup_enqueue_failed",
        )
        self.assertEqual(raised.exception.cleanup_reference, cleanup_reference)


if __name__ == "__main__":
    unittest.main()
