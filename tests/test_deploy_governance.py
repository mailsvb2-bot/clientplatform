from pathlib import Path

from scripts.check_deploy_governance import deploy_governance_problems


def test_production_deploy_is_read_only_toward_github() -> None:
    assert deploy_governance_problems() == []


def test_deploy_governance_rejects_server_pushes(tmp_path: Path) -> None:
    wrapper = tmp_path / "deploy.sh"
    production = tmp_path / "clientplatform_production_deploy.py"
    wrapper.write_text(
        'exec python3 "$ROOT/scripts/clientplatform_production_deploy.py" "$@"\n',
        encoding="utf-8",
    )
    production.write_text("git push origin main\n", encoding="utf-8")

    problems = deploy_governance_problems(wrapper, production)

    assert "production deploy must not push to GitHub" in problems


def test_deploy_governance_requires_canonical_runtime_safety(tmp_path: Path) -> None:
    wrapper = tmp_path / "deploy.sh"
    production = tmp_path / "clientplatform_production_deploy.py"
    wrapper.write_text("echo unsafe-wrapper\n", encoding="utf-8")
    production.write_text("print('unsafe deploy')\n", encoding="utf-8")

    problems = deploy_governance_problems(wrapper, production)

    assert (
        "deploy wrapper does not delegate to ClientPlatform production deploy" in problems
    )
    assert "canonical production deploy lock is missing" in problems
    assert "encrypted pre-deploy backup path is missing" in problems
    assert "readiness gate is missing" in problems
    assert "rollback implementation is missing" in problems


def test_deploy_governance_rejects_mutable_rollback(tmp_path: Path) -> None:
    wrapper = tmp_path / "deploy.sh"
    production = tmp_path / "clientplatform_production_deploy.py"
    wrapper.write_text(
        'exec python3 "$ROOT/scripts/clientplatform_production_deploy.py" "$@"\n',
        encoding="utf-8",
    )
    production.write_text("git reset --hard old-sha\n", encoding="utf-8")

    problems = deploy_governance_problems(wrapper, production)

    assert "runtime rollback must not mutate the source checkout" in problems


def test_deploy_governance_rejects_legacy_runtime_identity(tmp_path: Path) -> None:
    wrapper = tmp_path / "deploy.sh"
    production = tmp_path / "clientplatform_production_deploy.py"
    wrapper.write_text(
        'exec python3 "$ROOT/scripts/clientplatform_production_deploy.py" "$@"\n',
        encoding="utf-8",
    )
    production.write_text("SERVICE_NAME=clientplatform.service\n", encoding="utf-8")

    problems = deploy_governance_problems(wrapper, production)

    assert "obsolete systemd identity remains" in problems
