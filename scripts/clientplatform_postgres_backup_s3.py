from __future__ import annotations

"""Stream verified ClientPlatform PostgreSQL backup bundles to versioned S3.

The dump is hashed before upload and then sent as bounded chunks with an explicit
Content-Length and SigV4 payload hash. The complete dump is never loaded into
memory. The JSON metadata file is uploaded last and acts as the bundle commit
marker. Destination HEAD metadata is verified after every upload.
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlunsplit
from urllib.request import Request, urlopen

from scripts.clientplatform_s3_replication import (
    ReplicationConfig,
    ReplicationError,
    S3Client,
    _canonical_query,
    _normalize_header_value,
    _signing_key,
    _utc_now,
    config_from_env,
)

_BACKUP_NAME_RE = re.compile(r"^clientplatform-(\d{8})T(\d{6})Z\.dump$")
_METADATA_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_DEFAULT_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class UploadedFile:
    key: str
    size: int
    sha256: str


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


class FileChunkBody:
    """Re-openable bounded iterator accepted by urllib/http.client as request data."""

    def __init__(self, path: Path, *, chunk_bytes: int = _DEFAULT_CHUNK_BYTES) -> None:
        self.path = path
        self.chunk_bytes = int(chunk_bytes)
        if self.chunk_bytes < 64 * 1024 or self.chunk_bytes > 16 * 1024 * 1024:
            raise ValueError("chunk_bytes must be between 64 KiB and 16 MiB")

    def __iter__(self) -> Iterable[bytes]:
        with self.path.open("rb") as source:
            while True:
                chunk = source.read(self.chunk_bytes)
                if not chunk:
                    return
                yield chunk


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(_DEFAULT_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _headers_lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in headers.items()}


def _metadata_headers(metadata: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in metadata.items():
        normalized_name = str(name or "").strip().lower()
        normalized_value = _normalize_header_value(str(value or ""))
        if not _METADATA_NAME_RE.fullmatch(normalized_name):
            raise ValueError("invalid S3 metadata name")
        if not normalized_value or len(normalized_value) > 1024:
            raise ValueError("invalid S3 metadata value")
        result[f"x-amz-meta-{normalized_name}"] = normalized_value
    return result


def _authorization_headers_for_hash(
    *,
    method: str,
    host: str,
    path: str,
    query: Mapping[str, str],
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str,
    payload_sha256: str,
    extra_headers: Mapping[str, str],
    now: datetime,
) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
        raise ValueError("invalid payload SHA-256")
    amz_date = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    headers = {
        "host": host,
        "x-amz-content-sha256": payload_sha256,
        "x-amz-date": amz_date,
    }
    if session_token:
        headers["x-amz-security-token"] = session_token
    for name, value in extra_headers.items():
        lowered = str(name).strip().lower()
        if lowered in {"authorization", "host", "x-amz-date", "x-amz-content-sha256"}:
            continue
        headers[lowered] = _normalize_header_value(value)

    signed_names = sorted(headers)
    canonical_headers = "".join(
        f"{name}:{_normalize_header_value(headers[name])}\n" for name in signed_names
    )
    signed_headers = ";".join(signed_names)
    canonical_request = "\n".join(
        (
            method.upper(),
            quote(path or "/", safe="/-_.~"),
            _canonical_query(query),
            canonical_headers,
            signed_headers,
            payload_sha256,
        )
    )
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        )
    )
    signature = hmac.new(
        _signing_key(secret_key, date_stamp, region),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    result = {name: value for name, value in headers.items() if name != "host"}
    result["Authorization"] = (
        "AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return result


class StreamingS3Uploader:
    def __init__(
        self,
        config: ReplicationConfig,
        *,
        opener: Callable[..., object] = urlopen,
        clock: Callable[[], datetime] = _utc_now,
        chunk_bytes: int = _DEFAULT_CHUNK_BYTES,
        control_client: S3Client | None = None,
    ) -> None:
        self.config = config
        self._opener = opener
        self._clock = clock
        self._chunk_bytes = int(chunk_bytes)
        self._control = control_client or S3Client(config, clock=clock)

    def bucket_versioning(self, bucket: str) -> str:
        return self._control.bucket_versioning(bucket)

    def put_file(
        self,
        bucket: str,
        key: str,
        path: Path,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> UploadedFile:
        selected = path.expanduser().resolve(strict=True)
        if not selected.is_file():
            raise ValueError("backup upload source must be a regular file")
        size = selected.stat().st_size
        if size <= 0:
            raise ValueError("backup upload source must not be empty")
        if size > self.config.max_copy_bytes:
            raise ValueError("backup upload exceeds the configured single-object limit")
        normalized_key = str(key or "").strip().lstrip("/")
        if not normalized_key or "\x00" in normalized_key:
            raise ValueError("invalid backup object key")

        sha256 = _sha256_file(selected)
        headers = {
            "content-length": str(size),
            "content-type": str(content_type or "application/octet-stream").strip(),
            **_metadata_headers(metadata),
            "x-amz-meta-clientplatform-sha256": sha256,
            "x-amz-meta-clientplatform-size": str(size),
        }
        suffix = f"/{bucket}/{normalized_key}"
        request_path = f"{self.config.endpoint_path}{suffix}" or "/"
        signed = _authorization_headers_for_hash(
            method="PUT",
            host=self.config.endpoint_host,
            path=request_path,
            query={},
            region=self.config.region,
            access_key=self.config.access_key,
            secret_key=self.config.secret_key,
            session_token=self.config.session_token,
            payload_sha256=sha256,
            extra_headers=headers,
            now=self._clock(),
        )
        signed.update(headers)
        url = urlunsplit(
            (
                "https",
                self.config.endpoint_host,
                quote(request_path, safe="/-_.~%"),
                "",
                "",
            )
        )
        request = Request(
            url,
            data=FileChunkBody(selected, chunk_bytes=self._chunk_bytes),
            headers=signed,
            method="PUT",
        )
        try:
            with self._opener(request, timeout=self.config.timeout_seconds) as response:
                status = int(getattr(response, "status", 0))
                if status != 200:
                    raise ReplicationError("backup_upload_unexpected_s3_status", status=status)
                response.read(65_536)
        except HTTPError as exc:
            exc.read(65_536)
            raise ReplicationError("backup_upload_s3_http_error", status=exc.code) from None
        except (URLError, TimeoutError, OSError):
            raise ReplicationError("backup_upload_s3_transport_failure") from None

        remote = self._control.head_object(bucket, normalized_key)
        selected_headers = _headers_lower(remote or {})
        if (
            selected_headers.get("content-length") != str(size)
            or selected_headers.get("x-amz-meta-clientplatform-sha256") != sha256
            or selected_headers.get("x-amz-meta-clientplatform-size") != str(size)
        ):
            raise ReplicationError("backup_upload_verification_failed")
        return UploadedFile(key=normalized_key, size=size, sha256=sha256)


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


def _validated_bundle(dump_path: Path) -> tuple[Path, Path, Path, str, str]:
    dump = dump_path.expanduser().resolve(strict=True)
    match = _BACKUP_NAME_RE.fullmatch(dump.name)
    if match is None or not dump.is_file():
        raise ValueError("invalid ClientPlatform PostgreSQL dump path")
    checksum_path = dump.with_suffix(dump.suffix + ".sha256")
    metadata_path = dump.with_suffix(dump.suffix + ".json")
    if not checksum_path.is_file() or not metadata_path.is_file():
        raise ValueError("PostgreSQL backup bundle is incomplete")

    expected_parts = checksum_path.read_text(encoding="utf-8").split()
    if len(expected_parts) < 2 or expected_parts[1] != dump.name:
        raise ValueError("PostgreSQL backup checksum manifest is invalid")
    expected_sha = expected_parts[0].lower()
    actual_sha = _sha256_file(dump)
    if expected_sha != actual_sha:
        raise ValueError("PostgreSQL backup checksum mismatch")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("PostgreSQL backup metadata must be an object")
    if metadata.get("dump_file") != dump.name or metadata.get("sha256") != actual_sha:
        raise ValueError("PostgreSQL backup metadata does not match the dump")
    source_database = str(metadata.get("source_database") or "")
    if not source_database.startswith("clientplatform"):
        raise ValueError("refusing to upload a non-ClientPlatform backup")
    date_path = f"{match.group(1)[:4]}/{match.group(1)[4:6]}/{match.group(1)[6:8]}"
    return dump, checksum_path, metadata_path, actual_sha, date_path


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
    dump_path: Path,
    prefix: str,
    evidence_dir: Path,
    now: datetime | None = None,
) -> Path:
    started = now or _utc_now()
    dump, checksum, metadata, bundle_sha, date_path = _validated_bundle(dump_path)
    if uploader.bucket_versioning(config.backup_bucket) != "Enabled":
        raise ReplicationError("backup_bucket_versioning_not_enabled")
    base_key = f"{prefix.strip('/')}/{date_path}"
    bundle_id = dump.stem
    uploaded: list[UploadedFile] = []
    for path, content_type in (
        (dump, "application/octet-stream"),
        (checksum, "text/plain; charset=utf-8"),
        (metadata, "application/json"),
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
                    "clientplatform-bundle-role": (
                        "dump" if path == dump else "checksum" if path == checksum else "metadata"
                    ),
                },
            )
        )

    completed = now or _utc_now()
    evidence = {
        "schema_version": 1,
        "ok": True,
        "operation": "postgres_backup_s3_upload",
        "backup_bucket": config.backup_bucket,
        "bundle": bundle_id,
        "bundle_sha256": bundle_sha,
        "started_at": started.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "completed_at": completed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "objects": [
            {"key": item.key, "size": item.size, "sha256": item.sha256}
            for item in uploaded
        ],
    }
    return _write_evidence(evidence_dir, evidence)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", type=Path)
    parser.add_argument("--prefix")
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()

    config = config_from_env()
    evidence = upload_backup_bundle(
        StreamingS3Uploader(config),
        config,
        dump_path=args.dump,
        prefix=args.prefix or _prefix(),
        evidence_dir=args.evidence_dir or _evidence_dir(),
    )
    print(f"CLIENTPLATFORM_POSTGRES_BACKUP_S3_OK:{evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
