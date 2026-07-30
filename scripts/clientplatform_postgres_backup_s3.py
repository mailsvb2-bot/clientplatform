from __future__ import annotations

"""Upload verified age-encrypted ClientPlatform PostgreSQL bundles to S3.

Only ``.dump.age`` bundles are accepted. The encrypted payload, checksum, and
metadata are streamed to the dedicated versioned backup bucket. Metadata is
uploaded last and acts as the completed-bundle commit marker.
"""

import argparse
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Protocol

from scripts.clientplatform_postgres_backup_crypto import _encrypted_bundle
from scripts.clientplatform_s3_replication import (
    ReplicationConfig,
    ReplicationError,
    _utc_now,
    config_from_env,
)
from scripts.clientplatform_s3_stream_upload import (
    FileChunkBody,
    StreamingS3Uploader,
    UploadedFile,
)

_ENCRYPTED_NAME_RE = re.compile(r"^clientplatform-(\d{8})T(\d{6})Z\.dump\.age$")


class BackupObjectUploader(Protocol):
    def bucket_versioning(self, bucket: str) -> str: ...

    def put_file(
        self,
        bucket: str,
        key: str,
        path: Path,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> UploadedFile: ...


def _evidence_dir(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    raw = str(values.get("CLIENTPLATFORM_POSTGRES_BACKUP_S3_EVIDENCE_DIR") or "").strip()
    path = Path(raw or "/var/lib/clientplatform/postgres-backup-s3-evidence").expanduser()
    if not path.is_absolute():
        raise ValueError("PostgreSQL S3 evidence directory must be absolute")
    return path.resolve()


def _prefix(env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    raw = str(values.get("CLIENTPLATFORM_POSTGRES_BACKUP_S3_PREFIX") or "postgres").strip()
    normalized = raw.strip("/")
    if not normalized or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", normalized):
        raise ValueError("invalid PostgreSQL S3 backup prefix")
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ValueError("invalid PostgreSQL S3 backup prefix")
    return normalized


def _write_evidence(directory: Path, payload: Mapping[str, object]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    rendered = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=directory,
        prefix=".latest-",
        suffix=".json.tmp",
        delete=False,
    ) as temporary:
        temporary.write(rendered)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    target = directory / "latest.json"
    temporary_path.replace(target)
    target.chmod(0o600)
    return target


def upload_backup_bundle(
    uploader: BackupObjectUploader,
    config: ReplicationConfig,
    *,
    ciphertext_path: Path,
    prefix: str,
    evidence_dir: Path,
    now: datetime | None = None,
) -> Path:
    started = now or _utc_now()
    ciphertext, checksum, metadata, metadata_payload = _encrypted_bundle(ciphertext_path)
    match = _ENCRYPTED_NAME_RE.fullmatch(ciphertext.name)
    if match is None:
        raise ValueError("invalid encrypted ClientPlatform PostgreSQL backup name")
    if uploader.bucket_versioning(config.backup_bucket) != "Enabled":
        raise ReplicationError("backup_bucket_versioning_not_enabled")

    date = match.group(1)
    base_key = f"{prefix.strip('/')}/{date[:4]}/{date[4:6]}/{date[6:8]}"
    bundle_id = ciphertext.name.removesuffix(".dump.age")
    bundle_sha = str(metadata_payload["ciphertext_sha256"])
    uploaded: list[UploadedFile] = []
    for path, content_type, role in (
        (ciphertext, "application/octet-stream", "ciphertext"),
        (checksum, "text/plain; charset=utf-8", "checksum"),
        (metadata, "application/json", "metadata"),
    ):
        uploaded.append(
            uploader.put_file(
                config.backup_bucket,
                f"{base_key}/{path.name}",
                path,
                content_type=content_type,
                metadata={
                    "clientplatform-bundle": bundle_id,
                    "clientplatform-bundle-sha256": bundle_sha,
                    "clientplatform-bundle-role": role,
                    "clientplatform-encryption": "age-x25519",
                },
            )
        )

    completed = now or _utc_now()
    return _write_evidence(
        evidence_dir,
        {
            "schema_version": 1,
            "ok": True,
            "operation": "postgres_backup_s3_upload",
            "backup_bucket": config.backup_bucket,
            "bundle": bundle_id,
            "bundle_sha256": bundle_sha,
            "encryption": "age-x25519",
            "started_at": started.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "completed_at": completed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "objects": [
                {"key": item.key, "size": item.size, "sha256": item.sha256}
                for item in uploaded
            ],
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ciphertext", type=Path)
    parser.add_argument("--prefix")
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()

    config = config_from_env()
    evidence = upload_backup_bundle(
        StreamingS3Uploader(config),
        config,
        ciphertext_path=args.ciphertext,
        prefix=args.prefix or _prefix(),
        evidence_dir=args.evidence_dir or _evidence_dir(),
    )
    print(f"CLIENTPLATFORM_POSTGRES_BACKUP_S3_OK:{evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FileChunkBody",
    "StreamingS3Uploader",
    "UploadedFile",
    "upload_backup_bundle",
]
