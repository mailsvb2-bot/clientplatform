from __future__ import annotations

import unittest

from clientplatform.domain.programs import ContentKind
from clientplatform.transport.media import (
    HmacMediaGatewayResolver,
    MediaReferenceError,
    SafeMediaReferenceResolver,
)


class CredentialProvider:
    def resolve(self, reference: str) -> str:
        if reference != "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY":
            raise AssertionError("unexpected secret reference")
        return "media-signing-secret"


class BotIndependentMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_mode_rejects_bot_local_file_id(self) -> None:
        resolver = HmacMediaGatewayResolver(
            base_url="https://client.example/clientplatform",
            credential_provider=CredentialProvider(),
            signing_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY"
            ),
            clock=lambda: 1000,
        )
        with self.assertRaisesRegex(
            MediaReferenceError,
            "media_bot_local_reference_not_portable",
        ):
            await resolver.resolve("control-bot-file-id", ContentKind.AUDIO)

    async def test_gateway_mode_signs_private_s3_and_keeps_public_https(self) -> None:
        resolver = HmacMediaGatewayResolver(
            base_url="https://client.example/clientplatform",
            credential_provider=CredentialProvider(),
            signing_secret_reference=(
                "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY"
            ),
            ttl_seconds=300,
            clock=lambda: 1000,
        )
        signed = await resolver.resolve(
            "s3://clientplatform-production/program-media/object.pdf",
            ContentKind.DOCUMENT,
        )
        self.assertTrue(
            signed.startswith(
                "https://client.example/clientplatform/media/"
                "clientplatform-production/"
            )
        )
        self.assertIn("expires=1300", signed)
        self.assertIn("sig=", signed)

        public = "https://public.example/material.pdf"
        self.assertEqual(
            await resolver.resolve(public, ContentKind.DOCUMENT),
            public,
        )

    async def test_explicit_dev_resolver_may_keep_same_bot_file_id(self) -> None:
        self.assertEqual(
            await SafeMediaReferenceResolver().resolve(
                "same-bot-local-file-id",
                ContentKind.AUDIO,
            ),
            "same-bot-local-file-id",
        )


if __name__ == "__main__":
    unittest.main()
