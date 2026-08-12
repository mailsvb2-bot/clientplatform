from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_max_trust.sh"
ROOT_CERT = ROOT / "deploy" / "certs" / "russian_trusted_root_ca.crt"
SUB_CERT = ROOT / "deploy" / "certs" / "russian_trusted_sub_ca.crt"
ROOT_FINGERPRINT = "D26D2D0231B7C39F92CC738512BA54103519E4405D68B5BD703E9788CA8ECF31"
SUB_FINGERPRINT = "BBBDE2103E790B999EC62BD03CF625A5A2E7C316E10AFE6A490EEDEAD8B3FD9B"


def _openssl_fingerprint(path: Path) -> str:
    result = subprocess.run(
        ["openssl", "x509", "-in", str(path), "-noout", "-fingerprint", "-sha256"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return result.stdout.strip().split("=", 1)[1].replace(":", "").upper()


def test_max_trust_installer_shell_is_valid() -> None:
    result = subprocess.run(
        ["bash", "-n", str(INSTALLER)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, f"{INSTALLER}: {result.stderr}"


def test_vendored_max_certificates_have_expected_der_fingerprints() -> None:
    assert ROOT_CERT.is_file()
    assert SUB_CERT.is_file()
    assert _openssl_fingerprint(ROOT_CERT) == ROOT_FINGERPRINT
    assert _openssl_fingerprint(SUB_CERT) == SUB_FINGERPRINT

    chain = subprocess.run(
        ["openssl", "verify", "-CAfile", str(ROOT_CERT), str(SUB_CERT)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert chain.returncode == 0, chain.stderr


def test_max_trust_installer_verifies_vendored_certs_without_system_changes() -> None:
    env = os.environ.copy()
    env["MAX_TRUST_VERIFY_ONLY"] = "1"
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert "MAX_API2_TRUST_CERTS_OK" in result.stdout
    assert ROOT_FINGERPRINT in result.stdout
    assert SUB_FINGERPRINT in result.stdout


def test_max_trust_installer_uses_der_fingerprints_and_never_disables_tls() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert "deploy/certs/russian_trusted_root_ca.crt" in source
    assert "deploy/certs/russian_trusted_sub_ca.crt" in source
    assert ROOT_FINGERPRINT in source
    assert SUB_FINGERPRINT in source
    assert "-fingerprint -sha256" in source
    assert "CN=Russian Trusted Root CA" in source
    assert "CN=Russian Trusted Sub CA" in source
    assert "platform-api2.max.ru/me" in source
    assert "curl" not in source
    assert "--insecure" not in source
    assert "CERT_NONE" not in source
    assert "check_hostname = False" not in source
