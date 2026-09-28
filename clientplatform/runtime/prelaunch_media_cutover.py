from __future__ import annotations

import os
import shutil
from pathlib import Path


_PRODUCTION_MEDIA_ROOT = Path("/var/lib/clientplatform").resolve()
_LEGACY_AD_ASSET_DIR = _PRODUCTION_MEDIA_ROOT / "ad-assets"


def purge_prelaunch_legacy_ad_media() -> bool:
    """Delete the obsolete pre-launch ad-media directory, never arbitrary paths."""

    raw = str(os.getenv("CLIENTPLATFORM_AD_ASSET_DIR") or _LEGACY_AD_ASSET_DIR).strip()
    target = Path(raw).expanduser()
    try:
        resolved = target.resolve(strict=False)
        resolved.relative_to(_PRODUCTION_MEDIA_ROOT)
    except (OSError, ValueError) as exc:
        raise RuntimeError("legacy ad media path escapes ClientPlatform root") from exc
    if resolved == _PRODUCTION_MEDIA_ROOT:
        raise RuntimeError("refusing to purge ClientPlatform production root")
    if not target.exists() and not target.is_symlink():
        return False
    if target.is_symlink():
        target.unlink()
        return True
    if target.is_file():
        target.unlink()
        return True
    shutil.rmtree(target)
    return True


__all__ = ["purge_prelaunch_legacy_ad_media"]
