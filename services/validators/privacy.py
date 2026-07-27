from __future__ import annotations

import logging
from typing import Any

from a1.privacy_manifest import validate_a1_privacy_manifest
from services.db import get_connection
from services.privacy_manifest import validate_privacy_manifest
from services.validators.base import ValidationError

log = logging.getLogger(__name__)


def _a1_schema_present(conn: Any) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='businesses'"
        ).fetchone()
    except Exception:  # validator: allow-wide-except
        return False
    return row is not None


def validate_privacy_schema(strict: bool = True) -> None:
    with get_connection() as conn:
        try:
            legacy_report = validate_privacy_manifest(conn, strict=True)
            a1_report = (
                validate_a1_privacy_manifest(conn, strict=True)
                if _a1_schema_present(conn)
                else None
            )
        except RuntimeError as exc:
            if strict:
                raise ValidationError(str(exc)) from exc
            log.warning("Privacy manifest warning: %s", exc)
            return
    log.info(
        "Privacy manifests OK: user_owned_tables=%s business_scoped_tables=%s",
        len(legacy_report.discovered_user_tables),
        len(a1_report.discovered_business_tables) if a1_report is not None else 0,
    )
