#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# -------- helpers --------
die() { echo "❌ $*" >&2; exit 1; }
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "Command not found: $1"; }

require_cmd python

HAS_GIT=1
if ! command -v git >/dev/null 2>&1; then
  HAS_GIT=0
fi

ALLOW_UNTAGGED="${ALLOW_UNTAGGED:-0}"
ALLOW_DIRTY="${ALLOW_DIRTY:-0}"

VERSION=""
PREV_TAG=""

if [[ "$HAS_GIT" == "1" ]]; then
  if [[ "$ALLOW_DIRTY" != "1" ]]; then
    if ! git diff --quiet || ! git diff --cached --quiet; then
      die "Working tree is dirty. Commit changes before release (or set ALLOW_DIRTY=1)."
    fi
  fi

  if VERSION="$(git describe --tags --exact-match 2>/dev/null)"; then
    :
  else
    if [[ "$ALLOW_UNTAGGED" == "1" ]]; then
      SHA="$(git rev-parse --short HEAD)"
      VERSION="dev-${SHA}"
    else
      die "HEAD is not tagged. Create a release tag (e.g., git tag -a v16.0.0 -m '...')"
    fi
  fi

  PREV_TAG="$(git describe --tags --abbrev=0 "${VERSION}^" 2>/dev/null || true)"
else
  VERSION="manual"
  PREV_TAG=""
fi

OUT_DIR="${PROJECT_DIR}/dist"
ARCHIVE_NAME="metr_${VERSION}_prod_clean.zip"
CHANGELOG_NAME="CHANGELOG_${VERSION}.md"

clean_artifacts() {
  find "$PROJECT_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} + || true
  find "$PROJECT_DIR" -type d \( -name ".pytest_cache" -o -name ".ruff_cache" \) -prune -exec rm -rf {} + || true
  find "$PROJECT_DIR" -type f -name "*.pyc" -delete || true
  find "$PROJECT_DIR" -type f -name "*.pyo" -delete || true
  find "$PROJECT_DIR/logs" -type f -name "*.log" -delete 2>/dev/null || true
  rm -f "$PROJECT_DIR/=3.9," || true
  rm -f "$PROJECT_DIR/data.db" "$PROJECT_DIR/data.db-journal" "$PROJECT_DIR/data.db-wal" "$PROJECT_DIR/data.db-shm" || true
  rm -f "$PROJECT_DIR/data/data.db" "$PROJECT_DIR/data/data.db-journal" "$PROJECT_DIR/data/data.db-wal" "$PROJECT_DIR/data/data.db-shm" || true
}

assert_clean_tree() {
  if find "$PROJECT_DIR" -type d -name "__pycache__" | grep -q .; then
    die "__pycache__ found after cleanup. Refusing to build a dirty release."
  fi
  if find "$PROJECT_DIR" -type d \( -name ".pytest_cache" -o -name ".ruff_cache" \) | grep -q .; then
    die "dev/test cache directory found after cleanup. Refusing to build a dirty release."
  fi
  if find "$PROJECT_DIR/logs" -type f -name "*.log" 2>/dev/null | grep -q .; then
    die "runtime log files found after cleanup. Refusing to build a dirty release."
  fi
  if [[ -f "$PROJECT_DIR/=3.9," ]]; then
    die "Suspicious temporary file '=3.9,' found. Refusing to build a dirty release."
  fi
  if [[ -f "$PROJECT_DIR/data/data.db" || -f "$PROJECT_DIR/data.db" ]]; then
    die "Runtime DB artifact found. Refusing to package user data."
  fi
}

echo "▶ Release version: ${VERSION}"
if [[ -n "$PREV_TAG" ]]; then
  echo "▶ Previous tag:    ${PREV_TAG}"
else
  echo "▶ Previous tag:    (none found)"
fi

echo "▶ Cleaning caches, bytecode, logs and local DB artifacts..."
clean_artifacts
assert_clean_tree

echo "▶ Running strict validator (prod)..."
export APP_ENV=prod
export VALIDATOR_RELEASE_MODE=1
export PYTHONDONTWRITEBYTECODE=1
TMP_RELEASE_DB="$(mktemp -u "${TMPDIR:-/tmp}/clientplatform_release_db_XXXXXX.sqlite")"
trap '''rm -f "$TMP_RELEASE_DB" "$TMP_RELEASE_DB-journal" "$TMP_RELEASE_DB-wal" "$TMP_RELEASE_DB-shm"''' EXIT
export CLIENTPLATFORM_DB_PATH="$TMP_RELEASE_DB"
python scripts/validate_project.py

echo "▶ Running smoke checks (no polling)..."
python scripts/smoke.py
unset CLIENTPLATFORM_DB_PATH
rm -f "$TMP_RELEASE_DB" "$TMP_RELEASE_DB-journal" "$TMP_RELEASE_DB-wal" "$TMP_RELEASE_DB-shm"
trap - EXIT

echo "▶ Re-checking cleanliness after validator/smoke..."
clean_artifacts
assert_clean_tree
mkdir -p "$OUT_DIR"

echo "▶ Generating changelog..."
{
  echo "# Changelog ${VERSION}"
  echo
  if [[ "$HAS_GIT" == "1" && -n "$PREV_TAG" ]]; then
    echo "Changes since **${PREV_TAG}**:"
    echo
    git log --no-merges --pretty=format:"- %s (%h)" "${PREV_TAG}..${VERSION}"
    echo
    echo
    echo "## Diff stats"
    echo
    git diff --stat "${PREV_TAG}..${VERSION}"
    echo
  elif [[ "$HAS_GIT" == "1" ]]; then
    echo "First tagged release (no previous tag found)."
    echo
    git log --no-merges --pretty=format:"- %s (%h)" -n 50
    echo
  else
    echo "Git is not available; changelog is not generated from git history."
    echo
  fi
} > "${OUT_DIR}/${CHANGELOG_NAME}"

echo "▶ Packaging archive via clean staging tree..."
python scripts/build_clean_release.py "$PROJECT_DIR" "${OUT_DIR}/${ARCHIVE_NAME}"

echo "✅ Done:"
echo "   - ${OUT_DIR}/${ARCHIVE_NAME}"
echo "   - ${OUT_DIR}/${CHANGELOG_NAME}"
