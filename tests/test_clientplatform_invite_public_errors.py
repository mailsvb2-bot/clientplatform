from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from clientplatform.application import activity as activity_application
from clientplatform.domain.activity import ActivityInvariantViolation, ActivityNotFound


@contextmanager
def _fake_db():
    yield object()


class ClientPlatformInvitePublicErrorTests(unittest.TestCase):
    def test_known_repository_states_have_stable_russian_copy(self) -> None:
        cases = {
            "customer invite was not found": "недействительна",
            "invalid customer invite token": "недействительна",
            "customer invite has already been used": "уже использована",
            "customer invite is not active": "больше не активна",
            "customer invite was claimed concurrently": "только что использовал",
        }
        for internal_message, expected_fragment in cases.items():
            with self.subTest(internal_message=internal_message):
                public = activity_application.customer_invite_error_message(
                    ActivityInvariantViolation(internal_message)
                )
                self.assertIn(expected_fragment, public)
                self.assertNotIn("customer invite", public)

    def test_unknown_internal_error_uses_generic_safe_copy(self) -> None:
        public = activity_application.customer_invite_error_message(
            ActivityInvariantViolation("claimed invite lost its customer reference")
        )
        self.assertIn("Не удалось использовать эту ссылку", public)
        self.assertNotIn("customer reference", public)

    def test_claim_maps_not_found_after_transaction_rollback(self) -> None:
        with (
            patch.object(activity_application, "get_db", _fake_db),
            patch.object(activity_application, "ActivityRepository") as repository_class,
        ):
            repository_class.return_value.claim_customer_invite.side_effect = ActivityNotFound(
                "customer invite was not found"
            )
            with self.assertRaisesRegex(ActivityInvariantViolation, "недействительна"):
                activity_application.claim_customer_invite(
                    token="x" * 32,
                    telegram_user_id=700001,
                    username="customer",
                    display_name="Клиент",
                )

    def test_claim_maps_malformed_token_error(self) -> None:
        with (
            patch.object(activity_application, "get_db", _fake_db),
            patch.object(activity_application, "ActivityRepository") as repository_class,
        ):
            repository_class.return_value.claim_customer_invite.side_effect = ValueError(
                "invalid customer invite token"
            )
            with self.assertRaisesRegex(ActivityInvariantViolation, "недействительна"):
                activity_application.claim_customer_invite(
                    token="bad",
                    telegram_user_id=700001,
                    username=None,
                    display_name="Клиент",
                )


if __name__ == "__main__":
    unittest.main()
