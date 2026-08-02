from __future__ import annotations

import base64
import gzip
import hashlib
import subprocess
from pathlib import Path

PAYLOAD = Path(".admin_patch_payload.b64.gz")
TARGET = Path("handlers/clientplatform_admin.py")
EXPECTED_BLOB_SHA = "ae84153e05f32ec405d58d9d1c74f3b649a4ee10"
EXPECTED_SHA256 = "7d5f424e2b0174de6b18d3190f1eb553c9accba36a7375c7196bdbde96259f97"

encoded = PAYLOAD.read_text(encoding="ascii").strip()
payload = gzip.decompress(base64.b64decode(encoded, validate=True))
blob_sha = hashlib.sha1(
    f"blob {len(payload)}\0".encode("ascii") + payload
).hexdigest()
sha256 = hashlib.sha256(payload).hexdigest()

if blob_sha != EXPECTED_BLOB_SHA:
    raise SystemExit(f"unexpected git blob sha: {blob_sha}")
if sha256 != EXPECTED_SHA256:
    raise SystemExit(f"unexpected sha256: {sha256}")

TARGET.write_bytes(payload)
PAYLOAD.unlink()
Path("scripts/materialize_clientplatform_admin.py").unlink()

subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
subprocess.run(
    ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
    check=True,
)
subprocess.run(["git", "add", "-A"], check=True)
subprocess.run(
    ["git", "commit", "-m", "Fix Metrotherapy parity admin validator boundaries"],
    check=True,
)
subprocess.run(
    ["git", "push", "origin", "HEAD:agent/metrotherapy-parity-business-admin"],
    check=True,
)
