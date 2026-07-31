from __future__ import annotations

import errno
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clientplatform.domain.programs import ContentKind
from scripts.clientplatform_container_audio_assets import (
    prepare_container_audio_assets,
)
from services.audio_asset_integrity import validate_release_assets


class ContainerAudioCrossDevicePublicationTests(unittest.TestCase):
    @staticmethod
    def _write_audio_tree(root: Path) -> None:
        audio = root / "audio"
        (audio / "demo").mkdir(parents=True)
        (audio / "full").mkdir(parents=True)
        (audio / "demo" / "work.ogg").write_bytes(b"OggS" + b"w" * 128)
        (audio / "full" / "1_work.ogg").write_bytes(b"OggS" + b"1" * 128)

    def test_publication_never_renames_source_across_filesystems(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "app"
            assets = Path(raw) / "immutable-audio"
            root.mkdir()
            self._write_audio_tree(root)
            source = root / "audio"
            original_replace = Path.replace

            def reject_source_replace(path: Path, target: Path) -> Path:
                if path == source:
                    raise OSError(errno.EXDEV, "Invalid cross-device link")
                return original_replace(path, target)

            with mock.patch.object(Path, "replace", reject_source_replace):
                published = prepare_container_audio_assets(
                    root=root,
                    asset_root=assets,
                    require=True,
                )

            self.assertIsNotNone(published)
            self.assertTrue(source.is_symlink())
            self.assertEqual(
                validate_release_assets(root, require_versioned=True),
                published,
            )


class ProgramContentKindContractTests(unittest.TestCase):
    def test_programs_support_all_user_material_families(self) -> None:
        self.assertEqual(
            {item.value for item in ContentKind},
            {
                "audio",
                "video",
                "text",
                "document",
                "image",
                "link",
                "task",
                "mixed",
            },
        )


if __name__ == "__main__":
    unittest.main()
