from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from runtime import messenger_webhooks


@pytest.mark.asyncio
async def test_ad_oauth_health_is_additive_and_backward_compatible() -> None:
    disabled = await messenger_webhooks._health(SimpleNamespace(app={}))
    assert json.loads(disabled.body) == {
        "ok": True,
        "service": "http-ingress",
    }

    enabled = await messenger_webhooks._health(
        SimpleNamespace(app={"clientplatform_ad_oauth_bot": object()})
    )
    assert json.loads(enabled.body) == {
        "ok": True,
        "service": "http-ingress",
        "ad_oauth": True,
    }
