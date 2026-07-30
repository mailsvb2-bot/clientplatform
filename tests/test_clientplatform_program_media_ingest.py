from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from clientplatform.application.program_media import ProgramMediaIngestPolicy
from clientplatform.domain.programs import ContentKind
from clientplatform.infrastructure.program_media_store import (
    ProgramMediaStore,
    ProgramMediaStoreConfig,
    ProgramMediaStoreError,
    program_media_store_config,
)
from handlers.clientplatform_program_media import (
    ProgramMediaIngestError,
    materialize_program_content,
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


class RecordingOpener:
    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.upload_headers: dict[str, str] = {}

    def __call__(self, request: Any, *, timeout: float) -> FakeResponse:
        if timeout != 12.0:
            raise AssertionError("unexpected timeout")
        self.requests.append(request)
        headers = {name.lower(): value for name, value in request.header_items()}
        if request.get_method() == "PUT":
            self.upload_headers = headers
            uploaded = b"".join(iter(request.data))
            if hashlib.sha256(uploaded).hexdigest() != headers[
                "x-amz-content-sha256"
            ]:
                raise AssertionError("payload digest mismatch")
            return FakeResponse(status=200)
        if request.get_method() != "HEAD":
            raise AssertionError("unexpected request method")
        return FakeResponse(
            status=200,
            headers={
                "Content-Length": self.upload_headers["content-length"],
                "X-Amz-Meta-Clientplatform-Sha256": self.upload_headers[
                    "x-amz-meta-clientplatform-sha256"
                ],
                "X-Amz-Meta-Clientplatform-Size": self.upload_headers[
                    "x-amz-meta-clientplatform-size"
                ],
                "X-Amz-Meta-Clientplatform-Kind": self.upload_headers[
                    "x-amz-meta-clientplatform-kind"
                ],
            },
        )


class FakeStore:
    def __init__(self) -> None:
        self.paths: list[Path] = []
        self.calls: list[dict[str, Any]] = []

    def put_file(self, path: Path, **kwargs: Any) -> Any:
        self.paths.append(path)
        self.calls.append(kwargs)
        if path.read_bytes() != b"program-media":
            raise AssertionError("downloaded payload mismatch")
        return SimpleNamespace(
            reference="s3://clientplatform-production/program-media/object.ogg"
        )


class FakeBot:
    async def get_file(self, file_id: str) -> Any:
        if file_id != "control-bot-file-id":
            raise AssertionError("unexpected Telegram file id")
        return SimpleNamespace(file_path="voice/file.ogg", file_size=13)

    async def download_file(
        self,
        file_path: str,
        *,
        destination: Path,
        timeout: float,
    ) -> None:
        if file_path != "voice/file.ogg" or timeout != 30.0:
            raise AssertionError("unexpected Telegram download request")
        destination.write_bytes(b"program-media")


class FakeMessage:
    def __init__(self, *, text: str | None = None, voice: Any = None) -> None:
        self.text = text
        self.audio = None
        self.voice = voice
        self.video = None
        self.document = None
        self.photo: list[Any] = []
        self.bot = FakeBot()


def enabled_env() -> dict[str, str]:
    return {
        "CLIENTPLATFORM_PROGRAM_MEDIA_INGEST_ENABLED": "1",
        "CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT": "https://s3.example.test",
        "CLIENTPLATFORM_MEDIA_GATEWAY_S3_REGION": "test-1",
        "CLIENTPLATFORM_STORAGE_BUCKET": "clientplatform-production",
        "CLIENTPLATFORM_SECRET_S3_ACCESS_KEY": "access-key",
        "CLIENTPLATFORM_SECRET_S3_SECRET_KEY": "secret-key",
        "CLIENTPLATFORM_PROGRAM_MEDIA_MAX_BYTES": "20000000",
        "CLIENTPLATFORM_PROGRAM_MEDIA_TIMEOUT_SEC": "30",
    }


def enabled_policy() -> ProgramMediaIngestPolicy:
    return ProgramMediaIngestPolicy(
        enabled=True,
        max_bytes=20_000_000,
        timeout_seconds=30.0,
    )


class ProgramMediaStoreTests(unittest.TestCase):
    def test_config_is_fail_closed_and_bounded(self) -> None:
        self.assertFalse(program_media_store_config({}).enabled)
        with self.assertRaisesRegex(ProgramMediaStoreError, "size_limit_invalid"):
            program_media_store_config(
                {
                    **enabled_env(),
                    "CLIENTPLATFORM_PROGRAM_MEDIA_MAX_BYTES": "20000001",
                }
            )
        with self.assertRaisesRegex(ProgramMediaStoreError, "endpoint_requires_https"):
            program_media_store_config(
                {
                    **enabled_env(),
                    "CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT": (
                        "http://s3.example.test"
                    ),
                }
            )

    def test_private_store_streams_and_verifies_without_identifying_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "lesson.ogg"
            source.write_bytes(b"program-media")
            opener = RecordingOpener()
            business_id = str(uuid4())
            store = ProgramMediaStore(
                ProgramMediaStoreConfig(
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
                ),
                opener=opener,
            )

            stored = store.put_file(
                source,
                business_id=business_id,
                content_kind=ContentKind.AUDIO,
                content_type="audio/ogg",
                extension="ogg",
            )

        self.assertTrue(
            stored.reference.startswith(
                "s3://clientplatform-production/program-media/"
            )
        )
        self.assertNotIn(business_id, stored.reference)
        self.assertNotIn("secret-key", stored.reference)
        self.assertEqual(
            [request.get_method() for request in opener.requests],
            ["PUT", "HEAD"],
        )
        self.assertEqual(stored.size, len(b"program-media"))


class ProgramMediaIngestTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_does_not_require_storage(self) -> None:
        kind, reference = await materialize_program_content(
            FakeMessage(text="  Текст урока  "),
            business_id=str(uuid4()),
        )
        self.assertEqual(kind, ContentKind.TEXT)
        self.assertEqual(reference, "Текст урока")

    async def test_media_is_externalized_and_temporary_file_is_removed(self) -> None:
        store = FakeStore()
        message = FakeMessage(
            voice=SimpleNamespace(
                file_id="control-bot-file-id",
                file_size=13,
                mime_type="audio/ogg",
            )
        )

        kind, reference = await materialize_program_content(
            message,
            business_id=str(uuid4()),
            policy=enabled_policy(),
            store_media=store.put_file,
        )

        self.assertEqual(kind, ContentKind.AUDIO)
        self.assertTrue(reference.startswith("s3://clientplatform-production/"))
        self.assertEqual(store.calls[0]["content_kind"], ContentKind.AUDIO)
        self.assertEqual(store.calls[0]["extension"], "ogg")
        self.assertFalse(store.paths[0].exists())

    async def test_media_is_rejected_before_download_when_reported_too_large(
        self,
    ) -> None:
        message = FakeMessage(
            voice=SimpleNamespace(
                file_id="control-bot-file-id",
                file_size=20_000_001,
                mime_type="audio/ogg",
            )
        )
        with self.assertRaisesRegex(ProgramMediaIngestError, "program_media_too_large"):
            await materialize_program_content(
                message,
                business_id=str(uuid4()),
                policy=enabled_policy(),
                store_media=FakeStore().put_file,
            )

    async def test_disabled_ingest_never_downloads_or_persists_media(self) -> None:
        message = FakeMessage(
            voice=SimpleNamespace(
                file_id="control-bot-file-id",
                file_size=13,
                mime_type="audio/ogg",
            )
        )
        with self.assertRaisesRegex(
            ProgramMediaIngestError,
            "program_media_ingest_disabled",
        ):
            await materialize_program_content(
                message,
                business_id=str(uuid4()),
                policy=ProgramMediaIngestPolicy(
                    enabled=False,
                    max_bytes=20_000_000,
                    timeout_seconds=30.0,
                ),
                store_media=FakeStore().put_file,
            )


if __name__ == "__main__":
    unittest.main()
