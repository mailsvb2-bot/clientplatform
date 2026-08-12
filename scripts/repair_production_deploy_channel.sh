#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/clientplatform}"
REPO="${REPO:-mailsvb2-bot/clientplatform}"
SSH_HOST="${CLIENTPLATFORM_PRODUCTION_SSH_HOST:-}"
SSH_USER="${CLIENTPLATFORM_PRODUCTION_SSH_USER:-root}"
SSH_PORT="${CLIENTPLATFORM_PRODUCTION_SSH_PORT:-22}"
SSH_PRIVATE_KEY_FILE="${CLIENTPLATFORM_PRODUCTION_SSH_PRIVATE_KEY_FILE:-}"

log() {
  printf '=== %s ===\n' "$*"
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command is missing: $1"
}

for command_name in git gh ssh-keygen awk grep sed paste; do
  require_command "$command_name"
done

[ -d "$APP_DIR/.git" ] || fail "ClientPlatform git repository not found: $APP_DIR"
[ -n "$SSH_HOST" ] || fail "set CLIENTPLATFORM_PRODUCTION_SSH_HOST to the verified production SSH host"
[ -n "$SSH_PRIVATE_KEY_FILE" ] \
  || fail "set CLIENTPLATFORM_PRODUCTION_SSH_PRIVATE_KEY_FILE to an existing dedicated SSH private key"
[ -f "$SSH_PRIVATE_KEY_FILE" ] || fail "SSH private key file does not exist"

case "$SSH_HOST" in
  *[!A-Za-z0-9._:-]*|'') fail "CLIENTPLATFORM_PRODUCTION_SSH_HOST has unsupported characters" ;;
esac
case "$SSH_USER" in
  *[!A-Za-z0-9._-]*|'') fail "CLIENTPLATFORM_PRODUCTION_SSH_USER has unsupported characters" ;;
esac
case "$SSH_PORT" in
  *[!0-9]*|'') fail "CLIENTPLATFORM_PRODUCTION_SSH_PORT must be numeric" ;;
esac
[ "$SSH_PORT" -ge 1 ] && [ "$SSH_PORT" -le 65535 ] \
  || fail "CLIENTPLATFORM_PRODUCTION_SSH_PORT is outside 1..65535"

cd "$APP_DIR"

origin="$(git remote get-url origin 2>/dev/null || true)"
case "$origin" in
  https://github.com/mailsvb2-bot/clientplatform|\
  https://github.com/mailsvb2-bot/clientplatform.git|\
  git@github.com:mailsvb2-bot/clientplatform|\
  git@github.com:mailsvb2-bot/clientplatform.git|\
  ssh://git@github.com/mailsvb2-bot/clientplatform|\
  ssh://git@github.com/mailsvb2-bot/clientplatform.git)
    ;;
  *)
    fail "unexpected repository origin; refusing to configure production automation"
    ;;
esac

current_branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
[ "$current_branch" = "main" ] || fail "production checkout must be on main"
[ -z "$(git status --porcelain=v1 --untracked-files=no --ignore-submodules=none)" ] \
  || fail "tracked production worktree is dirty"

local_branches="$(git for-each-ref --format='%(refname:short)' refs/heads | LC_ALL=C sort)"
local_count="$(printf '%s\n' "$local_branches" | sed '/^$/d' | wc -l | tr -d '[:space:]')"
local_csv="$(printf '%s\n' "$local_branches" | sed '/^$/d' | paste -sd, -)"
[ "$local_count" = "1" ] && [ "$local_csv" = "main" ] \
  || fail "server must have exactly one local branch main; got count=$local_count branches=$local_csv"

log "verify GitHub CLI authentication and repository administration"
gh auth status >/dev/null
gh api "repos/$REPO" --jq '.full_name + " permission=" + (.permissions.admin | tostring)' \
  | grep -F "$REPO permission=true" >/dev/null \
  || fail "the authenticated gh account does not have repository admin permission"

log "validate the existing dedicated SSH private key without printing it"
chmod 0600 "$SSH_PRIVATE_KEY_FILE"
ssh-keygen -y -f "$SSH_PRIVATE_KEY_FILE" >/dev/null

host_public_key_file=""
for candidate in \
  /etc/ssh/ssh_host_ed25519_key.pub \
  /etc/ssh/ssh_host_ecdsa_key.pub \
  /etc/ssh/ssh_host_rsa_key.pub
do
  if [ -s "$candidate" ]; then
    host_public_key_file="$candidate"
    break
  fi
done
[ -n "$host_public_key_file" ] || fail "no local OpenSSH host public key is available"

host_key="$(awk 'NF >= 2 {print $1 " " $2; exit}' "$host_public_key_file")"
[ -n "$host_key" ] || fail "failed to read the local OpenSSH host public key"
known_host_name="$SSH_HOST"
if [ "$SSH_PORT" != "22" ]; then
  known_host_name="[$SSH_HOST]:$SSH_PORT"
fi
known_hosts_line="$known_host_name $host_key"

log "configure dedicated ClientPlatform GitHub Actions SSH secrets"
printf '%s' "$SSH_HOST" \
  | gh secret set CLIENTPLATFORM_PRODUCTION_SSH_HOST --repo "$REPO"
printf '%s' "$SSH_USER" \
  | gh secret set CLIENTPLATFORM_PRODUCTION_SSH_USER --repo "$REPO"
cat "$SSH_PRIVATE_KEY_FILE" \
  | gh secret set CLIENTPLATFORM_PRODUCTION_SSH_PRIVATE_KEY --repo "$REPO"
printf '%s\n' "$known_hosts_line" \
  | gh secret set CLIENTPLATFORM_PRODUCTION_SSH_KNOWN_HOSTS --repo "$REPO"
printf '%s' "$SSH_PORT" \
  | gh secret set CLIENTPLATFORM_PRODUCTION_SSH_PORT --repo "$REPO"

for secret_name in \
  CLIENTPLATFORM_PRODUCTION_SSH_HOST \
  CLIENTPLATFORM_PRODUCTION_SSH_USER \
  CLIENTPLATFORM_PRODUCTION_SSH_PRIVATE_KEY \
  CLIENTPLATFORM_PRODUCTION_SSH_KNOWN_HOSTS \
  CLIENTPLATFORM_PRODUCTION_SSH_PORT
do
  gh secret list --repo "$REPO" | awk '{print $1}' | grep -Fx "$secret_name" >/dev/null \
    || fail "GitHub Actions secret was not created: $secret_name"
done

cat <<EOF
REPAIR_OK
SERVER_LOCAL_BRANCH_COUNT=$local_count
SERVER_LOCAL_BRANCHES=$local_csv
GITHUB_PRODUCTION_TRANSPORT=dedicated_ssh
GITHUB_PRODUCTION_SSH_HOST=$SSH_HOST
GITHUB_PRODUCTION_SSH_USER=$SSH_USER
GITHUB_PRODUCTION_SSH_PORT=$SSH_PORT
NEXT_STEP=Run the Production server topology probe workflow_dispatch and require ops/clientplatform-server-single-main=success.
EOF
