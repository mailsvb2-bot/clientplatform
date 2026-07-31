from __future__ import annotations

"""Seal legacy local audio inside the read-only ClientPlatform container image."""

import argparse
import os
import shutil
import sys
from pathlib import Path

from services.audio_asset_integrity import (
    AudioAssetInfo,
    seal_asset_dir,
    validate_asset_dir,
    validate_release_assets,
    write_release_pointer,
)


class ContainerAudioAssetError(RuntimeError):
    """Sanitized build-time failure for container audio publication."""


def _assign_runtime_ownership(
    *,
    destination: Path,
    audio_link: Path,
    pointer: Path,
    owner_uid: int | None,
    owner_gid: int | None,
) -> None:
    if owner_uid is None and owner_gid is None:
        return
    if owner_uid is None or owner_gid is None or owner_uid < 0 or owner_gid < 0:
        raise ContainerAudioAssetError("container_audio_owner_invalid")

    try:
        paths = [destination, *sorted(destination.rglob("*"))]
        for path in paths:
            os.chown(path, owner_uid, owner_gid, follow_symlinks=False)
        os.chown(audio_link, owner_uid, owner_gid, follow_symlinks=False)
        os.chown(pointer, owner_uid, owner_gid, follow_symlinks=False)
    except OSError as exc:
        raise ContainerAudioAssetError("container_audio_owner_assignment_failed") from exc


def prepare_container_audio_assets(
    *,
    root: Path,
    asset_root: Path,
    require: bool,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> AudioAssetInfo | None:
    release_root = root.expanduser().resolve(strict=True)
    source = release_root / "audio"
    pointer = release_root / ".audio-assets.json"

    if source.is_symlink():
        try:
            verified = validate_release_assets(
                release_root,
                require_versioned=require or pointer.exists(),
            )
            if verified is not None:
                _assign_runtime_ownership(
                    destination=Path(verified.asset_dir),
                    audio_link=source,
                    pointer=pointer,
                    owner_uid=owner_uid,
                    owner_gid=owner_gid,
                )
            return verified
        except ContainerAudioAssetError:
            raise
        except (OSError, ValueError) as exc:
            raise ContainerAudioAssetError(
                "container_audio_existing_release_invalid"
            ) from exc

    if not source.exists():
        if require:
            raise ContainerAudioAssetError("container_audio_source_missing")
        return None
    if not source.is_dir():
        raise ContainerAudioAssetError("container_audio_source_not_directory")

    destination_root = asset_root.expanduser().resolve(strict=False)
    destination_root.mkdir(parents=True, exist_ok=True)
    if not destination_root.is_dir() or destination_root.is_symlink():
        raise ContainerAudioAssetError("container_audio_asset_root_invalid")

    try:
        source_info = seal_asset_dir(source)
    except (OSError, ValueError) as exc:
        raise ContainerAudioAssetError("container_audio_seal_failed") from exc

    destination = destination_root / source_info.asset_sha256
    try:
        if destination.exists():
            if not destination.is_dir() or destination.is_symlink():
                raise ContainerAudioAssetError(
                    "container_audio_digest_target_invalid"
                )
            validate_asset_dir(
                destination,
                expected_sha256=source_info.asset_sha256,
                expected_file_count=source_info.file_count,
            )
            shutil.rmtree(source)
        else:
            source.replace(destination)
        source.symlink_to(destination, target_is_directory=True)
        published = write_release_pointer(release_root, destination)
        _assign_runtime_ownership(
            destination=destination,
            audio_link=source,
            pointer=pointer,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        verified = validate_release_assets(release_root, require_versioned=True)
    except ContainerAudioAssetError:
        raise
    except (OSError, ValueError) as exc:
        raise ContainerAudioAssetError("container_audio_publication_failed") from exc

    if verified is None or verified != published:
        raise ContainerAudioAssetError("container_audio_verification_mismatch")
    return verified


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/app"))
    parser.add_argument(
        "--asset-root",
        type=Path,
        default=Path("/opt/clientplatform/audio-assets"),
    )
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--owner-uid", type=int)
    parser.add_argument("--owner-gid", type=int)
    args = parser.parse_args()

    try:
        result = prepare_container_audio_assets(
            root=args.root,
            asset_root=args.asset_root,
            require=bool(args.require),
            owner_uid=args.owner_uid,
            owner_gid=args.owner_gid,
        )
    except ContainerAudioAssetError as exc:
        print(f"CLIENTPLATFORM_CONTAINER_AUDIO_ASSETS_FAILED:{exc}", file=sys.stderr)
        return 1

    if result is None:
        print("CLIENTPLATFORM_CONTAINER_AUDIO_ASSETS_SKIPPED:no_source")
    else:
        print(
            "CLIENTPLATFORM_CONTAINER_AUDIO_ASSETS_OK:"
            f"sha256={result.asset_sha256}:files={result.file_count}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
