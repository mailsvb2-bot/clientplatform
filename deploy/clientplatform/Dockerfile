FROM python:3.12-slim-bookworm AS python-runtime

FROM postgres:16-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/usr/lib/postgresql/16/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

# Reuse the official Python 3.12 runtime and the PostgreSQL 16 toolchain from
# their immutable upstream images. Only Debian's standard repository is needed
# at build time; no external PostgreSQL package repository or signing key is used.
COPY --from=python-runtime /usr/local /usr/local

RUN apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=60 update \
    && apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=60 install \
       --yes --no-install-recommends \
       age \
       ca-certificates \
       libbz2-1.0 \
       libexpat1 \
       libffi8 \
       libgdbm-compat4 \
       libgdbm6 \
       liblzma5 \
       libncursesw6 \
       libreadline8 \
       libsqlite3-0 \
       libssl3 \
       libuuid1 \
       netbase \
       tzdata \
       zlib1g \
    && rm -rf /var/lib/apt/lists/* \
    && python --version \
    && python -c "import bz2, ctypes, lzma, readline, sqlite3, ssl, uuid, zlib" \
    && test "$(pg_dump --version | awk '{print $3}' | cut -d. -f1)" = "16" \
    && test "$(pg_restore --version | awk '{print $3}' | cut -d. -f1)" = "16" \
    && test "$(psql --version | awk '{print $3}' | cut -d. -f1)" = "16" \
    && age --version \
    && groupadd --system --gid 10001 clientplatform \
    && useradd --system --uid 10001 --gid clientplatform --home /var/lib/clientplatform clientplatform \
    && mkdir -p \
       /app \
       /var/lib/clientplatform \
       /var/log/clientplatform \
       /var/backups/clientplatform \
    && chown -R clientplatform:clientplatform \
       /app \
       /var/lib/clientplatform \
       /var/log/clientplatform \
       /var/backups/clientplatform

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --disable-pip-version-check --require-hashes -r /app/requirements.txt
COPY --chown=clientplatform:clientplatform . /app

RUN set -eu; \
    test -r /app/scripts/clientplatform_production_preflight.py; \
    test -r /app/scripts/clientplatform_program_media_preflight.py; \
    test -r /app/scripts/clientplatform_bot_gateway_preflight.py; \
    test -r /app/scripts/clientplatform_ad_connections_preflight.py; \
    test -r /app/scripts/clientplatform_sales_production_smoke.py; \
    test -r /app/deploy/clientplatform/container-entrypoint.sh; \
    python -m py_compile \
       /app/scripts/clientplatform_production_preflight.py \
       /app/scripts/clientplatform_program_media_preflight.py \
       /app/scripts/clientplatform_bot_gateway_preflight.py \
       /app/scripts/clientplatform_ad_connections_preflight.py \
       /app/scripts/clientplatform_sales_production_smoke.py; \
    chmod 0555 /app/deploy/clientplatform/container-entrypoint.sh

USER clientplatform
EXPOSE 8181 8182 8191

ENTRYPOINT ["/app/deploy/clientplatform/container-entrypoint.sh"]
CMD []
