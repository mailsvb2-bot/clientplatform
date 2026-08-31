from __future__ import annotations

from scripts import vk_provider_audit


def _configure(monkeypatch) -> None:
    monkeypatch.setenv("VK_GROUP_TOKEN", "vk-secret-token")
    monkeypatch.setenv("VK_GROUP_ID", "238191212")
    monkeypatch.setenv("VK_SECRET", "callback-secret")
    monkeypatch.setenv("VK_CONFIRMATION_TOKEN", "confirmation-code")
    monkeypatch.setenv("VK_API_VERSION", "5.199")
    monkeypatch.setenv("MESSENGER_PUBLIC_BASE_URL", "https://bot.example.test")


def _success_call(_token: str, _version: str, method: str, params: dict):
    assert _token == "vk-secret-token"
    assert _version == "5.199"
    if method != "groups.getTokenPermissions":
        assert params["group_id"] == 238191212
    if method == "groups.getById":
        return {"response": {"groups": [{"id": 238191212, "name": "Metrotherapy"}]}}, 200
    if method == "groups.getTokenPermissions":
        return {
            "response": {
                "mask": 266240,
                "permissions": [
                    {"name": "messages", "setting": 4096},
                    {"name": "manage", "setting": 262144},
                ],
            }
        }, 200
    if method == "groups.getCallbackConfirmationCode":
        return {"response": {"code": "confirmation-code"}}, 200
    if method == "groups.getCallbackServers":
        return {
            "response": {
                "count": 1,
                "items": [
                    {
                        "id": 77,
                        "title": "Metrotherapy",
                        "url": "https://bot.example.test/webhooks/vk",
                        "secret_key": "callback-secret",
                        "status": "ok",
                    }
                ],
            }
        }, 200
    if method == "groups.getCallbackSettings":
        assert params["server_id"] == 77
        return {
            "response": {
                "api_version": "5.199",
                "events": {"message_new": 1, "message_event": 1},
            }
        }, 200
    raise AssertionError(f"unexpected method: {method}")


def test_vk_provider_audit_confirms_callback_server_and_events(monkeypatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(vk_provider_audit, "_api_call", _success_call)

    message, code = vk_provider_audit.run()

    assert code == 0
    assert message == (
        "status=ok stage=callback_settings group=238191212 code=200 error=NONE "
        "api=5.199 webhook=present server=ok secret=match confirmation=match "
        "message_new=1 message_event=1"
    )
    assert "vk-secret-token" not in message
    assert "callback-secret" not in message
    assert "confirmation-code" not in message


def test_vk_provider_audit_reports_missing_manage_permission(monkeypatch) -> None:
    _configure(monkeypatch)

    def fake_call(token: str, version: str, method: str, params: dict):
        if method == "groups.getTokenPermissions":
            return {
                "response": {
                    "mask": 4096,
                    "permissions": [{"name": "messages", "setting": 4096}],
                }
            }, 200
        return _success_call(token, version, method, params)

    monkeypatch.setattr(vk_provider_audit, "_api_call", fake_call)

    message, code = vk_provider_audit.run()

    assert code == 5
    assert "stage=permissions" in message
    assert "TOKEN_PERMISSION_MISSING_MANAGE" in message
    assert "granted=messages" in message


def test_vk_provider_audit_fails_when_message_event_is_disabled(monkeypatch) -> None:
    _configure(monkeypatch)

    def fake_call(token: str, version: str, method: str, params: dict):
        payload, code = _success_call(token, version, method, params)
        if method == "groups.getCallbackSettings":
            payload = {
                "response": {
                    "api_version": "5.199",
                    "events": {"message_new": 1, "message_event": 0},
                }
            }
        return payload, code

    monkeypatch.setattr(vk_provider_audit, "_api_call", fake_call)

    message, code = vk_provider_audit.run()

    assert code == 12
    assert "EVENTS_DISABLED" in message
    assert "message_new=1 message_event=0" in message


def test_vk_provider_audit_fails_when_callback_belongs_to_wrong_secret(monkeypatch) -> None:
    _configure(monkeypatch)

    def fake_call(token: str, version: str, method: str, params: dict):
        payload, code = _success_call(token, version, method, params)
        if method == "groups.getCallbackServers":
            payload["response"]["items"][0]["secret_key"] = "wrong-secret"
        return payload, code

    monkeypatch.setattr(vk_provider_audit, "_api_call", fake_call)

    message, code = vk_provider_audit.run()

    assert code == 9
    assert "SECRET_MISMATCH" in message
    assert "wrong-secret" not in message
    assert "callback-secret" not in message


def test_vk_provider_audit_sanitizes_vk_api_errors(monkeypatch) -> None:
    _configure(monkeypatch)

    def fake_call(_token: str, _version: str, _method: str, _params: dict):
        return {
            "error": {
                "error_code": 5,
                "error_msg": "User authorization failed: vk-secret-token",
            }
        }, 5

    monkeypatch.setattr(vk_provider_audit, "_api_call", fake_call)

    message, code = vk_provider_audit.run()

    assert code == 1
    assert "VK_ERROR_5" in message
    assert "vk-secret-token" not in message
