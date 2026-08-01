from __future__ import annotations

import importlib.util
import ipaddress
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "clientplatform"
RELAY = DEPLOY / "telegram_ipv6_connect_relay.py"
ACTIVATOR = DEPLOY / "activate-local-ipv6-telegram-egress.sh"
COMPOSE = DEPLOY / "compose.production.yml"


def _load_relay() -> ModuleType:
    spec = importlib.util.spec_from_file_location("telegram_ipv6_connect_relay", RELAY)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_relay_accepts_only_exact_telegram_connect_target() -> None:
    relay = _load_relay()

    assert relay._valid_connect_request(
        b"CONNECT api.telegram.org:443 HTTP/1.1\r\nHost: api.telegram.org:443\r\n\r\n"
    )
    assert not relay._valid_connect_request(
        b"CONNECT example.com:443 HTTP/1.1\r\n\r\n"
    )
    assert not relay._valid_connect_request(
        b"CONNECT api.telegram.org:80 HTTP/1.1\r\n\r\n"
    )
    assert not relay._valid_connect_request(
        b"GET https://api.telegram.org/ HTTP/1.1\r\n\r\n"
    )


def test_local_relay_rejects_public_clients() -> None:
    relay = _load_relay()
    networks = tuple(
        ipaddress.ip_network(value) for value in relay.DEFAULT_ALLOWED_SUBNETS
    )

    assert relay._allowed_peer("127.0.0.1", networks)
    assert relay._allowed_peer("172.20.0.5", networks)
    assert not relay._allowed_peer("185.104.114.163", networks)
    assert not relay._allowed_peer("203.0.113.10", networks)


def test_local_relay_forces_ipv6_for_telegram_upstream() -> None:
    text = RELAY.read_text(encoding="utf-8")

    assert 'TARGET_HOST = "api.telegram.org"' in text
    assert "TARGET_PORT = 443" in text
    assert "family=socket.AF_INET6" in text
    assert "asyncio.open_connection" in text
    assert "0.0.0.0" in text
    assert "HTTP/1.1 403 Forbidden" in text


def test_activation_fails_before_changes_when_host_ipv6_is_unavailable() -> None:
    text = ACTIVATOR.read_text(encoding="utf-8")

    direct_probe = text.index("curl -6")
    direct_failure = text.index('fail "direct_telegram_ipv6_unavailable"')
    git_reset = text.index('git reset --hard "$TARGET_SHA"')
    env_update = text.index('"TELEGRAM_PROXY_URL": f"http://host.docker.internal:{port}"')
    deploy = text.index("scripts.clientplatform_production_deploy")
    get_me = text.index("me = await bot.get_me()")
    success = text.index("CLIENTPLATFORM_LOCAL_IPV6_EGRESS_OK")

    assert direct_probe < direct_failure < git_reset < env_update < deploy < get_me < success
    assert 'fail "wrong_server"' in text
    assert "--recover-unavailable-baseline" in text
    assert '"TELEGRAM_TRANSPORT": "polling"' in text
    assert '"TELEGRAM_WEBHOOK_ENABLED": "0"' in text


def test_activation_removes_only_the_accidental_squid_configuration() -> None:
    text = ACTIVATOR.read_text(encoding="utf-8")

    assert "visible_hostname clientplatform-telegram-relay" in text
    assert 'fail "unrelated_squid_conflict"' in text
    assert "ACCIDENTAL_SQUID_REMOVED" in text
    assert "clientplatform-telegram-ipv6-relay.service" in text
    assert "DynamicUser=yes" in text
    assert "NoNewPrivileges=yes" in text
    assert "RestrictAddressFamilies=AF_INET AF_INET6" in text


def test_compose_exposes_only_the_docker_host_gateway_name() -> None:
    text = COMPOSE.read_text(encoding="utf-8")

    assert '"host.docker.internal:host-gateway"' in text
    assert "network_mode: host" not in text
    assert "privileged: true" not in text


def test_invalid_external_relay_scripts_are_removed() -> None:
    assert not (DEPLOY / "install-telegram-http-relay.sh").exists()
    assert not (DEPLOY / "activate-telegram-http-relay.sh").exists()


def test_activation_script_has_strict_shell_contract() -> None:
    text = ACTIVATOR.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail\n")
