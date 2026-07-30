from __future__ import annotations

"""Authenticated age encryption for ClientPlatform PostgreSQL backup bundles.

Backup workers receive only a public age recipient. The private identity required
for decryption is intentionally operator-supplied during restore and must not be
stored in the regular application environment.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

_AGE_RECIPIENT_RE = re.compile(r"^age1[0-9a-z]{50,100}$")
_BACKUP_NAME_RE = re.compile(r"^clientplatform-(\d{8})T(\d{6})Z\.dump$")
_ENCRYPTED_NAME_RE = re.compile(r"^clientplatform-(\d{8})T(\d{6})Z\.dump\.age$")
RunCommand = Callable[[Sequence[str]], None]


@dataclass(frozen=True, slots=True)
class EncryptedBackupBundle:
    ciphertext: Path
    checksum: Path
    metadata: Path
    ciphertext_sha256: str
    plaintext_sha256: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_age(command: Sequence[str]) -> None:
    completed = subprocess.run(  # nosec B603 - fixed age executable and reviewed arguments
        list(command),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"age failed with exit code {completed.returncode}")


def _recipient(value: str) -> str:
    selected = str(value or "").strip()
    if not _AGE_RECIPIENT_RE.fullmatch(selected):
        raise ValueError("CLIENTPLATFORM_BACKUP_AGE_RECIPIENT must be an X25519 age recipient")
    return selected


def recipient_from_env(env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    return _recipient(str(values.get("CLIENTPLATFORM_BACKUP_AGE_RECIPIENT") or ""))


def _plaintext_bundle(dump_path: Path) -> tuple[Path, Path, Path, dict[str, object], str]:
    dump = dump_path.expanduser().resolve(strict=True)
    if not dump.is_file() or _BACKUP_NAME_RE.fullmatch(dump.name) is None:
        raise ValueError("invalid ClientPlatform PostgreSQL dump path")
    checksum = dump.with_suffix(dump.suffix + ".sha256")
    metadata = dump.with_suffix(dump.suffix + ".json")
    if not checksum.is_file() or not metadata.is_file():
        raise ValueError("plaintext PostgreSQL backup bundle is incomplete")
    parts = checksum.read_text(encoding="utf-8").split()
    if len(parts) < 2 or parts[1] != dump.name:
        raise ValueError("plaintext PostgreSQL checksum manifest is invalid")
    actual = _sha256_file(dump)
    if parts[0].lower() != actual:
        raise ValueError("plaintext PostgreSQL backup checksum mismatch")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("plaintext PostgreSQL backup metadata must be an object")
    if payload.get("dump_file") != dump.name or payload.get("sha256") != actual:
        raise ValueError("plaintext PostgreSQL backup metadata does not match the dump")
    if not str(payload.get("source_database") or "").startswith("clientplatform"):
        raise ValueError("refusing to encrypt a non-ClientPlatform backup")
    return dump, checksum, metadata, payload, actual


def _encrypted_bundle(ciphertext_path: Path) -> tuple[Path, Path, Path, dict[str, object]]:
    ciphertext = ciphertext_path.expanduser().resolve(strict=True)
    if not ciphertext.is_file() or _ENCRYPTED_NAME_RE.fullmatch(ciphertext.name) is None:
        raise ValueError("invalid encrypted ClientPlatform PostgreSQL backup path")
    checksum = ciphertext.with_suffix(ciphertext.suffix + ".sha256")
    metadata = ciphertext.with_suffix(ciphertext.suffix + ".json")
    if not checksum.is_file() or not metadata.is_file():
        raise ValueError("encrypted PostgreSQL backup bundle is incomplete")
    parts = checksum.read_text(encoding="utf-8").split()
    actual = _sha256_file(ciphertext)
    if len(parts) < 2 or parts[1] != ciphertext.name or parts[0].lower() != actual:
        raise ValueError("encrypted PostgreSQL backup checksum mismatch")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("encrypted PostgreSQL backup metadata must be an object")
    encryption = payload.get("encryption")
    if (
        payload.get("encrypted_file") != ciphertext.name
        or payload.get("ciphertext_sha256") != actual
        or not isinstance(encryption, dict)
        or encryption.get("format") != "age-x25519"
    ):
        raise ValueError("encrypted PostgreSQL backup metadata does not match the ciphertext")
    if not str(payload.get("source_database") or "").startswith("clientplatform"):
        raise ValueError("refusing to decrypt a non-ClientPlatform backup")
    return ciphertext, checksum, metadata, payload


def encrypt_backup_bundle(
    dump_path: Path,
    *,
    recipient: str,
    run_command: RunCommand = _run_age,
    remove_plaintext: bool = True,
    now: float | None = None,
) -> EncryptedBackupBundle:
    selected_recipient = _recipient(recipient)
    dump, plaintext_checksum, plaintext_metadata, source_metadata, plaintext_sha = (
        _plaintext_bundle(dump_path)
    )
    ciphertext = dump.with_suffix(dump.suffix + ".age")
    partial = ciphertext.with_suffix(ciphertext.suffix + ".partial")
    checksum = ciphertext.with_suffix(ciphertext.suffix + ".sha256")
    metadata = ciphertext.with_suffix(ciphertext.suffix + ".json")
    partial.unlink(missing_ok=True)
    try:
        run_command(
            (
                "age",
                "--encrypt",
                "--recipient",
                selected_recipient,
                "--output",
                str(partial),
                str(dump),
            )
        )
        if not partial.is_file() or partial.stat().st_size <= 0:
            raise RuntimeError("age did not create encrypted backup output")
        partial.chmod(0o600)
        partial.replace(ciphertext)
        ciphertext.chmod(0o600)
        ciphertext_sha = _sha256_file(ciphertext)
        checksum.write_text(f"{ciphertext_sha}  {ciphertext.name}\n", encoding="utf-8")
        checksum.chmod(0o600)
        created_at = datetime.fromtimestamp(now or time.time(), tz=timezone.utc).isoformat(
            timespec="seconds"
        )
        metadata.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "created_at": created_at,
                    "source_database": source_metadata["source_database"],
                    "encrypted_file": ciphertext.name,
                    "ciphertext_sha256": ciphertext_sha,
                    "plaintext_dump_file": dump.name,
                    "plaintext_sha256": plaintext_sha,
                    "encryption": {
                        "format": "age-x25519",
                        "recipient_fingerprint": hashlib.sha256(
                            selected_recipient.encode("ascii")
                        ).hexdigest(),
                    },
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        metadata.chmod(0o600)
    except (OSError, RuntimeError, ValueError, TypeError):
        partial.unlink(missing_ok=True)
        ciphertext.unlink(missing_ok=True)
        checksum.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)
        raise

    if remove_plaintext:
        dump.unlink(missing_ok=True)
        plaintext_checksum.unlink(missing_ok=True)
        plaintext_metadata.unlink(missing_ok=True)
    return EncryptedBackupBundle(
        ciphertext=ciphertext,
        checksum=checksum,
        metadata=metadata,
        ciphertext_sha256=ciphertext_sha,
        plaintext_sha256=plaintext_sha,
    )


def decrypt_backup_bundle(
    ciphertext_path: Path,
    *,
    identity_file: Path,
    output_path: Path,
    run_command: RunCommand = _run_age,
) -> Path:
    ciphertext, _, _, metadata = _encrypted_bundle(ciphertext_path)
    identity = identity_file.expanduser().resolve(strict=True)
    if not identity.is_file():
        raise ValueError("age identity path must be a regular file")
    if os.stat(identity).st_mode & 0o077:
        raise ValueError("age identity file must not be accessible to group or other users")
    output = output_path.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".partial")
    partial.unlink(missing_ok=True)
    try:
        run_command(
            (
                "age",
                "--decrypt",
                "--identity",
                str(identity),
                "--output",
                str(partial),
                str(ciphertext),
            )
        )
        if not partial.is_file() or partial.stat().st_size <= 0:
            raise RuntimeError("age did not create decrypted backup output")
        if _sha256_file(partial) != metadata.get("plaintext_sha256"):
            raise ValueError("decrypted PostgreSQL backup checksum mismatch")
        partial.chmod(0o600)
        partial.replace(output)
        output.chmod(0o600)
        return output
    except (OSError, RuntimeError, ValueError, TypeError):
        partial.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    encrypt = subparsers.add_parser("encrypt")
    encrypt.add_argument("dump", type=Path)
    encrypt.add_argument("--keep-plaintext", action="store_true")
    decrypt = subparsers.add_parser("decrypt")
    decrypt.add_argument("ciphertext", type=Path)
    decrypt.add_argument("--identity", required=True, type=Path)
    decrypt.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.command == "encrypt":
        bundle = encrypt_backup_bundle(
            args.dump,
            recipient=recipient_from_env(),
            remove_plaintext=not args.keep_plaintext,
        )
        print(f"CLIENTPLATFORM_BACKUP_ENCRYPTED_OK:{bundle.ciphertext}")
        return 0
    output = decrypt_backup_bundle(
        args.ciphertext,
        identity_file=args.identity,
        output_path=args.output,
    )
    print(f"CLIENTPLATFORM_BACKUP_DECRYPTED_OK:{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
