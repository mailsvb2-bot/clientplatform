from __future__ import annotations

import base64
from uuid import UUID


def uuid_token(value: str) -> str:
    return base64.urlsafe_b64encode(UUID(str(value)).bytes).decode("ascii").rstrip("=")


def token_uuid(value: str) -> str:
    padded = str(value) + "=" * (-len(str(value)) % 4)
    return str(UUID(bytes=base64.urlsafe_b64decode(padded.encode("ascii"))))
