from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def _forbidden_tokens() -> tuple[str, ...]:
    removed_latin = "metro" + "therapy"
    removed_cyrillic = "метро" + "терап"
    inherited_prefix = "MET" + "RO_"
    inherited_lower_underscore = "met" + "ro_"
    inherited_lower_hyphen = "met" + "ro-"
    synthetic_source_alias = "clientplatform" + "-bot-telegram"
    return (
        removed_latin,
        removed_cyrillic,
        inherited_prefix,
        inherited_lower_underscore,
        inherited_lower_hyphen,
        synthetic_source_alias,
    )


def main() -> None:
    violations: list[str] = []
    tokens = _forbidden_tokens()
    for path in _tracked_files():
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        lowered = text.casefold()
        for token in tokens:
            haystack = text if token.isupper() else lowered
            needle = token if token.isupper() else token.casefold()
            if needle in haystack:
                violations.append(f"{path.relative_to(ROOT)}: forbidden product residue")
                break
    if violations:
        rendered = "\n".join(f"- {item}" for item in violations)
        raise SystemExit(f"CLIENTPLATFORM_PRODUCT_PURITY_FAILED:\n{rendered}")

    app = (ROOT / "app.py").read_text(encoding="utf-8")
    required = "dp.include_router(clientplatform_entry.router)"
    if app.count(required) != 1:
        raise SystemExit("CLIENTPLATFORM_PRODUCT_PURITY_FAILED: canonical entry router must be unique")
    forbidden_router_fragments = (
        "dp.include_router(start.router)",
        "dp.include_router(menu.router)",
        "dp.include_router(demo.router)",
        "dp.include_router(mood.router)",
        "dp.include_router(gift_flow.router)",
        "dp.include_router(weather.router)",
        "dp.include_router(payments.router)",
    )
    if any(fragment in app for fragment in forbidden_router_fragments):
        raise SystemExit("CLIENTPLATFORM_PRODUCT_PURITY_FAILED: removed product router is registered")

    live_release_files = (
        "app.py",
        "services/validator.py",
        "services/db/schema/__init__.py",
        "scripts/regression_gate.py",
        "scripts/all_user_scenario_gate.py",
        "scripts/smoke.py",
        "scripts/production_gate.py",
        "scripts/post_deploy_verify.py",
        "scripts/postgres_ci_smoke.py",
        "scripts/server_quality_gate.sh",
        "scripts/critical_static_gate.py",
        ".github/workflows/release-gate.yml",
        ".github/workflows/ci.yml",
        ".github/workflows/postgres-smoke.yml",
        "deploy/clientplatform/clientplatform.production.env.example",
    )
    retired_release_fragments = (
        "TOKEN_" + "ECONOMY_ENABLED",
        "TOKEN_" + "ENFORCEMENT_MODE",
        "practice_" + "start_7",
        "practice_" + "personal_month",
        "probe_" + "auto_audio",
        "probe_" + "user_journey_e2e",
        "probe_" + "deep_user_journeys",
        "probe_" + "payment_reconciliation_live",
        "services." + "practice_tokens",
        "services.payments." + "telegram_stars",
    )
    for relative in live_release_files:
        text = (ROOT / relative).read_text(encoding="utf-8")
        found = [fragment for fragment in retired_release_fragments if fragment in text]
        if found:
            raise SystemExit(
                f"CLIENTPLATFORM_PRODUCT_PURITY_FAILED: retired release dependency in {relative}: {found}"
            )

    retired_runtime_paths = (
        "core/engine.py",
        "core/ai/decision_core.py",
        "core/ai/action_gateway.py",
        "core/runtime/self_healing.py",
        "services/scheduler.py",
        "runtime/payment_http.py",
        "services/payments",
        "services/auto_audio.py",
        "services/mood.py",
        "services/practice_tokens.py",
        "services/subscription.py",
    )
    present = [relative for relative in retired_runtime_paths if (ROOT / relative).exists()]
    if present:
        raise SystemExit(
            "CLIENTPLATFORM_PRODUCT_PURITY_FAILED: retired runtime path still present: "
            + ", ".join(present)
        )

    webhooks = (ROOT / "runtime" / "messenger_webhooks.py").read_text(encoding="utf-8")
    if '"/pay/yookassa"' in webhooks or "_register_audio_routes" in webhooks:
        raise SystemExit("CLIENTPLATFORM_PRODUCT_PURITY_FAILED: removed public product route is active")

    print("CLIENTPLATFORM_PRODUCT_PURITY_OK")


if __name__ == "__main__":
    main()
