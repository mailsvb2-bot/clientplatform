"""Run the hermetic matrix of canonical ClientPlatform user scenarios.

The gate names the product-critical verticals explicitly and gives every group a
private SQLite database. It never calls live providers and never depends on the
retired imported product model.
"""

from __future__ import annotations

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
    tests: tuple[str, ...]


_SAFE_PARENT_ENV_KEYS = (
    "PATH", "PYTHONPATH", "HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP",
    "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
)


def _smoke_bot_token() -> str:
    return "".join(("1234", "56789", ":", "ABCDE", "FGHIJ", "KLMNO", "PQRST", "UVWXY", "Zabcd", "efghi"))


BASE_ENV = {
    "APP_ENV": "test",
    "LOAD_DOTENV": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
    "VALIDATOR_RELEASE_MODE": "1",
    "VALIDATOR_GUARDRAILS_STRICT": "1",
    "CLIENTPLATFORM_DB_ENGINE": "sqlite",
    "DATABASE_URL": "",
    "BOT_TOKEN": _smoke_bot_token(),
    "ADMIN_IDS": "1",
    "TELEGRAM_TRANSPORT": "polling",
    "TELEGRAM_WEBHOOK_ENABLED": "0",
    "TELEGRAM_LEGACY_TOKEN_WEBHOOK_ENABLED": "0",
    "MESSENGER_WEBHOOK_ENABLED": "0",
    "MAX_WEBHOOK_ENABLED": "0",
    "VK_WEBHOOK_ENABLED": "0",
}

CLIENTPLATFORM_SCENARIO_TESTS = (
    "tests/test_clientplatform_first_vertical_ingress_replay.py",
    "tests/test_clientplatform_first_vertical_e2e.py",
    "tests/test_handlers_clientplatform_managed_bot_entry.py",
    "tests/test_clientplatform_program_media_ingest.py",
    "tests/test_clientplatform_voice_media_delivery.py",
    "tests/test_clientplatform_program_progress_portal.py",
)

OWNER_RUNTIME_TESTS = (
    "tests/test_clientplatform_control_bot_behavior.py",
    "tests/test_clientplatform_managed_bot_lifecycle.py",
    "tests/test_clientplatform_runtime_ownership.py",
    "tests/test_clientplatform_health_readiness.py",
    "tests/test_clientplatform_native_runtime_policy.py",
)

OMNICHANNEL_TESTS = (
    "tests/test_clientplatform_canonical_omnichannel_no_telegram.py",
    "tests/test_clientplatform_omnichannel_runtime.py",
    "tests/test_clientplatform_native_messenger_onboarding.py",
    "tests/test_clientplatform_native_customer_interactions.py",
    "tests/test_clientplatform_native_member_full_parity.py",
    "tests/test_clientplatform_dual_role_entry.py",
    "tests/test_clientplatform_channel_neutral_invites.py",
    "tests/test_clientplatform_messenger_switching.py",
)

COMMERCIAL_OUTCOME_TESTS = (
    "tests/test_clientplatform_business_payment_outcomes_migration.py",
    "tests/test_clientplatform_payment_evidence_m4001.py",
    "tests/test_clientplatform_public_storefront_sales_signal.py",
    "tests/test_clientplatform_promotions.py",
    "tests/test_clientplatform_revenue_attribution.py",
    "tests/test_clientplatform_outcomes.py",
    "tests/test_clientplatform_booking.py",
    "tests/test_clientplatform_commercial_ladder.py",
)

STEPS = (
    ScenarioStep("ClientPlatform canonical first vertical", CLIENTPLATFORM_SCENARIO_TESTS),
    ScenarioStep("ClientPlatform owner and runtime", OWNER_RUNTIME_TESTS),
    ScenarioStep("ClientPlatform omnichannel parity", OMNICHANNEL_TESTS),
    ScenarioStep("ClientPlatform commercial outcomes", COMMERCIAL_OUTCOME_TESTS),
)


def _isolated_parent_env() -> dict[str, str]:
    return {
        key: value
        for key in _SAFE_PARENT_ENV_KEYS
        if (value := os.environ.get(key)) is not None
    }


def _step_env(db_path: Path) -> dict[str, str]:
    env = _isolated_parent_env()
    env.update(BASE_ENV)
    env["CLIENTPLATFORM_DB_PATH"] = str(db_path)
    return env


def _run(step: ScenarioStep) -> int:
    temp_dir = Path(tempfile.mkdtemp(prefix="clientplatform_user_scenarios_"))
    db_path = temp_dir / "scenario.db"
    command = (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *step.tests,
    )
    print(f"==> {step.name}", flush=True)
    print("cmd:", " ".join(command), flush=True)
    try:
        completed = subprocess.run(  # nosec B603 - static command, shell=False
            command,
            cwd=ROOT,
            env=_step_env(db_path),
            check=False,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    if completed.returncode != 0:
        print(
            f"ALL_USER_SCENARIOS_FAILED step={step.name!r} code={completed.returncode}",
            flush=True,
        )
    return int(completed.returncode)


def main() -> int:
    for step in STEPS:
        code = _run(step)
        if code:
            return code
    total_files = sum(len(step.tests) for step in STEPS)
    print(
        f"ALL_USER_SCENARIOS_OK groups={len(STEPS)} test_files={total_files}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
