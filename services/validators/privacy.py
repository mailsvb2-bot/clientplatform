from __future__ import annotations

import logging
from typing import Any

from clientplatform.privacy_manifest import validate_clientplatform_privacy_manifest
from services.db import get_connection
from services.privacy_manifest import validate_privacy_manifest
from services.validators.base import ValidationError

log = logging.getLogger(__name__)


def _clientplatform_schema_present(conn: Any) -> bool:
    try:
        return conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='businesses'"
        ).fetchone() is not None
    except Exception:  # validator: allow-wide-except
        return False


def validate_privacy_schema(strict: bool = True) -> None:
    """Validate global identity data and, when present, tenant-scoped data."""
    with get_connection() as conn:
        try:
            global_report = validate_privacy_manifest(conn, strict=True)
            tenant_report = (
                validate_clientplatform_privacy_manifest(
                    conn, strict=True, require_complete=True
                )
                if _clientplatform_schema_present(conn)
                else None
            )
        except RuntimeError as exc:
            if strict:
                raise ValidationError(str(exc)) from exc
            log.warning("Privacy manifest warning: %s", exc)
            return
    log.info(
        "Privacy manifests OK: global_user_tables=%s business_scoped_tables=%s",
        len(global_report.discovered_user_tables),
        len(tenant_report.discovered_business_tables) if tenant_report else 0,
    )
