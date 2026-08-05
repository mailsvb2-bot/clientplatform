#!/usr/bin/env bash
set -Eeuo pipefail

HOST_DIR="${CLIENTPLATFORM_AD_CREDENTIAL_HOST_DIR:-/var/lib/clientplatform/ad-secrets}"
IDENTITY="$HOST_DIR/identity.txt"
RUNTIME_UID="${CLIENTPLATFORM_RUNTIME_UID:-10001}"
RUNTIME_GID="${CLIENTPLATFORM_RUNTIME_GID:-10001}"

fail() {
  echo "CLIENTPLATFORM_AD_CREDENTIAL_AGE_FAILED:$1" >&2
  exit 1
}

if [ "$(id -u)" -ne 0 ]; then
  fail root_required
fi

if ! command -v age-keygen >/dev/null 2>&1; then
  fail age_keygen_missing
fi

case "$HOST_DIR" in
  /var/lib/clientplatform/ad-secrets|/var/lib/clientplatform/ad-secrets/*) ;;
  *) fail unsafe_host_dir ;;
esac

if [ -L "$HOST_DIR" ] || [ -L "$IDENTITY" ]; then
  fail symlink_path_rejected
fi
if [ -e "$HOST_DIR" ] && [ ! -d "$HOST_DIR" ]; then
  fail host_dir_not_directory
fi
if [ -e "$IDENTITY" ] && [ ! -f "$IDENTITY" ]; then
  fail identity_not_regular_file
fi

install -d -o "$RUNTIME_UID" -g "$RUNTIME_GID" -m 0700 "$HOST_DIR"

if [ -L "$HOST_DIR" ]; then
  fail symlink_path_rejected
fi

if [ ! -s "$IDENTITY" ]; then
  temporary="$HOST_DIR/.identity.$$.tmp"
  rm -f "$temporary"
  age-keygen -o "$temporary" >/dev/null 2>&1
  if [ -L "$temporary" ] || [ ! -f "$temporary" ]; then
    rm -f "$temporary"
    fail generated_identity_not_regular_file
  fi
  chown "$RUNTIME_UID:$RUNTIME_GID" "$temporary"
  chmod 0600 "$temporary"
  mv -f "$temporary" "$IDENTITY"
fi

if [ -L "$IDENTITY" ] || [ ! -f "$IDENTITY" ]; then
  fail identity_not_regular_file
fi

chown "$RUNTIME_UID:$RUNTIME_GID" "$IDENTITY"
chmod 0600 "$IDENTITY"

host_mode="$(stat -c '%a' "$HOST_DIR")"
identity_mode="$(stat -c '%a' "$IDENTITY")"
host_uid="$(stat -c '%u' "$HOST_DIR")"
host_gid="$(stat -c '%g' "$HOST_DIR")"
identity_uid="$(stat -c '%u' "$IDENTITY")"
identity_gid="$(stat -c '%g' "$IDENTITY")"

test "$host_mode" = "700" || fail host_dir_permissions_invalid
test "$identity_mode" = "600" || fail identity_permissions_invalid
test "$host_uid:$host_gid" = "$RUNTIME_UID:$RUNTIME_GID" \
  || fail host_dir_owner_invalid
test "$identity_uid:$identity_gid" = "$RUNTIME_UID:$RUNTIME_GID" \
  || fail identity_owner_invalid

recipient="$(age-keygen -y "$IDENTITY")"
case "$recipient" in
  age1*) ;;
  *) fail recipient_invalid ;;
esac

# Verify an encrypt/decrypt round trip without printing the identity or plaintext.
probe="$(mktemp)"
trap 'rm -f "$probe"' EXIT
printf 'clientplatform-ad-vault-probe' \
  | age --encrypt --recipient "$recipient" \
  | age --decrypt --identity "$IDENTITY" >"$probe"
test "$(cat "$probe")" = "clientplatform-ad-vault-probe"

echo "CLIENTPLATFORM_AD_CREDENTIAL_AGE_OK:$IDENTITY"
echo "CLIENTPLATFORM_AD_CREDENTIAL_RECIPIENT=$recipient"
