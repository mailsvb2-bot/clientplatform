from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path

# Ensure project root is on sys.path even when running:
#   python scripts/validate_project.py
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_RELEASE_MODE = os.getenv("VALIDATOR_RELEASE_MODE") == "1"

# During validation, never create/ship a real SQLite DB inside the repo.
# Use a temporary DB outside the project tree so release hygiene checks stay stable.
if _RELEASE_MODE:
    import tempfile

    _tmp_dir = Path(tempfile.mkdtemp(prefix="clientplatform_validator_"))
    if not os.getenv("CLIENTPLATFORM_DB_PATH"):
        os.environ["CLIENTPLATFORM_DB_PATH"] = str(_tmp_dir / "validator.db")
    os.environ.setdefault("LOG_PATH", str(_tmp_dir / "validator_app.log"))
    os.environ.setdefault("STORE_LOG_PATH", str(_tmp_dir / "validator_store.log"))

# In release validation mode, use a harmless identity value for import-time
# production fail-fast checks. Real deployments provide their own credentials.
if _RELEASE_MODE:
    os.environ.setdefault("BOT_TOKEN", "000000:VALIDATION")
    os.environ.setdefault("ADMIN_IDS", "1")

# A built immutable release intentionally contains deterministic project bytecode.
# Validation must neither delete those files nor create new bytecode beside them.
if _RELEASE_MODE:
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def _compiled_artifact_snapshot() -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for pattern in ("*.pyc", "*.pyo"):
        for path in ROOT.rglob(pattern):
            if path.is_file():
                relative = path.relative_to(ROOT).as_posix()
                snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


_RELEASE_BYTECODE_SNAPSHOT = _compiled_artifact_snapshot() if _RELEASE_MODE else {}

from core.logging import setup_logging

setup_logging()

from services.schema import init_db
from services.validator import ValidationError, validate_all

log = logging.getLogger(__name__)


def main() -> int:
    try:
        init_db()

        # Local dev UX: allow running validation even if a stateful DB exists in the repo.
        # Release hygiene should still be strict in CI / release mode.
        strict = os.getenv("VALIDATOR_STRICT", "0").strip().lower() in {"1", "true", "yes", "on"}
        if _RELEASE_MODE:
            strict = True

        validate_all(strict=strict)
        if _RELEASE_MODE:
            current_bytecode = _compiled_artifact_snapshot()
            if current_bytecode != _RELEASE_BYTECODE_SNAPSHOT:
                raise RuntimeError("release validation changed compiled bytecode artifacts")
        log.info("✅ Validation OK")
        return 0
    except ValidationError as e:
        log.error("❌ Validation failed: %s", e)
        return 2
    except (OSError, RuntimeError, ValueError, TypeError, ImportError):  # validator: allow-wide-except
        log.exception("❌ Unexpected error during validation")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
