FROM python:3.12-slim

ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="ClientPlatform Visual Provider Gateway" \
      org.opencontainers.image.description="Provider-neutral visual generation adapter boundary for ClientPlatform" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.source="https://github.com/mailsvb2-bot/clientplatform"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VISUAL_GATEWAY_HOST=0.0.0.0 \
    VISUAL_GATEWAY_PORT=8097

WORKDIR /app
COPY visual_provider_gateway/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY visual_provider_gateway /app/visual_provider_gateway

CMD ["python", "-m", "visual_provider_gateway.run"]
