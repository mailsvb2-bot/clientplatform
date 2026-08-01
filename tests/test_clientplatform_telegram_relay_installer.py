from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "clientplatform"
INSTALLER = DEPLOY / "install-telegram-http-relay.sh"
ACTIVATOR = DEPLOY / "activate-telegram-http-relay.sh"


def test_relay_installer_is_fail_closed_and_telegram_only() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    assert "CLIENTPLATFORM_SOURCE_IP" in text
    assert "acl clientplatform_source src ${SOURCE_IP}/32" in text
    assert "acl telegram_api dstdomain api.telegram.org" in text
    assert "acl telegram_tls_port port 443" in text
    assert "acl CONNECT method CONNECT" in text
    assert (
        "http_access allow clientplatform_source telegram_api "
        "telegram_tls_port CONNECT"
    ) in text
    assert "http_access deny all" in text
    assert "http_access allow all" not in text
    assert "cache deny all" in text


def test_relay_installer_proves_telegram_before_success_marker() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    probe = text.index("https://api.telegram.org/")
    marker = text.index("CLIENTPLATFORM_TELEGRAM_RELAY_OK")

    assert probe < marker
    assert "/usr/sbin/squid -k parse" in text
    assert "systemctl is-active --quiet squid" in text
    assert "listen_socket_missing" in text


def test_relay_installer_does_not_embed_bot_or_proxy_secrets() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    assert "BOT_TOKEN" not in text
    assert "TELEGRAM_PROXY_URL" not in text
    assert "Proxy-Authorization" not in text
    assert "password" not in text.lower()


def test_relay_activator_proves_route_before_mutating_production_env() -> None:
    text = ACTIVATOR.read_text(encoding="utf-8")

    route_probe = text.index("https://api.telegram.org/")
    env_update = text.index('"TELEGRAM_PROXY_URL": relay_url')
    deploy = text.index("update-production.sh")
    get_me = text.index("me = await bot.get_me()")
    success = text.index("CLIENTPLATFORM_TELEGRAM_RELAY_ACTIVATION_OK")

    assert route_probe < env_update < deploy < get_me < success
    assert "--recover-unavailable-baseline" in text
    assert "CLIENTPLATFORM_EXPECTED_SHA" in text
    assert '"TELEGRAM_TRANSPORT": "polling"' in text
    assert '"TELEGRAM_WEBHOOK_ENABLED": "0"' in text


def test_relay_activator_rejects_credentials_and_uses_exact_sha() -> None:
    text = ACTIVATOR.read_text(encoding="utf-8")

    assert "parsed.username is not None" in text
    assert "parsed.password is not None" in text
    assert 're.fullmatch(r"[0-9a-f]{40}", expected_sha)' in text
    assert 'test "$(git -C "$ROOT" rev-parse HEAD)" = "$EXPECTED_SHA"' in text
    assert 'proxy_mode != "http_connect"' in text
    assert "webhook.url" in text


def test_relay_scripts_are_bash_syntax_checked_by_release_gate_contract() -> None:
    for path in (INSTALLER, ACTIVATOR):
        text = path.read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail\n")
