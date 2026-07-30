from __future__ import annotations

import pytest

from clientplatform.domain.programs import ContentKind
from clientplatform.transport.media import (
    HmacMediaGatewayResolver,
    MediaReferenceError,
    SafeMediaReferenceResolver,
)


class CredentialProvider:
    def resolve(self, reference: str) -> str:
        assert reference == "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY"
        return "media-signing-secret"


@pytest.mark.asyncio
async def test_gateway_mode_rejects_bot_local_file_id() -> None:
    resolver = HmacMediaGatewayResolver(
        base_url="https://client.example/clientplatform",
        credential_provider=CredentialProvider(),
        signing_secret_reference=(
            "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY"
        ),
        clock=lambda: 1000,
    )
    with pytest.raises(
        MediaReferenceError,
        match="media_bot_local_reference_not_portable",
    ):
        await resolver.resolve("control-bot-file-id", ContentKind.AUDIO)


@pytest.mark.asyncio
async def test_gateway_mode_signs_private_s3_and_keeps_public_https() -> None:
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
    assert signed.startswith(
        "https://client.example/clientplatform/media/clientplatform-production/"
    )
    assert "expires=1300" in signed
    assert "sig=" in signed

    public = "https://public.example/material.pdf"
    assert await resolver.resolve(public, ContentKind.DOCUMENT) == public


@pytest.mark.asyncio
async def test_explicit_dev_resolver_may_keep_same_bot_file_id() -> None:
    assert (
        await SafeMediaReferenceResolver().resolve(
            "same-bot-local-file-id",
            ContentKind.AUDIO,
        )
        == "same-bot-local-file-id"
    )
