from __future__ import annotations

"""Sanitized production audit for the VK community bot and Callback API.

The script intentionally prints only non-secret status fields. It validates the
community token, callback server ownership/status, secret and confirmation-code
matches, API version, and the two events required by ClientPlatform.
"""

import hmac
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


API_BASE = "https://api.vk.com/method"
DEFAULT_API_VERSION = "5.199"


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _safe_error(payload: dict[str, Any], fallback: str = "VK_API_ERROR") -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        raw_code = error.get("error_code")
        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            code = 0
        return f"VK_ERROR_{code}" if code else fallback
    raw = payload.get("error_code") or payload.get("error") or fallback
    return str(raw).strip().upper().replace(" ", "_")[:80] or fallback


def _api_call(token: str, api_version: str, method: str, params: dict[str, Any]) -> tuple[dict[str, Any], int]:
    body = urllib.parse.urlencode(
        {
            **params,
            "access_token": token,
            "v": api_version,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{API_BASE}/{method}",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8", "replace")
            http_status = int(response.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        http_status = int(exc.code)
    except ssl.SSLCertVerificationError:
        return {"error": "TLS_VERIFY_ERROR"}, 0
    except socket.gaierror:
        return {"error": "DNS_ERROR"}, 0
    except (TimeoutError, socket.timeout):
        return {"error": "NETWORK_TIMEOUT"}, 0
    except ConnectionRefusedError:
        return {"error": "CONNECTION_REFUSED"}, 0
    except (urllib.error.URLError, OSError, ssl.SSLError):
        return {"error": "NETWORK_OR_TLS_ERROR"}, 0

    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"error": "INVALID_JSON"}, http_status
    if not isinstance(payload, dict):
        return {"error": "INVALID_RESPONSE"}, http_status
    if isinstance(payload.get("error"), dict):
        try:
            vk_code = int(payload["error"].get("error_code") or 0)
        except (TypeError, ValueError):
            vk_code = 0
        return payload, vk_code or http_status
    return payload, http_status


def _error(stage: str, *, group_id: str, code: int, payload: dict[str, Any], details: str = "") -> tuple[str, int]:
    suffix = f" {details}" if details else ""
    return (
        f"status=error stage={stage} group={group_id or 'unknown'} code={int(code)} "
        f"error={_safe_error(payload)}{suffix}"
    ), 1


def _response_object(payload: dict[str, Any]) -> dict[str, Any]:
    response = payload.get("response")
    return response if isinstance(response, dict) else {}


def _group_ids(payload: dict[str, Any]) -> list[int]:
    response = payload.get("response")
    items: Any
    if isinstance(response, list):
        items = response
    elif isinstance(response, dict):
        items = response.get("groups") or response.get("items") or []
    else:
        items = []
    result: list[int] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                result.append(int(item.get("id")))
            except (TypeError, ValueError):
                continue
    return result


def _servers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = _response_object(payload).get("items") or []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _permission_names(payload: dict[str, Any]) -> set[str]:
    permissions = _response_object(payload).get("permissions") or []
    if not isinstance(permissions, list):
        return set()
    return {
        str(item.get("name") or "").strip().lower()
        for item in permissions
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) == 1
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def run() -> tuple[str, int]:
    token = _env("VK_GROUP_TOKEN")
    group_id_raw = _env("VK_GROUP_ID")
    secret = _env("VK_SECRET")
    confirmation = _env("VK_CONFIRMATION_TOKEN")
    public_base = _env("MESSENGER_PUBLIC_BASE_URL").rstrip("/")
    api_version = _env("VK_API_VERSION") or DEFAULT_API_VERSION

    missing = [
        name
        for name, value in (
            ("VK_GROUP_TOKEN", token),
            ("VK_GROUP_ID", group_id_raw),
            ("VK_SECRET", secret),
            ("VK_CONFIRMATION_TOKEN", confirmation),
            ("MESSENGER_PUBLIC_BASE_URL", public_base),
        )
        if not value
    ]
    if missing:
        return (
            "status=error stage=config group=unknown code=0 error=MISSING_" + "_".join(missing)
        ), 2
    try:
        group_id = int(group_id_raw)
    except ValueError:
        return "status=error stage=config group=unknown code=0 error=VK_GROUP_ID_INVALID", 3
    if group_id <= 0:
        return "status=error stage=config group=unknown code=0 error=VK_GROUP_ID_INVALID", 3
    if not public_base.startswith("https://"):
        return f"status=error stage=config group={group_id} code=0 error=PUBLIC_BASE_INVALID", 4

    group_payload, code = _api_call(
        token,
        api_version,
        "groups.getById",
        {"group_id": group_id},
    )
    if code != 200 or group_id not in _group_ids(group_payload):
        return _error("group", group_id=str(group_id), code=code, payload=group_payload)

    permissions_payload, code = _api_call(
        token,
        api_version,
        "groups.getTokenPermissions",
        {},
    )
    if code != 200:
        return _error("permissions", group_id=str(group_id), code=code, payload=permissions_payload)
    permissions = _permission_names(permissions_payload)
    missing_permissions = [name for name in ("messages", "manage") if name not in permissions]
    if missing_permissions:
        missing = "_".join(name.upper() for name in missing_permissions)
        granted = ",".join(sorted(permissions)) or "none"
        return (
            f"status=error stage=permissions group={group_id} code=200 "
            f"error=TOKEN_PERMISSION_MISSING_{missing} granted={granted}"
        ), 5

    confirmation_payload, code = _api_call(
        token,
        api_version,
        "groups.getCallbackConfirmationCode",
        {"group_id": group_id},
    )
    confirmation_code = str(_response_object(confirmation_payload).get("code") or "").strip()
    if code != 200:
        return _error("confirmation", group_id=str(group_id), code=code, payload=confirmation_payload)
    if not confirmation_code or not hmac.compare_digest(confirmation_code, confirmation):
        return (
            f"status=error stage=confirmation group={group_id} code=200 error=CONFIRMATION_MISMATCH"
        ), 6

    servers_payload, code = _api_call(
        token,
        api_version,
        "groups.getCallbackServers",
        {"group_id": group_id},
    )
    if code != 200:
        return _error("servers", group_id=str(group_id), code=code, payload=servers_payload)

    expected_url = f"{public_base}/webhooks/vk"
    matching = [
        server
        for server in _servers(servers_payload)
        if str(server.get("url") or "").strip().rstrip("/") == expected_url
    ]
    if not matching:
        return (
            f"status=error stage=servers group={group_id} code=200 error=WEBHOOK_NOT_REGISTERED "
            f"api={api_version}"
        ), 7
    matching.sort(key=lambda item: str(item.get("status") or "") != "ok")
    server = matching[0]
    server_status = str(server.get("status") or "unknown").strip().lower()
    if server_status != "ok":
        safe_status = server_status.upper().replace(" ", "_")[:40] or "UNKNOWN"
        return (
            f"status=error stage=servers group={group_id} code=200 error=SERVER_{safe_status} "
            f"api={api_version} webhook=present"
        ), 8

    server_secret = str(server.get("secret_key") or "").strip()
    if not server_secret or not hmac.compare_digest(server_secret, secret):
        return (
            f"status=error stage=servers group={group_id} code=200 error=SECRET_MISMATCH "
            f"api={api_version} webhook=present server=ok"
        ), 9
    try:
        server_id = int(server.get("id"))
    except (TypeError, ValueError):
        return (
            f"status=error stage=servers group={group_id} code=200 error=SERVER_ID_INVALID "
            f"api={api_version} webhook=present"
        ), 10

    settings_payload, code = _api_call(
        token,
        api_version,
        "groups.getCallbackSettings",
        {"group_id": group_id, "server_id": server_id},
    )
    if code != 200:
        return _error("callback_settings", group_id=str(group_id), code=code, payload=settings_payload)
    callback_settings = _response_object(settings_payload)
    callback_api_version = str(callback_settings.get("api_version") or "").strip()
    events = callback_settings.get("events") if isinstance(callback_settings.get("events"), dict) else {}
    message_new = _enabled(events.get("message_new"))
    message_event = _enabled(events.get("message_event"))
    if callback_api_version and callback_api_version != api_version:
        return (
            f"status=error stage=callback_settings group={group_id} code=200 error=API_VERSION_MISMATCH "
            f"api={callback_api_version} expected={api_version} webhook=present server=ok"
        ), 11
    if not message_new or not message_event:
        return (
            f"status=error stage=callback_settings group={group_id} code=200 error=EVENTS_DISABLED "
            f"api={callback_api_version or api_version} webhook=present server=ok "
            f"message_new={int(message_new)} message_event={int(message_event)}"
        ), 12

    return (
        f"status=ok stage=callback_settings group={group_id} code=200 error=NONE "
        f"api={callback_api_version or api_version} webhook=present server=ok "
        "secret=match confirmation=match message_new=1 message_event=1"
    ), 0


def main() -> int:
    message, code = run()
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
