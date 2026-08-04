from __future__ import annotations

import subprocess
from pathlib import Path


def _script() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "clientplatform"
        / "update-production.sh"
    )


def test_update_production_shell_is_syntactically_valid() -> None:
    subprocess.run(["sh", "-n", str(_script())], check=True)


def test_update_production_waits_for_stability_and_fails_closed() -> None:
    source = _script().read_text(encoding="utf-8")

    assert 'python3 -m scripts.clientplatform_production_deploy "$@"' in source
    assert "CLIENTPLATFORM_UPDATE_STABILITY_OK" in source
    assert "post_deploy_application_crashed" in source
    assert "post_deploy_container_restarted" in source
    assert "post_deploy_readiness_lost" in source
    assert "production_post_deploy_rollback" in source
    assert "CLIENTPLATFORM_PRODUCTION_POST_DEPLOY_ROLLBACK_OK" in source
