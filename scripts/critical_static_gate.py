from __future__ import annotations

import argparse
import subprocess  # nosec B404 - fixed local quality tools without shell
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_RUNTIME_HARDENING_FILES = (
    "core/middlewares.py",
    "core/paths.py",
    "core/runtime_env.py",
    "core/runtime_paths.py",
    "core/telegram_bot.py",
    "runtime/health_server.py",
    "runtime/messenger_ingress.py",
    "runtime/messenger_ingress_reliability.py",
    "runtime/messenger_max_sender.py",
    "runtime/messenger_transport_errors.py",
    "runtime/messenger_vk_sender.py",
    "runtime/messenger_webhooks.py",
    "services/db/read_only.py",
    "services/db/runtime.py",
    "services/db/sql_compat_guard.py",
    "services/messenger/delivery_outbox.py",
    "services/messenger/observability.py",
    "services/messenger/provider_transport.py",
    "services/messenger/webhook_dedupe.py",
    "clientplatform/runtime/health.py",
    "clientplatform/runtime/lifecycle.py",
    "clientplatform/runtime/owner.py",
)

_CLIENTPLATFORM_MONEY_FILES = (
    "clientplatform/application/admin_ops.py",
    "clientplatform/application/outcomes.py",
    "clientplatform/domain/outcomes.py",
    "clientplatform/infrastructure/outcome_repository.py",
    "clientplatform/infrastructure/revenue_attribution_repository.py",
    "services/migrations/clientplatform_business_payment_outcomes_v1.py",
)

_CLIENTPLATFORM_AUTOMATION_POLICY_FILES = (
    "clientplatform/application/automation_policy.py",
    "clientplatform/domain/automation_policy.py",
    "clientplatform/infrastructure/automation_policy_repository.py",
    "services/db/schema/clientplatform_automation_policy.py",
)


_CLIENTPLATFORM_PLATFORM_SUPPORT_TYPE_FILES = (
    "clientplatform/application/support_cases.py",
    "clientplatform/domain/support_cases.py",
    "clientplatform/infrastructure/support_case_repository.py",
    "services/platform_support_access.py",
    "services/db/schema/clientplatform_platform_support.py",
)

_CLIENTPLATFORM_PLATFORM_SUPPORT_SECURITY_FILES = (
    *_CLIENTPLATFORM_PLATFORM_SUPPORT_TYPE_FILES,
    "handlers/clientplatform_entry.py",
)

_CLIENTPLATFORM_PLATFORM_DIRECTORY_TYPE_FILES = (
    "clientplatform/application/platform_directory.py",
    "clientplatform/domain/platform_directory.py",
    "clientplatform/infrastructure/platform_operator_audit_repository.py",
    "scripts/postgres_platform_directory_smoke.py",
)

_CLIENTPLATFORM_PLATFORM_DIRECTORY_SECURITY_FILES = (
    *_CLIENTPLATFORM_PLATFORM_DIRECTORY_TYPE_FILES,
)


_CLIENTPLATFORM_MANAGED_BOT_TYPE_FILES = (
    "clientplatform/application/existing_bot_onboarding.py",
    "clientplatform/application/managed_bot_onboarding.py",
    "clientplatform/domain/bot_provisioning.py",
    "clientplatform/infrastructure/managed_bot_credentials.py",
    "clientplatform/infrastructure/managed_bot_onboarding_repository.py",
    "clientplatform/runtime/secrets.py",
    "services/migrations/clientplatform_managed_bot_provider_v1.py",
)

_CLIENTPLATFORM_MANAGED_BOT_SECURITY_FILES = (
    *_CLIENTPLATFORM_MANAGED_BOT_TYPE_FILES,
    "clientplatform/application/managed_bot_owner.py",
    "clientplatform/runtime/bot_provisioning.py",
    "handlers/clientplatform_existing_bot_onboarding.py",
    "handlers/clientplatform_managed_bot_onboarding.py",
    "scripts/clientplatform_bot_gateway_preflight.py",
)

