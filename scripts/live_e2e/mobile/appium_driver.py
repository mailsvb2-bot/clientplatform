"""Appium W3C driver for ClientPlatform real mobile/tablet live probes."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_ELEMENT_KEY = "element-6066-11e4-a52e-4f735466cecf"
_ALLOWED_LOCATORS = {
    "accessibility id",
    "id",
    "xpath",
    "class name",
    "-android uiautomator",
    "-ios predicate string",
    "-ios class chain",
}
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{25,}\b"),
    re.compile(r"\blive_[A-Za-z0-9]{20,}\b"),
    re.compile(r"^\s*Bearer\s+\S+", re.IGNORECASE),
)
_FORBIDDEN_PROFILE_KEY_FRAGMENTS = {
    "password",
    "passwd",
    "secret",
    "token",
    "cookie",
    "authorization",
    "credential",
}


class AppiumProbeError(RuntimeError):
    pass


def profile_contains_secret_like_key(value: object) -> bool:
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)
    if isinstance(value, dict):
        for key, item in value.items():
            folded = str(key).casefold()
            if any(fragment in folded for fragment in _FORBIDDEN_PROFILE_KEY_FRAGMENTS):
                return True
            if profile_contains_secret_like_key(item):
                return True
    elif isinstance(value, list):
        return any(profile_contains_secret_like_key(item) for item in value)
    return False


class AppiumClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id = ""

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: int = 30,
    ) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - dedicated Appium endpoint
                raw = response.read().decode("utf-8")
        except (OSError, urllib.error.URLError) as exc:
            raise AppiumProbeError(f"appium_request_failed:{method}:{path}") from exc
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError as exc:
            raise AppiumProbeError(f"appium_invalid_json:{method}:{path}") from exc
        if isinstance(parsed, dict):
            value = parsed.get("value")
            if isinstance(value, dict) and value.get("error"):
                raise AppiumProbeError(f"appium_error:{value.get('error')}")
        return parsed

    def create_session(self, capabilities: dict[str, Any]) -> None:
        response = self._request(
            "POST",
            "/session",
            {"capabilities": {"alwaysMatch": capabilities}},
            timeout=60,
        )
        value = response.get("value") if isinstance(response, dict) else None
        session_id = ""
        if isinstance(value, dict):
            session_id = str(value.get("sessionId") or "")
        if not session_id and isinstance(response, dict):
            session_id = str(response.get("sessionId") or "")
        if not session_id:
            raise AppiumProbeError("appium_session_id_missing")
        self.session_id = session_id

    def close(self) -> None:
        if not self.session_id:
            return
        try:
            self._request("DELETE", f"/session/{self.session_id}")
        finally:
            self.session_id = ""

    def source(self) -> str:
        response = self._request("GET", f"/session/{self.session_id}/source")
        value = response.get("value") if isinstance(response, dict) else ""
        return str(value or "")

    def screenshot(self, path: Path) -> None:
        response = self._request("GET", f"/session/{self.session_id}/screenshot")
        encoded = response.get("value") if isinstance(response, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise AppiumProbeError("appium_screenshot_missing")
        path.write_bytes(base64.b64decode(encoded))

    def find(self, locator: dict[str, Any]) -> str:
        using = str(locator.get("using") or "").strip()
        value = str(locator.get("value") or "").strip()
        if using not in _ALLOWED_LOCATORS or not value:
            raise AppiumProbeError("invalid_locator")
        response = self._request(
            "POST",
            f"/session/{self.session_id}/element",
            {"using": using, "value": value},
        )
        result = response.get("value") if isinstance(response, dict) else None
        if not isinstance(result, dict):
            raise AppiumProbeError("element_not_found")
        element_id = str(result.get(_ELEMENT_KEY) or result.get("ELEMENT") or "")
        if not element_id:
            raise AppiumProbeError("element_id_missing")
        return element_id

    def click(self, element_id: str) -> None:
        self._request(
            "POST",
            f"/session/{self.session_id}/element/{element_id}/click",
            {},
        )

    def clear(self, element_id: str) -> None:
        self._request(
            "POST",
            f"/session/{self.session_id}/element/{element_id}/clear",
            {},
        )

    def type_text(self, element_id: str, text: str) -> None:
        self._request(
            "POST",
            f"/session/{self.session_id}/element/{element_id}/value",
            {"text": text, "value": list(text)},
        )

    def navigate(self, url: str) -> None:
        self._request("POST", f"/session/{self.session_id}/url", {"url": url})

    def execute_mobile(self, name: str, args: dict[str, Any] | None = None) -> Any:
        response = self._request(
            "POST",
            f"/session/{self.session_id}/execute/sync",
            {"script": f"mobile: {name}", "args": [args or {}]},
            timeout=60,
        )
        return response.get("value") if isinstance(response, dict) else None

    def orientation(self) -> str:
        response = self._request("GET", f"/session/{self.session_id}/orientation")
        value = response.get("value") if isinstance(response, dict) else ""
        orientation = str(value or "").strip().upper()
        if orientation not in {"PORTRAIT", "LANDSCAPE"}:
            raise AppiumProbeError("appium_orientation_invalid")
        return orientation

    def set_orientation(self, orientation: str) -> None:
        value = orientation.strip().upper()
        if value not in {"PORTRAIT", "LANDSCAPE"}:
            raise AppiumProbeError("requested_orientation_invalid")
        self._request(
            "POST",
            f"/session/{self.session_id}/orientation",
            {"orientation": value},
            timeout=60,
        )


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _count(text: str, needle: str) -> int:
    return text.casefold().count(needle.casefold())


def _capabilities(plan: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    target = plan["target"]
    device_os = str(target["device_os"])
    udid = str(os.environ.get("CLIENTPLATFORM_E2E_DEVICE_UDID") or "").strip()
    if not udid:
        raise AppiumProbeError("missing_environment:CLIENTPLATFORM_E2E_DEVICE_UDID")

    is_apple = device_os in {"ios", "ipados"}
    caps: dict[str, Any] = {
        "platformName": "iOS" if is_apple else "Android",
        "appium:automationName": "XCUITest" if is_apple else "UiAutomator2",
        "appium:udid": udid,
        "appium:noReset": True,
        "appium:newCommandTimeout": 180,
    }

    browser_name = str(profile.get("browser_name") or "").strip()
    app_id = str(profile.get("app_id") or "").strip()
    if browser_name:
        caps["browserName"] = browser_name
    elif app_id:
        caps["appium:bundleId" if is_apple else "appPackage"] = app_id
        activity = str(profile.get("app_activity") or "").strip()
        if activity and not is_apple:
            caps["appium:appActivity"] = activity
    else:
        raise AppiumProbeError("profile_requires_browser_name_or_app_id")
    return caps


def _open_native_chat(client: AppiumClient, profile: dict[str, Any]) -> None:
    locator = profile.get("chat_locator")
    if isinstance(locator, dict):
        client.click(client.find(locator))
        return
    if profile.get("chat_already_open") is not True:
        raise AppiumProbeError("profile_requires_chat_locator_or_chat_already_open")


def _poll_expected(
    client: AppiumClient,
    *,
    expected: str,
    before_hash: str,
    before_count: int,
    timeout_seconds: float,
) -> tuple[str, int, str]:
    deadline = time.monotonic() + timeout_seconds
    last_text = ""
    while time.monotonic() < deadline:
        last_text = client.source()
        after_hash = _hash(last_text)
        expected_count = _count(last_text, expected)
        if expected_count > before_count and after_hash != before_hash:
            return after_hash, expected_count, last_text
        time.sleep(1.0)
    raise AppiumProbeError("provider_response_assertion_timeout")


def _assert_anchor(source: str, anchor: str, stage: str) -> None:
    if not anchor or _count(source, anchor) <= 0:
        raise AppiumProbeError(f"compatibility_state_anchor_missing:{stage}")


def _run_compatibility(
    client: AppiumClient,
    *,
    plan: dict[str, Any],
    profile: dict[str, Any],
    evidence_dir: Path,
) -> list[dict[str, Any]]:
    anchor = str(plan.get("state_anchor") or "").strip()
    if not anchor:
        raise AppiumProbeError("compatibility_state_anchor_required")

    _open_native_chat(client, profile)
    records: list[dict[str, Any]] = []
    for index, probe in enumerate(plan["probes"], start=1):
        probe_id = str(probe.get("id") or "")
        kind = str(probe.get("kind") or "")
        before = client.source()
        _assert_anchor(before, anchor, f"{probe_id}:before")
        before_hash = _hash(before)

        if kind == "background-resume":
            client.execute_mobile("backgroundApp", {"seconds": 3})
            after = client.source()
            _assert_anchor(after, anchor, f"{probe_id}:after")
            shot = evidence_dir / f"compatibility-{index:02d}-{probe_id}.png"
            client.screenshot(shot)
            records.append(
                {
                    "id": probe_id,
                    "status": "ok",
                    "expected_text_asserted": True,
                    "state_preserved": True,
                    "background_restored": True,
                    "before_hash": before_hash,
                    "after_hash": _hash(after),
                    "screenshot": shot.name,
                }
            )
            continue

        if kind == "orientation-roundtrip":
            initial = client.orientation()
            requested = "LANDSCAPE" if initial == "PORTRAIT" else "PORTRAIT"
            client.set_orientation(requested)
            time.sleep(1.0)
            observed = client.orientation()
            changed = observed == requested
            if probe.get("require_change") is True and not changed:
                raise AppiumProbeError("orientation_change_required_but_not_observed")
            rotated_source = client.source()
            _assert_anchor(rotated_source, anchor, f"{probe_id}:rotated")
            if changed:
                client.set_orientation(initial)
                time.sleep(1.0)
            final_orientation = client.orientation()
            if changed and final_orientation != initial:
                raise AppiumProbeError("orientation_restore_failed")
            final_source = client.source()
            _assert_anchor(final_source, anchor, f"{probe_id}:restored")
            shot = evidence_dir / f"compatibility-{index:02d}-{probe_id}.png"
            client.screenshot(shot)
            records.append(
                {
                    "id": probe_id,
                    "status": "ok",
                    "expected_text_asserted": True,
                    "state_preserved": True,
                    "orientation_verified": True,
                    "orientation_changed": changed,
                    "initial_orientation": initial,
                    "requested_orientation": requested,
                    "observed_orientation": observed,
                    "final_orientation": final_orientation,
                    "before_hash": before_hash,
                    "after_hash": _hash(final_source),
                    "screenshot": shot.name,
                }
            )
            continue

        raise AppiumProbeError(f"unsupported_compatibility_probe:{kind}")

    return records


def execute(plan: dict[str, Any]) -> dict[str, Any]:
    profile = plan.get("profile")
    if not isinstance(profile, dict):
        raise AppiumProbeError("profile_missing")
    if profile_contains_secret_like_key(profile):
        raise AppiumProbeError("profile_contains_secret_like_key")

    evidence_dir = Path(str(plan["evidence_dir"])).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    result_path = Path(str(plan["result_path"])).resolve()
    if not result_path.is_relative_to(evidence_dir):
        raise AppiumProbeError("result_path_outside_evidence_dir")

    base_url = str(
        os.environ.get("CLIENTPLATFORM_E2E_APPIUM_URL")
        or "http://127.0.0.1:4723"
    )
    client = AppiumClient(base_url)
    channel = str(plan["channel"])
    mode = str(plan.get("mode") or "transport")
    records: list[dict[str, Any]] = []
    try:
        client.create_session(_capabilities(plan, profile))
        if mode == "compatibility":
            records.extend(
                _run_compatibility(
                    client,
                    plan=plan,
                    profile=profile,
                    evidence_dir=evidence_dir,
                )
            )
        elif channel == "cockpit":
            url = str(profile.get("url") or "").strip()
            if not url:
                raise AppiumProbeError("cockpit_profile_url_missing")
            client.navigate("about:blank")
            before_text = client.source()
            before_hash = _hash(before_text)
            for index, probe in enumerate(plan["probes"], start=1):
                expected = str(probe["expect"])
                before_count = _count(before_text, expected)
                client.navigate(url)
                after_hash, expected_count, after_text = _poll_expected(
                    client,
                    expected=expected,
                    before_hash=before_hash,
                    before_count=before_count,
                    timeout_seconds=float(profile.get("response_timeout_seconds") or 30),
                )
                shot = evidence_dir / f"{channel}-{index:02d}-{probe['id']}.png"
                client.screenshot(shot)
                records.append(
                    {
                        "id": probe["id"],
                        "status": "ok",
                        "expected_text_asserted": True,
                        "ui_changed": True,
                        "before_hash": before_hash,
                        "after_hash": after_hash,
                        "before_expected_count": before_count,
                        "after_expected_count": expected_count,
                        "screenshot": shot.name,
                    }
                )
                before_text = after_text
                before_hash = after_hash
        else:
            _open_native_chat(client, profile)
            composer_locator = profile.get("composer_locator")
            send_locator = profile.get("send_locator")
            if not isinstance(composer_locator, dict) or not isinstance(send_locator, dict):
                raise AppiumProbeError("native_profile_requires_composer_and_send_locators")
            for index, probe in enumerate(plan["probes"], start=1):
                expected = str(probe["expect"])
                before_text = client.source()
                before_hash = _hash(before_text)
                before_count = _count(before_text, expected)
                composer = client.find(composer_locator)
                if profile.get("clear_before_input", True):
                    client.clear(composer)
                client.type_text(composer, str(probe["input"]))
                client.click(client.find(send_locator))
                after_hash, expected_count, _ = _poll_expected(
                    client,
                    expected=expected,
                    before_hash=before_hash,
                    before_count=before_count,
                    timeout_seconds=float(profile.get("response_timeout_seconds") or 30),
                )
                shot = evidence_dir / f"{channel}-{index:02d}-{probe['id']}.png"
                client.screenshot(shot)
                records.append(
                    {
                        "id": probe["id"],
                        "status": "ok",
                        "expected_text_asserted": True,
                        "ui_changed": True,
                        "before_hash": before_hash,
                        "after_hash": after_hash,
                        "before_expected_count": before_count,
                        "after_expected_count": expected_count,
                        "screenshot": shot.name,
                    }
                )
    finally:
        client.close()

    result = {
        "target_id": plan["target"]["id"],
        "channel": channel,
        "mode": mode,
        "probes": records,
    }
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    args = parser.parse_args()
    plan_path = Path(args.plan).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    result = execute(plan)
    print(
        "CLIENTPLATFORM_APPIUM_PROBES_OK "
        + json.dumps(
            {"target_id": result["target_id"], "channel": result["channel"], "probes": len(result["probes"])},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
