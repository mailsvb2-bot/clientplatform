from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path
from typing import Protocol


class AdCredentialVaultError(RuntimeError):
    """Credential material could not be sealed or opened safely."""


class AdCredentialVault(Protocol):
    def seal(self, plaintext: str) -> str: ...

    def open(self, ciphertext: str) -> str: ...


class InMemoryAdCredentialVault:
    """Hermetic test vault; production composition must use AgeAdCredentialVault."""

    _PREFIX = "memory-v1:"

    def seal(self, plaintext: str) -> str:
        payload = str(plaintext).encode("utf-8")
        return self._PREFIX + base64.urlsafe_b64encode(payload).decode("ascii")

    def open(self, ciphertext: str) -> str:
        raw = str(ciphertext or "")
        if not raw.startswith(self._PREFIX):
            raise AdCredentialVaultError("credential ciphertext format is invalid")
        try:
            return base64.urlsafe_b64decode(raw.removeprefix(self._PREFIX)).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise AdCredentialVaultError("credential ciphertext is invalid") from exc


class AgeAdCredentialVault:
    """Seal per-business OAuth material with an age identity outside PostgreSQL."""

    _PREFIX = "age-v1:"

    def __init__(self, identity_path: str | Path | None = None) -> None:
        configured = identity_path or os.getenv(
            "CLIENTPLATFORM_AD_CREDENTIAL_IDENTITY_FILE",
            "/var/lib/clientplatform/ad-secrets/identity.txt",
        )
        self._identity_path = Path(configured)

    @property
    def identity_path(self) -> Path:
        return self._identity_path

    def seal(self, plaintext: str) -> str:
        value = str(plaintext)
        if not value:
            raise AdCredentialVaultError("credential plaintext must not be empty")
        recipient = self._recipient()
        completed = subprocess.run(
            ["age", "--encrypt", "--recipient", recipient],
            input=value.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout:
            raise AdCredentialVaultError("credential encryption failed")
        return self._PREFIX + base64.urlsafe_b64encode(completed.stdout).decode("ascii")

    def open(self, ciphertext: str) -> str:
        raw = str(ciphertext or "")
        if not raw.startswith(self._PREFIX):
            raise AdCredentialVaultError("credential ciphertext format is invalid")
        self._ensure_identity()
        try:
            payload = base64.urlsafe_b64decode(raw.removeprefix(self._PREFIX))
        except ValueError as exc:
            raise AdCredentialVaultError("credential ciphertext is invalid") from exc
        completed = subprocess.run(
            ["age", "--decrypt", "--identity", str(self._identity_path)],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0:
            raise AdCredentialVaultError("credential decryption failed")
        try:
            value = completed.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AdCredentialVaultError("credential plaintext encoding is invalid") from exc
        if not value:
            raise AdCredentialVaultError("credential plaintext is empty")
        return value

    def _recipient(self) -> str:
        self._ensure_identity()
        completed = subprocess.run(
            ["age-keygen", "-y", str(self._identity_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        recipient = completed.stdout.strip()
        if completed.returncode != 0 or not recipient.startswith("age1"):
            raise AdCredentialVaultError("credential recipient derivation failed")
        return recipient

    def _ensure_identity(self) -> None:
        if self._identity_path.is_file() and self._identity_path.stat().st_size > 0:
            self._assert_private_permissions()
            return
        self._identity_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self._identity_path.parent, 0o700)
        temporary = self._identity_path.with_suffix(".tmp")
        temporary.unlink(missing_ok=True)
        completed = subprocess.run(
            ["age-keygen", "-o", str(temporary)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0 or not temporary.is_file():
            temporary.unlink(missing_ok=True)
            raise AdCredentialVaultError("credential identity generation failed")
        os.chmod(temporary, 0o600)
        try:
            os.replace(temporary, self._identity_path)
        except FileExistsError:
            temporary.unlink(missing_ok=True)
        self._assert_private_permissions()

    def _assert_private_permissions(self) -> None:
        mode = self._identity_path.stat().st_mode & 0o777
        if mode != 0o600:
            raise AdCredentialVaultError("credential identity permissions must be 0600")


__all__ = [
    "AdCredentialVault",
    "AdCredentialVaultError",
    "AgeAdCredentialVault",
    "InMemoryAdCredentialVault",
]
