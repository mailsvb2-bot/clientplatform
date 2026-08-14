ARG PYTHON_IMAGE=python:3.12.11-slim-bookworm
FROM ${PYTHON_IMAGE}
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="ClientPlatform Visual Creative Gateway" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/mailsvb2-bot/clientplatform"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 VISUAL_GATEWAY_STATE_DIR=/var/lib/visual-gateway
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY visual_gateway/requirements.txt ./visual_gateway/requirements.txt
RUN pip install --no-cache-dir -r visual_gateway/requirements.txt
COPY visual_gateway ./visual_gateway
RUN useradd --system --uid 10001 --create-home gateway \
 && mkdir -p /var/lib/visual-gateway \
 && chown -R gateway:gateway /var/lib/visual-gateway /app
USER gateway
VOLUME ["/var/lib/visual-gateway"]
EXPOSE 8080
CMD ["python", "-m", "visual_gateway.server"]
