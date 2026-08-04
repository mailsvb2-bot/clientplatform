from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_encrypted_backup_documentation_keeps_identity_outside_runtime_env() -> None:
    documentation = (
        ROOT / "docs/operations/clientplatform-backup-age.md"
    ).read_text(encoding="utf-8")
    assert "configure-backup-age.sh" in documentation
    assert "Do not place it in Git" in documentation
    assert "same backup bucket" in documentation
