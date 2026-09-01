#!/usr/bin/env python3
from __future__ import annotations

"""Atomically add the one-time privacy export contract to a live env file.

The migration is intentionally narrow: it manages only three non-secret keys,
preserves every other byte of the environment file, writes a timestamped backup
before replacement, and never prints existing environment values.
"""

import argparse
import fcntl
import os
import re
import shlex
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

MANAGED_KEYS = (
    "PRIVACY_EXPORT_HTTP_ENABLED",
    "PRIVACY_EXPORT_PUBLIC_BASE_URL",
    "PRIVACY_EXPORT_TOKEN_TTL_MINUTES",
)
_ASSIGNMENT_RE = re.compile(
    r"^(?P<prefix>[ \t]*(?:export[ \t]+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*?)(?P<newline>\r?\n)?$"
)
_MARKER = "# One-time privacy export rollout (managed atomically)"


class MigrationError(RuntimeError):
    """Raised when the live env file cannot be migrated safely."""


@dataclass(frozen=True)
class MigrationResult:
    changed: bool
    backup_path: Path | None
    public_base_url: str
    ttl_minutes: int


def _validated_https_url(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise MigrationError("public base URL must be an absolute HTTPS URL without credentials")
    if parsed.query or parsed.fragment:
        raise MigrationError("public base URL must not contain query parameters or a fragment")
    return raw


def _validated_ttl(value: int | str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise MigrationError("TTL must be an integer") from exc
    if not 2 <= parsed <= 30:
        raise MigrationError("TTL must be between 2 and 30 minutes")
    return parsed


def _decoded_assignment_value(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    try:
        tokens = shlex.split(value, comments=False, posix=True)
    except ValueError:
        return ""
    return tokens[0] if len(tokens) == 1 else ""


def _active_assignments(lines: list[str]) -> dict[str, tuple[int, str]]:
    found: dict[str, tuple[int, str]] = {}
    duplicates: set[str] = set()
    for index, line in enumerate(lines):
        match = _ASSIGNMENT_RE.match(line)
        if match is None:
            continue
        key = match.group("key")
        if key not in MANAGED_KEYS:
            continue
        if key in found:
            duplicates.add(key)
            continue
        found[key] = (index, _decoded_assignment_value(match.group("value")))
    if duplicates:
        names = ", ".join(sorted(duplicates))
        raise MigrationError(f"duplicate managed environment keys: {names}")
    return found


def _preferred_newline(lines: list[str]) -> str:
    for line in lines:
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith("\n"):
            return "\n"
    return "\n"


def _target_values(
    assignments: dict[str, tuple[int, str]],
    *,
    fallback_public_base_url: str,
    fallback_ttl_minutes: int,
) -> dict[str, str]:
    fallback_url = _validated_https_url(fallback_public_base_url)
    fallback_ttl = _validated_ttl(fallback_ttl_minutes)

    existing_url = assignments.get("PRIVACY_EXPORT_PUBLIC_BASE_URL", (-1, ""))[1]
    try:
        public_url = _validated_https_url(existing_url) if existing_url else fallback_url
    except MigrationError:
        public_url = fallback_url

    existing_ttl = assignments.get("PRIVACY_EXPORT_TOKEN_TTL_MINUTES", (-1, ""))[1]
    try:
        ttl = _validated_ttl(existing_ttl) if existing_ttl else fallback_ttl
    except MigrationError:
        ttl = fallback_ttl

    return {
        "PRIVACY_EXPORT_HTTP_ENABLED": "1",
        "PRIVACY_EXPORT_PUBLIC_BASE_URL": public_url,
        "PRIVACY_EXPORT_TOKEN_TTL_MINUTES": str(ttl),
    }


def _render_updated_text(text: str, targets: dict[str, str]) -> str:
    lines = text.splitlines(keepends=True)
    assignments = _active_assignments(lines)
    newline = _preferred_newline(lines)

    for key, value in targets.items():
        existing = assignments.get(key)
        if existing is None:
            continue
        index, _old_value = existing
        matched = _ASSIGNMENT_RE.match(lines[index])
        if matched is None:  # pragma: no cover - guarded by _active_assignments
            raise MigrationError(f"managed key disappeared while rendering: {key}")
        prefix = matched.group("prefix") or ""
        line_ending = matched.group("newline") or newline
        lines[index] = f"{prefix}{key}={value}{line_ending}"

    missing = [key for key in MANAGED_KEYS if key not in assignments]
    if missing:
        if lines and not lines[-1].endswith(("\n", "\r\n")):
            lines[-1] += newline
        if lines and lines[-1].strip():
            lines.append(newline)
        if not any(line.rstrip("\r\n") == _MARKER for line in lines):
            lines.append(f"{_MARKER}{newline}")
        for key in missing:
            lines.append(f"{key}={targets[key]}{newline}")

    return "".join(lines)


def _open_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise MigrationError("cannot open migration lock file") from exc
    os.fchmod(fd, 0o600)
    return fd


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_exclusive_file(path: Path, data: bytes, *, mode: int, uid: int, gid: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        os.fchmod(fd, mode)
        try:
            os.fchown(fd, uid, gid)
        except PermissionError:
            if os.geteuid() == 0:
                raise
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)


def _atomic_replace(path: Path, data: bytes, *, mode: int, uid: int, gid: int) -> None:
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.privacy-export.", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        os.fchmod(fd, mode)
        try:
            os.fchown(fd, uid, gid)
        except PermissionError:
            if os.geteuid() == 0:
                raise
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(fd)
        fd = -1
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)


def _backup_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.name}.bak.privacy-export.{stamp}.{os.getpid()}")


def migrate_env_file(
    env_file: Path,
    *,
    fallback_public_base_url: str,
    fallback_ttl_minutes: int = 10,
) -> MigrationResult:
    path = Path(env_file).expanduser()
    if not path.is_absolute():
        raise MigrationError("environment file path must be absolute")
    if path.is_symlink():
        raise MigrationError("environment file must not be a symbolic link")
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise MigrationError("environment file does not exist") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise MigrationError("environment file must be a regular file")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & stat.S_IWOTH:
        raise MigrationError("environment file must not be world-writable")

    lock_path = path.with_name(f".{path.name}.privacy-export.lock")
    lock_fd = _open_lock(lock_path)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if path.is_symlink():
            raise MigrationError("environment file became a symbolic link")
        current_metadata = path.stat()
        if not stat.S_ISREG(current_metadata.st_mode):
            raise MigrationError("environment file is no longer regular")
        current_mode = stat.S_IMODE(current_metadata.st_mode)
        if current_mode & stat.S_IWOTH:
            raise MigrationError("environment file became world-writable")
        original = path.read_bytes()
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError("environment file must be valid UTF-8") from exc

        assignments = _active_assignments(text.splitlines(keepends=True))
        targets = _target_values(
            assignments,
            fallback_public_base_url=fallback_public_base_url,
            fallback_ttl_minutes=fallback_ttl_minutes,
        )
        updated_text = _render_updated_text(text, targets)
        updated = updated_text.encode("utf-8")
        if updated == original:
            return MigrationResult(
                changed=False,
                backup_path=None,
                public_base_url=targets["PRIVACY_EXPORT_PUBLIC_BASE_URL"],
                ttl_minutes=int(targets["PRIVACY_EXPORT_TOKEN_TTL_MINUTES"]),
            )

        backup = _backup_path(path)
        _write_exclusive_file(
            backup,
            original,
            mode=current_mode,
            uid=current_metadata.st_uid,
            gid=current_metadata.st_gid,
        )
        try:
            _atomic_replace(
                path,
                updated,
                mode=current_mode,
                uid=current_metadata.st_uid,
                gid=current_metadata.st_gid,
            )

            verified_text = path.read_text(encoding="utf-8")
            verified_assignments = _active_assignments(verified_text.splitlines(keepends=True))
            for key, expected in targets.items():
                actual = verified_assignments.get(key, (-1, ""))[1]
                if actual != expected:
                    raise MigrationError(f"post-write verification failed for {key}")
        except (MigrationError, OSError, UnicodeError) as exc:
            try:
                _atomic_replace(
                    path,
                    original,
                    mode=current_mode,
                    uid=current_metadata.st_uid,
                    gid=current_metadata.st_gid,
                )
            except OSError as rollback_exc:
                raise MigrationError(
                    "environment migration failed and automatic rollback also failed"
                ) from rollback_exc
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError("post-write verification failed") from exc
        return MigrationResult(
            changed=True,
            backup_path=backup,
            public_base_url=targets["PRIVACY_EXPORT_PUBLIC_BASE_URL"],
            ttl_minutes=int(targets["PRIVACY_EXPORT_TOKEN_TTL_MINUTES"]),
        )
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        default="/etc/clientplatform/clientplatform.env",
        help="absolute path to the existing authoritative environment file",
    )
    parser.add_argument(
        "--public-base-url",
        default="https://clientplatform-bot.clientplatform.ru",
        help="safe HTTPS fallback used only when the existing privacy URL is missing or invalid",
    )
    parser.add_argument(
        "--ttl-minutes",
        type=int,
        default=10,
        help="safe fallback TTL in minutes; accepted range is 2..30",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = migrate_env_file(
            Path(args.env_file),
            fallback_public_base_url=args.public_base_url,
            fallback_ttl_minutes=args.ttl_minutes,
        )
    except MigrationError as exc:
        print(f"PRIVACY_EXPORT_ENV_MIGRATION_FAILED: {exc}", file=sys.stderr)
        return 2
    backup = str(result.backup_path) if result.backup_path is not None else "none"
    print(
        "PRIVACY_EXPORT_ENV_MIGRATION_OK "
        f"changed={int(result.changed)} backup={backup} "
        f"public_base_url={result.public_base_url} ttl_minutes={result.ttl_minutes}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
