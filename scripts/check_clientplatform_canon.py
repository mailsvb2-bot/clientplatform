from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANON = ROOT / "docs" / "CLIENTPLATFORM_CANON_TZ.md"
PROVENANCE = ROOT / "docs" / "BASELINE_PROVENANCE.md"
README = ROOT / "README.md"
BASELINE_SHA = "b4ac43c2961fb581078aedc25efeffd2ab4ecb34"


REQUIRED_CANON_FRAGMENTS = (
    "единственный нормативный документ продукта ClientPlatform",
    "Центральный управляющий бот ClientPlatform",
    "Персональные клиентские боты",
    "Мультитенантность и изоляция данных",
    "Публичный репозиторий",
    "Первый обязательный вертикальный сценарий",
    "Regression Wall",
    "Критерии готовности MVP",
    "Не подключать ClientPlatform к production-инфраструктуре других продуктов",
)

REQUIRED_PROVENANCE_FRAGMENTS = (
    "mailsvb2-bot/clientplatform",
    BASELINE_SHA,
    "must use only ClientPlatform-owned production resources",
)

REQUIRED_README_FRAGMENTS = (
    "# ClientPlatform",
    "ClientPlatform — мультитенантная платформа",
    "docs/CLIENTPLATFORM_CANON_TZ.md",
    "docs/BASELINE_PROVENANCE.md",
    BASELINE_SHA,
    "разрешено запускать только с собственной production-конфигурацией ClientPlatform",
)


def _read_required(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"clientplatform canon gate failed: missing required file {path.relative_to(ROOT)}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise SystemExit(f"clientplatform canon gate failed: empty required file {path.relative_to(ROOT)}")
    return text


def _require_fragments(label: str, text: str, fragments: tuple[str, ...]) -> None:
    normalized = text.casefold()
    missing = [fragment for fragment in fragments if fragment.casefold() not in normalized]
    if missing:
        rendered = "\n".join(f"- {fragment}" for fragment in missing)
        raise SystemExit(f"clientplatform canon gate failed: {label} lost required contracts:\n{rendered}")


def main() -> None:
    canon = _read_required(CANON)
    provenance = _read_required(PROVENANCE)
    readme = _read_required(README)

    if len(canon.splitlines()) < 250:
        raise SystemExit("clientplatform canon gate failed: canonical specification is unexpectedly truncated")

    _require_fragments("docs/CLIENTPLATFORM_CANON_TZ.md", canon, REQUIRED_CANON_FRAGMENTS)
    _require_fragments("docs/BASELINE_PROVENANCE.md", provenance, REQUIRED_PROVENANCE_FRAGMENTS)
    _require_fragments("README.md", readme, REQUIRED_README_FRAGMENTS)

    if "репозиторий остаётся публичным" not in readme.casefold():
        raise SystemExit("clientplatform canon gate failed: public-repository safety notice is missing")

    print("CLIENTPLATFORM_CANON_GATE_OK")


if __name__ == "__main__":
    main()