_CLIENTPLATFORM_SALES_UI_FILES = (
    "clientplatform/application/owner_booking_journey.py",
    "clientplatform/application/sales_agent.py",
    "clientplatform/application/sales_orchestration.py",
    "clientplatform/application/sales_ui.py",
    "clientplatform/infrastructure/sales_action_repository.py",
    "clientplatform/infrastructure/sales_ui_repository.py",
    "handlers/clientplatform_sales.py",
    "handlers/clientplatform_sales_install.py",
)

_CLIENTPLATFORM_YANDEX_ANALYTICS_FILES = (
    "clientplatform/application/yandex_growth_analytics.py",
    "clientplatform/integrations/yandex_direct_analytics.py",
    "handlers/clientplatform_yandex_analytics.py",
)

_CLIENTPLATFORM_EXTERNAL_PRODUCT_FILES = (
    "clientplatform/application/external_products.py",
    "clientplatform/domain/external_products.py",
    "clientplatform/infrastructure/attribution_repository.py",
    "clientplatform/infrastructure/external_product_repository.py",
    "clientplatform/runtime/external_product_http.py",
    "services/db/schema/clientplatform_external_products.py",
)


_CLIENTPLATFORM_EMAIL_OUTBOUND_FILES = (
    "clientplatform/application/email_connections.py",
    "clientplatform/application/partner_runtime.py",
    "clientplatform/domain/email_outbound.py",
    "clientplatform/infrastructure/connection_credentials.py",
    "clientplatform/infrastructure/safe_unified_dispatch_outbox.py",
    "clientplatform/runtime/dispatch_runtime.py",
    "clientplatform/transport/email.py",
    "handlers/clientplatform_partner_growth.py",
    "services/migrations/clientplatform_email_outbound_v1.py",
)


_CLIENTPLATFORM_NATIVE_MESSENGER_FILES = (
    "clientplatform/application/dispatch_worker.py",
    "clientplatform/application/max_dispatch_pacing.py",
    "clientplatform/application/native_messenger_onboarding.py",
    "clientplatform/runtime/max_two_phase_media.py",
    "clientplatform/runtime/messenger_channel_ingress.py",
    "clientplatform/runtime/messenger_provider_clients.py",
    "clientplatform/runtime/native_messenger_http_admission.py",
    "clientplatform/runtime/native_messenger_reconciliation.py",
    "clientplatform/runtime/native_messenger_setup_http.py",
    "clientplatform/runtime/native_messenger_setup_links.py",
    "clientplatform/transport/base.py",
    "clientplatform/transport/native_messenger.py",
    "scripts/clientplatform_messenger_channels_preflight.py",
)

TYPE_CONTRACT_FILES = (
    "check_db.py",
    "clientplatform/application/ad_oauth_sessions.py",
    "clientplatform/application/native_member_interactions.py",
    "clientplatform/infrastructure/ad_oauth_session_store.py",
    "clientplatform/infrastructure/native_messenger_provisioning_repository.py",
    "clientplatform/infrastructure/tenancy_repository.py",
    "clientplatform/privacy_manifest.py",
    "handlers/clientplatform_booking_wizard_ux.py",
    "scripts/all_user_scenario_gate.py",
    "scripts/archive_legacy_sqlite.py",
    "scripts/backup_db.py",
    "scripts/check_deploy_governance.py",
    "scripts/clientplatform_prepare_production_env.py",
    "scripts/clientplatform_sales_production_smoke.py",
    "scripts/post_deploy_verify.py",
    "scripts/postgres_ci_smoke.py",
    "scripts/probe_scheduler_job_live.py",
    "scripts/production_gate.py",
    "scripts/register_max_webhook.py",
    "scripts/restore_db.py",
    "scripts/stress_db.py",
    "services/accounts/identity.py",
    *_CLIENTPLATFORM_AUTOMATION_POLICY_FILES,
    *_CLIENTPLATFORM_PLATFORM_SUPPORT_TYPE_FILES,
    *_CLIENTPLATFORM_PLATFORM_DIRECTORY_TYPE_FILES,
    *_CLIENTPLATFORM_MANAGED_BOT_TYPE_FILES,
    *_CLIENTPLATFORM_EXTERNAL_PRODUCT_FILES,
    *_CLIENTPLATFORM_EMAIL_OUTBOUND_FILES,
    *_CLIENTPLATFORM_NATIVE_MESSENGER_FILES,
    *_CLIENTPLATFORM_SALES_UI_FILES,
    *_CLIENTPLATFORM_YANDEX_ANALYTICS_FILES,
    *_CLIENTPLATFORM_MONEY_FILES,
    *_RUNTIME_HARDENING_FILES,
)

