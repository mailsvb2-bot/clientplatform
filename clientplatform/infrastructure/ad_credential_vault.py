from __future__ import annotations

import base64
import os
import stat
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
    """Seal per-business OAuth material with a separately provisioned age identity."""

    _PREFIX = "age-v1:"

    def __init__(self, identity_path: str | Path | None = None) -> None:
        configured = identity_path or os.getenv(
            "CLIENTPLATFORM_AD_CREDENTIAL_IDENTITY_FILE",
            "/run/secrets/clientplatform-ad/identity.txt",
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
        try:
            self._identity_path.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise AdCredentialVaultError("credential identity cannot be inspected") from exc
        else:
            self._assert_private_identity()
            return

        if _deployed_environment() or not _allow_identity_generation():
            raise AdCredentialVaultError(
                "advertising credential identity must be provisioned before startup"
            )
        self._identity_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self._identity_path.parent, 0o700)
        self._assert_private_directory()
        temporary = self._identity_path.with_suffix(".tmp")
        temporary.unlink(missing_ok=True)
        completed = subprocess.run(
            ["age-keygen", "-o", str(temporary)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0:
            temporary.unlink(missing_ok=True)
            raise AdCredentialVaultError("credential identity generation failed")
        try:
            temporary_stat = temporary.lstat()
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise AdCredentialVaultError("credential identity generation failed") from exc
        if not stat.S_ISREG(temporary_stat.st_mode) or stat.S_ISLNK(
            temporary_stat.st_mode
        ):
            temporary.unlink(missing_ok=True)
            raise AdCredentialVaultError("credential identity generation was unsafe")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self._identity_path)
        self._assert_private_identity()

    def _assert_private_directory(self) -> None:
        try:
            directory_stat = self._identity_path.parent.lstat()
        except OSError as exc:
            raise AdCredentialVaultError(
                "credential identity directory cannot be inspected"
            ) from exc
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(
            directory_stat.st_mode
        ):
            raise AdCredentialVaultError("credential identity directory is unsafe")
        if directory_stat.st_mode & 0o777 != 0o700:
            raise AdCredentialVaultError(
                "credential identity directory permissions must be 0700"
            )
        if hasattr(os, "geteuid") and directory_stat.st_uid != os.geteuid():
            raise AdCredentialVaultError("credential identity directory owner is invalid")

    def _assert_private_identity(self) -> None:
        self._assert_private_directory()
        try:
            identity_stat = self._identity_path.lstat()
        except OSError as exc:
            raise AdCredentialVaultError("credential identity cannot be inspected") from exc
        if stat.S_ISLNK(identity_stat.st_mode) or not stat.S_ISREG(identity_stat.st_mode):
            raise AdCredentialVaultError("credential identity must be a regular file")
        if identity_stat.st_size <= 0:
            raise AdCredentialVaultError("credential identity must not be empty")
        if identity_stat.st_mode & 0o777 != 0o600:
            raise AdCredentialVaultError("credential identity permissions must be 0600")
        if hasattr(os, "geteuid") and identity_stat.st_uid != os.geteuid():
            raise AdCredentialVaultError("credential identity owner is invalid")


def _deployed_environment() -> bool:
    return (os.getenv("APP_ENV") or "dev").strip().lower() in {
        "prod",
        "production",
        "stage",
        "staging",
    }


def _allow_identity_generation() -> bool:
    return (
        os.getenv("CLIENTPLATFORM_AD_CREDENTIAL_ALLOW_GENERATE") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "AdCredentialVault",
    "AdCredentialVaultError",
    "AgeAdCredentialVault",
    "InMemoryAdCredentialVault",
]
