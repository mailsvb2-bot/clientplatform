from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

CONTRACT_VERSION = "1.0"
FORMATS: dict[str, tuple[int, int]] = {
    "square": (1080, 1080),
    "feed": (1080, 1350),
    "story": (1080, 1920),
    "landscape": (1200, 628),
}
_SCOPE_RE = re.compile(r"[A-Za-z0-9_.:@/-]{1,160}")
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9_.:@/-]{8,200}")
_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
FONT_PRESETS = (
    "auto",
    "modern",
    "strict",
    "friendly",
    "premium",
    "editorial",
    "elegant",
    "bold_ad",
)
_FONT_PRESET_PATHS: dict[str, dict[str, tuple[str, ...]]] = {
    "modern": {
        "headline": (
            "/usr/share/fonts/truetype/lato/Lato-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
        "body": (
            "/usr/share/fonts/truetype/lato/Lato-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ),
        "cta": (
            "/usr/share/fonts/truetype/lato/Lato-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
    },
    "strict": {
        "headline": (
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
        "body": (
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ),
        "cta": (
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
    },
    "friendly": {
        "headline": ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",),
        "body": ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",),
        "cta": ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",),
    },
    "premium": {
        "headline": (
            "/usr/share/fonts/truetype/noto/NotoSerif-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        ),
        "body": (
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ),
        "cta": (
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
    },
    "editorial": {
        "headline": (
            "/usr/share/fonts/truetype/noto/NotoSerif-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        ),
        "body": (
            "/usr/share/fonts/truetype/noto/NotoSerif-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        ),
        "cta": (
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
    },
    "elegant": {
        "headline": (
            "/usr/share/fonts/truetype/liberation2/LiberationSerif-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        ),
        "body": (
            "/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        ),
        "cta": (
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
    },
    "bold_ad": {
        "headline": (
            "/usr/share/fonts/truetype/lato/Lato-Heavy.ttf",
            "/usr/share/fonts/truetype/lato/Lato-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
        "body": (
            "/usr/share/fonts/truetype/lato/Lato-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ),
        "cta": (
            "/usr/share/fonts/truetype/lato/Lato-Heavy.ttf",
            "/usr/share/fonts/truetype/lato/Lato-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
    },
}


class GatewayError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    token: str
    upstream_url: str
    upstream_token: str
    state_dir: Path
    asset_dir: Path | None = None
    transient_assets_required: bool = False
    transient_asset_ttl_seconds: int = 21_600
    transient_asset_cleanup_limit: int = 200
    daily_generation_limit: int = 100
    upstream_timeout_seconds: int = 90
    upstream_media_idle_timeout_seconds: int = 60
    max_json_bytes: int = 128 * 1024
    max_source_bytes: int = 128 * 1024 * 1024
    font_path: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        token = str(os.getenv("VISUAL_GATEWAY_TOKEN", "") or "").strip()
        if not token:
            raise RuntimeError("VISUAL_GATEWAY_TOKEN is required")
        upstream_url = _safe_base_url(os.getenv("VISUAL_GATEWAY_UPSTREAM_URL", ""))
        return cls(
            token=token,
            upstream_url=upstream_url,
            upstream_token=str(os.getenv("VISUAL_GATEWAY_UPSTREAM_TOKEN", "") or "").strip(),
            state_dir=Path(os.getenv("VISUAL_GATEWAY_STATE_DIR", "/var/lib/visual-gateway")).expanduser(),
            asset_dir=Path(os.getenv("VISUAL_GATEWAY_ASSET_DIR", "/tmp/visual-gateway-assets")).expanduser(),
            transient_assets_required=str(
                os.getenv("VISUAL_GATEWAY_TRANSIENT_OUTPUT_REQUIRED", "0") or "0"
            ).strip().lower() in {"1", "true", "yes", "on"},
            transient_asset_ttl_seconds=_env_int("VISUAL_GATEWAY_TRANSIENT_ASSET_TTL_SECONDS", 21_600, 300, 86_400),
            transient_asset_cleanup_limit=_env_int("VISUAL_GATEWAY_TRANSIENT_ASSET_CLEANUP_LIMIT", 200, 1, 5_000),
            daily_generation_limit=_env_int("VISUAL_GATEWAY_DAILY_GENERATION_LIMIT", 100, 1, 100000),
            upstream_timeout_seconds=_env_int("VISUAL_GATEWAY_UPSTREAM_TIMEOUT_SECONDS", 90, 5, 300),
            upstream_media_idle_timeout_seconds=_env_int(
                "VISUAL_GATEWAY_UPSTREAM_MEDIA_IDLE_TIMEOUT_SECONDS",
                60,
                5,
                300,
            ),
            max_json_bytes=_env_int("VISUAL_GATEWAY_MAX_JSON_BYTES", 128 * 1024, 4096, 2 * 1024 * 1024),
            max_source_bytes=_env_int("VISUAL_GATEWAY_MAX_SOURCE_BYTES", 128 * 1024 * 1024, 1024 * 1024, 512 * 1024 * 1024),
            font_path=str(os.getenv("VISUAL_GATEWAY_FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf") or ""),
        )


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _safe_base_url(value: object) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("VISUAL_GATEWAY_UPSTREAM_URL must be a plain http(s) base URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("invalid VISUAL_GATEWAY_UPSTREAM_URL port") from exc
    suffix = f":{port}" if port else ""
    return f"{parsed.scheme}://{parsed.hostname}{suffix}{parsed.path.rstrip('/')}"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Store:
    def __init__(
        self,
        root: Path,
        *,
        asset_root: Path | None = None,
        asset_ttl_seconds: int = 21_600,
        asset_cleanup_limit: int = 200,
    ) -> None:
        self.root = root.resolve()
        self.assets = (asset_root or (self.root / "assets")).resolve()
        self.db_path = self.root / "gateway.sqlite3"
        self.asset_ttl_seconds = max(300, min(int(asset_ttl_seconds), 86_400))
        self.asset_cleanup_limit = max(1, min(int(asset_cleanup_limit), 5_000))
        self.root.mkdir(parents=True, exist_ok=True)
        self.assets.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS render_packs (
                    id TEXT PRIMARY KEY,
                    scope_id TEXT NOT NULL,
                    source_job_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    formats_json TEXT NOT NULL,
                    composition_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT NOT NULL DEFAULT '',
                    claim_token TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(scope_id, request_hash)
                );
                CREATE TABLE IF NOT EXISTS render_idempotency (
                    scope_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    pack_id TEXT NOT NULL REFERENCES render_packs(id) ON DELETE CASCADE,
                    PRIMARY KEY(scope_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS render_assets (
                    pack_id TEXT NOT NULL REFERENCES render_packs(id) ON DELETE CASCADE,
                    format_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    mime_type TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    path TEXT NOT NULL,
                    asset_ready INTEGER NOT NULL,
                    PRIMARY KEY(pack_id, format_id)
                );
                CREATE TABLE IF NOT EXISTS generation_usage (
                    scope_id TEXT NOT NULL,
                    utc_day TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY(scope_id, utc_day)
                );
                CREATE TABLE IF NOT EXISTS generation_idempotency (
                    scope_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    utc_day TEXT NOT NULL,
                    PRIMARY KEY(scope_id, idempotency_key)
                );
                """
            )
            conn.execute("UPDATE render_packs SET claim_token='' WHERE status='running'")

    def cleanup_transient_assets(self) -> int:
        cutoff = time.time() - self.asset_ttl_seconds
        removed = 0
        try:
            candidates = sorted(
                (item for item in self.assets.rglob("*") if item.is_file()),
                key=lambda item: item.stat().st_mtime,
            )
        except OSError:
            return 0
        for candidate in candidates:
            if removed >= self.asset_cleanup_limit:
                break
            try:
                resolved = candidate.resolve()
                resolved.relative_to(self.assets)
                if resolved.stat().st_mtime > cutoff:
                    continue
                resolved.unlink(missing_ok=True)
                removed += 1
            except (OSError, ValueError):
                continue
        if removed:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT pack_id,format_id,path FROM render_assets WHERE asset_ready=1"
                ).fetchall()
                for row in rows:
                    path = (self.assets / str(row["path"])).resolve()
                    try:
                        path.relative_to(self.assets)
                    except ValueError:
                        conn.execute(
                            "UPDATE render_assets SET asset_ready=0 WHERE pack_id=? AND format_id=?",
                            (str(row["pack_id"]), str(row["format_id"])),
                        )
                        continue
                    if not path.is_file():
                        conn.execute(
                            "UPDATE render_assets SET asset_ready=0 WHERE pack_id=? AND format_id=?",
                            (str(row["pack_id"]), str(row["format_id"])),
                        )
        return removed

    def reserve_generation(self, scope_id: str, idempotency_key: str, request_hash: str, limit: int) -> str:
        """Reserve provider egress once per exact idempotent request."""
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            idem = conn.execute(
                "SELECT request_hash FROM generation_idempotency WHERE scope_id=? AND idempotency_key=?",
                (scope_id, idempotency_key),
            ).fetchone()
            if idem:
                if str(idem["request_hash"]) != request_hash:
                    conn.execute("ROLLBACK")
                    raise GatewayError(409, "generation_idempotency_conflict")
                conn.execute("COMMIT")
                return "replay"
            row = conn.execute(
                "SELECT count FROM generation_usage WHERE scope_id=? AND utc_day=?",
                (scope_id, day),
            ).fetchone()
            count = int(row["count"]) if row else 0
            if count >= limit:
                conn.execute("ROLLBACK")
                return "quota"
            conn.execute(
                "INSERT INTO generation_usage(scope_id,utc_day,count) VALUES(?,?,1) "
                "ON CONFLICT(scope_id,utc_day) DO UPDATE SET count=count+1",
                (scope_id, day),
            )
            conn.execute(
                "INSERT INTO generation_idempotency(scope_id,idempotency_key,request_hash,utc_day) VALUES(?,?,?,?)",
                (scope_id, idempotency_key, request_hash, day),
            )
            conn.execute("COMMIT")
            return "new"

    def get_or_create_pack(
        self,
        *,
        scope_id: str,
        source_job_id: str,
        idempotency_key: str,
        request_hash: str,
        formats: list[str],
        composition: dict[str, Any],
    ) -> tuple[str, bool]:
        now = int(time.time())
        pack_id = "rp_" + hashlib.sha256(f"{scope_id}|{request_hash}".encode()).hexdigest()[:32]
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            idem = conn.execute(
                "SELECT request_hash, pack_id FROM render_idempotency WHERE scope_id=? AND idempotency_key=?",
                (scope_id, idempotency_key),
            ).fetchone()
            if idem:
                if str(idem["request_hash"]) != request_hash:
                    conn.execute("ROLLBACK")
                    raise GatewayError(409, "render_idempotency_conflict")
                conn.execute("COMMIT")
                return str(idem["pack_id"]), False
            existing = conn.execute(
                "SELECT id FROM render_packs WHERE scope_id=? AND request_hash=?",
                (scope_id, request_hash),
            ).fetchone()
            created = existing is None
            if created:
                conn.execute(
                    "INSERT INTO render_packs(id,scope_id,source_job_id,request_hash,formats_json,composition_json,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?, 'running', ?, ?)",
                    (pack_id, scope_id, source_job_id, request_hash, _canonical(formats), _canonical(composition), now, now),
                )
            else:
                pack_id = str(existing["id"])
            conn.execute(
                "INSERT INTO render_idempotency(scope_id,idempotency_key,request_hash,pack_id) VALUES(?,?,?,?)",
                (scope_id, idempotency_key, request_hash, pack_id),
            )
            conn.execute("COMMIT")
        return pack_id, created

    def claim(self, pack_id: str) -> str:
        token = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status,claim_token FROM render_packs WHERE id=?", (pack_id,)).fetchone()
            if not row or str(row["status"]) != "running" or str(row["claim_token"]):
                conn.execute("ROLLBACK")
                return ""
            conn.execute("UPDATE render_packs SET claim_token=?,updated_at=? WHERE id=?", (token, int(time.time()), pack_id))
            conn.execute("COMMIT")
        return token

    def fail(self, pack_id: str, claim_token: str, code: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE render_packs SET status='failed',error_code=?,claim_token='',updated_at=? WHERE id=? AND claim_token=?",
                (code[:160], int(time.time()), pack_id, claim_token),
            )

    def succeed(self, pack_id: str, claim_token: str, assets: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT claim_token FROM render_packs WHERE id=?", (pack_id,)).fetchone()
            if not row or str(row["claim_token"]) != claim_token:
                conn.execute("ROLLBACK")
                raise GatewayError(409, "render_claim_lost")
            conn.execute("DELETE FROM render_assets WHERE pack_id=?", (pack_id,))
            for asset in assets:
                conn.execute(
                    "INSERT INTO render_assets(pack_id,format_id,kind,width,height,mime_type,sha256,path,asset_ready) VALUES(?,?,?,?,?,?,?,?,1)",
                    (pack_id, asset["format_id"], asset["kind"], asset["width"], asset["height"], asset["mime_type"], asset["sha256"], asset["path"]),
                )
            conn.execute(
                "UPDATE render_packs SET status='succeeded',error_code='',claim_token='',updated_at=? WHERE id=?",
                (int(time.time()), pack_id),
            )
            conn.execute("COMMIT")

    def pack(self, pack_id: str, scope_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM render_packs WHERE id=? AND scope_id=?", (pack_id, scope_id)).fetchone()
            if not row:
                raise GatewayError(404, "render_pack_not_found")
            assets = conn.execute(
                "SELECT format_id,kind,width,height,mime_type,sha256,asset_ready FROM render_assets WHERE pack_id=? ORDER BY format_id",
                (pack_id,),
            ).fetchall()
        return {
            "id": str(row["id"]),
            "scope_id": str(row["scope_id"]),
            "source_job_id": str(row["source_job_id"]),
            "status": str(row["status"]),
            "error_code": str(row["error_code"]),
            "assets": [
                {
                    "format_id": str(item["format_id"]),
                    "kind": str(item["kind"]),
                    "width": int(item["width"]),
                    "height": int(item["height"]),
                    "mime_type": str(item["mime_type"]),
                    "sha256": str(item["sha256"]),
                    "asset_ready": bool(item["asset_ready"]),
                    "quality": {"technical_score": 100},
                }
                for item in assets
            ],
        }

    def asset(self, pack_id: str, scope_id: str, format_id: str) -> tuple[Path, str, str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT a.path,a.mime_type,a.sha256,a.asset_ready,p.status FROM render_assets a JOIN render_packs p ON p.id=a.pack_id "
                "WHERE a.pack_id=? AND p.scope_id=? AND a.format_id=?",
                (pack_id, scope_id, format_id),
            ).fetchone()
        if not row or str(row["status"]) != "succeeded" or not bool(row["asset_ready"]):
            raise GatewayError(404, "render_asset_not_found")
        path = (self.assets / str(row["path"])).resolve()
        try:
            path.relative_to(self.assets)
        except ValueError:
            raise GatewayError(409, "render_asset_missing") from None
        if not path.is_file():
            raise GatewayError(409, "render_asset_missing")
        raw_digest = _sha256_file(path)
        if raw_digest != str(row["sha256"]):
            raise GatewayError(409, "render_asset_digest_mismatch")
        return path, str(row["mime_type"]), raw_digest


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _auth(request: web.Request) -> None:
    config: GatewayConfig = request.app["config"]
    supplied = request.headers.get("Authorization", "")
    expected = f"Bearer {config.token}"
    if not hmac.compare_digest(supplied, expected):
        raise GatewayError(401, "unauthorized")


@web.middleware
async def _boundary(request: web.Request, handler):
    try:
        if request.path.startswith("/v1/"):
            _auth(request)
        return await handler(request)
    except GatewayError as exc:
        return web.json_response({"error_code": exc.code}, status=exc.status)
    except web.HTTPException:
        raise


async def _json_body(request: web.Request) -> dict[str, Any]:
    config: GatewayConfig = request.app["config"]
    if request.content_length is not None and request.content_length > config.max_json_bytes:
        raise GatewayError(413, "request_too_large")
    raw = await request.content.read(config.max_json_bytes + 1)
    if len(raw) > config.max_json_bytes:
        raise GatewayError(413, "request_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GatewayError(400, "invalid_json") from None
    if not isinstance(value, dict):
        raise GatewayError(400, "invalid_json")
    return value


def _scope(value: object) -> str:
    scope_id = str(value or "").strip()
    if _SCOPE_RE.fullmatch(scope_id) is None:
        raise GatewayError(400, "invalid_scope")
    return scope_id


def _upstream_headers(config: GatewayConfig, *, json_body: bool = False) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if config.upstream_token:
        headers["Authorization"] = f"Bearer {config.upstream_token}"
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


async def _upstream_json(request: web.Request, method: str, path: str, *, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    config: GatewayConfig = request.app["config"]
    session: ClientSession = request.app["session"]
    try:
        async with session.request(
            method,
            config.upstream_url + path,
            json=payload,
            headers=_upstream_headers(config, json_body=payload is not None),
        ) as response:
            raw = await response.content.read(config.max_json_bytes + 1)
            if len(raw) > config.max_json_bytes:
                raise GatewayError(502, "provider_gateway_response_too_large")
            try:
                value = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise GatewayError(502, "provider_gateway_invalid_json") from None
            if not isinstance(value, dict):
                raise GatewayError(502, "provider_gateway_invalid_json")
            return response.status, value
    except (ClientError, asyncio.TimeoutError):
        raise GatewayError(502, "provider_gateway_unavailable") from None


async def capabilities(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "contract_version": CONTRACT_VERSION,
            "capabilities": ["generation", "semantic_qa", "render_pack", "usage"],
            "render_formats": list(FORMATS),
        }
    )


async def proxy_generation_create(request: web.Request) -> web.Response:
    payload = await _json_body(request)
    scope_id = _scope(payload.get("scope_id"))
    idem = str(payload.get("idempotency_key") or "").strip()
    if _IDEMPOTENCY_RE.fullmatch(idem) is None:
        raise GatewayError(400, "invalid_idempotency_key")
    store: Store = request.app["store"]
    config: GatewayConfig = request.app["config"]
    request_hash = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    reservation = store.reserve_generation(scope_id, idem, request_hash, config.daily_generation_limit)
    if reservation == "quota":
        raise GatewayError(429, "generation_quota_exceeded")
    status, value = await _upstream_json(request, "POST", "/v1/creative/generations", payload=payload)
    if status < 200 or status >= 300:
        raise GatewayError(502, f"provider_gateway_http_{status}")
    if str(value.get("scope_id") or "") != scope_id:
        raise GatewayError(502, "provider_gateway_scope_mismatch")
    return web.json_response(value, status=status)


async def proxy_generation_get(request: web.Request) -> web.Response:
    job_id = str(request.match_info["job_id"])
    if _ID_RE.fullmatch(job_id) is None:
        raise GatewayError(400, "invalid_job_id")
    scope_id = _scope(request.query.get("scope_id"))
    query = urllib.parse.urlencode({"scope_id": scope_id})
    status, value = await _upstream_json(request, "GET", f"/v1/creative/generations/{urllib.parse.quote(job_id, safe='')}?{query}")
    if status < 200 or status >= 300:
        return web.json_response(value, status=status)
    if str(value.get("scope_id") or "") != scope_id:
        raise GatewayError(502, "provider_gateway_scope_mismatch")
    return web.json_response(value, status=status)


async def _read_upstream_content_limited(response, *, limit: int) -> bytes:
    expected_length = response.content_length
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise GatewayError(502, "provider_gateway_source_too_large")
        chunks.append(chunk)
    if expected_length is not None and total != int(expected_length):
        raise GatewayError(502, "provider_gateway_source_incomplete")
    return b"".join(chunks)


async def _upstream_content(request: web.Request, job_id: str, scope_id: str) -> tuple[bytes, str]:
    config: GatewayConfig = request.app["config"]
    session: ClientSession = request.app["session"]
    query = urllib.parse.urlencode({"scope_id": scope_id})
    connect_timeout = max(
        1.0,
        min(float(config.upstream_timeout_seconds), 30.0),
    )
    media_timeout = ClientTimeout(
        total=None,
        connect=connect_timeout,
        sock_connect=connect_timeout,
        sock_read=float(config.upstream_media_idle_timeout_seconds),
    )
    try:
        async with session.get(
            config.upstream_url + f"/v1/creative/generations/{urllib.parse.quote(job_id, safe='')}/content?{query}",
            headers=_upstream_headers(config),
            timeout=media_timeout,
        ) as response:
            if response.status < 200 or response.status >= 300:
                raise GatewayError(502, f"provider_gateway_http_{response.status}")
            if response.content_length is not None and response.content_length > config.max_source_bytes:
                raise GatewayError(502, "provider_gateway_source_too_large")
            raw = await _read_upstream_content_limited(
                response,
                limit=config.max_source_bytes,
            )
            mime = str(response.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0].strip().lower()
            return raw, mime
    except (ClientError, asyncio.TimeoutError):
        raise GatewayError(502, "provider_gateway_unavailable") from None


async def proxy_generation_semantic_qa(request: web.Request) -> web.Response:
    """Proxy advisory image-semantic QA through the canonical visual boundary."""

    job_id = str(request.match_info["job_id"])
    if _ID_RE.fullmatch(job_id) is None:
        raise GatewayError(400, "invalid_job_id")
    payload = await _json_body(request)
    scope_id = _scope(payload.get("scope_id"))
    contract = payload.get("contract")
    if not isinstance(contract, dict):
        raise GatewayError(400, "invalid_semantic_qa_contract")
    status, value = await _upstream_json(
        request,
        "POST",
        f"/v1/creative/generations/{urllib.parse.quote(job_id, safe='')}/semantic-qa",
        payload={"scope_id": scope_id, "contract": contract},
    )
    if status < 200 or status >= 300:
        return web.json_response(value, status=status)
    qa_status = str(value.get("status") or "").strip().lower()
    issues = value.get("issues")
    summary = value.get("summary")
    if (
        qa_status not in {"pass", "needs_review", "unavailable"}
        or not isinstance(issues, list)
        or not isinstance(summary, str)
    ):
        raise GatewayError(502, "provider_gateway_invalid_semantic_qa")
    return web.json_response(value, status=status)


async def proxy_generation_content(request: web.Request) -> web.Response:
    job_id = str(request.match_info["job_id"])
    if _ID_RE.fullmatch(job_id) is None:
        raise GatewayError(400, "invalid_job_id")
    scope_id = _scope(request.query.get("scope_id"))
    raw, mime = await _upstream_content(request, job_id, scope_id)
    return web.Response(body=raw, content_type=mime)


async def proxy_simple_get(request: web.Request) -> web.Response:
    path = "/v1/" + str(request.match_info["endpoint"])
    status, value = await _upstream_json(request, "GET", path)
    return web.json_response(value, status=status)


def _render_request(payload: dict[str, Any]) -> tuple[str, str, str, list[str], dict[str, Any], str]:
    source_job_id = str(payload.get("source_job_id") or "").strip()
    if _ID_RE.fullmatch(source_job_id) is None:
        raise GatewayError(400, "invalid_source_job_id")
    scope_id = _scope(payload.get("scope_id"))
    idem = str(payload.get("idempotency_key") or "").strip()
    if _IDEMPOTENCY_RE.fullmatch(idem) is None:
        raise GatewayError(400, "invalid_idempotency_key")
    raw_formats = payload.get("formats")
    if not isinstance(raw_formats, list) or not raw_formats or len(raw_formats) > 4:
        raise GatewayError(400, "invalid_render_formats")
    formats: list[str] = []
    for raw in raw_formats:
        token = str(raw or "").strip().lower()
        if token not in FORMATS or token in formats:
            raise GatewayError(400, "invalid_render_formats")
        formats.append(token)
    composition = payload.get("composition")
    if not isinstance(composition, dict):
        raise GatewayError(400, "invalid_render_composition")
    _font_preset(composition, resolve_auto=False)
    composition_json = _canonical(composition)
    if len(composition_json.encode("utf-8")) > 64 * 1024:
        raise GatewayError(413, "render_composition_too_large")
    exact = {
        "source_job_id": source_job_id,
        "scope_id": scope_id,
        "formats": formats,
        "composition": composition,
    }
    request_hash = hashlib.sha256(_canonical(exact).encode("utf-8")).hexdigest()
    return source_job_id, scope_id, idem, formats, composition, request_hash


async def _source_job(request: web.Request, source_job_id: str, scope_id: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"scope_id": scope_id})
    status, value = await _upstream_json(
        request,
        "GET",
        f"/v1/creative/generations/{urllib.parse.quote(source_job_id, safe='')}?{query}",
    )
    if status != 200:
        raise GatewayError(502, f"provider_gateway_http_{status}")
    if str(value.get("id") or "") != source_job_id or str(value.get("scope_id") or "") != scope_id:
        raise GatewayError(502, "provider_gateway_scope_mismatch")
    kind = str(value.get("kind") or "").strip().lower()
    if kind not in {"image", "video"}:
        raise GatewayError(502, "provider_gateway_invalid_kind")
    if str(value.get("status") or "").lower() != "succeeded" or value.get("asset_ready") is not True:
        raise GatewayError(409, "visual_source_not_ready")
    return value


def _color(value: object, default: str) -> str:
    token = str(value or default).strip().upper()
    return token if _COLOR_RE.fullmatch(token) else default


def _font_preset(
    composition: dict[str, Any],
    *,
    resolve_auto: bool,
) -> str:
    typography = composition.get("typography")
    if typography is None:
        preset = "auto"
    else:
        if not isinstance(typography, dict) or set(typography) != {"preset"}:
            raise GatewayError(400, "invalid_render_typography")
        preset = str(typography.get("preset") or "auto").strip().lower()
        if preset not in FONT_PRESETS:
            raise GatewayError(400, "invalid_render_typography")
    if preset != "auto" or not resolve_auto:
        return preset

    headline = " ".join(str(composition.get("headline") or "").split())
    body = " ".join(str(composition.get("body") or "").split())
    cta = " ".join(str(composition.get("cta") or "").split())
    if len(body) >= 220:
        return "editorial"
    if len(headline) <= 28 and len(body) <= 110 and cta:
        return "bold_ad"
    if len(headline) >= 72:
        return "strict"
    return "modern"


def _font(
    config: GatewayConfig,
    size: int,
    *,
    preset: str,
    role: str,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = list(_FONT_PRESET_PATHS.get(preset, {}).get(role, ()))
    if config.font_path and config.font_path not in candidates:
        candidates.append(config.font_path)
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default(size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int, max_lines: int) -> list[str]:
    words = " ".join(str(text or "").split()).split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
            if len(lines) >= max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    return lines


def _overlay(config: GatewayConfig, width: int, height: int, composition: dict[str, Any]) -> Image.Image:
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    brand = composition.get("brand") if isinstance(composition.get("brand"), dict) else {}
    primary = _color(brand.get("primary_color"), "#172033")
    accent = _color(brand.get("accent_color"), "#E9C46A")
    text_color = _color(brand.get("text_color"), "#FFFFFF")
    headline = " ".join(str(composition.get("headline") or "").split())[:160]
    body = " ".join(str(composition.get("body") or "").split())[:500]
    cta = " ".join(str(composition.get("cta") or "").split())[:80]
    layout = str(composition.get("layout") or "lower_card").strip().lower()
    card_h = max(250, int(height * 0.36))
    top = int(height * 0.06) if layout == "top_card" else height - card_h - int(height * 0.06)
    left = int(width * 0.06)
    right = width - left
    radius = max(20, int(min(width, height) * 0.025))
    draw.rounded_rectangle((left, top, right, top + card_h), radius=radius, fill=primary + "E8")
    pad = max(24, int(width * 0.04))
    x = left + pad
    y = top + pad
    maxw = right - x - pad
    font_preset = _font_preset(composition, resolve_auto=True)
    hfont = _font(
        config,
        max(32, int(width * 0.052)),
        preset=font_preset,
        role="headline",
    )
    bfont = _font(
        config,
        max(24, int(width * 0.030)),
        preset=font_preset,
        role="body",
    )
    cfont = _font(
        config,
        max(24, int(width * 0.032)),
        preset=font_preset,
        role="cta",
    )
    for line in _wrap(draw, headline, hfont, maxw, 2):
        draw.text((x, y), line, font=hfont, fill=text_color)
        y += int(hfont.size * 1.22) if hasattr(hfont, "size") else 42
    y += max(8, int(height * 0.008))
    for line in _wrap(draw, body, bfont, maxw, 3):
        draw.text((x, y), line, font=bfont, fill=text_color)
        y += int(bfont.size * 1.30) if hasattr(bfont, "size") else 32
    if cta:
        bbox = draw.textbbox((0, 0), cta, font=cfont)
        cta_w = min(maxw, bbox[2] + pad * 2)
        cta_h = bbox[3] - bbox[1] + max(16, pad // 2)
        cta_y = min(top + card_h - cta_h - pad, y + max(12, pad // 2))
        draw.rounded_rectangle((x, cta_y, x + cta_w, cta_y + cta_h), radius=cta_h // 2, fill=accent)
        draw.text((x + pad, cta_y + max(6, pad // 4)), cta, font=cfont, fill=primary)
    return layer


def _durable_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp-" + uuid.uuid4().hex)
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(path.parent, os.O_DIRECTORY)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _render_image(config: GatewayConfig, source: bytes, pack_dir: Path, formats: list[str], composition: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        with Image.open(io.BytesIO(source)) as opened:
            base = ImageOps.exif_transpose(opened).convert("RGB")
            base.load()
    except (UnidentifiedImageError, OSError, ValueError):
        raise GatewayError(502, "provider_gateway_invalid_image") from None
    assets: list[dict[str, Any]] = []
    for format_id in formats:
        width, height = FORMATS[format_id]
        fitted = ImageOps.fit(base, (width, height), method=Image.Resampling.LANCZOS)
        canvas = fitted.convert("RGBA")
        canvas.alpha_composite(_overlay(config, width, height, composition))
        output = io.BytesIO()
        canvas.convert("RGB").save(output, format="JPEG", quality=92, optimize=True, progressive=True)
        raw = output.getvalue()
        target = pack_dir / f"{format_id}.jpg"
        _durable_write(target, raw)
        assets.append({
            "format_id": format_id,
            "kind": "image",
            "width": width,
            "height": height,
            "mime_type": "image/jpeg",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "path": str(target),
        })
    return assets


def _render_video(config: GatewayConfig, source: bytes, mime: str, pack_dir: Path, formats: list[str], composition: dict[str, Any]) -> list[dict[str, Any]]:
    suffix = mimetypes.guess_extension(mime) or ".mp4"
    source_path = pack_dir / f"source{suffix}"
    _durable_write(source_path, source)
    assets: list[dict[str, Any]] = []
    for format_id in formats:
        width, height = FORMATS[format_id]
        overlay_path = pack_dir / f"overlay-{format_id}.png"
        overlay_bytes = io.BytesIO()
        _overlay(config, width, height, composition).save(overlay_bytes, format="PNG")
        _durable_write(overlay_path, overlay_bytes.getvalue())
        target = pack_dir / f"{format_id}.mp4"
        cmd = [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(source_path), "-i", str(overlay_path),
            "-filter_complex", f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}[b];[b][1:v]overlay=0:0[v]",
            "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(target),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=240)
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise GatewayError(500, "video_render_backend_failed") from None
        assets.append({
            "format_id": format_id,
            "kind": "video",
            "width": width,
            "height": height,
            "mime_type": "video/mp4",
            "sha256": _sha256_file(target),
            "path": str(target),
        })
    source_path.unlink(missing_ok=True)
    for overlay in pack_dir.glob("overlay-*.png"):
        overlay.unlink(missing_ok=True)
    return assets


async def create_render_pack(request: web.Request) -> web.Response:
    payload = await _json_body(request)
    source_job_id, scope_id, idem, formats, composition, request_hash = _render_request(payload)
    store: Store = request.app["store"]
    store.cleanup_transient_assets()
    pack_id, _ = store.get_or_create_pack(
        scope_id=scope_id,
        source_job_id=source_job_id,
        idempotency_key=idem,
        request_hash=request_hash,
        formats=formats,
        composition=composition,
    )
    claim = store.claim(pack_id)
    if claim:
        try:
            job = await _source_job(request, source_job_id, scope_id)
            raw, mime = await _upstream_content(request, source_job_id, scope_id)
            kind = str(job.get("kind") or "").lower()
            if kind == "image" and not mime.startswith("image/"):
                raise GatewayError(502, "provider_gateway_kind_mime_mismatch")
            if kind == "video" and not mime.startswith("video/"):
                raise GatewayError(502, "provider_gateway_kind_mime_mismatch")
            pack_dir = store.assets / pack_id
            pack_dir.mkdir(parents=True, exist_ok=True)
            if kind == "image":
                assets = await asyncio.to_thread(_render_image, request.app["config"], raw, pack_dir, formats, composition)
            else:
                assets = await asyncio.to_thread(_render_video, request.app["config"], raw, mime, pack_dir, formats, composition)
            for asset in assets:
                asset["path"] = str(Path(asset["path"]).resolve().relative_to(store.assets))
            store.succeed(pack_id, claim, assets)
        except GatewayError as exc:
            store.fail(pack_id, claim, exc.code)
        except (OSError, sqlite3.Error):
            store.fail(pack_id, claim, "render_internal_error")
    return web.json_response(store.pack(pack_id, scope_id))


async def get_render_pack(request: web.Request) -> web.Response:
    pack_id = str(request.match_info["pack_id"])
    if _ID_RE.fullmatch(pack_id) is None:
        raise GatewayError(400, "invalid_render_pack_id")
    scope_id = _scope(request.query.get("scope_id"))
    request.app["store"].cleanup_transient_assets()
    return web.json_response(request.app["store"].pack(pack_id, scope_id))


async def render_content(request: web.Request) -> web.StreamResponse:
    pack_id = str(request.match_info["pack_id"])
    format_id = str(request.match_info["format_id"]).strip().lower()
    if _ID_RE.fullmatch(pack_id) is None or format_id not in FORMATS:
        raise GatewayError(400, "invalid_render_asset")
    scope_id = _scope(request.query.get("scope_id"))
    request.app["store"].cleanup_transient_assets()
    path, mime, digest = request.app["store"].asset(pack_id, scope_id, format_id)
    response = web.FileResponse(path)
    response.content_type = mime
    response.headers["X-Content-SHA256"] = digest
    return response


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "contract_version": CONTRACT_VERSION})


async def _startup(app: web.Application) -> None:
    config: GatewayConfig = app["config"]
    app["session"] = ClientSession(timeout=ClientTimeout(total=config.upstream_timeout_seconds))


async def _cleanup(app: web.Application) -> None:
    session = app.get("session")
    if session is not None:
        await session.close()


def create_app(config: GatewayConfig | None = None) -> web.Application:
    config = config or GatewayConfig.from_env()
    app = web.Application(middlewares=[_boundary], client_max_size=config.max_json_bytes)
    app["config"] = config
    asset_root = (config.asset_dir or (config.state_dir / "assets")).expanduser().resolve()
    if config.transient_assets_required:
        transient_root = Path(tempfile.gettempdir()).resolve()
        try:
            asset_root.relative_to(transient_root)
        except ValueError as exc:
            raise RuntimeError("visual_gateway_persistent_user_media_forbidden") from exc
    app["store"] = Store(
        config.state_dir,
        asset_root=asset_root,
        asset_ttl_seconds=config.transient_asset_ttl_seconds,
        asset_cleanup_limit=config.transient_asset_cleanup_limit,
    )
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    app.router.add_get("/health", health)
    app.router.add_get("/v1/capabilities", capabilities)
    app.router.add_post("/v1/creative/generations", proxy_generation_create)
    app.router.add_get("/v1/creative/generations/{job_id}", proxy_generation_get)
    app.router.add_post(
        "/v1/creative/generations/{job_id}/semantic-qa",
        proxy_generation_semantic_qa,
    )
    app.router.add_get("/v1/creative/generations/{job_id}/content", proxy_generation_content)
    app.router.add_get("/v1/{endpoint:providers|usage}", proxy_simple_get)
    app.router.add_post("/v1/creative/render-packs", create_render_pack)
    app.router.add_get("/v1/creative/render-packs/{pack_id}", get_render_pack)
    app.router.add_get("/v1/creative/render-packs/{pack_id}/content/{format_id}", render_content)
    return app


def main() -> None:
    web.run_app(create_app(), host="0.0.0.0", port=_env_int("PORT", 8080, 1, 65535))


if __name__ == "__main__":
    main()
