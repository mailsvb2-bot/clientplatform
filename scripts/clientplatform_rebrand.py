from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


LEGACY_UPPER = "A" + "1"
LEGACY_LOWER = "a" + "1"
BRAND = "clientplatform"
BRAND_CLASS = "ClientPlatform"
BRAND_ENV = "CLIENTPLATFORM"


def _run(*args: str) -> str:
    completed = subprocess.run(
        args,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.decode("utf-8", errors="strict")


def tracked_paths() -> list[Path]:
    raw = subprocess.run(
        ("git", "ls-files", "-z"),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    return [Path(item.decode("utf-8")) for item in raw.split(b"\0") if item]


def transform_name(value: str) -> str:
    result = value
    result = result.replace(f"{LEGACY_UPPER}_", f"{BRAND_ENV}_")
    result = result.replace(f"{LEGACY_LOWER}_", f"{BRAND}_")
    result = result.replace(f"{LEGACY_UPPER}-", f"{BRAND}-")
    result = result.replace(f"{LEGACY_LOWER}-", f"{BRAND}-")
    if result == LEGACY_UPPER or result == LEGACY_LOWER:
        return BRAND
    return result


def transform_path(path: Path) -> Path:
    return Path(*(transform_name(part) for part in path.parts))


def transform_text(text: str) -> str:
    result = text
    result = result.replace(f"{LEGACY_UPPER}_", f"{BRAND_ENV}_")
    result = result.replace(f"{LEGACY_LOWER}_", f"{BRAND}_")
    result = result.replace(f"{LEGACY_UPPER}-", f"{BRAND}-")
    result = result.replace(f"{LEGACY_LOWER}-", f"{BRAND}-")
    result = result.replace(f"/{LEGACY_LOWER}/", f"/{BRAND}/")
    result = result.replace(f"/{LEGACY_LOWER}", f"/{BRAND}")
    result = result.replace(f"{LEGACY_LOWER}/", f"{BRAND}/")
    result = result.replace(f"{LEGACY_LOWER}.", f"{BRAND}.")

    upper_camel = re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(LEGACY_UPPER)}(?=[A-Z])"
    )
    upper_standalone = re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(LEGACY_UPPER)}(?=$|[^A-Za-z0-9])"
    )
    lower_token = re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(LEGACY_LOWER)}(?=$|[^A-Za-z0-9])"
    )
    result = upper_camel.sub(BRAND_CLASS, result)
    result = upper_standalone.sub(BRAND, result)
    result = lower_token.sub(BRAND, result)
    return result


def _read_text(path: Path) -> str | None:
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def apply_content_changes(paths: list[Path]) -> int:
    changed = 0
    for path in paths:
        if not path.is_file():
            continue
        text = _read_text(path)
        if text is None:
            continue
        updated = transform_text(text)
        if updated == text:
            continue
        path.write_text(updated, encoding="utf-8", newline="")
        changed += 1
    return changed


def apply_path_changes(paths: list[Path]) -> int:
    moves: list[tuple[Path, Path]] = []
    targets: set[Path] = set()
    for source in paths:
        target = transform_path(source)
        if source == target:
            continue
        if target in targets or (target.exists() and target not in paths):
            raise RuntimeError(f"rebrand_path_collision:{source}:{target}")
        targets.add(target)
        moves.append((source, target))

    for source, target in sorted(
        moves,
        key=lambda pair: (len(pair[0].parts), str(pair[0])),
        reverse=True,
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        _run("git", "mv", str(source), str(target))
    return len(moves)


def remaining_legacy() -> list[str]:
    failures: list[str] = []
    for path in tracked_paths():
        expected_path = transform_path(path)
        if expected_path != path:
            failures.append(f"path:{path}->{expected_path}")
        if not path.is_file():
            continue
        text = _read_text(path)
        if text is None:
            continue
        if transform_text(text) != text:
            failures.append(f"content:{path}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if args.check:
        failures = remaining_legacy()
        if failures:
            print("Legacy brand references remain:")
            for failure in failures:
                print(failure)
            return 1
        print("clientplatform brand gate passed")
        return 0

    paths = tracked_paths()
    content_changes = apply_content_changes(paths)
    path_changes = apply_path_changes(paths)
    print(
        f"clientplatform rebrand applied: content_files={content_changes}, "
        f"renamed_paths={path_changes}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
