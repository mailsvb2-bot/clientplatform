from __future__ import annotations

import subprocess
from pathlib import Path


def replace_exact(path: str, old: str, new: str, *, count: int = 1) -> None:
    target = Path(path)
    source = target.read_text(encoding="utf-8")
    actual = source.count(old)
    if actual != count:
        raise SystemExit(f"{path}: expected {count} occurrences, found {actual}: {old[:120]!r}")
    target.write_text(source.replace(old, new, count), encoding="utf-8")


# scripts/runtime_contract.py
replace_exact(
    "scripts/runtime_contract.py",
    '''def _payment_public_base_url() -> str:\n    return _first_value("PAYMENT_PUBLIC_BASE_URL", "MESSENGER_PUBLIC_BASE_URL", "PUBLIC_BASE_URL").rstrip("/")\n\n\n''',
    '''def _payment_public_base_url() -> str:\n    return _first_value("PAYMENT_PUBLIC_BASE_URL", "MESSENGER_PUBLIC_BASE_URL", "PUBLIC_BASE_URL").rstrip("/")\n\n\ndef _privacy_export_public_base_url() -> str:\n    return _first_value(\n        "PRIVACY_EXPORT_PUBLIC_BASE_URL",\n        "MESSENGER_PUBLIC_BASE_URL",\n        "PAYMENT_PUBLIC_BASE_URL",\n        "PUBLIC_BASE_URL",\n    ).rstrip("/")\n\n\n''',
)
replace_exact(
    "scripts/runtime_contract.py",
    '''def _positive_int(name: str, default: int, errors: list[str]) -> int:\n    raw = _value(name) or str(default)\n    try:\n        value = int(raw)\n    except ValueError:\n        errors.append(f"{name} must be an integer, got {raw!r}")\n        return default\n    if value <= 0:\n        errors.append(f"{name} must be positive, got {value}")\n        return default\n    return value\n\n\n''',
    '''def _positive_int(name: str, default: int, errors: list[str]) -> int:\n    raw = _value(name) or str(default)\n    try:\n        value = int(raw)\n    except ValueError:\n        errors.append(f"{name} must be an integer, got {raw!r}")\n        return default\n    if value <= 0:\n        errors.append(f"{name} must be positive, got {value}")\n        return default\n    return value\n\n\ndef _bounded_int(\n    name: str,\n    default: int,\n    *,\n    minimum: int,\n    maximum: int,\n    errors: list[str],\n) -> int:\n    value = _positive_int(name, default, errors)\n    if value < minimum or value > maximum:\n        errors.append(f"{name} must be between {minimum} and {maximum}, got {value}")\n    return value\n\n\n''',
)
replace_exact(
    "scripts/runtime_contract.py",
    '''def _payment_enabled() -> bool:\n    explicit = _optional_flag("PAYMENT_HTTP_ENABLED")\n    if explicit is not None:\n        return explicit\n    return _truthy("MESSENGER_WEBHOOK_ENABLED")\n\n\n''',
    '''def _payment_enabled() -> bool:\n    explicit = _optional_flag("PAYMENT_HTTP_ENABLED")\n    if explicit is not None:\n        return explicit\n    return _truthy("MESSENGER_WEBHOOK_ENABLED")\n\n\ndef _privacy_export_enabled() -> bool:\n    explicit = _optional_flag("PRIVACY_EXPORT_HTTP_ENABLED")\n    return bool(explicit) if explicit is not None else False\n\n\n''',
)
replace_exact(
    "scripts/runtime_contract.py",
    '''def _http_ingress_enabled() -> bool:\n    return _payment_enabled() or _max_enabled() or _vk_enabled()\n''',
    '''def _http_ingress_enabled() -> bool:\n    return (\n        _payment_enabled()\n        or _privacy_export_enabled()\n        or _max_enabled()\n        or _vk_enabled()\n    )\n''',
)
replace_exact(
    "scripts/runtime_contract.py",
    '''    app_env = (_value("APP_ENV") or "dev").lower()\n    prod = app_env in {"prod", "production"}\n''',
    '''    app_env = (_value("APP_ENV") or "dev").lower()\n    prod = app_env in {"prod", "production"}\n    secure_env = app_env in {"prod", "production", "stage", "staging"}\n''',
)
replace_exact(
    "scripts/runtime_contract.py",
    '''        if _truthy("HEALTHCHECK_ENABLED", "1") is False:\n            errors.append("HEALTHCHECK_ENABLED must be 1 in prod")\n\n    payment_enabled = _payment_enabled()\n    max_enabled = _max_enabled()\n    vk_enabled = _vk_enabled()\n    ingress_enabled = _http_ingress_enabled()\n''',
    '''        if _truthy("HEALTHCHECK_ENABLED", "1") is False:\n            errors.append("HEALTHCHECK_ENABLED must be 1 in prod")\n        if not _privacy_export_enabled():\n            errors.append("PRIVACY_EXPORT_HTTP_ENABLED must be 1 in prod")\n\n    payment_enabled = _payment_enabled()\n    privacy_export_enabled = _privacy_export_enabled()\n    max_enabled = _max_enabled()\n    vk_enabled = _vk_enabled()\n    ingress_enabled = _http_ingress_enabled()\n''',
)
replace_exact(
    "scripts/runtime_contract.py",
    '''    if payment_enabled and not _payment_public_base_url():\n        errors.append("PAYMENT_PUBLIC_BASE_URL or MESSENGER_PUBLIC_BASE_URL is required when payment HTTP ingress is enabled")\n\n    public_base = _value("MESSENGER_PUBLIC_BASE_URL")\n''',
    '''    if payment_enabled and not _payment_public_base_url():\n        errors.append("PAYMENT_PUBLIC_BASE_URL or MESSENGER_PUBLIC_BASE_URL is required when payment HTTP ingress is enabled")\n\n    if privacy_export_enabled:\n        privacy_base = _privacy_export_public_base_url()\n        if not privacy_base:\n            errors.append(\n                "PRIVACY_EXPORT_PUBLIC_BASE_URL or MESSENGER_PUBLIC_BASE_URL is required "\n                "when privacy export HTTP ingress is enabled"\n            )\n        elif not privacy_base.startswith(("https://", "http://")):\n            errors.append("privacy export public base URL must be a full http(s) URL")\n        elif secure_env and not privacy_base.startswith("https://"):\n            errors.append("privacy export public base URL must start with https:// in secure environments")\n        _bounded_int(\n            "PRIVACY_EXPORT_TOKEN_TTL_MINUTES",\n            10,\n            minimum=2,\n            maximum=30,\n            errors=errors,\n        )\n\n    public_base = _value("MESSENGER_PUBLIC_BASE_URL")\n''',
)
replace_exact(
    "scripts/runtime_contract.py",
    '''        warnings.append("HTTP ingress is disabled; YooKassa/MAX/VK web endpoints will not be served by this process")\n''',
    '''        warnings.append(\n            "HTTP ingress is disabled; YooKassa/privacy export/MAX/VK web endpoints "\n            "will not be served by this process"\n        )\n''',
)

