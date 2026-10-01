from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_IAM_AUDIENCE = "https://iam.api.cloud.yandex.net/iam/v1/tokens"
_IAM_URL = _IAM_AUDIENCE
_cache_lock = threading.Lock()
_cached_token = ""
_cached_expires_epoch = 0.0
_cached_key_fingerprint = ""
_cached_art_token = ""
_cached_art_expires_epoch = 0.0
_cached_art_key_fingerprint = ""


@dataclass(frozen=True, slots=True)
class YandexIamTokenResult:
    configured: bool
    available: bool
    token: str = ""
    expires_at_epoch: float = 0.0
    auth_mode: str = ""
    error_code: str = ""


@dataclass(frozen=True, slots=True)
class _AuthorizedKey:
    key_id: str
    service_account_id: str
    private_key: str


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _safe_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _parse_authorized_key(raw: str) -> _AuthorizedKey | None:
    raw = str(raw or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    key_id = str(payload.get("id") or payload.get("key_id") or "").strip()
    service_account_id = str(
        payload.get("service_account_id")
        or payload.get("serviceAccountId")
        or ""
    ).strip()
    private_key = str(payload.get("private_key") or payload.get("privateKey") or "")
    if "\\n" in private_key and "\n" not in private_key:
        private_key = private_key.replace("\\n", "\n")
    private_key = private_key.strip()
    if not key_id or not service_account_id or "PRIVATE KEY" not in private_key:
        return None
    return _AuthorizedKey(
        key_id=key_id,
        service_account_id=service_account_id,
        private_key=private_key,
    )


def _read_authorized_key(raw_env: str, file_env: str) -> _AuthorizedKey | None:
    raw = str(os.getenv(raw_env, "") or "").strip()
    path = str(os.getenv(file_env, "") or "").strip()
    if not raw and path:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError:
            return None
    return _parse_authorized_key(raw)


def _authorized_key_source_error(
    raw_env: str,
    file_env: str,
    *,
    prefix: str,
) -> str:
    """Classify a configured-but-unusable authorized-key source without exposing it."""

    raw = str(os.getenv(raw_env, "") or "").strip()
    path = str(os.getenv(file_env, "") or "").strip()
    if raw:
        return f"{prefix}_invalid_authorized_key"
    if not path:
        return ""
    try:
        with Path(path).open("rb") as handle:
            handle.read(1)
    except FileNotFoundError:
        return f"{prefix}_authorized_key_file_missing"
    except OSError:
        return f"{prefix}_authorized_key_file_unreadable"
    return f"{prefix}_invalid_authorized_key"


def _load_authorized_key() -> _AuthorizedKey | None:
    return _read_authorized_key(
        "YANDEX_BILLING_AUTHORIZED_KEY_JSON",
        "YANDEX_BILLING_AUTHORIZED_KEY_FILE",
    )


def _load_art_authorized_key() -> _AuthorizedKey | None:
    dedicated_raw = str(os.getenv("YANDEX_ART_AUTHORIZED_KEY_JSON", "") or "").strip()
    dedicated_file = str(os.getenv("YANDEX_ART_AUTHORIZED_KEY_FILE", "") or "").strip()
    if dedicated_raw or dedicated_file:
        return _read_authorized_key(
            "YANDEX_ART_AUTHORIZED_KEY_JSON",
            "YANDEX_ART_AUTHORIZED_KEY_FILE",
        )
    return _load_authorized_key()


def _fingerprint(key: _AuthorizedKey) -> str:
    import hashlib

    return hashlib.sha256(
        (key.key_id + "|" + key.service_account_id + "|" + key.private_key).encode("utf-8")
    ).hexdigest()


def _sign_ps256(signing_input: bytes, private_key: str) -> bytes:
    key_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="clientplatform-yandex-key-",
            suffix=".pem",
            delete=False,
        ) as handle:
            key_path = handle.name
            os.chmod(key_path, 0o600)
            handle.write(private_key)
            if not private_key.endswith("\n"):
                handle.write("\n")
        result = subprocess.run(  # nosec B603 - fixed openssl command and private temp key path
            [
                "openssl",
                "dgst",
                "-sha256",
                "-sigopt",
                "rsa_padding_mode:pss",
                "-sigopt",
                "rsa_pss_saltlen:digest",
                "-sign",
                key_path,
            ],
            input=signing_input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
    finally:
        if key_path:
            try:
                Path(key_path).unlink(missing_ok=True)
            except OSError:
                pass
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError("yandex_iam_ps256_sign_failed")
    return bytes(result.stdout)


def _create_jwt(key: _AuthorizedKey, *, now_epoch: int | None = None) -> str:
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    header = {
        "alg": "PS256",
        "kid": key.key_id,
        "typ": "JWT",
    }
    payload = {
        "aud": _IAM_AUDIENCE,
        "exp": now + 3600,
        "iat": now,
        "iss": key.service_account_id,
    }
    encoded_header = _b64url(_safe_json(header).encode("utf-8"))
    encoded_payload = _b64url(_safe_json(payload).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = _b64url(_sign_ps256(signing_input, key.private_key))
    return f"{encoded_header}.{encoded_payload}.{signature}"


def _parse_expires_at(value: object) -> float:
    raw = str(value or "").strip()
    if not raw:
        return time.time() + 3000
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return time.time() + 3000
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _exchange_jwt(jwt_token: str) -> tuple[str, float]:
    body = json.dumps({"jwt": jwt_token}, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        _IAM_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310 - fixed Yandex IAM endpoint
            raw = response.read(256 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"yandex_billing_iam_http_{int(exc.code)}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"yandex_billing_iam_transport_{type(exc.reason).__name__}"
        ) from None
    except TimeoutError:
        raise RuntimeError("yandex_billing_iam_transport_TimeoutError") from None
    except OSError as exc:
        raise RuntimeError(
            f"yandex_billing_iam_transport_{type(exc).__name__}"
        ) from None
    if len(raw) > 256 * 1024:
        raise RuntimeError("yandex_billing_iam_response_too_large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise RuntimeError("yandex_billing_iam_invalid_response") from None
    except json.JSONDecodeError:
        raise RuntimeError("yandex_billing_iam_invalid_response") from None
    if not isinstance(payload, dict):
        raise RuntimeError("yandex_billing_iam_invalid_response")
    token = str(payload.get("iamToken") or "").strip()
    if not token:
        raise RuntimeError("yandex_billing_iam_missing_token")
    return token, _parse_expires_at(payload.get("expiresAt"))


def get_yandex_billing_iam_token() -> YandexIamTokenResult:
    global _cached_token, _cached_expires_epoch, _cached_key_fingerprint
    static_token = str(os.getenv("YANDEX_BILLING_IAM_TOKEN", "") or "").strip()
    key = _load_authorized_key()
    if key is None:
        if static_token:
            return YandexIamTokenResult(
                configured=True,
                available=True,
                token=static_token,
                auth_mode="static_iam_token",
            )
        source_error = _authorized_key_source_error(
            "YANDEX_BILLING_AUTHORIZED_KEY_JSON",
            "YANDEX_BILLING_AUTHORIZED_KEY_FILE",
            prefix="yandex_billing",
        )
        if source_error:
            return YandexIamTokenResult(
                configured=True,
                available=False,
                auth_mode="authorized_key",
                error_code=source_error,
            )
        return YandexIamTokenResult(configured=False, available=False)

    fingerprint = _fingerprint(key)
    now = time.time()
    with _cache_lock:
        if (
            _cached_token
            and _cached_key_fingerprint == fingerprint
            and _cached_expires_epoch - now > 300
        ):
            return YandexIamTokenResult(
                configured=True,
                available=True,
                token=_cached_token,
                expires_at_epoch=_cached_expires_epoch,
                auth_mode="authorized_key",
            )

    try:
        jwt_token = _create_jwt(key)
        token, expires_epoch = _exchange_jwt(jwt_token)
    except FileNotFoundError:
        return YandexIamTokenResult(
            configured=True,
            available=False,
            auth_mode="authorized_key",
            error_code="yandex_billing_iam_signer_unavailable",
        )
    except subprocess.TimeoutExpired:
        return YandexIamTokenResult(
            configured=True,
            available=False,
            auth_mode="authorized_key",
            error_code="yandex_billing_iam_sign_timeout",
        )
    except RuntimeError as exc:
        code = str(exc or "").strip()
        return YandexIamTokenResult(
            configured=True,
            available=False,
            auth_mode="authorized_key",
            error_code=(
                code
                if code.startswith("yandex_billing_iam_")
                else "yandex_billing_iam_unavailable"
            ),
        )

    with _cache_lock:
        _cached_token = token
        _cached_expires_epoch = expires_epoch
        _cached_key_fingerprint = fingerprint
    return YandexIamTokenResult(
        configured=True,
        available=True,
        token=token,
        expires_at_epoch=expires_epoch,
        auth_mode="authorized_key",
    )


def _art_error_code(code: str) -> str:
    raw = str(code or "").strip()
    if raw.startswith("yandex_billing_iam_"):
        return "yandex_art_iam_" + raw.removeprefix("yandex_billing_iam_")
    return "yandex_art_iam_unavailable"


def yandex_art_renewable_auth_configured() -> bool:
    return bool(
        str(os.getenv("YANDEX_ART_IAM_TOKEN", "") or "").strip()
        or str(os.getenv("YANDEX_ART_AUTHORIZED_KEY_JSON", "") or "").strip()
        or str(os.getenv("YANDEX_ART_AUTHORIZED_KEY_FILE", "") or "").strip()
        or str(os.getenv("YANDEX_BILLING_AUTHORIZED_KEY_JSON", "") or "").strip()
        or str(os.getenv("YANDEX_BILLING_AUTHORIZED_KEY_FILE", "") or "").strip()
    )


def get_yandex_art_iam_token() -> YandexIamTokenResult:
    global _cached_art_token, _cached_art_expires_epoch, _cached_art_key_fingerprint
    static_token = str(os.getenv("YANDEX_ART_IAM_TOKEN", "") or "").strip()
    if static_token:
        return YandexIamTokenResult(
            configured=True,
            available=True,
            token=static_token,
            auth_mode="static_iam_token",
        )

    key = _load_art_authorized_key()
    configured = yandex_art_renewable_auth_configured()
    if key is None:
        if not configured:
            return YandexIamTokenResult(configured=False, available=False)
        dedicated_raw = str(os.getenv("YANDEX_ART_AUTHORIZED_KEY_JSON", "") or "").strip()
        dedicated_file = str(os.getenv("YANDEX_ART_AUTHORIZED_KEY_FILE", "") or "").strip()
        if dedicated_raw or dedicated_file:
            source_error = _authorized_key_source_error(
                "YANDEX_ART_AUTHORIZED_KEY_JSON",
                "YANDEX_ART_AUTHORIZED_KEY_FILE",
                prefix="yandex_art",
            )
        else:
            source_error = _authorized_key_source_error(
                "YANDEX_BILLING_AUTHORIZED_KEY_JSON",
                "YANDEX_BILLING_AUTHORIZED_KEY_FILE",
                prefix="yandex_art",
            )
        return YandexIamTokenResult(
            configured=True,
            available=False,
            auth_mode="authorized_key",
            error_code=source_error or "yandex_art_invalid_authorized_key",
        )

    fingerprint = _fingerprint(key)
    now = time.time()
    with _cache_lock:
        if (
            _cached_art_token
            and _cached_art_key_fingerprint == fingerprint
            and _cached_art_expires_epoch - now > 300
        ):
            return YandexIamTokenResult(
                configured=True,
                available=True,
                token=_cached_art_token,
                expires_at_epoch=_cached_art_expires_epoch,
                auth_mode="authorized_key",
            )

    try:
        jwt_token = _create_jwt(key)
        token, expires_epoch = _exchange_jwt(jwt_token)
    except FileNotFoundError:
        return YandexIamTokenResult(
            configured=True,
            available=False,
            auth_mode="authorized_key",
            error_code="yandex_art_iam_signer_unavailable",
        )
    except subprocess.TimeoutExpired:
        return YandexIamTokenResult(
            configured=True,
            available=False,
            auth_mode="authorized_key",
            error_code="yandex_art_iam_sign_timeout",
        )
    except RuntimeError as exc:
        return YandexIamTokenResult(
            configured=True,
            available=False,
            auth_mode="authorized_key",
            error_code=_art_error_code(str(exc or "")),
        )

    with _cache_lock:
        _cached_art_token = token
        _cached_art_expires_epoch = expires_epoch
        _cached_art_key_fingerprint = fingerprint
    return YandexIamTokenResult(
        configured=True,
        available=True,
        token=token,
        expires_at_epoch=expires_epoch,
        auth_mode="authorized_key",
    )


def clear_yandex_art_iam_cache() -> None:
    global _cached_art_token, _cached_art_expires_epoch, _cached_art_key_fingerprint
    with _cache_lock:
        _cached_art_token = ""
        _cached_art_expires_epoch = 0.0
        _cached_art_key_fingerprint = ""


def clear_yandex_billing_iam_cache() -> None:
    global _cached_token, _cached_expires_epoch, _cached_key_fingerprint
    with _cache_lock:
        _cached_token = ""
        _cached_expires_epoch = 0.0
        _cached_key_fingerprint = ""


__all__ = [
    "YandexIamTokenResult",
    "clear_yandex_art_iam_cache",
    "clear_yandex_billing_iam_cache",
    "get_yandex_art_iam_token",
    "get_yandex_billing_iam_token",
    "yandex_art_renewable_auth_configured",
]
