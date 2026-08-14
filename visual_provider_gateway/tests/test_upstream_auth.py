from __future__ import annotations

from fastapi.testclient import TestClient

from visual_provider_gateway.app import app


def test_upstream_token_authenticates_canonical_clientplatform_principal(monkeypatch):
    monkeypatch.delenv("VISUAL_GATEWAY_CLIENT_TOKENS_JSON", raising=False)
    monkeypatch.delenv("VISUAL_GATEWAY_TOKEN", raising=False)
    monkeypatch.setenv("VISUAL_GATEWAY_UPSTREAM_TOKEN", "upstream-secret-material-123")
    monkeypatch.setenv("VISUAL_CREATIVE_ENABLED", "1")

    client = TestClient(app)
    response = client.get(
        "/v1/providers",
        headers={"Authorization": "Bearer upstream-secret-material-123"},
    )

    assert response.status_code == 200
    assert response.json()["client_id"] == "clientplatform"


def test_wrong_upstream_token_fails_closed(monkeypatch):
    monkeypatch.delenv("VISUAL_GATEWAY_CLIENT_TOKENS_JSON", raising=False)
    monkeypatch.delenv("VISUAL_GATEWAY_TOKEN", raising=False)
    monkeypatch.setenv("VISUAL_GATEWAY_UPSTREAM_TOKEN", "upstream-secret-material-123")

    client = TestClient(app)
    response = client.get(
        "/v1/providers",
        headers={"Authorization": "Bearer wrong-secret-material-456"},
    )

    assert response.status_code == 401
