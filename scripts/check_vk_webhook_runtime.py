from __future__ import annotations

"""Server-side VK webhook runtime preflight.

Run in the production app runtime namespace, for example:

    docker exec clientplatform-production-app-1 python scripts/check_vk_webhook_runtime.py

The script does not print secrets. It checks the env/runtime contract that must
be true before VK Callback API can confirm the webhook URL.
"""

import os
import socket
import sys
from urllib.parse import urlparse


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _present(name: str) -> bool:
    return bool(_env(name))


def _truthy(name: str) -> bool:
    return _env(name).lower() in {"1", "true", "yes", "on", "webhook"}


def _print_presence(name: str, *, required: bool = True) -> bool:
    ok = _present(name)
    marker = "OK" if ok else ("FAIL" if required else "WARN")
    print(f"{marker}: {name}={'<set>' if ok else '<empty>'}")
    return ok or not required


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []

    print("VK webhook runtime preflight")
    print("=" * 32)

    if not _truthy("MESSENGER_WEBHOOK_ENABLED"):
        failures.append("MESSENGER_WEBHOOK_ENABLED must be 1")
        print("FAIL: MESSENGER_WEBHOOK_ENABLED is not enabled")
    else:
        print("OK: MESSENGER_WEBHOOK_ENABLED=1")

    for name in ["MESSENGER_PUBLIC_BASE_URL", "VK_GROUP_ID", "VK_GROUP_TOKEN", "VK_CONFIRMATION_TOKEN"]:
        if not _print_presence(name):
            failures.append(f"{name} is required")

    if not _print_presence("VK_SECRET", required=False):
        warnings.append("VK_SECRET is empty; secret verification cannot be enforced")

    public_base = _env("MESSENGER_PUBLIC_BASE_URL").rstrip("/")
    if public_base:
        parsed = urlparse(public_base)
        if parsed.scheme != "https":
            failures.append("MESSENGER_PUBLIC_BASE_URL must start with https:// for production VK Callback API")
            print(f"FAIL: MESSENGER_PUBLIC_BASE_URL scheme is {parsed.scheme!r}, expected 'https'")
        else:
            print("OK: MESSENGER_PUBLIC_BASE_URL uses https")
        print(f"INFO: expected VK callback URL: {public_base}/webhooks/vk")

    bind_host = _env("MESSENGER_WEBHOOK_HOST") or _env("WEBHOOK_HOST") or "127.0.0.1"
    raw_port = _env("MESSENGER_WEBHOOK_PORT") or _env("WEBHOOK_PORT") or "8081"
    try:
        port = int(raw_port)
    except ValueError:
        failures.append(f"MESSENGER_WEBHOOK_PORT/WEBHOOK_PORT must be integer, got {raw_port!r}")
        port = 8081
    probe_host = "127.0.0.1" if bind_host == "0.0.0.0" else ("::1" if bind_host == "::" else bind_host)
    if _port_open(probe_host, port):
        print(f"OK: local webhook listener is accepting TCP on {probe_host}:{port} (bind={bind_host})")
    else:
        failures.append(f"no local webhook listener on {probe_host}:{port} (bind={bind_host})")
        print(f"FAIL: no local webhook listener on {probe_host}:{port} (bind={bind_host})")

    print("\nReverse proxy must route /webhooks/* to the app listener on this port:")
    print(f"  app ingress port: {port}")
    print("\nVK dashboard callback URL must be:")
    print(f"  {public_base}/webhooks/vk" if public_base else "  <MESSENGER_PUBLIC_BASE_URL>/webhooks/vk")

    for warning in warnings:
        print(f"WARN: {warning}")

    if failures:
        print("\nVK WEBHOOK PREFLIGHT: FAILED")
        for item in failures:
            print(f"ERROR: {item}")
        return 2

    print("\nVK WEBHOOK PREFLIGHT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
