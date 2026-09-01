from __future__ import annotations

import unittest

from clientplatform.application.native_member_interactions import parse_native_member_interaction


# Every cpm action that existed on main before the beginner-UX pass. The UX may
# reorganize or relabel these actions, but it must never silently delete them.
LEGACY_NATIVE_ACTIONS = frozenset(
    {
        'acquire', 'activity-edit-help', 'activity-edit-text', 'ad-spend',
        'ad-spend-launch', 'attention', 'automation-approve', 'automation-reject',
        'automation-revoke', 'autopilot', 'autopilot-disable', 'autopilot-enable',
        'behavior', 'booking-open', 'bookings', 'connect-max', 'connect-telegram',
        'connect-vk', 'copy', 'customer', 'customers', 'format-disable',
        'format-enable', 'formats', 'funnel', 'funnel2', 'growth',
        'growth-lifecycle', 'growth-more', 'growth-sales', 'invite-new', 'invites',
        'manage', 'manage-more', 'member', 'member-add-help', 'member-add-text',
        'member-revoke', 'member-role', 'members', 'menu', 'menu-all', 'messengers',
        'money', 'offering-new', 'offering-new-text', 'offers', 'pay-refund',
        'pay-refund-ok', 'payment-new', 'payment-new-text', 'payments', 'permissions',
        'price-set', 'price-set-text', 'prices', 'program-create',
        'program-create-text', 'program-deliver', 'program-deliver-text',
        'program-lesson', 'program-lesson-text', 'program-publish', 'programs',
        'publication-cancel', 'publication-cancel-ok', 'publication-new',
        'publication-new-text', 'publication-publish', 'publication-schedule',
        'publication-schedule-text', 'publications', 'reactivate',
        'reactivate-approve', 'recent', 'release', 'retention', 'sales',
        'sales-actions', 'sales-assign', 'sales-close-help', 'sales-followup-cancel',
        'sales-followup-help', 'sales-followup-menu', 'sales-followup-optout-help',
        'sales-handoff-claim', 'sales-handoff-resolve', 'sales-handoffs', 'sales-lead',
        'sales-next-help', 'sales-note-help', 'sales-recent', 'sales-reopen',
        'sales-result-menu', 'sales-stage', 'sales-unassign', 'segments', 'system',
        'tariff', 'team', 'today', 'today-full', 'work', 'work-more',
    }
)


class NativeActionPreservationTests(unittest.TestCase):
    def test_all_preexisting_cpm_actions_are_still_recognized(self) -> None:
        missing: list[str] = []
        for action in sorted(LEGACY_NATIVE_ACTIONS):
            parsed = parse_native_member_interaction(f"cpm:{action}")
            if parsed.action != action:
                missing.append(action)
        self.assertEqual(missing, [])

    def test_preexisting_advanced_text_grammar_still_works(self) -> None:
        cases = {
            "деятельность Ремонт автомобилей": "activity-edit-text",
            "время abcdef12 05.09.2026 15:00 90": "booking-open-text",
            "черновик vk | Заголовок | Полный текст": "publication-new-text",
            "оплата 3500 RUB abcdef12 fedcba98 | консультация": "payment-new-text",
            "цена abcdef12 5000 RUB": "price-set-text",
            "сотрудник 123456 manager": "member-add-text",
            "программа Первый урок": "program-create-text",
            "урок abcdef12 text | Введение | Добро пожаловать": "program-lesson-text",
            "выдать abcdef12 fedcba98": "program-deliver-text",
            "предложение services | Диагностика | Проверка перед покупкой": "offering-new-text",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_native_member_interaction(raw).action, expected)


if __name__ == "__main__":
    unittest.main()
