from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _float_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        return float(default)
    return value if math.isfinite(value) else float(default)


DEFAULT_BACKUP_DIR = Path(os.getenv("CLIENTPLATFORM_POSTGRES_BACKUP_DIR", "/var/backups/clientplatform/postgres"))
SUPPORTED_SUFFIXES = (".dump", ".sql", ".sql.gz")
DEFAULT_MAX_BACKUP_AGE_HOURS = _float_env("CLIENTPLATFORM_POSTGRES_BACKUP_MAX_AGE_HOURS", 72.0)


@dataclass(frozen=True)
class DisasterRecoveryStatus:
    backup_dir: str
    latest_backup: str | None
    latest_backup_size_bytes: int
    latest_backup_mtime_utc: str | None
    latest_backup_sha256: str | None
    backup_count: int
    restore_target_configured: bool
    status: str
    reason: str
    latest_backup_age_seconds: int | None = None
    max_backup_age_hours: float = DEFAULT_MAX_BACKUP_AGE_HOURS
    latest_backup_fresh: bool | None = None

    @property
    def marker(self) -> str:
        return {"GREEN": "✅", "YELLOW": "⚠️", "RED": "🛑"}.get(self.status, "⚠️")

    @property
    def ok(self) -> bool:
        return self.status in {"GREEN", "YELLOW"} and bool(self.latest_backup)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "backup_dir": self.backup_dir,
            "latest_backup": self.latest_backup,
            "latest_backup_size_bytes": self.latest_backup_size_bytes,
            "latest_backup_mtime_utc": self.latest_backup_mtime_utc,
            "latest_backup_age_seconds": self.latest_backup_age_seconds,
            "max_backup_age_hours": self.max_backup_age_hours,
            "latest_backup_fresh": self.latest_backup_fresh,
            "latest_backup_sha256": self.latest_backup_sha256,
            "backup_count": self.backup_count,
            "restore_target_configured": self.restore_target_configured,
            "status": self.status,
            "reason": self.reason,
        }


def _supported(path: Path) -> bool:
    name = path.name.lower()
    return path.is_file() and any(name.endswith(suffix) for suffix in SUPPORTED_SUFFIXES)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _coerce_max_age(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_BACKUP_AGE_HOURS
    return parsed if math.isfinite(parsed) else DEFAULT_MAX_BACKUP_AGE_HOURS


def _freshness(*, mtime_utc: datetime, max_age_hours: float) -> tuple[int, bool]:
    age_seconds = max(int((datetime.now(tz=UTC) - mtime_utc).total_seconds()), 0)
    safe_max_age = _coerce_max_age(max_age_hours)
    if safe_max_age <= 0:
        return age_seconds, True
    return age_seconds, age_seconds <= int(safe_max_age * 3600)


def _empty_status(
    *,
    backup_dir: Path,
    target_configured: bool,
    reason: str,
    max_backup_age_hours: float,
) -> DisasterRecoveryStatus:
    return DisasterRecoveryStatus(
        str(backup_dir),
        None,
        0,
        None,
        None,
        0,
        target_configured,
        "RED",
        reason,
        max_backup_age_hours=max_backup_age_hours,
    )


def disaster_recovery_status(
    *,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    include_hash: bool = False,
    max_backup_age_hours: float = DEFAULT_MAX_BACKUP_AGE_HOURS,
) -> DisasterRecoveryStatus:
    safe_max_age = _coerce_max_age(max_backup_age_hours)
    target_configured = bool(
        (os.getenv("CLIENTPLATFORM_RESTORE_DRILL_DATABASE_URL") or os.getenv("RESTORE_DATABASE_URL") or "").strip()
    )
    try:
        backup_dir_exists = backup_dir.exists()
    except OSError:
        return _empty_status(
            backup_dir=backup_dir,
            target_configured=target_configured,
            reason="backup_dir_unreadable",
            max_backup_age_hours=safe_max_age,
        )
    if not backup_dir_exists:
        return _empty_status(
            backup_dir=backup_dir,
            target_configured=target_configured,
            reason="backup_dir_missing",
            max_backup_age_hours=safe_max_age,
        )

    try:
        entries = list(backup_dir.iterdir())
    except OSError:
        return _empty_status(
            backup_dir=backup_dir,
            target_configured=target_configured,
            reason="backup_dir_unreadable",
            max_backup_age_hours=safe_max_age,
        )

    backups: list[tuple[float, Path, os.stat_result]] = []
    unreadable_candidates = 0
    for path in entries:
        try:
            if not _supported(path):
                continue
            stat = path.stat()
        except OSError:
            unreadable_candidates += 1
            continue
        backups.append((float(stat.st_mtime), path, stat))
    backups.sort(key=lambda item: item[0], reverse=True)

    if not backups:
        return _empty_status(
            backup_dir=backup_dir,
            target_configured=target_configured,
            reason="backup_unreadable" if unreadable_candidates else "backup_missing",
            max_backup_age_hours=safe_max_age,
        )

    _mtime, latest, stat = backups[0]
    latest_mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    latest_age_seconds, latest_fresh = _freshness(
        mtime_utc=latest_mtime,
        max_age_hours=safe_max_age,
    )

    if not latest_fresh:
        status = "RED"
        reason = f"backup_stale_gt_{safe_max_age:g}h"
    elif target_configured:
        status = "GREEN"
        reason = "restore_target_configured"
    else:
        status = "YELLOW"
        reason = "backup_exists_restore_target_missing"

    latest_hash: str | None = None
    if include_hash:
        try:
            latest_hash = _hash(latest)
        except OSError:
            status = "RED"
            reason = "backup_hash_failed"

    return DisasterRecoveryStatus(
        backup_dir=str(backup_dir),
        latest_backup=str(latest),
        latest_backup_size_bytes=int(stat.st_size),
        latest_backup_mtime_utc=latest_mtime.isoformat(),
        latest_backup_sha256=latest_hash,
        backup_count=len(backups),
        restore_target_configured=target_configured,
        status=status,
        reason=reason,
        latest_backup_age_seconds=latest_age_seconds,
        max_backup_age_hours=safe_max_age,
        latest_backup_fresh=bool(latest_fresh),
    )


def format_disaster_recovery_status_for_admin() -> str:
    status = disaster_recovery_status(include_hash=False)
    lines = [
        "🧯 Disaster recovery / backup proof",
        "",
        f"Статус: {status.marker} {status.status}",
        f"Backup dir: {status.backup_dir}",
        f"Backup count: {status.backup_count}",
        f"Latest backup: {status.latest_backup or '-'}",
        f"Latest size: {status.latest_backup_size_bytes}",
        f"Latest mtime UTC: {status.latest_backup_mtime_utc or '-'}",
        f"Latest age seconds: {status.latest_backup_age_seconds if status.latest_backup_age_seconds is not None else '-'}",
        f"Max backup age hours: {status.max_backup_age_hours:g}",
        f"Latest backup fresh: {status.latest_backup_fresh}",
        f"Restore target configured: {status.restore_target_configured}",
        f"Reason: {status.reason}",
    ]
    if status.status == "GREEN":
        lines.append("\nИтог: backup свежий, backup найден, drill target настроен; можно запускать restore drill как gate.")
    elif status.status == "YELLOW":
        lines.append("\nИтог: backup найден, но drill target не настроен. Это не доказывает восстановление.")
    else:
        lines.append("\nИтог: backup proof не закрыт. Релизная зрелость ниже production-grade.")
    return "\n".join(lines)


__all__ = ["DisasterRecoveryStatus", "disaster_recovery_status", "format_disaster_recovery_status_for_admin"]
