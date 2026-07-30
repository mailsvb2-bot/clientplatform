from __future__ import annotations

import asyncio
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
    _reported_size,
    _safe_extension,
    _select_media,
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
    def __init__(self, error: ProgramMediaStoreError | None = None) -> None:
        self.paths: list[Path] = []
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def put_file(self, path: Path, **kwargs: Any) -> Any:
        self.paths.append(path)
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if path.read_bytes() != b"program-media":
            raise AssertionError("downloaded payload mismatch")
        return SimpleNamespace(
            reference="s3://clientplatform-production/program-media/object.ogg"
        )


class FakeBot:
    def __init__(
        self,
        *,
        remote_path: str = "voice/file.ogg",
        remote_size: Any = 13,
        payload: bytes = b"program-media",
        download_error: BaseException | None = None,
    ) -> None:
        self.remote_path = remote_path
        self.remote_size = remote_size
        self.payload = payload
        self.download_error = download_error

    async def get_file(self, file_id: str) -> Any:
        if file_id not in {
            "control-bot-file-id",
            "audio-id",
            "video-id",
            "document-id",
            "photo-large-id",
        }:
            raise AssertionError("unexpected Telegram file id")
        return SimpleNamespace(
            file_path=self.remote_path,
            file_size=self.remote_size,
        )

    async def download_file(
        self,
        file_path: str,
        *,
        destination: Path,
        timeout: float,
    ) -> None:
        if file_path != self.remote_path or timeout != 30.0:
            raise AssertionError("unexpected Telegram download request")
        if self.download_error is not None:
            raise self.download_error
        destination.write_bytes(self.payload)


class FakeMessage:
    def __init__(
        self,
        *,
        text: str | None = None,
        audio: Any = None,
        voice: Any = None,
        video: Any = None,
        document: Any = None,
        photo: list[Any] | None = None,
        bot: FakeBot | None = None,
    ) -> None:
        self.text = text
        self.audio = audio
        self.voice = voice
        self.video = video
        self.document = document
        self.photo = list(photo or [])
        self.bot = bot or FakeBot()


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


class ProgramMediaSelectionTests(unittest.TestCase):
    def test_all_supported_media_kinds_are_normalized(self) -> None:
        cases = (
            (
                FakeMessage(
                    audio=SimpleNamespace(
                        file_id="audio-id",
                        file_size="13",
                        mime_type=None,
                        file_name="track.MP3",
                    )
                ),
                ContentKind.AUDIO,
                "audio-id",
                "audio/mpeg",
                "mp3",
            ),
            (
                FakeMessage(
                    video=SimpleNamespace(
                        file_id="video-id",
                        file_size=None,
                        mime_type=None,
                        file_name="movie.bad extension",
                    )
                ),
                ContentKind.VIDEO,
                "video-id",
                "video/mp4",
                "mp4",
            ),
            (
                FakeMessage(
                    document=SimpleNamespace(
                        file_id="document-id",
                        file_size="invalid",
                        mime_type=None,
                        file_name=None,
                    )
                ),
                ContentKind.DOCUMENT,
                "document-id",
                "application/octet-stream",
                "bin",
            ),
            (
                FakeMessage(
                    photo=[
                        SimpleNamespace(file_id="photo-small-id", file_size=5),
                        SimpleNamespace(file_id="photo-large-id", file_size=13),
                    ]
                ),
                ContentKind.IMAGE,
                "photo-large-id",
                "image/jpeg",
                "jpg",
            ),
        )
        for message, kind, file_id, content_type, extension in cases:
            with self.subTest(kind=kind):
                selected = _select_media(message)
                self.assertIsNotNone(selected)
                assert selected is not None
                self.assertEqual(selected.content_kind, kind)
                self.assertEqual(selected.file_id, file_id)
                self.assertEqual(selected.content_type, content_type)
                self.assertEqual(selected.extension, extension)

    def test_extension_and_reported_size_fail_closed(self) -> None:
        self.assertEqual(_safe_extension("folder/file.PDF", "bin"), "pdf")
        self.assertEqual(_safe_extension("folder/no-extension", "bin"), "bin")
        self.assertEqual(_safe_extension("unsafe.bad extension", "bin"), "bin")
        self.assertIsNone(_reported_size(SimpleNamespace()))
        self.assertIsNone(_reported_size(SimpleNamespace(file_size="invalid")))
        self.assertIsNone(_reported_size(SimpleNamespace(file_size=-1)))
        self.assertEqual(_reported_size(SimpleNamespace(file_size="15")), 15)


class ProgramMediaIngestTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_does_not_require_storage(self) -> None:
        kind, reference = await materialize_program_content(
            FakeMessage(text="  Текст урока  "),
            business_id=str(uuid4()),
        )
        self.assertEqual(kind, ContentKind.TEXT)
        self.assertEqual(reference, "Текст урока")

    async def test_empty_message_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "поддерживаются"):
            await materialize_program_content(
                FakeMessage(),
                business_id=str(uuid4()),
            )

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

    async def test_download_and_storage_failures_leave_no_temporary_file(self) -> None:
        cases = (
            (
                FakeBot(remote_path=""),
                FakeStore(),
                "program_media_telegram_path_missing",
            ),
            (
                FakeBot(remote_size=20_000_001),
                FakeStore(),
                "program_media_too_large",
            ),
            (
                FakeBot(payload=b""),
                FakeStore(),
                "program_media_download_empty",
            ),
            (
                FakeBot(payload=b"different"),
                FakeStore(),
                "program_media_download_size_mismatch",
            ),
            (
                FakeBot(download_error=asyncio.TimeoutError()),
                FakeStore(),
                "program_media_telegram_transport_failure",
            ),
            (
                FakeBot(),
                FakeStore(
                    ProgramMediaStoreError(
                        "program_media_upload_transport_failure",
                        retryable=True,
                    )
                ),
                "program_media_upload_transport_failure",
            ),
        )
        for bot, store, code in cases:
            with self.subTest(code=code):
                message = FakeMessage(
                    voice=SimpleNamespace(
                        file_id="control-bot-file-id",
                        file_size=13,
                        mime_type="audio/ogg",
                    ),
                    bot=bot,
                )
                with self.assertRaisesRegex(ProgramMediaIngestError, code):
                    await materialize_program_content(
                        message,
                        business_id=str(uuid4()),
                        policy=enabled_policy(),
                        store_media=store.put_file,
                    )
                for path in store.paths:
                    self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
