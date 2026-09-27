from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True, slots=True)
class YandexBillingSnapshot:
    configured: bool
    available: bool
    active: bool = False
    balance: Decimal | None = None
    currency: str = ""
    error_code: str = ""


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"yandex_billing_http_{int(exc.code)}"
    if isinstance(exc, urllib.error.URLError):
        return f"yandex_billing_transport_{type(exc.reason).__name__}"
    return f"yandex_billing_transport_{type(exc).__name__}"


def get_yandex_billing_snapshot() -> YandexBillingSnapshot:
    account_id = str(os.getenv("YANDEX_BILLING_ACCOUNT_ID", "") or "").strip()
    iam_token = str(os.getenv("YANDEX_BILLING_IAM_TOKEN", "") or "").strip()
    if not account_id or not iam_token:
        return YandexBillingSnapshot(configured=False, available=False)

    quoted = urllib.parse.quote(account_id, safe="")
    request = urllib.request.Request(
        "https://billing.api.cloud.yandex.net/billing/v1/billingAccounts/" + quoted,
        headers={
            "Authorization": "Bearer " + iam_token,
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310 - fixed Yandex Billing endpoint
            raw = response.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                return YandexBillingSnapshot(
                    configured=True,
                    available=False,
                    error_code="yandex_billing_response_too_large",
                )
    except urllib.error.HTTPError as exc:
        return YandexBillingSnapshot(
            configured=True,
            available=False,
            error_code=_safe_error(exc),
        )
    except urllib.error.URLError as exc:
        return YandexBillingSnapshot(
            configured=True,
            available=False,
            error_code=_safe_error(exc),
        )
    except TimeoutError as exc:
        return YandexBillingSnapshot(
            configured=True,
            available=False,
            error_code=_safe_error(exc),
        )
    except OSError as exc:
        return YandexBillingSnapshot(
            configured=True,
            available=False,
            error_code=_safe_error(exc),
        )

    try:
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        balance = Decimal(str(payload.get("balance") or "0"))
    except UnicodeDecodeError:
        return YandexBillingSnapshot(
            configured=True,
            available=False,
            error_code="yandex_billing_invalid_response",
        )
    except json.JSONDecodeError:
        return YandexBillingSnapshot(
            configured=True,
            available=False,
            error_code="yandex_billing_invalid_response",
        )
    except InvalidOperation:
        return YandexBillingSnapshot(
            configured=True,
            available=False,
            error_code="yandex_billing_invalid_response",
        )
    except ValueError:
        return YandexBillingSnapshot(
            configured=True,
            available=False,
            error_code="yandex_billing_invalid_response",
        )

    return YandexBillingSnapshot(
        configured=True,
        available=True,
        active=bool(payload.get("active")),
        balance=balance,
        currency=str(payload.get("currency") or ""),
    )


def billing_thresholds() -> tuple[Decimal, ...]:
    raw = str(
        os.getenv(
            "YANDEX_BILLING_ALERT_THRESHOLDS",
            "5000,2000,1000,500,100,0",
        )
        or ""
    )
    values: set[Decimal] = set()
    for part in raw.split(","):
        try:
            value = Decimal(part.strip())
        except InvalidOperation:
            continue
        values.add(value)
    return tuple(sorted(values, reverse=True))


def crossed_balance_threshold(
    balance: Decimal,
    previous_balance: Decimal | None,
) -> Decimal | None:
    for threshold in reversed(billing_thresholds()):
        if balance <= threshold and (
            previous_balance is None or previous_balance > threshold
        ):
            return threshold
    return None


__all__ = [
    "YandexBillingSnapshot",
    "billing_thresholds",
    "crossed_balance_threshold",
    "get_yandex_billing_snapshot",
]
