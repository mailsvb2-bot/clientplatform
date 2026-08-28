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
    "runtime/messenger_ingress.py",
    "runtime/messenger_max_sender.py",
    "runtime/messenger_transport_errors.py",
    "runtime/messenger_vk_sender.py",
    "services/audio_asset_integrity.py",
    "services/auto_audio.py",
    "services/db/read_only.py",
    "services/db/runtime.py",
    "services/db/sql_compat_guard.py",
    "services/messenger/media_assets.py",
    "services/messenger/observability.py",
    "services/messenger/provider_transport.py",
    "services/prewarm.py",
    "services/scheduler.py",
    "services/validators/audio.py",
)

_REWARD_HARDENING_FILES = (
    "services/bonuses.py",
    "services/gift_claims.py",
    "services/migrations/practice_reward_grants_v1.py",
    "services/practice_tokens.py",
    "services/referrals.py",
    "services/reward_tokens.py",
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
    "clientplatform/infrastructure/ad_oauth_session_store.py",
    "clientplatform/infrastructure/native_messenger_provisioning_repository.py",
    "dashboard/sla_dashboard.py",
    "dashboard/sla_retention_money.py",
    "handlers/clientplatform_booking_wizard_ux.py",
    "handlers/info.py",
    "runtime/messenger_ingress_reliability.py",
    "runtime/messenger_media_http.py",
    "runtime/messenger_webhooks.py",
    "runtime/payment_http.py",
    "runtime/payment_webhook_admission.py",
    "scripts/all_user_scenario_gate.py",
    "scripts/archive_legacy_sqlite.py",
    "scripts/backup_db.py",
    "scripts/check_deploy_governance.py",
    "scripts/immutable_release.py",
    "scripts/probe_auto_audio_dry_run.py",
    "scripts/probe_payment_reconciliation_live.py",
    "scripts/probe_scheduler_job_live.py",
    "scripts/probe_user_journey_e2e.py",
    "scripts/production_gate.py",
    "scripts/register_max_webhook.py",
    "scripts/restore_db.py",
    "scripts/stress_db.py",
    "scripts/user_scenario_gate.py",
    "services/accounts/identity.py",
    "services/messenger/audio_access.py",
    "services/messenger/delivery_outbox.py",
    "services/messenger/progress_charts.py",
    "services/messenger/text_ui.py",
    "services/messenger/webhook_dedupe.py",
    "services/payments/checkout_intent.py",
    "services/payments/receipt_contract.py",
    "services/payments/retry_queue.py",
    "services/payments/telegram_stars.py",
    "services/payments/telegram_stars_refunds.py",
    "services/payments/verified_reconciliation.py",
    "services/payments/yookassa_checkout.py",
    "services/payments/yookassa_refunds.py",
    "services/practice_token_lots.py",
    "services/practice_tokens_access_core.py",
    "services/practice_tokens_wallet.py",
    "services/premium_entitlements.py",
    "services/privacy_controls.py",
    "services/probe_safety.py",
    "services/sales_desk.py",
    "services/sales_desk_repository.py",
    "services/sales_desk_sync.py",
    *_CLIENTPLATFORM_MANAGED_BOT_TYPE_FILES,
    *_CLIENTPLATFORM_EXTERNAL_PRODUCT_FILES,
    *_CLIENTPLATFORM_EMAIL_OUTBOUND_FILES,
    *_CLIENTPLATFORM_NATIVE_MESSENGER_FILES,
    *_CLIENTPLATFORM_SALES_UI_FILES,
    *_CLIENTPLATFORM_YANDEX_ANALYTICS_FILES,
    *_REWARD_HARDENING_FILES,
    *_RUNTIME_HARDENING_FILES,
)

SECURITY_SCAN_PATHS = (
    "check_db.py",
    "clientplatform/application/ad_oauth_sessions.py",
    "clientplatform/infrastructure/ad_oauth_session_store.py",
    "clientplatform/infrastructure/native_messenger_provisioning_repository.py",
    "clientplatform/integrations/yandex_screen_code.py",
    "dashboard/sla_dashboard.py",
    "dashboard/sla_retention_money.py",
    "handlers/clientplatform_booking_wizard_ux.py",
    "handlers/clientplatform_yandex_screen_code.py",
    "handlers/info.py",
    "runtime/ad_oauth_http.py",
    "runtime/messenger_ingress_reliability.py",
    "runtime/messenger_media_http.py",
    "runtime/messenger_webhooks.py",
    "runtime/payment_http.py",
    "runtime/payment_webhook_admission.py",
    "scripts/all_user_scenario_gate.py",
    "scripts/archive_legacy_sqlite.py",
    "scripts/backup_db.py",
    "scripts/clientplatform_ad_connections_preflight.py",
    "scripts/clientplatform_prepare_production_env.py",
    "scripts/immutable_release.py",
    "scripts/probe_auto_audio_dry_run.py",
    "scripts/probe_payment_reconciliation_live.py",
    "scripts/probe_scheduler_job_live.py",
    "scripts/probe_user_journey_e2e.py",
    "scripts/production_gate.py",
    "scripts/register_max_webhook.py",
    "scripts/restore_db.py",
    "scripts/stress_db.py",
    "scripts/user_scenario_gate.py",
    "services/accounts/identity.py",
    "services/messenger/audio_access.py",
    "services/messenger/delivery_outbox.py",
    "services/messenger/progress_charts.py",
    "services/messenger/text_ui.py",
    "services/messenger/webhook_dedupe.py",
    "services/payments",
    "services/practice_token_lots.py",
    "services/practice_tokens_access_core.py",
    "services/practice_tokens_wallet.py",
    "services/premium_entitlements.py",
    "services/privacy_controls.py",
    "services/probe_safety.py",
    "services/sales_desk.py",
    "services/sales_desk_db.py",
    "services/sales_desk_repository.py",
    "services/sales_desk_sync.py",
    *_CLIENTPLATFORM_MANAGED_BOT_SECURITY_FILES,
    *_CLIENTPLATFORM_EXTERNAL_PRODUCT_FILES,
    *_CLIENTPLATFORM_EMAIL_OUTBOUND_FILES,
    *_CLIENTPLATFORM_NATIVE_MESSENGER_FILES,
    *_CLIENTPLATFORM_SALES_UI_FILES,
    *_CLIENTPLATFORM_YANDEX_ANALYTICS_FILES,
    *_REWARD_HARDENING_FILES,
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