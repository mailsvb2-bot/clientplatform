from __future__ import annotations

import os
from typing import Any

from services.db import get_db_ro


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class NativeRuntimePolicyError(RuntimeError):
    """Native-only process mode would violate the canonical runtime contract."""


def _canonical_omnichannel_enabled() -> bool:
    return str(
        os.getenv("CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED") or ""
    ).strip().lower() in _TRUE_VALUES


def _row_count(row: Any) -> int:
    if row is None:
        return 0
    return int(row["c"] if hasattr(row, "keys") else row[0])


def assert_native_only_runtime_policy() -> None:
    """Fail closed before native-only workers start.

    Native-only mode is valid only with canonical tenant-scoped VK/MAX ingress.
    Telegram work must not be silently claimed while Telegram network I/O is
    intentionally disabled, so all active Telegram connections have to be
    disabled first. Queued work remains durable and can resume after Telegram is
    re-enabled.
    """

    if not _canonical_omnichannel_enabled():
        raise NativeRuntimePolicyError(
            "native-only runtime requires CLIENTPLATFORM_OMNICHANNEL_INGRESS_ENABLED=1"
        )

    with get_db_ro() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM connections
            WHERE platform='telegram' AND status='active'
            """
        ).fetchone()
    if _row_count(row):
        raise NativeRuntimePolicyError(
            "native-only runtime requires active Telegram connections to be disabled "
            "before Telegram network I/O is turned off"
        )


__all__ = ["NativeRuntimePolicyError", "assert_native_only_runtime_policy"]
