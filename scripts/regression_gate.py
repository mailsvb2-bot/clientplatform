from __future__ import annotations

"""Canonical one-command regression contour for CI and local release checks."""

import os
import shlex
import shutil
# Reviewed: CI/local release gate invokes fixed Python commands, no user-controlled shell.
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_SURFACE = (
    "services",
    "scripts",
    "handlers",
    "core",
    "runtime",
    "config",
    "app.py",
    "main.py",
)
GENERATED_PYTHON_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
GENERATED_FILE_SUFFIXES = {".pyc", ".pyo"}
VIRTUALENV_DIR_NAMES = {".venv", "venv", "env"}
VIRTUALENV_DIR_PREFIXES = (".venv-", "venv-", "env-")
SKIP_CLEANUP_DIRS = {".git", *VIRTUALENV_DIR_NAMES}
PROD_ENV_FILE = Path(os.environ.get("CLIENTPLATFORM_PROD_ENV_FILE", "/etc/clientplatform/clientplatform.env"))
FULL_GATE_PROD_OVERRIDE = "ALLOW_FULL_REGRESSION_ON_PROD"


@dataclass(frozen=True)
class GateStep:
    name: str
    cmd: tuple[str, ...]
    env: dict[str, str] | None = None
    env_file: Path | None = None
    skip_if_missing_env_file: bool = False


BASE_ENV = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "LOAD_DOTENV": "0",
    "TELEGRAM_TRANSPORT": "polling",
    "TELEGRAM_WEBHOOK_ENABLED": "0",
    "TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED": "0",
}

STRICT_VALIDATOR_ENV = {
    "APP_ENV": "test",
    "VALIDATOR_RELEASE_MODE": "1",
    "VALIDATOR_GUARDRAILS_STRICT": "1",
}

PYTEST_ENV = {
    "APP_ENV": "test",
    "LOAD_DOTENV": "0",
}

PROD_LIKE_VALIDATOR_ENV = {
    "LOAD_DOTENV": "0",
    "VALIDATOR_RELEASE_MODE": "1",
    "VALIDATOR_GUARDRAILS_STRICT": "1",
}

HERMETIC_PROD_VALIDATOR_ENV = {
    **PROD_LIKE_VALIDATOR_ENV,
    "APP_ENV": "prod",
    "CLIENTPLATFORM_DB_ENGINE": "postgres",
    "DATABASE_URL": "postgresql://ci:ci@127.0.0.1:5432/clientplatform_ci_contract",
    "BOT_TOKEN": "000000:CI",
    "ADMIN_IDS": "1",
    "TELEGRAM_TRANSPORT": "polling",
    "TELEGRAM_WEBHOOK_ENABLED": "0",
    "TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED": "0",
    "PRIVACY_EXPORT_HTTP_ENABLED": "1",
}

