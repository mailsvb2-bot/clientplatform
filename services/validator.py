from __future__ import annotations
import os
import logging
import sqlite3

log = logging.getLogger(__name__)

"""Project validators.

Split into smaller modules under services/validators to reduce regression risk.
Public API remains in this module for backward compatibility.
"""

from services.validators.base import ValidationError
from services.validators.db import (
    validate_no_real_db,
    validate_db_schema,
    validate_schema_decomposition,
)
from services.validators.runtime import (
    validate_background_tasks,
    validate_single_scheduler,
    validate_wide_except_policy,
)
from services.validators.release import (
    validate_release_hygiene,
    validate_compileall,
)
from services.validators.architecture import validate_architecture_contracts
from services.validators.prod import validate_prod_guardrails
from services.validators.privacy import validate_privacy_schema


def validate_all(strict: bool = True) -> None:
    """Run project validators."""
    release_mode = os.getenv("VALIDATOR_RELEASE_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}
    guardrails_strict = os.getenv("VALIDATOR_GUARDRAILS_STRICT", "0").strip().lower() in {"1", "true", "yes", "on"}

    validate_prod_guardrails(strict=True)
    validate_db_schema(strict=strict)
    validate_privacy_schema(strict=strict)
    validate_background_tasks(strict=strict)
    validate_single_scheduler(strict=guardrails_strict)
    validate_schema_decomposition(strict=guardrails_strict)

    if release_mode:
        validate_no_real_db(strict=True)
        validate_release_hygiene(strict=True)
        validate_compileall(strict=True)
        validate_wide_except_policy(strict=True)
        validate_architecture_contracts(strict=True)
    elif guardrails_strict:
        validate_architecture_contracts(strict=True)

    try:
        from pathlib import Path
        from services.db import DB_PATH, get_connection
        from services.db.runtime import CONFIG, redacted_db_target

        db_path = Path(DB_PATH).resolve()
        size = db_path.stat().st_size if CONFIG.uses_sqlite and db_path.exists() else 0
        businesses = -1
        customers = -1
        with get_connection() as conn:
            try:
                row = conn.execute("SELECT COUNT(*) FROM businesses").fetchone()
                businesses = int(row[0] if row is not None else -1)
            except Exception:  # validator: allow-wide-except
                businesses = -1
            try:
                row = conn.execute("SELECT COUNT(*) FROM customers").fetchone()
                customers = int(row[0] if row is not None else -1)
            except Exception:  # validator: allow-wide-except
                customers = -1

        log.info(
            "DB in use: %s (engine=%s, size=%s bytes), businesses=%s, customers=%s",
            redacted_db_target(), CONFIG.engine, size, businesses, customers,
        )
    except (OSError, sqlite3.Error):
        log.exception("Failed to print DB diagnostics")
    except Exception:  # validator: allow-wide-except
        log.exception("Failed to print DB diagnostics")

    log.info("Startup validation OK")
