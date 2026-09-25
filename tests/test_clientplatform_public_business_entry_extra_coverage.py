from __future__ import annotations

import unittest
from unittest.mock import patch

from clientplatform.application import public_business_entry as public_entry


class PublicBusinessEntryExtraCoverageTests(unittest.TestCase):
    def test_public_url_rejects_spaces_and_invalid_business_uuid(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            public_entry.public_business_entry_url(
                public_base_url="https://bad host.test",
                business_id="00000000-0000-0000-0000-000000000000",
            )
        with self.assertRaises(ValueError):
            public_entry.public_business_entry_url(
                public_base_url="https://clientplatform.example.test",
                business_id="broken",
            )

    def test_entry_lookup_rejects_unknown_token(self) -> None:
        with patch.object(public_entry, "get_db_ro") as db:
            db.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = None
            with self.assertRaisesRegex(ValueError, "invalid"):
                public_entry.get_public_business_entry(business_token="broken")

    def test_capture_rejects_invalid_email_after_identity_normalization(self) -> None:
        fake_entry = public_entry.PublicBusinessEntry(
            business_id="00000000-0000-0000-0000-000000000001",
            business_token="token",
            business_name="Test",
            activity_description="",
        )
        with patch.object(public_entry, "get_public_business_entry", return_value=fake_entry), patch.object(
            public_entry, "normalize_identity_subject", return_value=("email", "broken")
        ):
            with self.assertRaisesRegex(ValueError, "email is invalid"):
                public_entry.capture_public_business_lead(
                    business_token="token",
                    display_name="User",
                    email="broken",
                    phone=None,
                )

    def test_bounded_input_limit(self) -> None:
        with self.assertRaises(ValueError):
            public_entry._bounded("x" * 11, maximum=10)


if __name__ == "__main__":
    unittest.main()
