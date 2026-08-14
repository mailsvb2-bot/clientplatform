from __future__ import annotations

import os

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "visual_provider_gateway.app:app",
        host=os.getenv("VISUAL_GATEWAY_HOST", "127.0.0.1"),
        port=int(os.getenv("VISUAL_GATEWAY_PORT", "8097")),
        workers=1,
    )
