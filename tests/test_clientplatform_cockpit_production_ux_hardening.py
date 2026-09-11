from __future__ import annotations

import unittest
from pathlib import Path

from clientplatform.application import cockpit
from clientplatform.application.cockpit_action_routing import (
    build_cockpit_section_start_payload,
    parse_cockpit_action_start_payload,
)
from clientplatform.domain.tenancy import PlatformRole, TenantContext

ROOT = Path(__file__).resolve().parents[1]
BUSINESS = "11111111-1111-4111-8111-111111111111"
MEMBER = "22222222-2222-4222-8222-222222222222"


def actor(role: PlatformRole) -> TenantContext:
    return TenantContext(
        business_id=BUSINESS,
        user_id=101,
        membership_id=MEMBER,
        role=role,
    )


class CockpitProductionUxHardeningTests(unittest.TestCase):
    def test_creative_is_one_role_aware_canonical_section(self) -> None:
        owner = {item.id: item for item in cockpit.cockpit_navigation(actor(PlatformRole.OWNER))}
        analyst = {item.id: item for item in cockpit.cockpit_navigation(actor(PlatformRole.ANALYST))}
        self.assertEqual(owner["creative"].status, "available")
        self.assertEqual(analyst["creative"].status, "restricted")
        self.assertIn("фирменном стиле", owner["creative"].summary)
        payload = build_cockpit_section_start_payload(
            business_id=BUSINESS,
            section="creative",
        )
        parsed = parse_cockpit_action_start_payload(payload)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.business_id, BUSINESS)
        self.assertEqual(parsed.section, "creative")

    def test_business_context_generation_is_shared_by_every_async_surface(self) -> None:
        shell = (ROOT / "clientplatform/runtime/cockpit_http.py").read_text(encoding="utf-8")
        self.assertIn("businessContextGeneration", shell)
        self.assertIn("captureBusinessContext", shell)
        self.assertIn("assertBusinessContextCurrent", shell)
        self.assertIn("clientplatform:business-context-changing", shell)
        self.assertIn("generation !== businessContextGeneration", shell)
        self.assertIn("payload && payload.business_id", shell)
        for filename in (
            "cockpit_customers.js",
            "cockpit_calendar.js",
            "cockpit_sales.js",
            "cockpit_connections.js",
            "cockpit_settings.js",
            "cockpit_business_workspace.js",
        ):
            source = (ROOT / "clientplatform/runtime" / filename).read_text(encoding="utf-8")
            with self.subTest(filename=filename):
                self.assertIn("captureBusinessContext", source)
                self.assertIn("assertBusinessContextCurrent", source)
                self.assertIn("clientplatform:business-context-changing", source)
                self.assertIn("workspace_context_changed", source)

    def test_shell_uses_progressive_disclosure_and_clear_telegram_handoffs(self) -> None:
        source = (ROOT / "clientplatform/runtime/cockpit_http.py").read_text(encoding="utf-8")
        self.assertIn("className = 'nav-group'", source)
        self.assertIn("details.open = Boolean(openByDefault)", source)
        self.assertIn("groupIndex === 0", source)
        self.assertIn("'В боте'", source)
        self.assertIn("Продолжить в Telegram", source)
        self.assertNotIn("Ни одна функция не скрыта и не отключена", source)
        self.assertNotIn("Все возможности раздела", source)

    def test_high_value_create_actions_are_before_long_lists(self) -> None:
        source = (ROOT / "clientplatform/runtime/cockpit_http.py").read_text(encoding="utf-8")
        self.assertLess(source.index('id="calendar-manage"'), source.index('id="calendar-list"'))
        self.assertLess(source.index('id="services-add"'), source.index('id="services-list"'))
        self.assertLess(source.index('id="growth-creative"'), source.index('id="growth-metrics"'))
        workspace = (ROOT / "clientplatform/runtime/cockpit_business_workspace.js").read_text(encoding="utf-8")
        self.assertIn('openCanonical("creative", growthCreative)', workspace)
        self.assertIn('hasAvailableSection("creative")', workspace)

    def test_mobile_and_desktop_layout_have_accessible_interaction_sizes(self) -> None:
        source = (ROOT / "clientplatform/runtime/cockpit_http.py").read_text(encoding="utf-8")
        self.assertIn("max-width:980px", source)
        self.assertIn(".metrics{grid-template-columns:repeat(2,minmax(0,1fr))}", source)
        self.assertIn(".money{grid-template-columns:1fr}", source)
        self.assertIn("min-height:46px", source)
        self.assertIn("min-height:48px", source)
        self.assertNotIn("min-height:40px", source)
        self.assertNotIn("min-height:42px", source)
        self.assertIn("button:focus-visible", source)
        self.assertIn("--button:var(--tg-theme-button-color,#1f6fcf)", source)
        self.assertIn("--hint:var(--tg-theme-hint-color,#596775)", source)

    def test_sales_uses_progressive_disclosure_confirmation_and_programmatic_state(self) -> None:
        shell = (ROOT / "clientplatform/runtime/cockpit_http.py").read_text(encoding="utf-8")
        sales = (ROOT / "clientplatform/runtime/cockpit_sales.js").read_text(encoding="utf-8")
        self.assertIn('<details id="sales-note-block"', shell)
        self.assertIn('<details id="sales-result-block"', shell)
        self.assertIn("noteBlock.open = false", sales)
        self.assertIn("resultBlock.open = false", sales)
        self.assertIn("confirmAction(\"Подтвердить результат", sales)
        self.assertIn("confirmAction(\"Подтвердить «Не состоялось»", sales)
        self.assertIn('setAttribute("aria-pressed", active ? "true" : "false")', sales)

    def test_dynamic_price_fields_and_timezone_control_have_human_labels(self) -> None:
        workspace = (ROOT / "clientplatform/runtime/cockpit_business_workspace.js").read_text(encoding="utf-8")
        settings = (ROOT / "clientplatform/runtime/cockpit_settings.js").read_text(encoding="utf-8")
        shell = (ROOT / "clientplatform/runtime/cockpit_http.py").read_text(encoding="utf-8")
        self.assertIn('text(amountLabel, "Цена")', workspace)
        self.assertIn('text(currencyLabel, "Валюта")', workspace)
        self.assertIn("amountLabel.htmlFor = amount.id", workspace)
        self.assertIn('<select id="settings-timezone" required>', shell)
        self.assertIn('["Europe/Moscow", "Москва"]', settings)
        self.assertIn("timeZoneOffset", settings)
        self.assertNotIn("Часовой пояс нужен в формате IANA", settings)

    def test_period_controls_expose_selected_state_and_auth_failure_is_coherent(self) -> None:
        shell = (ROOT / "clientplatform/runtime/cockpit_http.py").read_text(encoding="utf-8")
        workspace = (ROOT / "clientplatform/runtime/cockpit_business_workspace.js").read_text(encoding="utf-8")
        self.assertIn('aria-pressed="false">7 дней', shell)
        self.assertIn('setAttribute("aria-pressed", active ? "true" : "false")', workspace)
        self.assertIn('id="status" class="status" role="status" aria-live="polite"', shell)
        self.assertIn("text(businessTitle, 'Ваш бизнес')", shell)
        self.assertIn("statusAction.focus", shell)

    def test_settings_navigation_describes_only_features_that_exist_here(self) -> None:
        owner = {item.id: item for item in cockpit.cockpit_navigation(actor(PlatformRole.OWNER))}
        self.assertEqual(owner["settings"].title, "Настройки бизнеса")
        self.assertIn("часовой пояс", owner["settings"].summary)
        self.assertNotIn("экспорт", owner["settings"].summary.lower())
        self.assertNotIn("приват", owner["settings"].summary.lower())


if __name__ == "__main__":
    unittest.main()
