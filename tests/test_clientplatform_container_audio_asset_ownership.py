from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import clientplatform_container_audio_assets as container_audio


class ContainerAudioAssetOwnershipTests(unittest.TestCase):
    @staticmethod
    def _write_audio_tree(root: Path) -> None:
        audio = root / "audio"
        (audio / "demo").mkdir(parents=True)
        (audio / "full").mkdir(parents=True)
        (audio / "demo" / "work.ogg").write_bytes(b"OggS" + b"w" * 128)
        (audio / "demo" / "home.ogg").write_bytes(b"OggS" + b"h" * 128)
        (audio / "full" / "1_work.ogg").write_bytes(b"OggS" + b"1" * 128)
        (audio / "full" / "2_home.ogg").write_bytes(b"OggS" + b"2" * 128)

    def test_prepare_assigns_manifest_pointer_link_and_tree_to_runtime_user(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "app"
            assets = Path(raw) / "immutable-audio"
            root.mkdir()
            self._write_audio_tree(root)

            with mock.patch.object(container_audio.os, "chown") as chown:
                info = container_audio.prepare_container_audio_assets(
                    root=root,
                    asset_root=assets,
                    require=True,
                    owner_uid=10001,
                    owner_gid=10001,
                )

            self.assertIsNotNone(info)
            assert info is not None
            calls = {
                (Path(call.args[0]), call.args[1], call.args[2], call.kwargs)
                for call in chown.call_args_list
            }
            expected_paths = {
                assets / info.asset_sha256,
                assets / info.asset_sha256 / ".asset-manifest.json",
                root / "audio",
                root / ".audio-assets.json",
            }
            observed_paths = {path for path, _, _, _ in calls}
            self.assertTrue(expected_paths.issubset(observed_paths))
            self.assertTrue(
                all(uid == 10001 and gid == 10001 for _, uid, gid, _ in calls)
            )
            self.assertTrue(
                all(kwargs == {"follow_symlinks": False} for _, _, _, kwargs in calls)
            )

    def test_prepare_rejects_partial_runtime_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "app"
            root.mkdir()
            self._write_audio_tree(root)

            with self.assertRaisesRegex(
                container_audio.ContainerAudioAssetError,
                "container_audio_owner_invalid",
            ):
                container_audio.prepare_container_audio_assets(
                    root=root,
                    asset_root=Path(raw) / "immutable-audio",
                    require=True,
                    owner_uid=10001,
                )


if __name__ == "__main__":
    unittest.main()
