from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANON = ROOT / "docs" / "A1_CANON_TZ.md"
PROVENANCE = ROOT / "docs" / "BASELINE_PROVENANCE.md"
README = ROOT / "README.md"
BASELINE_SHA = "b4ac43c2961fb581078aedc25efeffd2ab4ecb34"


REQUIRED_CANON_FRAGMENTS = (
    "единственный нормативный документ продукта А1",
    "Центральный управляющий бот А1",
    "Персональные клиентские боты",
    "Мультитенантность и изоляция данных",
    "Публичный репозиторий",
    "Первый обязательный вертикальный сценарий",
    "Regression Wall",
    "Критерии готовности MVP",
    "не подключать ClientPlatform к production-инфраструктуре Метротерапии",
)

REQUIRED_PROVENANCE_FRAGMENTS = (
    "mailsvb2-bot/metrotherapy-bot-telegram",
    "mailsvb2-bot/clientplatform",
    BASELINE_SHA,
    "must never use Metrotherapy production",
)

REQUIRED_README_FRAGMENTS = (
    "ClientPlatform / А1",
    "docs/A1_CANON_TZ.md",
    "docs/BASELINE_PROVENANCE.md",
    BASELINE_SHA,
    "запрещено запускать с production-конфигурацией Метротерапии",
)


def _read_required(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"A1 canon gate failed: missing required file {path.relative_to(ROOT)}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise SystemExit(f"A1 canon gate failed: empty required file {path.relative_to(ROOT)}")
    return text


def _require_fragments(label: str, text: str, fragments: tuple[str, ...]) -> None:
    missing = [fragment for fragment in fragments if fragment not in text]
    if missing:
        rendered = "\n".join(f"- {fragment}" for fragment in missing)
        raise SystemExit(f"A1 canon gate failed: {label} lost required contracts:\n{rendered}")


def main() -> None:
    canon = _read_required(CANON)
    provenance = _read_required(PROVENANCE)
    readme = _read_required(README)

    if len(canon.splitlines()) < 250:
        raise SystemExit("A1 canon gate failed: canonical specification is unexpectedly truncated")

    _require_fragments("docs/A1_CANON_TZ.md", canon, REQUIRED_CANON_FRAGMENTS)
    _require_fragments("docs/BASELINE_PROVENANCE.md", provenance, REQUIRED_PROVENANCE_FRAGMENTS)
    _require_fragments("README.md", readme, REQUIRED_README_FRAGMENTS)

    if "Репозиторий остаётся публичным" not in readme:
        raise SystemExit("A1 canon gate failed: public-repository safety notice is missing")

    print("A1_CANON_GATE_OK")


if __name__ == "__main__":
    main()
