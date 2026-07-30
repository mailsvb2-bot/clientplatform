from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

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
        assert timeout == 12.0
        self.requests.append(request)
        headers = {name.lower(): value for name, value in request.header_items()}
        if request.get_method() == "PUT":
            self.upload_headers = headers
            uploaded = b"".join(iter(request.data))
            assert hashlib.sha256(uploaded).hexdigest() == headers["x-amz-content-sha256"]
            return FakeResponse(status=200)
        assert request.get_method() == "HEAD"
        return FakeResponse(
            status=200,
            headers={
                "Content-Length": self.upload_headers["Content-length"],
                "X-Amz-Meta-Clientplatform-Sha256": self.upload_headers[
                    "X-amz-meta-clientplatform-sha256"
                ],
                "X-Amz-Meta-Clientplatform-Size": self.upload_headers[
                    "X-amz-meta-clientplatform-size"
                ],
                "X-Amz-Meta-Clientplatform-Kind": self.upload_headers[
                    "X-amz-meta-clientplatform-kind"
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
        assert path.read_bytes() == b"program-media"
        return SimpleNamespace(
            reference="s3://clientplatform-production/program-media/object.ogg"
        )


class FakeBot:
    async def get_file(self, file_id: str) -> Any:
        assert file_id == "control-bot-file-id"
        return SimpleNamespace(file_path="voice/file.ogg", file_size=13)

    async def download_file(
        self,
        file_path: str,
        *,
        destination: Path,
        timeout: float,
    ) -> None:
        assert file_path == "voice/file.ogg"
        assert timeout == 30.0
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


def test_config_is_fail_closed_and_bounded() -> None:
    assert program_media_store_config({}).enabled is False
    with pytest.raises(ProgramMediaStoreError, match="size_limit_invalid"):
        program_media_store_config(
            {
                **enabled_env(),
                "CLIENTPLATFORM_PROGRAM_MEDIA_MAX_BYTES": "20000001",
            }
        )
    with pytest.raises(ProgramMediaStoreError, match="endpoint_requires_https"):
        program_media_store_config(
            {
                **enabled_env(),
                "CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT": "http://s3.example.test",
            }
        )


def test_private_store_streams_and_verifies_without_identifying_key(tmp_path: Path) -> None:
    source = tmp_path / "lesson.ogg"
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

    assert stored.reference.startswith("s3://clientplatform-production/program-media/")
    assert business_id not in stored.reference
    assert "secret-key" not in stored.reference
    assert [request.get_method() for request in opener.requests] == ["PUT", "HEAD"]
    assert stored.size == len(b"program-media")


@pytest.mark.asyncio
async def test_text_does_not_require_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIENTPLATFORM_PROGRAM_MEDIA_INGEST_ENABLED", raising=False)
    kind, reference = await materialize_program_content(
        FakeMessage(text="  Текст урока  "),
        business_id=str(uuid4()),
    )
    assert kind == ContentKind.TEXT
    assert reference == "Текст урока"


@pytest.mark.asyncio
async def test_media_is_externalized_and_temporary_file_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in enabled_env().items():
        monkeypatch.setenv(name, value)
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
        store=store,  # type: ignore[arg-type]
    )

    assert kind == ContentKind.AUDIO
    assert reference.startswith("s3://clientplatform-production/")
    assert store.calls[0]["content_kind"] == ContentKind.AUDIO
    assert store.calls[0]["extension"] == "ogg"
    assert not store.paths[0].exists()


@pytest.mark.asyncio
async def test_media_is_rejected_before_download_when_reported_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in enabled_env().items():
        monkeypatch.setenv(name, value)
    message = FakeMessage(
        voice=SimpleNamespace(
            file_id="control-bot-file-id",
            file_size=20_000_001,
            mime_type="audio/ogg",
        )
    )
    with pytest.raises(ProgramMediaIngestError, match="program_media_too_large"):
        await materialize_program_content(
            message,
            business_id=str(uuid4()),
            store=FakeStore(),  # type: ignore[arg-type]
        )
