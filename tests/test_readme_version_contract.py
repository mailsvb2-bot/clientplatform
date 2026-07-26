from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_readme_uses_version_file_as_single_release_source() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

    assert version
    assert "`VERSION`" in readme
    assert re.search(r"(?i)канон\s+v\d", readme) is None
    assert re.search(r"(?i)верси(?:я|и)\s+v\d", readme) is None
