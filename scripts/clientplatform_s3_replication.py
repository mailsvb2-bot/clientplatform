from __future__ import annotations

"""Fail-closed S3-to-S3 backup replication for ClientPlatform.

The script uses the existing production S3 credentials and a path-style,
S3-compatible HTTPS endpoint. It never deletes destination objects during a
normal sync. The one-time ``prove`` command writes and verifies a reserved
probe object, then deletes the current probe objects from both buckets.
"""

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_PROBE_PREFIX = ".clientplatform-replication-probe/"
_COPY_LIMIT_BYTES = 5_000_000_000


class ReplicationError(RuntimeError):
    """Sanitized operational error suitable for logs and evidence."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class ReplicationConfig:
    endpoint: str
    endpoint_host: str
    endpoint_path: str
    region: str
    access_key: str
    secret_key: str
    session_token: str
    source_bucket: str
    backup_bucket: str
    evidence_dir: Path
    timeout_seconds: float
    max_copy_bytes: int


@dataclass(frozen=True, slots=True)
class ObjectEntry:
    key: str
    etag: str
    size: int
    last_modified: str


@dataclass(frozen=True, slots=True)
class S3Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


class S3Operations(Protocol):
    def bucket_versioning(self, bucket: str) -> str: ...

    def list_objects(self, bucket: str, *, prefix: str = "") -> list[ObjectEntry]: ...

    def head_object(self, bucket: str, key: str) -> Mapping[str, str] | None: ...

    def copy_object(
        self,
        *,
        source_bucket: str,
        backup_bucket: str,
        entry: ObjectEntry,
        source_headers: Mapping[str, str],
    ) -> None: ...

    def put_object(
        self,
        bucket: str,
        key: str,
        payload: bytes,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> None: ...

    def get_object(self, bucket: str, key: str) -> bytes: ...

    def delete_object(self, bucket: str, key: str) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _env_value(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "") or "").strip()


def _required(env: Mapping[str, str], name: str) -> str:
    value = _env_value(env, name)
    if not value or value.lower() in {"changeme", "change-me", "secret", "password"}:
        raise ReplicationError(f"missing_{name.lower()}")
    return value


def _bucket(value: str, *, name: str) -> str:
    normalized = value.strip().lower()
    if not _BUCKET_RE.fullmatch(normalized):
        raise ReplicationError(f"invalid_{name.lower()}")
    if not normalized.startswith("clientplatform-"):
        raise ReplicationError(f"non_dedicated_{name.lower()}")
    return normalized


def _evidence_dir(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    evidence_raw = _env_value(
        values, "CLIENTPLATFORM_S3_REPLICATION_EVIDENCE_DIR"
    )
    evidence_dir = Path(
        evidence_raw or "/var/lib/clientplatform/s3-replication-evidence"
    ).expanduser()
    if not evidence_dir.is_absolute():
        raise ReplicationError("replication_evidence_dir_must_be_absolute")
    return evidence_dir.resolve()


def config_from_env(env: Mapping[str, str] | None = None) -> ReplicationConfig:
    values = os.environ if env is None else env
    endpoint_raw = _required(values, "CLIENTPLATFORM_MEDIA_GATEWAY_S3_ENDPOINT").rstrip("/")
    parsed = urlsplit(endpoint_raw)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ReplicationError("s3_endpoint_must_use_https")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ReplicationError("invalid_s3_endpoint")
    endpoint = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    region = _required(values, "CLIENTPLATFORM_MEDIA_GATEWAY_S3_REGION")
    if len(region) > 64:
        raise ReplicationError("invalid_s3_region")
    source_bucket = _bucket(
        _required(values, "CLIENTPLATFORM_STORAGE_BUCKET"),
        name="source_bucket",
    )
    backup_bucket = _bucket(
        _required(values, "CLIENTPLATFORM_S3_BACKUP_BUCKET"),
        name="backup_bucket",
    )
    if source_bucket == backup_bucket:
        raise ReplicationError("source_and_backup_bucket_must_differ")
    if "backup" not in backup_bucket:
        raise ReplicationError("backup_bucket_name_must_contain_backup")
    evidence_dir = _evidence_dir(values)
    timeout_raw = _env_value(values, "CLIENTPLATFORM_S3_REPLICATION_TIMEOUT_SEC") or "30"
    max_copy_raw = (
        _env_value(values, "CLIENTPLATFORM_S3_REPLICATION_MAX_COPY_BYTES")
        or str(_COPY_LIMIT_BYTES)
    )
    try:
        timeout_seconds = float(timeout_raw)
        max_copy_bytes = int(max_copy_raw)
    except ValueError:
        raise ReplicationError("invalid_replication_numeric_configuration") from None
    if timeout_seconds <= 0 or timeout_seconds > 120:
        raise ReplicationError("invalid_replication_timeout")
    if max_copy_bytes <= 0 or max_copy_bytes > _COPY_LIMIT_BYTES:
        raise ReplicationError("invalid_replication_copy_limit")
    return ReplicationConfig(
        endpoint=endpoint,
        endpoint_host=parsed.netloc,
        endpoint_path=parsed.path.rstrip("/"),
        region=region,
        access_key=_required(values, "CLIENTPLATFORM_SECRET_S3_ACCESS_KEY"),
        secret_key=_required(values, "CLIENTPLATFORM_SECRET_S3_SECRET_KEY"),
        session_token=_env_value(values, "CLIENTPLATFORM_SECRET_S3_SESSION_TOKEN"),
        source_bucket=source_bucket,
        backup_bucket=backup_bucket,
        evidence_dir=evidence_dir.resolve(),
        timeout_seconds=timeout_seconds,
        max_copy_bytes=max_copy_bytes,
    )


def _normalize_header_value(value: str) -> str:
    return " ".join(str(value).strip().split())


def _canonical_query(query: Mapping[str, str]) -> str:
    pairs = []
    for key, value in query.items():
        pairs.append(
            (
                quote(str(key), safe="-_.~"),
                quote(str(value), safe="-_.~"),
            )
        )
    pairs.sort()
    return "&".join(f"{key}={value}" for key, value in pairs)


def _signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    date_key = hmac.new(
        f"AWS4{secret_key}".encode("utf-8"),
        date_stamp.encode("ascii"),
        hashlib.sha256,
    ).digest()
    region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
    service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


def _authorization_headers(
    *,
    method: str,
    host: str,
    path: str,
    query: Mapping[str, str],
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str,
    payload: bytes,
    extra_headers: Mapping[str, str],
    now: datetime,
) -> dict[str, str]:
    payload_hash = hashlib.sha256(payload).hexdigest()
    amz_date = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
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
            payload_hash,
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


def _xml_local(element: ET.Element, name: str) -> ET.Element | None:
    for item in element.iter():
        if item.tag.rsplit("}", 1)[-1] == name:
            return item
    return None


def _xml_text(element: ET.Element, name: str, default: str = "") -> str:
    selected = _xml_local(element, name)
    return str(selected.text or default).strip() if selected is not None else default


def _normalize_etag(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:].strip()
    return normalized.strip('"').lower()


def _headers_lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in headers.items()}


class S3Client:
    def __init__(
        self,
        config: ReplicationConfig,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._config = config
        self._clock = clock

    def _path(self, bucket: str, key: str = "") -> str:
        suffix = f"/{bucket}"
        if key:
            suffix += f"/{key}"
        return f"{self._config.endpoint_path}{suffix}" or "/"

    def _request(
        self,
        method: str,
        *,
        bucket: str,
        key: str = "",
        query: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        payload: bytes = b"",
        expected: Iterable[int] = (200,),
        allow_not_found: bool = False,
    ) -> S3Response:
        selected_query = dict(query or {})
        selected_headers = dict(headers or {})
        path = self._path(bucket, key)
        signed = _authorization_headers(
            method=method,
            host=self._config.endpoint_host,
            path=path,
            query=selected_query,
            region=self._config.region,
            access_key=self._config.access_key,
            secret_key=self._config.secret_key,
            session_token=self._config.session_token,
            payload=payload,
            extra_headers=selected_headers,
            now=self._clock(),
        )
        signed.update(selected_headers)
        url = f"{self._config.endpoint}{path[len(self._config.endpoint_path):]}"
        canonical_query = _canonical_query(selected_query)
        if canonical_query:
            url = f"{url}?{canonical_query}"
        request = Request(
            url,
            data=payload if method.upper() in {"PUT", "POST"} else None,
            headers=signed,
            method=method.upper(),
        )
        try:
            with urlopen(request, timeout=self._config.timeout_seconds) as response:
                body = b"" if method.upper() == "HEAD" else response.read()
                result = S3Response(
                    status=int(response.status),
                    headers=_headers_lower(dict(response.headers.items())),
                    body=body,
                )
        except HTTPError as exc:
            if allow_not_found and exc.code == 404:
                return S3Response(status=404, headers={}, body=b"")
            body = exc.read(65_536)
            code = "s3_http_error"
            try:
                root = ET.fromstring(body)
                code = _xml_text(root, "Code", code)
            except ET.ParseError:
                pass
            raise ReplicationError(code.lower(), status=exc.code) from None
        except (URLError, TimeoutError, OSError):
            raise ReplicationError("s3_transport_failure") from None
        if result.status not in set(expected):
            raise ReplicationError("unexpected_s3_status", status=result.status)
        return result

    def bucket_versioning(self, bucket: str) -> str:
        response = self._request("GET", bucket=bucket, query={"versioning": ""})
        try:
            root = ET.fromstring(response.body)
        except ET.ParseError:
            raise ReplicationError("invalid_bucket_versioning_response") from None
        return _xml_text(root, "Status", "Suspended") or "Suspended"

    def list_objects(self, bucket: str, *, prefix: str = "") -> list[ObjectEntry]:
        entries: list[ObjectEntry] = []
        token = ""
        while True:
            query = {"list-type": "2", "max-keys": "1000"}
            if prefix:
                query["prefix"] = prefix
            if token:
                query["continuation-token"] = token
            response = self._request("GET", bucket=bucket, query=query)
            try:
                root = ET.fromstring(response.body)
            except ET.ParseError:
                raise ReplicationError("invalid_list_objects_response") from None
            for content in root.iter():
                if content.tag.rsplit("}", 1)[-1] != "Contents":
                    continue
                key = _xml_text(content, "Key")
                if not key:
                    raise ReplicationError("list_objects_missing_key")
                try:
                    size = int(_xml_text(content, "Size", "-1"))
                except ValueError:
                    raise ReplicationError("list_objects_invalid_size") from None
                if size < 0:
                    raise ReplicationError("list_objects_invalid_size")
                entries.append(
                    ObjectEntry(
                        key=key,
                        etag=_normalize_etag(_xml_text(content, "ETag")),
                        size=size,
                        last_modified=_xml_text(content, "LastModified"),
                    )
                )
            truncated = _xml_text(root, "IsTruncated", "false").lower() == "true"
            if not truncated:
                break
            token = _xml_text(root, "NextContinuationToken")
            if not token:
                raise ReplicationError("list_objects_missing_continuation_token")
        return entries

    def head_object(self, bucket: str, key: str) -> Mapping[str, str] | None:
        response = self._request(
            "HEAD",
            bucket=bucket,
            key=key,
            allow_not_found=True,
        )
        return None if response.status == 404 else response.headers

    def put_object(
        self,
        bucket: str,
        key: str,
        payload: bytes,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> None:
        headers = {"content-type": content_type}
        for name, value in metadata.items():
            headers[f"x-amz-meta-{name.lower()}"] = value
        self._request("PUT", bucket=bucket, key=key, headers=headers, payload=payload)

    def get_object(self, bucket: str, key: str) -> bytes:
        return self._request("GET", bucket=bucket, key=key).body

    def delete_object(self, bucket: str, key: str) -> None:
        self._request("DELETE", bucket=bucket, key=key, expected=(204,))

    def copy_object(
        self,
        *,
        source_bucket: str,
        backup_bucket: str,
        entry: ObjectEntry,
        source_headers: Mapping[str, str],
    ) -> None:
        source = _headers_lower(source_headers)
        headers: dict[str, str] = {
            "x-amz-copy-source": quote(
                f"/{source_bucket}/{entry.key}", safe="/-_.~"
            ),
            "x-amz-metadata-directive": "REPLACE",
            "x-amz-meta-clientplatform-source-etag": entry.etag,
            "x-amz-meta-clientplatform-source-size": str(entry.size),
        }
        for name in (
            "cache-control",
            "content-disposition",
            "content-encoding",
            "content-language",
            "content-type",
            "expires",
        ):
            if source.get(name):
                headers[name] = source[name]
        for name, value in source.items():
            if not name.startswith("x-amz-meta-"):
                continue
            if name in {
                "x-amz-meta-clientplatform-source-etag",
                "x-amz-meta-clientplatform-source-size",
            }:
                continue
            headers[name] = value
        response = self._request(
            "PUT",
            bucket=backup_bucket,
            key=entry.key,
            headers=headers,
        )
        try:
            root = ET.fromstring(response.body)
        except ET.ParseError:
            raise ReplicationError("invalid_copy_object_response") from None
        if _xml_local(root, "Error") is not None or not _xml_text(root, "ETag"):
            raise ReplicationError("copy_object_failed")


def _destination_matches(headers: Mapping[str, str] | None, entry: ObjectEntry) -> bool:
    if headers is None:
        return False
    selected = _headers_lower(headers)
    try:
        content_length = int(selected.get("content-length", "-1"))
    except ValueError:
        return False
    return (
        content_length == entry.size
        and _normalize_etag(selected.get("x-amz-meta-clientplatform-source-etag", ""))
        == entry.etag
        and selected.get("x-amz-meta-clientplatform-source-size", "")
        == str(entry.size)
    )


def _base_evidence(
    config: ReplicationConfig,
    operation: str,
    started: datetime,
) -> dict[str, object]:
    return {
        "version": 1,
        "ok": False,
        "operation": operation,
        "endpoint_host": config.endpoint_host,
        "region": config.region,
        "source_bucket": config.source_bucket,
        "backup_bucket": config.backup_bucket,
        "started_at": _iso(started),
        "completed_at": "",
        "source_versioning": "unknown",
        "backup_versioning": "unknown",
        "prefix": "",
        "scanned": 0,
        "copied": 0,
        "skipped": 0,
        "verified": 0,
        "failed": 0,
        "probe_verified": False,
        "errors": [],
    }


def _require_versioning(client: S3Operations, config: ReplicationConfig) -> tuple[str, str]:
    source = client.bucket_versioning(config.source_bucket)
    backup = client.bucket_versioning(config.backup_bucket)
    if source != "Enabled":
        raise ReplicationError("source_bucket_versioning_not_enabled")
    if backup != "Enabled":
        raise ReplicationError("backup_bucket_versioning_not_enabled")
    return source, backup


def sync_objects(
    client: S3Operations,
    config: ReplicationConfig,
    *,
    prefix: str = "",
    max_objects: int = 0,
    started: datetime | None = None,
) -> dict[str, object]:
    started_at = started or _utc_now()
    evidence = _base_evidence(config, "sync", started_at)
    evidence["prefix"] = prefix
    source_versioning, backup_versioning = _require_versioning(client, config)
    evidence["source_versioning"] = source_versioning
    evidence["backup_versioning"] = backup_versioning
    entries = client.list_objects(config.source_bucket, prefix=prefix)
    if max_objects > 0:
        entries = entries[:max_objects]
    evidence["scanned"] = len(entries)
    errors: list[str] = []
    for entry in entries:
        try:
            if entry.size > config.max_copy_bytes:
                raise ReplicationError("object_exceeds_single_copy_limit")
            destination = client.head_object(config.backup_bucket, entry.key)
            if _destination_matches(destination, entry):
                evidence["skipped"] = int(evidence["skipped"]) + 1
                evidence["verified"] = int(evidence["verified"]) + 1
                continue
            source_headers = client.head_object(config.source_bucket, entry.key)
            if source_headers is None:
                raise ReplicationError("source_object_disappeared_during_sync")
            client.copy_object(
                source_bucket=config.source_bucket,
                backup_bucket=config.backup_bucket,
                entry=entry,
                source_headers=source_headers,
            )
            copied = client.head_object(config.backup_bucket, entry.key)
            if not _destination_matches(copied, entry):
                raise ReplicationError("copied_object_verification_failed")
            evidence["copied"] = int(evidence["copied"]) + 1
            evidence["verified"] = int(evidence["verified"]) + 1
        except ReplicationError as exc:
            evidence["failed"] = int(evidence["failed"]) + 1
            if len(errors) < 20:
                errors.append(exc.code)
    evidence["errors"] = errors
    evidence["ok"] = int(evidence["failed"]) == 0
    evidence["completed_at"] = _iso(_utc_now())
    return evidence


def prove_replication(
    client: S3Operations,
    config: ReplicationConfig,
    *,
    started: datetime | None = None,
) -> dict[str, object]:
    started_at = started or _utc_now()
    evidence = _base_evidence(config, "prove", started_at)
    source_versioning, backup_versioning = _require_versioning(client, config)
    evidence["source_versioning"] = source_versioning
    evidence["backup_versioning"] = backup_versioning
    probe_key = f"{_PROBE_PREFIX}{secrets.token_hex(16)}.txt"
    payload = (
        f"clientplatform-s3-replication-proof\n{_iso(started_at)}\n".encode("utf-8")
        + secrets.token_bytes(32)
    )
    digest = hashlib.sha256(payload).hexdigest()
    entry = ObjectEntry(
        key=probe_key,
        etag=hashlib.md5(payload, usedforsecurity=False).hexdigest(),
        size=len(payload),
        last_modified=_iso(started_at),
    )
    source_created = False
    backup_created = False
    try:
        client.put_object(
            config.source_bucket,
            probe_key,
            payload,
            content_type="text/plain; charset=utf-8",
            metadata={"clientplatform-probe-sha256": digest},
        )
        source_created = True
        source_headers = client.head_object(config.source_bucket, probe_key)
        if source_headers is None:
            raise ReplicationError("probe_source_head_failed")
        source_etag = _normalize_etag(_headers_lower(source_headers).get("etag", ""))
        if source_etag:
            entry = ObjectEntry(
                key=entry.key,
                etag=source_etag,
                size=entry.size,
                last_modified=entry.last_modified,
            )
        client.copy_object(
            source_bucket=config.source_bucket,
            backup_bucket=config.backup_bucket,
            entry=entry,
            source_headers=source_headers,
        )
        backup_created = True
        copied = client.get_object(config.backup_bucket, probe_key)
        if not hmac.compare_digest(hashlib.sha256(copied).hexdigest(), digest):
            raise ReplicationError("probe_payload_mismatch")
        destination = client.head_object(config.backup_bucket, probe_key)
        if destination is None or not _destination_matches(destination, entry):
            raise ReplicationError("probe_destination_metadata_mismatch")
        evidence["scanned"] = 1
        evidence["copied"] = 1
        evidence["verified"] = 1
        evidence["probe_verified"] = True
        evidence["ok"] = True
    except ReplicationError as exc:
        evidence["failed"] = 1
        evidence["errors"] = [exc.code]
    finally:
        cleanup_errors: list[str] = []
        if source_created or backup_created:
            try:
                client.delete_object(config.backup_bucket, probe_key)
            except ReplicationError:
                cleanup_errors.append("backup_probe_cleanup_failed")
        if source_created:
            try:
                client.delete_object(config.source_bucket, probe_key)
            except ReplicationError:
                cleanup_errors.append("source_probe_cleanup_failed")
        if cleanup_errors:
            evidence["ok"] = False
            evidence["failed"] = int(evidence["failed"]) + len(cleanup_errors)
            evidence["errors"] = list(evidence["errors"]) + cleanup_errors
    evidence["completed_at"] = _iso(_utc_now())
    return evidence


def _write_evidence(config: ReplicationConfig, evidence: Mapping[str, object]) -> Path:
    config.evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    history = config.evidence_dir / f"s3-replication-{timestamp}.json"
    latest = config.evidence_dir / "latest.json"
    payload = json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    for target in (history, latest):
        fd, raw_path = tempfile.mkstemp(prefix=f".{target.name}.", dir=config.evidence_dir)
        temp_path = Path(raw_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, target)
            os.chmod(target, 0o600)
        finally:
            temp_path.unlink(missing_ok=True)
    return latest


@contextlib.contextmanager
def _single_run_lock(evidence_dir: Path):
    evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = evidence_dir / ".replication.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            import fcntl
        except ImportError:
            yield
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReplicationError("replication_already_running") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_latest(evidence_dir: Path) -> dict[str, object]:
    path = evidence_dir / "latest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReplicationError("replication_evidence_missing") from None
    except (OSError, json.JSONDecodeError):
        raise ReplicationError("replication_evidence_invalid") from None
    if not isinstance(payload, dict):
        raise ReplicationError("replication_evidence_invalid")
    return payload


def _emit_error(exc: ReplicationError, *, json_output: bool) -> int:
    payload = {"ok": False, "error": exc.code}
    if exc.status is not None:
        payload["status"] = exc.status
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"CLIENTPLATFORM_S3_REPLICATION_ERROR:{exc.code}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("sync", "prove", "status"))
    parser.add_argument("--prefix", default="")
    parser.add_argument("--max-objects", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        if args.operation == "status":
            payload = _read_latest(_evidence_dir())
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0 if bool(payload.get("ok")) else 1
        config = config_from_env()
        if args.max_objects < 0:
            raise ReplicationError("max_objects_must_be_non_negative")
        client = S3Client(config)
        with _single_run_lock(config.evidence_dir):
            if args.operation == "prove":
                evidence = prove_replication(client, config)
                marker = "CLIENTPLATFORM_S3_REPLICATION_PROOF_OK"
            else:
                evidence = sync_objects(
                    client,
                    config,
                    prefix=args.prefix,
                    max_objects=args.max_objects,
                )
                marker = "CLIENTPLATFORM_S3_REPLICATION_OK"
            path = _write_evidence(config, evidence)
        if args.json:
            print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
        elif bool(evidence.get("ok")):
            print(f"{marker}:{path}")
        else:
            errors = ",".join(str(item) for item in evidence.get("errors", []))
            print(f"CLIENTPLATFORM_S3_REPLICATION_ERROR:{errors}", file=sys.stderr)
        return 0 if bool(evidence.get("ok")) else 1
    except ReplicationError as exc:
        return _emit_error(exc, json_output=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
