from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from clientplatform.runtime.media_gateway import (
    MediaGatewayConfig,
    start_media_gateway_runtime,
    stop_media_gateway_runtime,
)
from clientplatform.transport.media import parse_s3_reference
from core.runtime_env import env_int


def _required_env(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"staging_configuration_missing:{name}")
    return value


def ephemeral_gateway_config() -> MediaGatewayConfig:
    filesystem_root = str(
        Path(_required_env("CLIENTPLATFORM_MEDIA_GATEWAY_FILESYSTEM_ROOT"))
        .expanduser()
        .resolve()
    )
    bucket, _ = parse_s3_reference(
        f"s3://{_required_env('CLIENTPLATFORM_MEDIA_GATEWAY_ALLOWED_BUCKETS')}/fixture.mp3"
    )
    return MediaGatewayConfig(
        enabled=True,
        host="127.0.0.1",
        port=env_int(
            "CLIENTPLATFORM_MEDIA_GATEWAY_PORT",
            8091,
            minimum=1,
            maximum=65_535,
        ),
        public_base_url=str(
            os.getenv("CLIENTPLATFORM_STAGING_GATEWAY_ROUTE_BASE_URL")
            or "https://staging.invalid/clientplatform"
        ).strip(),
        storage_mode="filesystem",
        allowed_buckets=frozenset({bucket}),
        filesystem_root=filesystem_root,
        s3_endpoint="",
        s3_region="",
        s3_access_key_reference="secret://env/CLIENTPLATFORM_SECRET_S3_ACCESS_KEY",
        s3_secret_key_reference="secret://env/CLIENTPLATFORM_SECRET_S3_SECRET_KEY",
        s3_session_token_reference="",
        signing_secret_reference=str(
            os.getenv("CLIENTPLATFORM_MEDIA_SIGNING_SECRET_REFERENCE")
            or "secret://env/CLIENTPLATFORM_SECRET_MEDIA_SIGNING_KEY"
        ).strip(),
        max_object_bytes=env_int(
            "CLIENTPLATFORM_MEDIA_GATEWAY_MAX_OBJECT_BYTES",
            10_485_760,
            minimum=1_048_576,
            maximum=262_144_000,
        ),
        upstream_timeout_seconds=20.0,
        chunk_size=65_536,
    )


async def run() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop_event.set)
        except NotImplementedError:
            continue

    config = ephemeral_gateway_config()
    runtime = await start_media_gateway_runtime(config)
    if runtime is None:
        raise RuntimeError("staging_gateway_failed_to_start")
    print(
        "clientplatform ephemeral gateway ready "
        f"on {config.host}:{config.port}{config.route_prefix}",
        flush=True,
    )
    try:
        await stop_event.wait()
    finally:
        await stop_media_gateway_runtime()


def main() -> int:
    try:
        asyncio.run(run())
    except RuntimeError as exc:
        print(f"clientplatform ephemeral gateway failed: {exc}", flush=True)
        return 1
    except ValueError as exc:
        print(f"clientplatform ephemeral gateway failed: {type(exc).__name__}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