STEPS = (
    GateStep(
        "release hygiene before checks",
        (sys.executable, "scripts/check_release_hygiene.py"),
    ),
    GateStep(
        "compile project surface",
        (sys.executable, "-m", "compileall", *PROJECT_SURFACE),
    ),
    GateStep(
        "hermetic smoke no polling",
        (sys.executable, "scripts/smoke.py"),
        STRICT_VALIDATOR_ENV,
    ),
    GateStep(
        "canonical ClientPlatform user scenario matrix",
        (sys.executable, "scripts/all_user_scenario_gate.py"),
        STRICT_VALIDATOR_ENV,
    ),
    GateStep(
        "full pytest regression gate",
        (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"),
        PYTEST_ENV,
    ),
    GateStep(
        "strict validation",
        (sys.executable, "scripts/validate_project.py"),
        STRICT_VALIDATOR_ENV,
    ),
    GateStep(
        "hermetic production contract validation",
        (
            sys.executable,
            "-c",
            "from services.validators.prod import validate_prod_guardrails; "
            "validate_prod_guardrails(strict=True); "
            "print('HERMETIC_PROD_CONTRACT_OK')",
        ),
        HERMETIC_PROD_VALIDATOR_ENV,
    ),
    GateStep(
        "optional prod-config validation",
        (sys.executable, "scripts/validate_project.py"),
        PROD_LIKE_VALIDATOR_ENV,
        env_file=PROD_ENV_FILE,
        skip_if_missing_env_file=True,
    ),
    GateStep(
        "ruff quality gate",
        (sys.executable, "scripts/check_ruff.py"),
    ),
    GateStep(
        "release hygiene after checks",
        (sys.executable, "scripts/check_release_hygiene.py"),
    ),
)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_env_file(path: Path) -> dict[str, str]:
    loaded: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = shlex.split(line, comments=True, posix=True)
        if not tokens:
            continue
        if tokens[0] == "export":
            tokens = tokens[1:]
        for token in tokens:
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            key = key.strip()
            if key:
                loaded[key] = value
    return loaded


def _is_live_prod_host() -> bool:
    """Detect a real production deployment and refuse heavy checks by default.

    Production identity is derived from the deployment contract, not from a
    hard-coded checkout path. This keeps the safety guard effective for /opt,
    containers, relocated systemd deployments and future host layouts.
    """
    if _truthy(os.getenv("CI")):
        return False
    if _truthy(os.getenv(FULL_GATE_PROD_OVERRIDE)):
        return False
    if not PROD_ENV_FILE.exists():
        return False

    try:
        file_env = _load_env_file(PROD_ENV_FILE)
    except OSError:
        return False
    app_env = (os.getenv("APP_ENV") or file_env.get("APP_ENV") or "").strip().lower()
    db_engine = (os.getenv("CLIENTPLATFORM_DB_ENGINE") or file_env.get("CLIENTPLATFORM_DB_ENGINE") or "").strip().lower()
    database_url = (os.getenv("DATABASE_URL") or file_env.get("DATABASE_URL") or "").strip().lower()
    postgres = db_engine in {"postgres", "postgresql", "pg"} or database_url.startswith(("postgresql://", "postgres://"))
    return app_env in {"prod", "production"} and postgres


def _guard_live_prod_host() -> int:
    if not _is_live_prod_host():
        return 0
    print(
        "REGRESSION_GATE_REFUSED_ON_LIVE_PROD: full pytest/regression is disabled on the live production deployment.\n"
        "Use lightweight production checks instead: scripts/clientplatform_sales_production_smoke.py, healthz, readyz, "
        "and scripts/post_deploy_verify.py --skip-pytest.\n"
        f"Emergency override, only with an approved maintenance window: {FULL_GATE_PROD_OVERRIDE}=1",
        flush=True,
    )
    return 2


def _is_local_virtualenv_dir(dirname: str) -> bool:
    return dirname in VIRTUALENV_DIR_NAMES or dirname.startswith(VIRTUALENV_DIR_PREFIXES)


def _cleanup_generated_python_artifacts() -> None:
    """Remove artifacts created by local focused tests, compileall and pytest.

    The release hygiene gate must still catch real shippable garbage such as DBs,
    logs or build fragments. Python bytecode and test/linter caches are deterministic
    local by-products of the gate itself, so the contour owns their cleanup.
    """
    for current, dirs, files in os.walk(ROOT):
        current_path = Path(current)
        dirs[:] = [name for name in dirs if name not in SKIP_CLEANUP_DIRS and not _is_local_virtualenv_dir(name)]
        for dirname in list(dirs):
            if dirname in GENERATED_PYTHON_DIRS:
                shutil.rmtree(current_path / dirname, ignore_errors=True)
                dirs.remove(dirname)
        for filename in files:
            if Path(filename).suffix in GENERATED_FILE_SUFFIXES:
                try:
                    (current_path / filename).unlink()
                except FileNotFoundError:
                    pass


def _run(step: GateStep) -> int:
    if step.name.startswith("release hygiene"):
        _cleanup_generated_python_artifacts()

    env = os.environ.copy()
    env.update(BASE_ENV)
    if step.env_file is not None:
        if not step.env_file.exists():
            if step.skip_if_missing_env_file:
                print(f"==> {step.name}", flush=True)
                print(f"skipped: env file not found: {step.env_file}", flush=True)
                return 0
            print(f"REGRESSION_GATE_FAILED step={step.name!r} missing_env_file={step.env_file}", flush=True)
            return 2
        env.update(_load_env_file(step.env_file))
    if step.env:
        env.update(step.env)

    print(f"==> {step.name}", flush=True)
    print("cmd:", " ".join(step.cmd), flush=True)
    if step.env_file is not None:
        print(f"env-file: {step.env_file}", flush=True)
    # Reviewed: each gate command is declared statically in STEPS and executed without shell.
    completed = subprocess.run(step.cmd, cwd=ROOT, env=env, check=False)  # nosec B603
    if completed.returncode != 0:
        print(f"REGRESSION_GATE_FAILED step={step.name!r} code={completed.returncode}", flush=True)
        return int(completed.returncode)
    return 0


def main() -> int:
    guard_code = _guard_live_prod_host()
    if guard_code:
        return guard_code
    for step in STEPS:
        code = _run(step)
        if code:
            return code
    print("REGRESSION_GATE_OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
