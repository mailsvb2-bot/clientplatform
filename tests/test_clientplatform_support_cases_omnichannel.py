from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.domain.support_cases import SupportCaseCategory, SupportCaseStatus
from services.messenger import clientplatform_entry as entry


class SupportCaseOmnichannelTests(unittest.TestCase):
    def test_support_parser_is_channel_neutral(self) -> None:
        parsed = entry.parse_clientplatform_entry_command(
            "support technical Messenger is unavailable"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.action, "support_create")
        self.assertEqual(parsed.value, "technical Messenger is unavailable")
        listed = entry.parse_clientplatform_entry_command("поддержка список")
        self.assertIsNotNone(listed)
        assert listed is not None
        self.assertEqual(listed.action, "support_list")

    def test_support_create_uses_one_application_owner_on_all_channels(self) -> None:
        business_id = "ad67e150-0d91-48c9-a879-44a44782250d"
        access = SimpleNamespace(business=SimpleNamespace(id=business_id, name="Acme"))
        actor = SimpleNamespace(business_id=business_id)
        case = SimpleNamespace(
            id="f3b3c9dd-fcb1-43ad-b911-32dfd81222ac",
            business_id=business_id,
            category=SupportCaseCategory.TECHNICAL,
            status=SupportCaseStatus.OPEN,
            summary="Messenger is unavailable",
        )
        calls: list[dict[str, object]] = []

        def create(**kwargs):
            calls.append(kwargs)
            return case

        with (
            patch.object(
                entry,
                "register_user_entry",
                side_effect=lambda user_id, **_kwargs: SimpleNamespace(user_id=user_id),
            ),
            patch.object(entry, "list_accessible_businesses", return_value=[access]),
            patch.object(entry, "resolve_tenant_context", return_value=actor),
            patch.object(entry, "create_support_case", side_effect=create),
        ):
            for platform in ("telegram", "vk", "max"):
                with self.subTest(platform=platform):
                    calls.clear()
                    user_id, replies = entry.handle_clientplatform_entry(
                        101,
                        platform=platform,
                        external_user_id="101",
                        text="support technical Messenger is unavailable",
                        event_key=f"{platform}:event:1",
                    )
                    self.assertEqual(user_id, 101)
                    self.assertEqual(len(calls), 1)
                    self.assertIs(calls[0]["actor"], actor)
                    self.assertEqual(calls[0]["category"], "technical")
                    self.assertIn("Case:", replies[0].text)


if __name__ == "__main__":
    unittest.main()
