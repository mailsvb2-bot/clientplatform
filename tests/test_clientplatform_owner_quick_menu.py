from __future__ import annotations

import unittest
from types import SimpleNamespace

from clientplatform.domain.activity import CapabilityStatus
from clientplatform.domain.tenancy import PlatformRole
from clientplatform.presentation.owner_quick_menu import build_owner_quick_actions


def _capability(key: str, *, active: bool = True) -> object:
    return SimpleNamespace(
        connector_key=key,
        status=CapabilityStatus.ACTIVE if active else CapabilityStatus.DISABLED,
    )


def _labels(actions: tuple[object, ...]) -> list[str]:
    return [str(getattr(item, "label")) for item in actions]


class OwnerQuickMenuTests(unittest.TestCase):
    def test_psychologist_gets_consultation_webinar_and_program_shortcuts(self) -> None:
        actions = build_owner_quick_actions(
            activity_description=(
                "Я психолог. Провожу индивидуальные консультации, вебинары "
                "и продаю обучающие программы."
            ),
            capabilities=[_capability("consultations"), _capability("programs")],
            role=PlatformRole.OWNER,
        )

        self.assertEqual(
            _labels(actions),
            [
                "💬 Клиенты и обращения",
                "📅 Записать на консультацию",
                "🎥 Провести вебинар",
                "🎓 Материалы и программы",
                "👥 Найти клиентов",
                "📊 Результаты",
                "▦ Все возможности",
            ],
        )

    def test_auto_service_gets_business_specific_booking_wording(self) -> None:
        actions = build_owner_quick_actions(
            activity_description="Автосервис: ремонтируем автомобили и делаем шиномонтаж.",
            capabilities=[_capability("services")],
            role=PlatformRole.OWNER,
        )

        labels = _labels(actions)
        self.assertIn("🚗 Записать машину", labels)
        self.assertIn("👥 Найти клиентов", labels)
        self.assertEqual(labels[-2:], ["📊 Результаты", "▦ Все возможности"])

    def test_disabled_capability_does_not_personalize_menu_by_itself(self) -> None:
        actions = build_owner_quick_actions(
            activity_description="Небольшой локальный бизнес.",
            capabilities=[_capability("programs", active=False)],
            role=PlatformRole.OWNER,
        )

        self.assertNotIn("🎓 Материалы и программы", _labels(actions))

    def test_quick_menu_is_stable_and_has_single_escape_to_full_power(self) -> None:
        kwargs = dict(
            activity_description="Консультации и вебинары для клиентов",
            capabilities=[_capability("consultations")],
            role=PlatformRole.OWNER,
        )
        first = build_owner_quick_actions(**kwargs)
        second = build_owner_quick_actions(**kwargs)

        self.assertEqual(first, second)
        self.assertEqual([item.key for item in first].count("all"), 1)
        self.assertEqual(first[-1].label, "▦ Все возможности")

    def test_role_filter_never_exposes_unreachable_shortcut_to_analyst(self) -> None:
        actions = build_owner_quick_actions(
            activity_description="Консультации, вебинары и программы",
            capabilities=[_capability("consultations"), _capability("programs")],
            role=PlatformRole.ANALYST,
        )

        self.assertEqual([item.key for item in actions], ["all"])
        self.assertEqual(actions[0].label, "▦ Все возможности")


if __name__ == "__main__":
    unittest.main()
