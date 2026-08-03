from __future__ import annotations

import unittest
from unittest.mock import patch

from clientplatform.domain.programs import ContentKind
from clientplatform.transport.media import (
    HmacMediaGatewayResolver,
    SafeMediaReferenceResolver,
)


class _Secrets:
    def resolve(self, _reference: str) -> str:
        return "secret"


class ClientPlatformExternalMediaTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_resolver_converts_supported_cloud_share_links(self) -> None:
        resolver = SafeMediaReferenceResolver()
        self.assertEqual(
            await resolver.resolve(
                "https://drive.google.com/file/d/abcDEF_123456/view",
                ContentKind.VIDEO,
            ),
            "https://drive.google.com/uc?export=download&id=abcDEF_123456",
        )
        self.assertEqual(
            await resolver.resolve(
                "https://www.dropbox.com/s/demo/video.mp4?dl=0",
                ContentKind.VIDEO,
            ),
            "https://dl.dropboxusercontent.com/s/demo/video.mp4",
        )

    async def test_yandex_is_resolved_through_fixed_public_api_helper(self) -> None:
        resolver = SafeMediaReferenceResolver()
        with patch(
            "clientplatform.transport.media._yandex_download_url_sync",
            return_value="https://downloader.disk.yandex.ru/disk/example/video.mp4",
        ) as helper:
            resolved = await resolver.resolve(
                "https://disk.yandex.ru/d/example",
                ContentKind.VIDEO,
            )
        self.assertEqual(
            resolved,
            "https://downloader.disk.yandex.ru/disk/example/video.mp4",
        )
        helper.assert_called_once_with("https://disk.yandex.ru/d/example")

    async def test_gateway_mode_keeps_external_media_outside_s3(self) -> None:
        resolver = HmacMediaGatewayResolver(
            base_url="https://media.example.test",
            credential_provider=_Secrets(),
            signing_secret_reference="secret://env/TEST",
        )
        resolved = await resolver.resolve(
            "https://drive.google.com/file/d/abcDEF_123456/view",
            ContentKind.VIDEO,
        )
        self.assertIn("uc?export=download", resolved)


if __name__ == "__main__":
    unittest.main()
