# Self-hosted GPU worker contract (v4)

The **ClientPlatform Visual Provider Gateway** can delegate generation to an operator-owned GPU worker instead of importing GPU frameworks into ClientPlatform. The worker may run Wan, Qwen-Image, FLUX, or another model selected by the operator.

The worker is configured on the provider gateway with `VISUAL_SELFHOST_BASE_URL`; application projects never call it directly.

## Submit

`POST /v1/creative/generations`

JSON request:

```json
{
  "kind": "image|video",
  "model": "optional operator-selected model id",
  "prompt": "...",
  "negative_prompt": "...",
  "aspect_ratio": "4:5",
  "duration_seconds": 8,
  "reference_url": "",
  "seed": null
}
```

`model` is optional. When `VISUAL_SELFHOST_IMAGE_MODEL` or `VISUAL_SELFHOST_VIDEO_MODEL` is configured on the Visual Provider Gateway, that exact value is forwarded to the worker. The worker owns the mapping from this stable model id to the actual local inference implementation.

Response:

```json
{
  "id": "provider-job-id",
  "status": "queued|running|succeeded|failed",
  "model": "actual-model-id",
  "mime_type": "video/mp4",
  "media_url": "https://...",
  "error_code": ""
}
```

For small images the worker may return `media_base64` instead of `media_url`. For video, prefer `media_url`; large video bytes should not be embedded in JSON.

## Poll

`GET /v1/creative/generations/{provider_job_id}`

Return the same response shape. Final success must expose one of:

- `media_url`;
- `media_base64`;
- `asset_path` inside the gateway's configured `VISUAL_CREATIVE_OUTPUT_DIR` when the gateway and worker intentionally share that filesystem.

## Security and operational rules

- Bind the GPU worker to a private service network whenever possible.
- Use `VISUAL_SELFHOST_TOKEN` when the worker is not protected by an equivalent service-to-service control.
- The Visual Provider Gateway rejects arbitrary shared `asset_path` values outside its configured output directory.
- Media downloads are bounded by `VISUAL_MAX_MEDIA_BYTES` (default 256 MiB).
- Provider JSON responses are bounded by `VISUAL_MAX_JSON_BYTES` (default 32 MiB).
- Non-self-hosted media URLs must use HTTPS by default; private/loopback destinations are rejected.