# scripts/prod_readiness_check.py
replace_exact(
    "scripts/prod_readiness_check.py",
    '''def _int(name: str, default: int, errors: list[str]) -> int:\n    raw = (os.getenv(name, str(default)) or str(default)).strip()\n    try:\n        value = int(raw)\n    except ValueError:\n        errors.append(f"{name} must be integer, got {raw!r}")\n        return default\n    if value <= 0:\n        errors.append(f"{name} must be positive, got {value}")\n        return default\n    return value\n\n\n''',
    '''def _int(name: str, default: int, errors: list[str]) -> int:\n    raw = (os.getenv(name, str(default)) or str(default)).strip()\n    try:\n        value = int(raw)\n    except ValueError:\n        errors.append(f"{name} must be integer, got {raw!r}")\n        return default\n    if value <= 0:\n        errors.append(f"{name} must be positive, got {value}")\n        return default\n    return value\n\n\ndef _bounded_int(\n    name: str,\n    default: int,\n    *,\n    minimum: int,\n    maximum: int,\n    errors: list[str],\n) -> int:\n    value = _int(name, default, errors)\n    if value < minimum or value > maximum:\n        errors.append(f"{name} must be between {minimum} and {maximum}, got {value}")\n    return value\n\n\n''',
)
replace_exact(
    "scripts/prod_readiness_check.py",
    '''def _payment_public_base_url() -> str:\n    return _first_env("PAYMENT_PUBLIC_BASE_URL", "MESSENGER_PUBLIC_BASE_URL", "PUBLIC_BASE_URL").rstrip("/")\n\n\n''',
    '''def _payment_public_base_url() -> str:\n    return _first_env("PAYMENT_PUBLIC_BASE_URL", "MESSENGER_PUBLIC_BASE_URL", "PUBLIC_BASE_URL").rstrip("/")\n\n\ndef _privacy_export_public_base_url() -> str:\n    return _first_env(\n        "PRIVACY_EXPORT_PUBLIC_BASE_URL",\n        "MESSENGER_PUBLIC_BASE_URL",\n        "PAYMENT_PUBLIC_BASE_URL",\n        "PUBLIC_BASE_URL",\n    ).rstrip("/")\n\n\n''',
)
replace_exact(
    "scripts/prod_readiness_check.py",
    '''def _payment_http_enabled() -> bool:\n    explicit = _optional_flag("PAYMENT_HTTP_ENABLED")\n    if explicit is not None:\n        return explicit\n    return _truthy("MESSENGER_WEBHOOK_ENABLED")\n\n\n''',
    '''def _payment_http_enabled() -> bool:\n    explicit = _optional_flag("PAYMENT_HTTP_ENABLED")\n    if explicit is not None:\n        return explicit\n    return _truthy("MESSENGER_WEBHOOK_ENABLED")\n\n\ndef _privacy_export_http_enabled() -> bool:\n    explicit = _optional_flag("PRIVACY_EXPORT_HTTP_ENABLED")\n    return bool(explicit) if explicit is not None else False\n\n\n''',
)
replace_exact(
    "scripts/prod_readiness_check.py",
    '''def _http_ingress_enabled() -> bool:\n    return _payment_http_enabled() or _max_webhook_enabled() or _vk_webhook_enabled()\n''',
    '''def _http_ingress_enabled() -> bool:\n    return (\n        _payment_http_enabled()\n        or _privacy_export_http_enabled()\n        or _max_webhook_enabled()\n        or _vk_webhook_enabled()\n    )\n''',
)
replace_exact(
    "scripts/prod_readiness_check.py",
    '''    payment_enabled = _payment_http_enabled()\n    max_enabled = _max_webhook_enabled()\n    vk_enabled = _vk_webhook_enabled()\n    ingress_enabled = payment_enabled or max_enabled or vk_enabled\n\n    if payment_enabled and not _payment_public_base_url():\n        errors.append("PAYMENT_PUBLIC_BASE_URL or MESSENGER_PUBLIC_BASE_URL is required when payment HTTP ingress is enabled")\n\n    public_base = (os.getenv("MESSENGER_PUBLIC_BASE_URL") or "").strip().rstrip("/")\n''',
    '''    payment_enabled = _payment_http_enabled()\n    privacy_export_enabled = _privacy_export_http_enabled()\n    max_enabled = _max_webhook_enabled()\n    vk_enabled = _vk_webhook_enabled()\n    ingress_enabled = payment_enabled or privacy_export_enabled or max_enabled or vk_enabled\n\n    if payment_enabled and not _payment_public_base_url():\n        errors.append("PAYMENT_PUBLIC_BASE_URL or MESSENGER_PUBLIC_BASE_URL is required when payment HTTP ingress is enabled")\n\n    if prod and not privacy_export_enabled:\n        errors.append("PRIVACY_EXPORT_HTTP_ENABLED must be 1 in prod")\n    if privacy_export_enabled:\n        privacy_base = _privacy_export_public_base_url()\n        if not privacy_base:\n            errors.append(\n                "PRIVACY_EXPORT_PUBLIC_BASE_URL or MESSENGER_PUBLIC_BASE_URL is required "\n                "when privacy export HTTP ingress is enabled"\n            )\n        elif not privacy_base.startswith(("https://", "http://")):\n            errors.append("privacy export public base URL must be a full http(s) URL")\n        elif prod and not privacy_base.startswith("https://"):\n            errors.append("privacy export public base URL must start with https:// in prod")\n        _bounded_int(\n            "PRIVACY_EXPORT_TOKEN_TTL_MINUTES",\n            10,\n            minimum=2,\n            maximum=30,\n            errors=errors,\n        )\n\n    public_base = (os.getenv("MESSENGER_PUBLIC_BASE_URL") or "").strip().rstrip("/")\n''',
)
replace_exact(
    "scripts/prod_readiness_check.py",
    '''        warnings.append("HTTP ingress is disabled; YooKassa/MAX/VK web endpoints will not be served by this process")\n''',
    '''        warnings.append(\n            "HTTP ingress is disabled; YooKassa/privacy export/MAX/VK web endpoints "\n            "will not be served by this process"\n        )\n''',
)

