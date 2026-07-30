from __future__ import annotations

"""Run the explicit hermetic matrix of critical public user scenarios.

This gate complements the full regression suite with named, auditable end-to-end
and integration journeys across ClientPlatform, Telegram, VK, MAX, payments,
gifts, account linking, privacy and durable delivery. It never calls live
providers and never inherits configured application storage or credentials.
"""

import os
import shutil
import subprocess  # nosec B404 - fixed local commands, no shell
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ScenarioStep:
    name: str
    command: tuple[str, ...]
    env: dict[str, str] | None = None


_SAFE_PARENT_ENV_KEYS = (
    "PATH",
    "PYTHONPATH",
    "HOME",
    "USERPROFILE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
)


def _smoke_bot_token() -> str:
    return "".join(
        (
            "1234",
            "56789",
            ":",
            "ABCDE",
            "FGHIJ",
            "KLMNO",
            "PQRST",
            "UVWXY",
            "Zabcd",
            "efghi",
        )
    )


BASE_ENV = {
    "APP_ENV": "test",
    "LOAD_DOTENV": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
    "VALIDATOR_RELEASE_MODE": "1",
    "VALIDATOR_GUARDRAILS_STRICT": "1",
    "VALIDATOR_SKIP_AUDIO": "1",
    "METRO_DB_ENGINE": "sqlite",
    "DATABASE_URL": "",
    "BOT_TOKEN": _smoke_bot_token(),
    "PAY_PROVIDER_TOKEN": "000000:SCENARIO",
    "ADMIN_IDS": "1",
    "TOKEN_ECONOMY_ENABLED": "1",
    "TOKEN_ENFORCEMENT_MODE": "hard",
    "TELEGRAM_TRANSPORT": "polling",
    "TELEGRAM_WEBHOOK_ENABLED": "0",
    "TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED": "0",
    "MESSENGER_WEBHOOK_ENABLED": "0",
    "MAX_WEBHOOK_ENABLED": "0",
    "VK_WEBHOOK_ENABLED": "0",
    "PAYMENT_HTTP_ENABLED": "0",
}

DEEP_ENV = {
    "DEMO_DIR": "tests/fixtures/audio/demo",
}

CLIENTPLATFORM_SCENARIO_TESTS = (
    # Canonical first vertical: durable ingress replay -> control-bot media ->
    # private storage -> program -> managed bot delivery -> retry/restart ->
    # customer progress -> tenant isolation.
    "tests/test_clientplatform_first_vertical_ingress_replay.py",
    "tests/test_clientplatform_first_vertical_e2e.py",
    # Public entry and the most important vertical boundaries remain visible by name.
    "tests/test_handlers_clientplatform_managed_bot_entry.py",
    "tests/test_clientplatform_program_media_ingest.py",
    "tests/test_clientplatform_voice_media_delivery.py",
    "tests/test_clientplatform_program_progress_portal.py",
)

SCENARIO_TESTS = (
    # Entry, menu, settings, weather, score and completion state machines.
    "tests/test_messenger_text_ui.py",
    "tests/test_messenger_state_transition_contract.py",
    "tests/test_messenger_done_flow.py",
    "tests/test_messenger_post_score_flow.py",
    "tests/test_messenger_pay_gift_text_ui.py",
    # Channel-specific full routes and parity.
    "tests/test_vk_user_journey_e2e.py",
    "tests/test_max_user_journey_e2e.py",
    "tests/test_messenger_completion_contracts.py",
    "tests/test_messenger_button_parity.py",
    "tests/test_messenger_deep_button_parity.py",
    "tests/test_cross_messenger_score_scales.py",
    # Telegram Stars purchase, gift, premium and refund paths.
    "tests/test_telegram_stars_payments.py",
    "tests/test_telegram_stars_premium.py",
    "tests/test_telegram_stars_refunds.py",
    # YooKassa checkout/reconciliation, refunds and recipient gift activation.
    "tests/test_payment_emulation_access_contract.py",
    "tests/test_yookassa_webhook_idempotency.py",
    "tests/test_yookassa_refunds.py",
    "tests/test_gift_checkout_contract.py",
    "tests/test_gift_claim_contract.py",
    "tests/test_gift_claim_concurrency.py",
    # Cross-messenger identity, bridge replay/race/rollback and routing.
    "tests/test_account_identity_foundation.py",
    "tests/test_bridge_atomic_link.py",
    "tests/test_messenger_bridge_text.py",
    "tests/test_account_native_premium_entitlements.py",
    # Delivery failure/retry/readiness, ordered parallelism and automatic channel selection.
    "tests/test_messenger_durable_delivery.py",
    "tests/test_messenger_delivery_health.py",
    "tests/test_messenger_delivery_pool.py",
    "tests/test_auto_delivery_channels.py",
    "tests/test_messenger_text_ui_delivery_channels.py",
    # User data export/erasure, public commands and schema-completeness contracts.
    "tests/test_privacy_controls.py",
    "tests/test_privacy_manifest.py",
    "tests/test_privacy_user_commands.py",
)

STEPS = (
    ScenarioStep(
        "ClientPlatform canonical first vertical",
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *CLIENTPLATFORM_SCENARIO_TESTS,
        ),
    ),
    ScenarioStep(
        "synthetic purchase-to-practice journey",
        (sys.executable, "scripts/user_scenario_gate.py", "--mode", "hermetic", "--json"),
    ),
    ScenarioStep(
        "deep token-audio-messenger journey",
        (sys.executable, "scripts/probe_deep_user_journeys.py", "--json"),
        DEEP_ENV,
    ),
    ScenarioStep(
        "cross-platform scenario matrix",
        (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *SCENARIO_TESTS),
    ),
)


def _isolated_parent_env() -> dict[str, str]:
    return {
        key: value
        for key in _SAFE_PARENT_ENV_KEYS
        if (value := os.environ.get(key)) is not None
    }


def _step_env(step: ScenarioStep, db_path: Path) -> dict[str, str]:
    env = _isolated_parent_env()
    env.update(BASE_ENV)
    env["METRO_DB_PATH"] = str(db_path)
    if step.env:
        env.update(step.env)
    return env


def _run(step: ScenarioStep) -> int:
    temp_dir = Path(tempfile.mkdtemp(prefix="clientplatform_all_user_scenarios_"))
    db_path = temp_dir / "scenario.db"
    env = _step_env(step, db_path)
    print(f"==> {step.name}", flush=True)
    print("cmd:", " ".join(step.command), flush=True)
    try:
        completed = subprocess.run(  # nosec B603 - commands are static and shell=False
            step.command,
            cwd=ROOT,
            env=env,
            check=False,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    if completed.returncode != 0:
        print(
            f"ALL_USER_SCENARIOS_FAILED step={step.name!r} "
            f"code={completed.returncode}",
            flush=True,
        )
    return int(completed.returncode)


def main() -> int:
    for step in STEPS:
        code = _run(step)
        if code:
            return code
    total_files = len(CLIENTPLATFORM_SCENARIO_TESTS) + len(SCENARIO_TESTS)
    print(
        f"ALL_USER_SCENARIOS_OK groups={len(STEPS)} test_files={total_files}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
