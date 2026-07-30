from __future__ import annotations

"""Create, encrypt, optionally upload, and retain ClientPlatform PostgreSQL backups."""

import argparse
import os
import time
from pathlib import Path
from typing import Mapping

from scripts.clientplatform_postgres_backup import create_backup
from scripts.clientplatform_postgres_backup_crypto import (
    EncryptedBackupBundle,
    encrypt_backup_bundle,
    recipient_from_env,
)
from scripts.clientplatform_postgres_backup_s3 import (
    StreamingS3Uploader,
    _evidence_dir,
    _prefix,
    upload_backup_bundle,
)
from scripts.clientplatform_s3_replication import config_from_env

_TRUE = frozenset({"1", "true", "yes", "on"})


def _truthy(env: Mapping[str, str], name: str) -> bool:
    return str(env.get(name, "") or "").strip().lower() in _TRUE


def _clean_plaintext_bundle(dump: Path) -> None:
    dump.unlink(missing_ok=True)
    dump.with_suffix(dump.suffix + ".sha256").unlink(missing_ok=True)
    dump.with_suffix(dump.suffix + ".json").unlink(missing_ok=True)
    dump.with_suffix(dump.suffix + ".partial").unlink(missing_ok=True)


def _prune_encrypted(directory: Path, *, retention_days: int, now: float) -> None:
    cutoff = now - retention_days * 86_400
    for ciphertext in directory.glob("clientplatform-*.dump.age"):
        if ciphertext.stat().st_mtime >= cutoff:
            continue
        ciphertext.unlink(missing_ok=True)
        ciphertext.with_suffix(ciphertext.suffix + ".sha256").unlink(missing_ok=True)
        ciphertext.with_suffix(ciphertext.suffix + ".json").unlink(missing_ok=True)


def run_backup_pipeline(env: Mapping[str, str] | None = None) -> tuple[EncryptedBackupBundle, Path | None]:
    values = os.environ if env is None else env
    if not _truthy(values, "CLIENTPLATFORM_BACKUP_ENCRYPTION_REQUIRED"):
        raise ValueError("CLIENTPLATFORM_BACKUP_ENCRYPTION_REQUIRED must be enabled")
    database_url = str(values.get("DATABASE_URL") or "").strip()
    backup_dir = Path(str(values.get("CLIENTPLATFORM_BACKUP_DIR") or "")).expanduser()
    if not backup_dir.is_absolute():
        raise ValueError("CLIENTPLATFORM_BACKUP_DIR must be absolute")
    retention_days = int(str(values.get("CLIENTPLATFORM_BACKUP_RETENTION_DAYS") or "30"))
    now = time.time()
    dump = create_backup(
        database_url=database_url,
        backup_dir=backup_dir,
        retention_days=retention_days,
        now=now,
    )
    try:
        encrypted = encrypt_backup_bundle(
            dump,
            recipient=recipient_from_env(values),
            remove_plaintext=True,
            now=now,
        )
    except (OSError, RuntimeError, ValueError, TypeError):
        _clean_plaintext_bundle(dump)
        raise

    _prune_encrypted(backup_dir, retention_days=retention_days, now=now)
    upload_evidence: Path | None = None
    if _truthy(values, "CLIENTPLATFORM_POSTGRES_BACKUP_S3_ENABLED"):
        config = config_from_env(values)
        upload_evidence = upload_backup_bundle(
            StreamingS3Uploader(config),
            config,
            ciphertext_path=encrypted.ciphertext,
            prefix=_prefix(values),
            evidence_dir=_evidence_dir(values),
        )
    return encrypted, upload_evidence


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    encrypted, evidence = run_backup_pipeline()
    print(f"CLIENTPLATFORM_ENCRYPTED_BACKUP_OK:{encrypted.ciphertext}")
    if evidence is not None:
        print(f"CLIENTPLATFORM_POSTGRES_BACKUP_S3_OK:{evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