# deploy/RUNTIME_CONTRACT.md
replace_exact(
    "deploy/RUNTIME_CONTRACT.md",
    '''- `HEALTHCHECK_PORT=8082`\n''',
    '''- `HEALTHCHECK_PORT=8082`\n- `PRIVACY_EXPORT_HTTP_ENABLED=1`\n- `PRIVACY_EXPORT_PUBLIC_BASE_URL=https://<public-host>`\n- `PRIVACY_EXPORT_TOKEN_TTL_MINUTES=10` (accepted range: 2..30)\n''',
)
replace_exact(
    "deploy/RUNTIME_CONTRACT.md",
    '''- audio media/access links\n''',
    '''- audio media/access links\n- one-time privacy export confirmation/download links\n''',
)
replace_exact(
    "deploy/RUNTIME_CONTRACT.md",
    '''This does not imply Telegram webhook mode. Telegram remains polling.\n''',
    '''This does not imply Telegram webhook mode. Telegram remains polling.\n\nThe live `/etc/metrotherapy/metrotherapy.env` file is authoritative and is not\nreplaced by immutable deploys. Before rollout, add the privacy export variables\nabove to that server-side file; otherwise production readiness must fail closed.\n''',
)

# tests/test_runtime_contract.py
replace_exact(
    "tests/test_runtime_contract.py",
    '''        "MESSENGER_PUBLIC_BASE_URL",\n        "PUBLIC_BASE_URL",\n''',
    '''        "MESSENGER_PUBLIC_BASE_URL",\n        "PAYMENT_HTTP_ENABLED",\n        "PAYMENT_PUBLIC_BASE_URL",\n        "PRIVACY_EXPORT_HTTP_ENABLED",\n        "PRIVACY_EXPORT_PUBLIC_BASE_URL",\n        "PRIVACY_EXPORT_TOKEN_TTL_MINUTES",\n        "PUBLIC_BASE_URL",\n''',
)
with Path("tests/test_runtime_contract.py").open("a", encoding="utf-8") as handle:
    handle.write(
        '''\n\ndef test_runtime_contract_requires_privacy_export_in_prod(monkeypatch):\n    errors, warnings = _run(\n        monkeypatch,\n        APP_ENV="prod",\n        TELEGRAM_TRANSPORT="polling",\n        TELEGRAM_WEBHOOK_ENABLED="0",\n        PRIVACY_EXPORT_HTTP_ENABLED="0",\n        METRO_DB_ENGINE="postgres",\n        DATABASE_URL="postgresql:///metrotherapy_test",\n        LOG_PATH="/tmp/metrotherapy.log",\n        HEALTHCHECK_ENABLED="1",\n    )\n\n    assert any("PRIVACY_EXPORT_HTTP_ENABLED" in error for error in errors)\n\n\ndef test_runtime_contract_accepts_privacy_export_as_http_ingress(monkeypatch):\n    errors, warnings = _run(\n        monkeypatch,\n        APP_ENV="dev",\n        PAYMENT_HTTP_ENABLED="0",\n        PRIVACY_EXPORT_HTTP_ENABLED="1",\n        PRIVACY_EXPORT_PUBLIC_BASE_URL="https://example.invalid",\n        PRIVACY_EXPORT_TOKEN_TTL_MINUTES="10",\n        MAX_WEBHOOK_ENABLED="0",\n        VK_WEBHOOK_ENABLED="0",\n    )\n\n    assert not any("PRIVACY_EXPORT" in error or "privacy export" in error for error in errors)\n    assert not any("HTTP ingress is disabled" in warning for warning in warnings)\n\n\ndef test_runtime_contract_rejects_insecure_privacy_url_and_bad_ttl(monkeypatch):\n    errors, warnings = _run(\n        monkeypatch,\n        APP_ENV="stage",\n        PRIVACY_EXPORT_HTTP_ENABLED="1",\n        PRIVACY_EXPORT_PUBLIC_BASE_URL="http://example.invalid",\n        PRIVACY_EXPORT_TOKEN_TTL_MINUTES="31",\n    )\n\n    assert any("https://" in error for error in errors)\n    assert any("PRIVACY_EXPORT_TOKEN_TTL_MINUTES" in error for error in errors)\n'''
    )

