from __future__ import annotations

import unittest

from clientplatform.domain.external_media import (
    ExternalMediaProvider,
    dropbox_direct_url,
    external_delivery_kind,
    google_drive_direct_url,
    normalize_external_media_url,
    onedrive_direct_url,
)
from clientplatform.domain.programs import ContentKind


class ClientPlatformExternalMediaTests(unittest.TestCase):
    def test_supported_clouds_are_detected_and_fragments_removed(self) -> None:
        cases = {
            "https://disk.yandex.ru/d/example": ExternalMediaProvider.YANDEX_DISK,
            "https://drive.google.com/file/d/abcDEF_123456/view": ExternalMediaProvider.GOOGLE_DRIVE,
            "https://www.dropbox.com/scl/fi/example/video.mp4?rlkey=abc": ExternalMediaProvider.DROPBOX,
            "https://1drv.ms/v/s!example": ExternalMediaProvider.ONEDRIVE,
        }
        for url, provider in cases.items():
            with self.subTest(url=url):
                result = normalize_external_media_url(url + "#fragment")
                self.assertEqual(result.provider, provider)
                self.assertNotIn("#", result.url)

    def test_local_credentials_and_insecure_urls_are_rejected(self) -> None:
        for url in (
            "http://disk.yandex.ru/file",
            "https://user:pass@example.com/file.mp4",
            "https://localhost/file.mp4",
            "https://127.0.0.1/file.mp4",
            "https://10.0.0.2/file.mp4",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_external_media_url(url)

    def test_cloud_direct_url_conversions(self) -> None:
        self.assertEqual(
            google_drive_direct_url("https://drive.google.com/file/d/abcDEF_123456/view"),
            "https://drive.google.com/uc?export=download&id=abcDEF_123456",
        )
        self.assertEqual(
            dropbox_direct_url("https://www.dropbox.com/s/example/file.mp4?dl=0"),
            "https://dl.dropboxusercontent.com/s/example/file.mp4",
        )
        self.assertTrue(
            onedrive_direct_url("https://1drv.ms/v/s!example").startswith(
                "https://api.onedrive.com/v1.0/shares/u!"
            )
        )

    def test_streaming_pages_are_delivered_as_links(self) -> None:
        youtube = normalize_external_media_url("https://youtu.be/abcdefghijk")
        self.assertEqual(youtube.provider, ExternalMediaProvider.STREAMING)
        self.assertEqual(
            external_delivery_kind(youtube, ContentKind.VIDEO),
            ContentKind.LINK,
        )


if __name__ == "__main__":
    unittest.main()
