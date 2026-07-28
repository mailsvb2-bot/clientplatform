FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 clientplatform \
    && useradd --system --uid 10001 --gid clientplatform --home /var/lib/clientplatform clientplatform \
    && mkdir -p /app /var/lib/clientplatform /var/log/clientplatform /var/backups/clientplatform \
    && chown -R clientplatform:clientplatform /app /var/lib/clientplatform /var/log/clientplatform /var/backups/clientplatform

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --disable-pip-version-check --require-hashes -r /app/requirements.txt
COPY --chown=clientplatform:clientplatform . /app

USER clientplatform
EXPOSE 8181 8182 8191

ENTRYPOINT ["python", "main.py"]
