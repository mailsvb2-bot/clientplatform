from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "clientplatform" / "install-telegram-http-relay.sh"


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
