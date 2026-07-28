from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clientplatform.runtime.media_gateway import (
    FilesystemMediaObjectStore,
    MediaGatewayConfig,
    MediaObjectNotFound,
    MediaObjectTooLarge,
    MediaRangeNotSatisfiable,
    _resolve_byte_range,
    _s3_authorization_headers,
    validate_media_gateway_config,
)
from clientplatform.transport.media import (
    MediaReferenceError,
    media_gateway_signature,
    verify_media_gateway_signature,
)


class MediaGatewaySignatureTests(unittest.TestCase):
    def test_signature_roundtrip_is_exact_path_and_expiry_bound(self) -> None:
        signature = media_gateway_signature(
            secret="gateway-secret",
            path="/clientplatform/media/private-bucket/audio/lesson%2001.mp3",
            expires=1300,
        )
        verify_media_gateway_signature(
            secret="gateway-secret",
            path="/clientplatform/media/private-bucket/audio/lesson%2001.mp3",
            expires="1300",
            signature=signature,
            clock=lambda: 1000.0,
        )
        with self.assertRaises(MediaReferenceError):
            verify_media_gateway_signature(
                secret="gateway-secret",
                path="/clientplatform/media/private-bucket/audio/lesson%2002.mp3",
                expires="1300",
                signature=signature,
                clock=lambda: 1000.0,
            )
        with self.assertRaises(MediaReferenceError):
            verify_media_gateway_signature(
                secret="gateway-secret",
                path="/clientplatform/media/private-bucket/audio/lesson%2001.mp3",
                expires="1301",
                signature=signature,
                clock=lambda: 1000.0,
            )

    def test_expired_or_excessively_future_urls_fail_closed(self) -> None:
        path = "/media/private-bucket/audio.mp3"
        expired = media_gateway_signature(secret="secret", path=path, expires=1000)
        with self.assertRaises(MediaReferenceError):
            verify_media_gateway_signature(
                secret="secret",
                path=path,
                expires=1000,
                signature=expired,
                clock=lambda: 1000.0,
            )
        future = media_gateway_signature(secret="secret", path=path, expires=2000)
        with self.assertRaises(MediaReferenceError):
            verify_media_gateway_signature(
                secret="secret",
                path=path,
                expires=2000,
                signature=future,
                clock=lambda: 1000.0,
                max_future_seconds=900,
            )


class MediaGatewayConfigurationTests(unittest.TestCase):
    def _config(self, **overrides) -> MediaGatewayConfig:
        value = {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 8091,
            "public_base_url": "https://media.example.test/clientplatform",
            "storage_mode": "filesystem",
            "allowed_buckets": frozenset({"private-bucket"}),
            "filesystem_root": "/tmp/clientplatform-media",
            "s3_endpoint": "",
            "s3_region": "",
            "s3_access_key_reference": "secret://env/CLIENTPLATFORM_SECRET_S3_ACCESS_KEY",
            "s3_secret_key_reference": "secret://env/CLIENTPLATFORM_SECRET_S3_SECRET_KEY",
            "s3_session_token_reference": "",
            "signing_secret_reference": "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY",
            "max_object_bytes": 10_000_000,
            "upstream_timeout_seconds": 30.0,
            "chunk_size": 65_536,
        }
        value.update(overrides)
        return MediaGatewayConfig(**value)

    def test_enabled_gateway_requires_https_allowlist_and_explicit_storage(self) -> None:
        validate_media_gateway_config(self._config())
        for config in (
            self._config(public_base_url="http://media.example.test"),
            self._config(allowed_buckets=frozenset()),
            self._config(storage_mode=""),
            self._config(filesystem_root="relative/path"),
            self._config(storage_mode="s3", s3_endpoint="", s3_region=""),
        ):
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    validate_media_gateway_config(config)

    def test_disabled_gateway_is_neutral(self) -> None:
        validate_media_gateway_config(
            self._config(
                enabled=False,
                public_base_url="",
                storage_mode="",
                allowed_buckets=frozenset(),
                filesystem_root="",
            )
        )


class FilesystemMediaObjectStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_and_range_reads_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "private-bucket" / "audio" / "lesson.mp3"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"0123456789")
            store = FilesystemMediaObjectStore(root=str(root), max_object_bytes=100)

            full = await store.open(bucket="private-bucket", key="audio/lesson.mp3")
            try:
                self.assertEqual(full.status, 200)
                self.assertEqual(full.content_length, 10)
                self.assertEqual(await full.read(100), b"0123456789")
            finally:
                await full.close()

            partial = await store.open(
                bucket="private-bucket",
                key="audio/lesson.mp3",
                range_header="bytes=2-5",
            )
            try:
                self.assertEqual(partial.status, 206)
                self.assertEqual(partial.content_range, "bytes 2-5/10")
                self.assertEqual(await partial.read(100), b"2345")
            finally:
                await partial.close()

    async def test_traversal_missing_and_oversize_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "private-bucket" / "large.bin"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"12345")
            store = FilesystemMediaObjectStore(root=str(root), max_object_bytes=4)
            with self.assertRaises(MediaObjectTooLarge):
                await store.open(bucket="private-bucket", key="large.bin")
            with self.assertRaises(MediaReferenceError):
                await store.open(bucket="private-bucket", key="../outside.bin")
            with self.assertRaises(MediaObjectNotFound):
                await store.open(bucket="private-bucket", key="missing.bin")

    def test_invalid_ranges_are_rejected(self) -> None:
        for header in ("items=0-1", "bytes=20-30", "bytes=5-2", "bytes=1-2,4-5"):
            with self.subTest(header=header):
                with self.assertRaises(MediaRangeNotSatisfiable):
                    _resolve_byte_range(header, 10)


class S3SignatureTests(unittest.TestCase):
    def test_sigv4_is_deterministic_range_bound_and_secret_free(self) -> None:
        headers, path = _s3_authorization_headers(
            host="objects.example.test",
            endpoint_path="/storage",
            bucket="private-bucket",
            key="program/audio 01.mp3",
            region="eu-west-1",
            access_key="ACCESS123",
            secret_key="SUPER_SECRET_KEY",
            session_token="SESSION_TOKEN",
            range_header="bytes=0-1023",
            clock=lambda: 1_753_680_000.0,
        )
        repeated, repeated_path = _s3_authorization_headers(
            host="objects.example.test",
            endpoint_path="/storage",
            bucket="private-bucket",
            key="program/audio 01.mp3",
            region="eu-west-1",
            access_key="ACCESS123",
            secret_key="SUPER_SECRET_KEY",
            session_token="SESSION_TOKEN",
            range_header="bytes=0-1023",
            clock=lambda: 1_753_680_000.0,
        )
        self.assertEqual(headers, repeated)
        self.assertEqual(path, repeated_path)
        self.assertEqual(path, "/storage/private-bucket/program/audio%2001.mp3")
        self.assertEqual(headers["Range"], "bytes=0-1023")
        self.assertIn("SignedHeaders=host;range;x-amz-content-sha256;x-amz-date;x-amz-security-token", headers["Authorization"])
        self.assertNotIn("SUPER_SECRET_KEY", repr(headers))


if __name__ == "__main__":
    unittest.main()
