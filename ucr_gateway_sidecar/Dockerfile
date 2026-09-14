FROM python:3.12-slim AS proto-builder

ENV UCR_REVISION=8097b41e69634c944c225f7071e80b991d4ddc02

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir grpcio-tools==1.80.0
RUN git init /src/ucr \
    && git -C /src/ucr remote add origin https://github.com/mailsvb2-bot/Universal-Communication-Runtime.git \
    && git -C /src/ucr fetch --depth=1 origin "${UCR_REVISION}" \
    && git -C /src/ucr checkout --detach FETCH_HEAD \
    && test "$(git -C /src/ucr rev-parse HEAD)" = "${UCR_REVISION}"
RUN mkdir -p /generated \
    && python -m grpc_tools.protoc \
        -I/src/ucr/proto \
        --python_out=/generated \
        --grpc_python_out=/generated \
        $(find /src/ucr/proto/ucr/v1 -maxdepth 1 -type f -name '*.proto' | sort) \
    && python -m compileall -q /generated

FROM python:3.12-slim

WORKDIR /app
COPY ucr_gateway_sidecar/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt \
    && groupadd --system ucrgateway \
    && useradd --system --gid ucrgateway --home-dir /nonexistent --shell /usr/sbin/nologin ucrgateway

COPY --from=proto-builder /generated /app/generated
COPY ucr_gateway_sidecar /app/ucr_gateway_sidecar

ENV PYTHONPATH=/app/generated:/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER ucrgateway
EXPOSE 8098
ENTRYPOINT ["python", "-m", "ucr_gateway_sidecar.server"]
