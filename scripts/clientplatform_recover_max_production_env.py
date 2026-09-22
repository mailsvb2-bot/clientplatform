from __future__ import annotations

"""Recover the production MAX bot configuration without exposing credentials.

The helper is intentionally narrow: it may recover a missing MAX bot token only
from root-owned historical ClientPlatform env snapshots next to the production
env file. The token is verified against MAX's official API before anything is
written. Missing non-token values are then derived or generated locally.
"""

import argparse
import hashlib
import json
import os
import secrets
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

OFFICIAL_MAX_API_BASE = "https://platform-api2.max.ru"
CANONICAL_MAX_LINK_BASE = "https://max.ru/{bot}?payload={payload}"
_TRUE_VALUES = {"1", "true", "yes", "on"}


class MaxProductionRecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecoveryReport:
    ok: bool
    token_source: str = ""
    bot_identity_verified: bool = False
    webhook_secret_generated: bool = False
    bot_link_derived: bool = False
    bot_name_derived: bool = False
    max_webhook_enabled: bool = False
    error_code: str = ""


def _parse_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return lines, values


def _safe_error(code: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._:-" else "_" for ch in str(code or ""))[:120]


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _historical_token_candidates(env_file: Path) -> list[tuple[str, float, str]]:
    candidates: dict[str, tuple[str, float, str]] = {}
    for path in env_file.parent.glob(env_file.name + ".*"):
        if path == env_file or path.is_symlink() or not path.is_file():
            continue
        try:
            _lines, values = _parse_env(path)
            token = str(values.get("MAX_BOT_TOKEN", "") or "").strip()
            if not token:
                continue
            fingerprint = _token_fingerprint(token)
            modified = float(path.stat().st_mtime)
            previous = candidates.get(fingerprint)
            if previous is None or modified > previous[1]:
                candidates[fingerprint] = (token, modified, path.name)
        except (OSError, UnicodeError):
            continue
    return sorted(candidates.values(), key=lambda item: item[1], reverse=True)


def _validate_api_base(values: dict[str, str]) -> str:
    configured = str(values.get("MAX_API_BASE_URL", "") or "").strip().rstrip("/")
    if not configured:
        return OFFICIAL_MAX_API_BASE
    if configured != OFFICIAL_MAX_API_BASE:
        raise MaxProductionRecoveryError("max_api_base_not_official")
    return configured


def _validate_public_base(values: dict[str, str]) -> str:
    value = str(values.get("MESSENGER_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise MaxProductionRecoveryError("messenger_public_base_invalid")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise MaxProductionRecoveryError("messenger_public_base_invalid")
    return value


def _max_me(token: str, api_base: str, *, timeout_sec: int) -> dict[str, Any] | None:
    request = urllib.request.Request(
        api_base + "/me",
        headers={"Authorization": token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=max(1, min(int(timeout_sec), 30)),
            context=ssl.create_default_context(),
        ) as response:
            status = int(response.status)
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        exc.read()
        return None
    except (urllib.error.URLError, OSError, ssl.SSLError):
        raise MaxProductionRecoveryError("max_api_transport_failed") from None
    if status != 200:
        return None
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _select_token(
    values: dict[str, str],
    env_file: Path,
    *,
    api_base: str,
    timeout_sec: int,
) -> tuple[str, str, dict[str, Any]]:
    current = str(values.get("MAX_BOT_TOKEN", "") or "").strip()
    if current:
        identity = _max_me(current, api_base, timeout_sec=timeout_sec)
        if identity is None:
            raise MaxProductionRecoveryError("current_max_bot_token_rejected")
        return current, "current", identity

    candidates = _historical_token_candidates(env_file)
    if not candidates:
        raise MaxProductionRecoveryError("max_bot_token_not_recoverable")

    valid: list[tuple[str, float, str, dict[str, Any]]] = []
    for token, modified, name in candidates:
        identity = _max_me(token, api_base, timeout_sec=timeout_sec)
        if identity is not None:
            valid.append((token, modified, name, identity))

    if not valid:
        raise MaxProductionRecoveryError("historical_max_bot_tokens_rejected")

    identities = {
        str(item[3].get("user_id") or item[3].get("username") or "").strip()
        for item in valid
    }
    identities.discard("")
    if len(identities) > 1:
        raise MaxProductionRecoveryError("multiple_valid_max_bot_identities")

    token, _modified, _name, identity = sorted(
        valid,
        key=lambda item: item[1],
        reverse=True,
    )[0]
    return token, "historical_backup", identity


def _safe_bot_name(identity: dict[str, Any]) -> str:
    username = str(identity.get("username") or "").strip().lstrip("@")
    if not username:
        raise MaxProductionRecoveryError("max_bot_username_missing")
    if len(username) > 128 or any(ch.isspace() for ch in username) or "/" in username:
        raise MaxProductionRecoveryError("max_bot_username_invalid")
    return username


def _set_values(lines: list[str], updates: dict[str, str]) -> list[str]:
    remaining = dict(updates)
    rendered: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                rendered.append(f"{key}={remaining.pop(key)}")
                continue
        rendered.append(raw)
    for key, value in remaining.items():
        rendered.append(f"{key}={value}")
    return rendered


def _atomic_write(env_file: Path, lines: list[str]) -> None:
    stat = env_file.stat()
    fd, temp_name = tempfile.mkstemp(prefix=env_file.name + ".", dir=str(env_file.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, stat.st_mode & 0o777)
        if hasattr(os, "chown"):
            os.chown(temp_name, stat.st_uid, stat.st_gid)
        os.replace(temp_name, env_file)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def recover(env_file: Path, *, timeout_sec: int = 15) -> RecoveryReport:
    if env_file.is_symlink() or not env_file.is_file():
        raise MaxProductionRecoveryError("production_env_missing")
    lines, values = _parse_env(env_file)
    api_base = _validate_api_base(values)
    _validate_public_base(values)
    token, token_source, identity = _select_token(
        values,
        env_file,
        api_base=api_base,
        timeout_sec=timeout_sec,
    )
    bot_name = _safe_bot_name(identity)

    existing_secret = str(values.get("MAX_WEBHOOK_SECRET", "") or "").strip()
    generated_secret = False
    if existing_secret:
        allowed = all(ch.isalnum() or ch in "_-" for ch in existing_secret)
        if not allowed or not 5 <= len(existing_secret) <= 256:
            raise MaxProductionRecoveryError("max_webhook_secret_invalid")
        webhook_secret = existing_secret
    else:
        webhook_secret = secrets.token_urlsafe(32)
        generated_secret = True

    current_link = str(values.get("MAX_BOT_LINK_BASE", "") or "").strip()
    link_derived = not current_link
    link_base = current_link or CANONICAL_MAX_LINK_BASE
    parsed_link = urllib.parse.urlsplit(
        link_base.replace("{bot}", "bot").replace("{payload}", "payload")
    )
    if parsed_link.scheme != "https" or parsed_link.hostname not in {"max.ru", "www.max.ru"}:
        raise MaxProductionRecoveryError("max_bot_link_base_invalid")

    bot_name_derived = not str(values.get("MAX_BOT_NAME", "") or "").strip()
    updates = {
        "MAX_API_BASE_URL": OFFICIAL_MAX_API_BASE,
        "MAX_BOT_TOKEN": token,
        "MAX_WEBHOOK_SECRET": webhook_secret,
        "MAX_BOT_LINK_BASE": link_base,
        "MAX_BOT_NAME": str(values.get("MAX_BOT_NAME", "") or "").strip() or bot_name,
        "MAX_WEBHOOK_ENABLED": "1",
    }
    _atomic_write(env_file, _set_values(lines, updates))
    return RecoveryReport(
        ok=True,
        token_source=token_source,
        bot_identity_verified=True,
        webhook_secret_generated=generated_secret,
        bot_link_derived=link_derived,
        bot_name_derived=bot_name_derived,
        max_webhook_enabled=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--timeout-sec", type=int, default=15)
    args = parser.parse_args()
    try:
        report = recover(Path(args.env_file), timeout_sec=int(args.timeout_sec))
    except MaxProductionRecoveryError as exc:
        report = RecoveryReport(ok=False, error_code=_safe_error(str(exc)))
    except (OSError, UnicodeError, ValueError):
        report = RecoveryReport(ok=False, error_code="unexpected_recovery_error")
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
