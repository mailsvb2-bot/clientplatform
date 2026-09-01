from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP_CONFIGURATOR = ROOT / "deploy/clientplatform/configure-backup-age.sh"


def test_health_probe_uses_clientplatform_service_identity() -> None:
    source = (ROOT / "runtime/health_server.py").read_text(encoding="utf-8")
    assert "_SERVICE_NAME = 'clientplatform'" in source
    assert "'service': str(payload.get('service') or _SERVICE_NAME)" in source
    assert source.count("'service': _SERVICE_NAME") == 2
    assert "'service': 'clientplatform'" not in source


def test_backup_age_configurator_is_fail_closed_and_syntax_valid() -> None:
    subprocess.run(["sh", "-n", str(BACKUP_CONFIGURATOR)], check=True)
    source = BACKUP_CONFIGURATOR.read_text(encoding="utf-8")
    assert "CLIENTPLATFORM_BACKUP_AGE_RECIPIENT=" in source
    assert "CLIENTPLATFORM_ENCRYPTED_BACKUP_OK:" in source
    assert "CLIENTPLATFORM_ENCRYPTED_BACKUP_VERIFIED_OK:" in source
    assert "CLIENTPLATFORM_BACKUP_AGE_CONFIGURED_OK" in source
    assert "chmod 0600" in source
    assert "--allow-local-backup" not in source
