from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "clientplatform"
CLEANUP = DEPLOY / "cleanup-failed-telegram-relays.sh"
COMPOSE = DEPLOY / "compose.production.yml"


def _env_cleanup_program() -> str:
    text = CLEANUP.read_text(encoding="utf-8")
    marker = 'python3 - "$ENV_FILE" <<\'PY\'\n'
    return text.split(marker, 1)[1].split("\nPY\n", 1)[0]


def _run_env_cleanup(tmp_path: Path, proxy_url: str) -> tuple[str, str]:
    env_file = tmp_path / "clientplatform.env"
    env_file.write_text(
        "BOT_TOKEN=synthetic\n"
        f"TELEGRAM_PROXY_URL={proxy_url}\n"
        "CLIENTPLATFORM_CONTROL_ENABLED=1\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-c", _env_cleanup_program(), str(env_file)],
        check=True,
        capture_output=True,
        text=True,
    )
    return env_file.read_text(encoding="utf-8"), result.stdout


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
    assert 'squid.conf.before-clientplatform-*' in text
    assert "PRE_CLIENTPLATFORM_SQUID_CONFIG_RESTORED" in text
    assert 'cp --preserve=mode,ownership,timestamps "$SQUID_BACKUP" "$SQUID_CONFIG"' in text
    assert "restored_squid_config_invalid" in text
    assert "restored_squid_start_failed" in text
    assert "known_failed_endpoints" in text
    assert '("http", "host.docker.internal", 3128)' in text
    assert '("http", "147.45.146.112", 3128)' in text
    assert "UNRELATED_TELEGRAM_PROXY_PRESERVED" in text
    assert "UNRELATED_OR_RESTORED_PORT_3128_LISTENER_PRESERVED" in text
    assert 'fail "port_3128_still_listening"' not in text


def test_cleanup_removes_only_exact_failed_proxy_endpoint(tmp_path: Path) -> None:
    cleaned, output = _run_env_cleanup(tmp_path, "http://147.45.146.112:3128")

    assert "TELEGRAM_PROXY_URL=" not in cleaned
    assert "ACCIDENTAL_TELEGRAM_PROXY_ENV_REMOVED" in output


def test_cleanup_preserves_unrelated_proxy_on_same_host(tmp_path: Path) -> None:
    preserved, output = _run_env_cleanup(tmp_path, "http://147.45.146.112:8080")

    assert "TELEGRAM_PROXY_URL=http://147.45.146.112:8080" in preserved
    assert "UNRELATED_TELEGRAM_PROXY_PRESERVED" in output
    assert "ACCIDENTAL_TELEGRAM_PROXY_ENV_REMOVED" not in output


def test_cleanup_preserves_unrelated_proxy_scheme_on_same_endpoint(tmp_path: Path) -> None:
    preserved, output = _run_env_cleanup(tmp_path, "https://147.45.146.112:3128")

    assert "TELEGRAM_PROXY_URL=https://147.45.146.112:3128" in preserved
    assert "UNRELATED_TELEGRAM_PROXY_PRESERVED" in output
    assert "ACCIDENTAL_TELEGRAM_PROXY_ENV_REMOVED" not in output


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
