#!/bin/sh
set -eu

ROOT="${CLIENTPLATFORM_ROOT:-/opt/clientplatform}"
COMPOSE_FILE="${CLIENTPLATFORM_COMPOSE_FILE:-$ROOT/deploy/clientplatform/compose.production.yml}"
COMPOSE_ENV="${CLIENTPLATFORM_COMPOSE_ENV:-$ROOT/deploy/clientplatform/.env}"
RUNTIME_ENV="${CLIENTPLATFORM_RUNTIME_ENV:-$ROOT/deploy/clientplatform/clientplatform.env}"
KEY_DIR="${CLIENTPLATFORM_BACKUP_KEY_DIR:-/root/.config/clientplatform}"
KEY_FILE="${CLIENTPLATFORM_BACKUP_IDENTITY_FILE:-$KEY_DIR/backup-age-identity.txt}"
APP_IMAGE="${CLIENTPLATFORM_APP_IMAGE:-clientplatform-production-app:latest}"

fail() {
  printf '%s\n' "CLIENTPLATFORM_BACKUP_AGE_CONFIGURE_FAILED:$1" >&2
  exit 1
}

[ "$(id -u)" -eq 0 ] || fail "root_required"
[ -d "$ROOT/.git" ] || fail "repository_missing:$ROOT"
[ -f "$COMPOSE_FILE" ] || fail "compose_file_missing:$COMPOSE_FILE"
[ -f "$COMPOSE_ENV" ] || fail "compose_env_missing:$COMPOSE_ENV"
[ -f "$RUNTIME_ENV" ] || fail "runtime_env_missing:$RUNTIME_ENV"
command -v docker >/dev/null 2>&1 || fail "docker_missing"
docker compose version >/dev/null 2>&1 || fail "docker_compose_missing"

compose() {
  docker compose --env-file "$COMPOSE_ENV" -f "$COMPOSE_FILE" "$@"
}

if ! docker image inspect "$APP_IMAGE" >/dev/null 2>&1; then
  compose build app backup
fi

mkdir -p "$KEY_DIR"
chown root:root "$KEY_DIR"
chmod 0700 "$KEY_DIR"

key_name="$(basename "$KEY_FILE")"
[ "$KEY_FILE" = "$KEY_DIR/$key_name" ] || fail "identity_must_be_inside_key_dir"

if [ ! -s "$KEY_FILE" ]; then
  rm -f "$KEY_FILE"
  docker run --rm \
    --user 0:0 \
    --volume "$KEY_DIR:/keys" \
    --entrypoint age-keygen \
    "$APP_IMAGE" \
    --output "/keys/$key_name"
fi

chown root:root "$KEY_FILE"
chmod 0600 "$KEY_FILE"

recipient="$(
  docker run --rm \
    --user 0:0 \
    --volume "$KEY_DIR:/keys:ro" \
    --entrypoint age-keygen \
    "$APP_IMAGE" \
    -y "/keys/$key_name"
)"
recipient="$(printf '%s' "$recipient" | tr -d '\r\n')"
printf '%s' "$recipient" | grep -Eq '^age1[0-9a-z]{50,100}$' || fail "invalid_generated_recipient"

runtime_uid="$(stat -c '%u' "$RUNTIME_ENV")"
runtime_gid="$(stat -c '%g' "$RUNTIME_ENV")"
runtime_mode="$(stat -c '%a' "$RUNTIME_ENV")"
tmp_env="$(mktemp "${RUNTIME_ENV}.tmp.XXXXXX")"
backup_output="$(mktemp /tmp/clientplatform-backup-age.XXXXXX)"
cleanup() {
  rm -f "$tmp_env" "$backup_output"
}
trap cleanup EXIT HUP INT TERM

awk -v recipient="$recipient" '
BEGIN { written = 0 }
/^CLIENTPLATFORM_BACKUP_AGE_RECIPIENT=/ {
  if (!written) {
    print "CLIENTPLATFORM_BACKUP_AGE_RECIPIENT=" recipient
    written = 1
  }
  next
}
{ print }
END {
  if (!written) {
    print "CLIENTPLATFORM_BACKUP_AGE_RECIPIENT=" recipient
  }
}
' "$RUNTIME_ENV" >"$tmp_env"

chown "$runtime_uid:$runtime_gid" "$tmp_env"
chmod "$runtime_mode" "$tmp_env"
mv -f "$tmp_env" "$RUNTIME_ENV"

if ! compose --profile operations run --rm backup >"$backup_output" 2>&1; then
  cat "$backup_output" >&2
  fail "encrypted_backup_failed"
fi
cat "$backup_output"
grep -q '^CLIENTPLATFORM_ENCRYPTED_BACKUP_OK:' "$backup_output" || fail "encrypted_backup_marker_missing"

compose --profile operations run --rm --entrypoint sh backup -c '
set -eu
backup_dir="${CLIENTPLATFORM_BACKUP_DIR:-/var/backups/clientplatform}"
latest="$(find "$backup_dir" -maxdepth 1 -type f -name "clientplatform-*.dump.age" | sort | tail -n 1)"
[ -n "$latest" ]
[ -s "$latest" ]
[ -s "$latest.sha256" ]
[ -s "$latest.json" ]
plaintext="${latest%.age}"
[ ! -e "$plaintext" ]
[ ! -e "$plaintext.sha256" ]
[ ! -e "$plaintext.json" ]
printf "%s\n" "CLIENTPLATFORM_ENCRYPTED_BACKUP_VERIFIED_OK:$latest"
'

printf '%s\n' "CLIENTPLATFORM_BACKUP_AGE_RECIPIENT=$recipient"
printf '%s\n' "CLIENTPLATFORM_BACKUP_AGE_IDENTITY=$KEY_FILE"
printf '%s\n' "CLIENTPLATFORM_BACKUP_AGE_CONFIGURED_OK"