# tests/test_prod_readiness_messenger_env.py
replace_exact(
    "tests/test_prod_readiness_messenger_env.py",
    '''        "PAYMENT_PUBLIC_BASE_URL",\n        "MAX_WEBHOOK_ENABLED",\n''',
    '''        "PAYMENT_PUBLIC_BASE_URL",\n        "PRIVACY_EXPORT_HTTP_ENABLED",\n        "PRIVACY_EXPORT_PUBLIC_BASE_URL",\n        "PRIVACY_EXPORT_TOKEN_TTL_MINUTES",\n        "MAX_WEBHOOK_ENABLED",\n''',
)
with Path("tests/test_prod_readiness_messenger_env.py").open("a", encoding="utf-8") as handle:
    handle.write(
        '''\n\ndef test_readiness_requires_privacy_export_in_prod(monkeypatch):\n    errors, warnings = _run(\n        monkeypatch,\n        APP_ENV="prod",\n        HEALTHCHECK_ENABLED="1",\n        PRIVACY_EXPORT_HTTP_ENABLED="0",\n    )\n\n    assert any("PRIVACY_EXPORT_HTTP_ENABLED" in error for error in errors)\n\n\ndef test_readiness_accepts_privacy_export_as_only_http_ingress(monkeypatch):\n    errors, warnings = _run(\n        monkeypatch,\n        **_base_dev_env(),\n        PAYMENT_HTTP_ENABLED="0",\n        PRIVACY_EXPORT_HTTP_ENABLED="1",\n        PRIVACY_EXPORT_PUBLIC_BASE_URL="https://metrotherapy.ru",\n        PRIVACY_EXPORT_TOKEN_TTL_MINUTES="10",\n        MAX_WEBHOOK_ENABLED="0",\n        VK_WEBHOOK_ENABLED="0",\n    )\n\n    assert not any("PRIVACY_EXPORT" in error or "privacy export" in error for error in errors)\n    assert not any("HTTP ingress is disabled" in warning for warning in warnings)\n\n\ndef test_readiness_rejects_invalid_privacy_export_contract(monkeypatch):\n    errors, warnings = _run(\n        monkeypatch,\n        **_base_dev_env(),\n        PRIVACY_EXPORT_HTTP_ENABLED="1",\n        PRIVACY_EXPORT_PUBLIC_BASE_URL="ftp://metrotherapy.ru",\n        PRIVACY_EXPORT_TOKEN_TTL_MINUTES="1",\n    )\n\n    assert any("full http(s) URL" in error for error in errors)\n    assert any("PRIVACY_EXPORT_TOKEN_TTL_MINUTES" in error for error in errors)\n'''
    )

subprocess.run(
    [
        "python",
        "-m",
        "py_compile",
        "scripts/runtime_contract.py",
        "scripts/prod_readiness_check.py",
    ],
    check=True,
)
subprocess.run(
    [
        "python",
        "-m",
        "pytest",
        "-q",
        "tests/test_runtime_contract.py",
        "tests/test_prod_readiness_messenger_env.py",
        "tests/test_privacy_export_download.py",
    ],
    check=True,
)
