from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .schema_migrations import ensure_schema

_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CLIENT_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_SCOPE_RE = re.compile(r"[A-Za-z0-9_.:@/-]{1,160}")
_IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9_.:@/-]{8,200}")


@dataclass(frozen=True, slots=True)
class StoredJob:
    id: str
    client_id: str
    scope_id: str
    idempotency_key: str
    request_fingerprint: str
    provider: str
    kind: str
    status: str
    provider_job_id: str
    model: str
    mime_type: str
    asset_path: str
    error_code: str
    created_at: int
    updated_at: int


def _client(value: object) -> str:
    token = str(value or "").strip()
    if not _CLIENT_RE.fullmatch(token):
        raise ValueError("invalid_visual_client_id")
    return token


def _scope(value: object) -> str:
    token = str(value or "global").strip() or "global"
    if not _SCOPE_RE.fullmatch(token):
        raise ValueError("invalid_visual_scope_id")
    return token


def _idempotency(value: object) -> str:
    token = str(value or "").strip()
    if not _IDEMPOTENCY_RE.fullmatch(token):
        raise ValueError("invalid_visual_idempotency_key")
    return token


def _job_id(value: object) -> str:
    token = str(value or "").strip()
    if not _ID_RE.fullmatch(token):
        raise KeyError(token)
    return token


class JobStore:
    """Durable app-scoped visual job registry.

    The store deliberately owns idempotency *before* provider I/O. If a caller
    retries the same request after an ambiguous network timeout, the retry sees
    the existing reservation instead of spending on a second provider job.
    Schema evolution is isolated in ``schema_migrations.py`` + SQL migration
    assets so this runtime module remains CRUD-only.
    """

    def __init__(self, path: str | None = None) -> None:
        configured = str(path or os.getenv("VISUAL_GATEWAY_DB", "data/visual_gateway.sqlite3")).strip()
        self.path = Path(configured).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        ensure_schema(self.path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def reserve(
        self,
        *,
        client_id: str,
        scope_id: str,
        idempotency_key: str,
        request_fingerprint: str,
        kind: str,
    ) -> tuple[StoredJob, bool]:
        client = _client(client_id)
        scope = _scope(scope_id)
        idem = _idempotency(idempotency_key)
        fingerprint = str(request_fingerprint or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("invalid_visual_request_fingerprint")
        visual_kind = str(kind or "").strip().lower()
        if visual_kind not in {"image", "video"}:
            raise ValueError("invalid_visual_kind")
        now = int(time.time())
        gateway_id = uuid.uuid4().hex
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM visual_jobs WHERE client_id=? AND scope_id=? AND idempotency_key=? LIMIT 1",
                (client, scope, idem),
            ).fetchone()
            if existing is not None:
                if str(existing["request_fingerprint"] or "") != fingerprint:
                    conn.rollback()
                    raise ValueError("visual_idempotency_payload_conflict")
                conn.commit()
                return StoredJob(**dict(existing)), False
            conn.execute(
                """
                INSERT INTO visual_jobs(
                    id, client_id, scope_id, idempotency_key, request_fingerprint, provider, kind, status,
                    provider_job_id, model, mime_type, asset_path, error_code,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, '', ?, 'running', '', '', '', '', '', ?, ?)
                """,
                (gateway_id, client, scope, idem, fingerprint, visual_kind, now, now),
            )
            conn.commit()
        return self.get(gateway_id, client_id=client, scope_id=scope), True

    def update(
        self,
        gateway_id: str,
        *,
        client_id: str,
        scope_id: str,
        provider: str,
        kind: str,
        status: str,
        provider_job_id: str = "",
        model: str = "",
        mime_type: str = "",
        asset_path: str = "",
        error_code: str = "",
    ) -> StoredJob:
        token = _job_id(gateway_id)
        client = _client(client_id)
        scope = _scope(scope_id)
        now = int(time.time())
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE visual_jobs
                SET provider=?, kind=?, status=?, provider_job_id=?, model=?, mime_type=?, asset_path=?, error_code=?, updated_at=?
                WHERE id=? AND client_id=? AND scope_id=?
                """,
                (provider, kind, status, provider_job_id, model, mime_type, asset_path, error_code, now, token, client, scope),
            )
            if cur.rowcount != 1:
                raise KeyError(token)
        return self.get(token, client_id=client, scope_id=scope)

    def get(self, gateway_id: str, *, client_id: str, scope_id: str) -> StoredJob:
        token = _job_id(gateway_id)
        client = _client(client_id)
        scope = _scope(scope_id)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM visual_jobs WHERE id=? AND client_id=? AND scope_id=?",
                (token, client, scope),
            ).fetchone()
        if row is None:
            raise KeyError(token)
        return StoredJob(**dict(row))

    def count_since(self, *, client_id: str, since_epoch: int, kind: str = "") -> int:
        client = _client(client_id)
        visual_kind = str(kind or "").strip().lower()
        if visual_kind and visual_kind not in {"image", "video"}:
            raise ValueError("invalid_visual_kind")
        with self._lock, self._connect() as conn:
            if visual_kind:
                row = conn.execute(
                    "SELECT COUNT(*) FROM visual_jobs WHERE client_id=? AND created_at>=? AND kind=?",
                    (client, max(0, int(since_epoch)), visual_kind),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM visual_jobs WHERE client_id=? AND created_at>=?",
                    (client, max(0, int(since_epoch))),
                ).fetchone()
        return int(row[0] if row is not None else 0)

    def active_count(self, *, client_id: str) -> int:
        client = _client(client_id)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM visual_jobs WHERE client_id=? AND status IN ('queued','running')",
                (client,),
            ).fetchone()
        return int(row[0] if row is not None else 0)

    @staticmethod
    def utc_day_start_epoch(now: int | None = None) -> int:
        current = datetime.fromtimestamp(int(now or time.time()), tz=timezone.utc)
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp())
