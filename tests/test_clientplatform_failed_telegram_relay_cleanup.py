from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "clientplatform"
CLEANUP = DEPLOY / "cleanup-failed-telegram-relays.sh"
COMPOSE = DEPLOY / "compose.production.yml"


def test_failed_relay_implementations_are_not_shipped() -> None:
    assert not (DEPLOY / "activate-local-ipv6-telegram-egress.sh").exists()
    assert not (DEPLOY / "telegram_ipv6_connect_relay.py").exists()
    assert not (DEPLOY / "activate-telegram-http-relay.sh").exists()
    assert not (DEPLOY / "install-telegram-http-relay.sh").exists()


def test_cleanup_targets_only_known_clientplatform_relay_markers() -> None:
    text = CLEANUP.read_text(encoding="utf-8")

    assert text.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail\n")
    assert "visible_hostname clientplatform-telegram-relay" in text
    assert "unrelated_active_squid_detected" in text
    assert "unrelated_local_relay_service_detected" in text
    assert "host.docker.internal" in text
    assert "147.45.146.112" in text
    assert "UNRELATED_TELEGRAM_PROXY_PRESERVED" in text


def test_cleanup_does_not_restart_or_deploy_application() -> None:
    text = CLEANUP.read_text(encoding="utf-8")

    assert "docker restart" not in text
    assert "docker compose up" not in text
    assert "clientplatform_production_deploy" not in text
    assert "update-production.sh" not in text
    assert "git reset" not in text
    assert "CLIENTPLATFORM_FAILED_TELEGRAM_RELAYS_CLEANED" in text


def test_compose_has_no_host_relay_escape_hatch() -> None:
    text = COMPOSE.read_text(encoding="utf-8")

    assert "host.docker.internal:host-gateway" not in text
    assert "network_mode: host" not in text
    assert "privileged: true" not in text


def test_compose_persists_reachable_telegram_route_only_for_app() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    app = text.split("\n  app:\n", 1)[1].split("\n  caddy:\n", 1)[0]
    postgres = text.split("\n  postgres:\n", 1)[1].split("\n  app:\n", 1)[0]
    caddy_and_operations = text.split("\n  caddy:\n", 1)[1]
    route = (
        '"api.telegram.org:'
        '${CLIENTPLATFORM_TELEGRAM_API_IPV4:-149.154.167.220}"'
    )

    assert "extra_hosts:" in app
    assert route in app
    assert route not in postgres
    assert route not in caddy_and_operations
    assert "149.154.166.110" not in text
