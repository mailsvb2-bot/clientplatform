#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG_DIR="${LOG_DIR:-/tmp/clientplatform_quality_logs}"
VENV_DIR="${VENV_DIR:-/tmp/clientplatform_quality_venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
mkdir -p "$LOG_DIR"

cleanup_local_artifacts() {
  rm -rf .mypy_cache .ruff_cache .pytest_cache __pycache__
  find . -path './.git' -prune -o -path './.venv' -prune -o -path './venv' -prune -o -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
  find . -path './.git' -prune -o -path './.venv' -prune -o -path './venv' -prune -o \( -name '*.pyc' -o -name '*.pyo' \) -type f -delete 2>/dev/null || true
}

run_step() {
  local name="$1"
  shift
  echo "==> $name"
  set +e
  "$@" >"$LOG_DIR/${name}.log" 2>&1
  local code=$?
  set -e
  if [ "$code" -ne 0 ]; then
    echo "FAIL: $name exit=$code"
    echo "--- tail $LOG_DIR/${name}.log ---"
    tail -n 80 "$LOG_DIR/${name}.log" || true
    echo "--- end tail ---"
    return "$code"
  fi
  echo "OK: $name"
}

is_placeholder_database_url() {
  local raw="${DATABASE_URL:-}"
  [ -z "$raw" ] && return 0
  case "$raw" in
    *USER*|*PASSWORD*|*HOST*|*DBNAME*|*example*|*localhost-placeholder*) return 0 ;;
    *) return 1 ;;
  esac
}

if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

python -m pip install --disable-pip-version-check pip==26.1.2 >"$LOG_DIR/pip_upgrade.log" 2>&1
python -m pip install --require-hashes -r requirements-dev.txt >"$LOG_DIR/pip_install.log" 2>&1

export PYTHONDONTWRITEBYTECODE=1

cleanup_local_artifacts

failures=0
run_step release_hygiene_before python scripts/check_release_hygiene.py || failures=$((failures+1))
run_step compile_project python -m compileall services scripts handlers core runtime config app.py main.py || failures=$((failures+1))
cleanup_local_artifacts

APP_ENV=test \
CLIENTPLATFORM_DB_ENGINE=sqlite \
DATABASE_URL= \
VALIDATOR_RELEASE_MODE=1 \
VALIDATOR_GUARDRAILS_STRICT=1 \
ADMIN_IDS=1 \
run_step smoke_sqlite python scripts/smoke.py || failures=$((failures+1))
cleanup_local_artifacts

APP_ENV=test LOAD_DOTENV=0 CLIENTPLATFORM_DB_ENGINE=sqlite DATABASE_URL= run_step pytest python -m pytest -q -p no:cacheprovider || failures=$((failures+1))
cleanup_local_artifacts

APP_ENV=test \
CLIENTPLATFORM_DB_ENGINE=sqlite \
DATABASE_URL= \
VALIDATOR_RELEASE_MODE=1 \
VALIDATOR_GUARDRAILS_STRICT=1 \
ADMIN_IDS=1 \
run_step strict_validation python scripts/validate_project.py || failures=$((failures+1))
cleanup_local_artifacts

APP_ENV=prod \
CLIENTPLATFORM_DB_ENGINE=postgres \
DATABASE_URL=postgresql://quality-check/clientplatform \
VALIDATOR_RELEASE_MODE=1 \
VALIDATOR_GUARDRAILS_STRICT=1 \
TELEGRAM_TRANSPORT=polling \
TELEGRAM_WEBHOOK_ENABLED=0 \
TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED=0 \
ADMIN_IDS=1 \
run_step prod_contract python -c 'from services.validators.prod import validate_prod_guardrails; validate_prod_guardrails(strict=True)' || failures=$((failures+1))
cleanup_local_artifacts

run_step ruff python scripts/check_ruff.py || failures=$((failures+1))
cleanup_local_artifacts
run_step critical_static_mypy python scripts/critical_static_gate.py mypy || failures=$((failures+1))
cleanup_local_artifacts
run_step critical_static_bandit python scripts/critical_static_gate.py bandit || failures=$((failures+1))
cleanup_local_artifacts
run_step pip_audit python -m pip_audit -r requirements.txt --progress-spinner off || failures=$((failures+1))
cleanup_local_artifacts
run_step release_hygiene_after python scripts/check_release_hygiene.py || failures=$((failures+1))

if ! is_placeholder_database_url; then
  APP_ENV=prod \
  CLIENTPLATFORM_DB_ENGINE=postgres \
  VALIDATOR_RELEASE_MODE=1 \
  VALIDATOR_GUARDRAILS_STRICT=1 \
  ADMIN_IDS=1 \
  run_step smoke_postgres python scripts/smoke.py || failures=$((failures+1))
else
  echo "SKIP: smoke_postgres because DATABASE_URL is empty or placeholder"
fi

cleanup_local_artifacts

if [ "$failures" -ne 0 ]; then
  echo "QUALITY GATE FAILED: $failures step(s). Logs: $LOG_DIR"
  exit 2
fi

echo "QUALITY GATE OK. Logs: $LOG_DIR"
