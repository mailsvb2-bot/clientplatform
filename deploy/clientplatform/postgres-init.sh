#!/bin/sh
set -eu

: "${CLIENTPLATFORM_POSTGRES_APP_PASSWORD:?CLIENTPLATFORM_POSTGRES_APP_PASSWORD is required}"

if psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --tuples-only --no-align \
  --command "SELECT 1 FROM pg_roles WHERE rolname='clientplatform_app'" | grep -qx 1; then
  exit 0
fi

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set ON_ERROR_STOP=1 \
  --set app_password="$CLIENTPLATFORM_POSTGRES_APP_PASSWORD" <<'SQL'
CREATE ROLE clientplatform_app
  LOGIN
  PASSWORD :'app_password'
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOREPLICATION;
GRANT CONNECT ON DATABASE clientplatform TO clientplatform_app;
GRANT USAGE, CREATE ON SCHEMA public TO clientplatform_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO clientplatform_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO clientplatform_app;
SQL
