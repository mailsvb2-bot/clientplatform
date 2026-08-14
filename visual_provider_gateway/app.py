from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .service import VisualGatewayService


@dataclass(frozen=True, slots=True)
class GatewayPrincipal:
    client_id: str


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern="^(image|video)$")
    prompt: str = Field(min_length=1, max_length=12000)
    country_code: str = Field(default="", max_length=8)
    preferred_provider: str = Field(default="", max_length=64)
    aspect_ratio: str = Field(default="1:1", max_length=16)
    duration_seconds: int = Field(default=5, ge=2, le=15)
    negative_prompt: str = Field(default="", max_length=4000)
    reference_url: str = Field(default="", max_length=4096)
    brand_context: str = Field(default="", max_length=4000)
    wait_seconds: int = Field(default=0, ge=0, le=60)
    seed: int | None = Field(default=None, ge=0, le=4294967295)
    scope_id: str = Field(default="global", min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:@/-]+$")
    idempotency_key: str = Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9_.:@/-]+$")


@lru_cache(maxsize=1)
def service() -> VisualGatewayService:
    return VisualGatewayService()


def _client_tokens() -> dict[str, str]:
    raw = str(os.getenv("VISUAL_GATEWAY_CLIENT_TOKENS_JSON", "") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("invalid_visual_gateway_client_tokens_json") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("invalid_visual_gateway_client_tokens_json")
    out: dict[str, str] = {}
    for key, value in parsed.items():
        client_id = str(key or "").strip()
        token = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", client_id) or len(token) < 16:
            raise RuntimeError("invalid_visual_gateway_client_token_entry")
        out[client_id] = token
    return out


def require_auth(authorization: str | None = Header(default=None)) -> GatewayPrincipal:
    clients = _client_tokens()
    expected = str(os.getenv("VISUAL_GATEWAY_TOKEN", "") or "").strip()
    allow_anonymous = str(os.getenv("VISUAL_GATEWAY_ALLOW_ANONYMOUS", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
    if not clients and not expected and allow_anonymous:
        return GatewayPrincipal(client_id="anonymous")

    observed = str(authorization or "").strip()
    prefix = "Bearer "
    if not observed.startswith(prefix):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    token = observed[len(prefix):]

    if clients:
        for client_id, expected in clients.items():
            if secrets.compare_digest(token, expected):
                return GatewayPrincipal(client_id=client_id)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    if expected:
        if secrets.compare_digest(token, expected):
            return GatewayPrincipal(client_id="legacy")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="gateway_auth_not_configured")


app = FastAPI(title="Visual Creative Gateway", version="4.0")


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/v1/providers")
def providers(country_code: str = "", principal: GatewayPrincipal = Depends(require_auth)) -> dict[str, Any]:
    payload = service().snapshot(country_code)
    payload["client_id"] = principal.client_id
    return payload


@app.get("/v1/usage")
def usage(principal: GatewayPrincipal = Depends(require_auth)) -> dict[str, Any]:
    return service().usage_snapshot(principal.client_id)


@app.post("/v1/creative/generations")
def generate(request: GenerateRequest, principal: GatewayPrincipal = Depends(require_auth)) -> dict[str, Any]:
    try:
        return service().submit(request.model_dump(), client_id=principal.client_id)
    except PermissionError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.get("/v1/creative/generations/{gateway_id}")
def poll(gateway_id: str, scope_id: str, principal: GatewayPrincipal = Depends(require_auth)) -> dict[str, Any]:
    try:
        return service().poll(gateway_id, client_id=principal.client_id, scope_id=scope_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job_not_found") from None


@app.get("/v1/creative/generations/{gateway_id}/content")
def content(gateway_id: str, scope_id: str, principal: GatewayPrincipal = Depends(require_auth)) -> Response:
    try:
        path, mime_type = service().content_path(gateway_id, client_id=principal.client_id, scope_id=scope_id)
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="content_not_ready") from None
    return FileResponse(path, media_type=mime_type, filename=path.name)