SECURITY_SCAN_PATHS = (
    "check_db.py",
    "clientplatform/application/ad_oauth_sessions.py",
    "clientplatform/application/native_member_interactions.py",
    "clientplatform/infrastructure/ad_oauth_session_store.py",
    "clientplatform/infrastructure/native_messenger_provisioning_repository.py",
    "clientplatform/infrastructure/tenancy_repository.py",
    "clientplatform/integrations/yandex_screen_code.py",
    "clientplatform/privacy_manifest.py",
    "handlers/clientplatform_booking_wizard_ux.py",
    "handlers/clientplatform_yandex_screen_code.py",
    "runtime/ad_oauth_http.py",
    "scripts/all_user_scenario_gate.py",
    "scripts/archive_legacy_sqlite.py",
    "scripts/backup_db.py",
    "scripts/clientplatform_ad_connections_preflight.py",
    "scripts/clientplatform_prepare_production_env.py",
    "scripts/clientplatform_sales_production_smoke.py",
    "scripts/post_deploy_verify.py",
    "scripts/postgres_ci_smoke.py",
    "scripts/probe_scheduler_job_live.py",
    "scripts/production_gate.py",
    "scripts/register_max_webhook.py",
    "scripts/restore_db.py",
    "scripts/stress_db.py",
    "services/accounts/identity.py",
    *_CLIENTPLATFORM_AUTOMATION_POLICY_FILES,
    *_CLIENTPLATFORM_PLATFORM_SUPPORT_SECURITY_FILES,
    *_CLIENTPLATFORM_PLATFORM_DIRECTORY_SECURITY_FILES,
    *_CLIENTPLATFORM_MANAGED_BOT_SECURITY_FILES,
    *_CLIENTPLATFORM_EXTERNAL_PRODUCT_FILES,
    *_CLIENTPLATFORM_EMAIL_OUTBOUND_FILES,
    *_CLIENTPLATFORM_NATIVE_MESSENGER_FILES,
    *_CLIENTPLATFORM_SALES_UI_FILES,
    *_CLIENTPLATFORM_YANDEX_ANALYTICS_FILES,
    *_CLIENTPLATFORM_MONEY_FILES,
    *_RUNTIME_HARDENING_FILES,
)


def missing_critical_paths() -> list[str]:
    declared = sorted(set(TYPE_CONTRACT_FILES) | set(SECURITY_SCAN_PATHS))
    return [relative for relative in declared if not (ROOT / relative).exists()]


def _run(command: list[str]) -> int:
    proc = subprocess.run(  # nosec B603 - fixed executable and repository-owned path manifest
        command,
        cwd=str(ROOT),
        check=False,
    )
    return int(proc.returncode)


def run_mypy() -> int:
    return _run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--follow-imports=skip",
            "--check-untyped-defs",
            *TYPE_CONTRACT_FILES,
        ]
    )


def run_bandit() -> int:
    return _run(
        [
            sys.executable,
            "-m",
            "bandit",
            "-q",
            "-r",
            "-c",
            "pyproject.toml",
            *SECURITY_SCAN_PATHS,
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the centralized critical static-analysis gate")
    parser.add_argument("check", choices=("manifest", "mypy", "bandit", "all"))
    args = parser.parse_args()

    missing = missing_critical_paths()
    if missing:
        print("CRITICAL_STATIC_MANIFEST_FAILED")
        for relative in missing:
            print(f"missing: {relative}")
        return 2
    print(
        "CRITICAL_STATIC_MANIFEST_OK "
        f"type_files={len(TYPE_CONTRACT_FILES)} security_paths={len(SECURITY_SCAN_PATHS)}"
    )

    if args.check == "manifest":
        return 0
    if args.check in {"mypy", "all"}:
        code = run_mypy()
        if code:
            return code
        print("CRITICAL_MYPY_OK")
    if args.check in {"bandit", "all"}:
        code = run_bandit()
        if code:
            return code
        print("CRITICAL_BANDIT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())