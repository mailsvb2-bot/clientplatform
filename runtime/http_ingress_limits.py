from __future__ import annotations

import os


def ingress_body_limit() -> int:
    raw = (os.getenv("HTTP_INGRESS_MAX_BODY_BYTES") or str(1024 * 1024)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 1024 * 1024
    return min(max(value, 4096), 10 * 1024 * 1024)


__all__ = ["ingress_body_limit"]
