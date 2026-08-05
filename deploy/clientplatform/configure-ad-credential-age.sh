#!/usr/bin/env bash
set -Eeuo pipefail

HOST_DIR="${CLIENTPLATFORM_AD_CREDENTIAL_HOST_DIR:-/var/lib/clientplatform/ad-secrets}"
IDENTITY="$HOST_DIR/identity.txt"
RUNTIME_UID="${CLIENTPLATFORM_RUNTIME_UID:-10001}"
RUNTIME_GID="${CLIENTPLATFORM_RUNTIME_GID:-10001}"

if [ "$(id -u)" -ne 0 ]; then
  echo "CLIENTPLATFORM_AD_CREDENTIAL_AGE_FAILED:root_required" >&2
  exit 1
fi

if ! command -v age-keygen >/dev/null 2>&1; then
  echo "CLIENTPLATFORM_AD_CREDENTIAL_AGE_FAILED:age_keygen_missing" >&2
  exit 1
fi

case "$HOST_DIR" in
  /var/lib/clientplatform/ad-secrets|/var/lib/clientplatform/ad-secrets/*) ;;
  *)
    echo "CLIENTPLATFORM_AD_CREDENTIAL_AGE_FAILED:unsafe_host_dir" >&2
    exit 1
    ;;
esac

install -d -o "$RUNTIME_UID" -g "$RUNTIME_GID" -m 0700 "$HOST_DIR"

if [ ! -s "$IDENTITY" ]; then
  temporary="$HOST_DIR/.identity.$$.tmp"
  rm -f "$temporary"
  age-keygen -o "$temporary" >/dev/null 2>&1
  chown "$RUNTIME_UID:$RUNTIME_GID" "$temporary"
  chmod 0600 "$temporary"
  mv -f "$temporary" "$IDENTITY"
fi

chown "$RUNTIME_UID:$RUNTIME_GID" "$IDENTITY"
chmod 0600 "$IDENTITY"

recipient="$(age-keygen -y "$IDENTITY")"
case "$recipient" in
  age1*) ;;
  *)
    echo "CLIENTPLATFORM_AD_CREDENTIAL_AGE_FAILED:recipient_invalid" >&2
    exit 1
    ;;
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
