from __future__ import annotations

import importlib.util
import json
import sqlite3
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from clientplatform.application.activity import extract_customer_invite_token
from clientplatform.domain.activity import ActivityInvariantViolation
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.messenger_channels import MessengerIngressRoute
from clientplatform.infrastructure import TenancyRepository
from clientplatform.infrastructure.customer_repository import CustomerRepository
from clientplatform.infrastructure.postgres_safe_activity_repository import ActivityRepository
from services.db.schema import create_or_update_tables


_STAMP = "2026-08-21T08:00:00+00:00"
_AIOHTTP_AVAILABLE = importlib.util.find_spec("aiohttp") is not None


class ChannelNeutralInviteRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        create_or_update_tables(self.conn)
        self.tenancy = TenancyRepository(self.conn)
        created = self.tenancy.create_business(owner_user_id=101, name="Практика")
        self.owner = self.tenancy.resolve_context(
            user_id=101, business_id=created.business.id
        )
        other = self.tenancy.create_business(owner_user_id=202, name="Другой бизнес")
        self.other = self.tenancy.resolve_context(
            user_id=202, business_id=other.business.id
        )
        self.activity = ActivityRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_vk_invite_claim_is_idempotent_for_same_identity(self) -> None:
        issued = self.activity.issue_customer_invite(actor=self.owner, now=_STAMP)
        first = self.activity.claim_customer_invite_identity(
            token=issued.token,
            platform="vk",
            external_subject="700001",
            username="vk_user",
            display_name="Анна",
            expected_business_id=self.owner.business_id,
            now="2026-08-21T08:01:00+00:00",
        )
        second = self.activity.claim_customer_invite_identity(
            token=issued.token,
            platform="vk",
            external_subject="700001",
            username="vk_user",
            display_name="Анна",
            expected_business_id=self.owner.business_id,
            now="2026-08-21T08:02:00+00:00",
        )
        self.assertEqual(first.customer_id, second.customer_id)
        self.assertTrue(second.already_connected)
        record = CustomerRepository(self.conn).find_by_identity(
            actor=self.owner,
            platform="vk",
            external_subject="700001",
        )
        self.assertEqual(first.customer_id, record.customer.id)

    def test_claimed_invite_cannot_be_reused_by_other_platform_identity(self) -> None:
        issued = self.activity.issue_customer_invite(actor=self.owner, now=_STAMP)
        self.activity.claim_customer_invite_identity(
            token=issued.token,
            platform="max",
            external_subject="800001",
            username=None,
            display_name="Иван",
            expected_business_id=self.owner.business_id,
            now="2026-08-21T08:01:00+00:00",
        )
        with self.assertRaisesRegex(ActivityInvariantViolation, "already been used"):
            self.activity.claim_customer_invite_identity(
                token=issued.token,
                platform="vk",
                external_subject="800001",
                username=None,
                display_name="Иван",
                expected_business_id=self.owner.business_id,
                now="2026-08-21T08:02:00+00:00",
            )

    def test_wrong_business_is_rejected_before_customer_or_invite_mutation(self) -> None:
        issued = self.activity.issue_customer_invite(actor=self.owner, now=_STAMP)
        with self.assertRaisesRegex(ActivityInvariantViolation, "another business"):
            self.activity.claim_customer_invite_identity(
                token=issued.token,
                platform="vk",
                external_subject="700002",
                username=None,
                display_name="Чужой маршрут",
                expected_business_id=self.other.business_id,
                now="2026-08-21T08:01:00+00:00",
            )
        invite = self.conn.execute(
            "SELECT status,claimed_customer_id FROM customer_invites WHERE id=?",
            (issued.invite.id,),
        ).fetchone()
        self.assertEqual("active", invite["status"])
        self.assertIsNone(invite["claimed_customer_id"])
        count = self.conn.execute("SELECT COUNT(*) AS c FROM customers").fetchone()["c"]
        self.assertEqual(0, int(count))

    def test_linked_staff_account_cannot_claim_customer_invite_over_vk(self) -> None:
        issued = self.activity.issue_customer_invite(actor=self.owner, now=_STAMP)
        with self.assertRaisesRegex(ActivityInvariantViolation, "собственного бизнеса"):
            self.activity.claim_customer_invite_identity(
                token=issued.token,
                platform="vk",
                external_subject="owner-vk",
                username=None,
                display_name="Владелец",
                claiming_account_id=101,
                expected_business_id=self.owner.business_id,
                now="2026-08-21T08:01:00+00:00",
            )
        row = self.conn.execute(
            "SELECT status,claimed_customer_id FROM customer_invites WHERE id=?",
            (issued.invite.id,),
        ).fetchone()
        self.assertEqual("active", row["status"])
        self.assertIsNone(row["claimed_customer_id"])


class ChannelNeutralInviteApplicationTests(unittest.TestCase):
    def test_existing_staff_account_is_forwarded_to_repository_boundary(self) -> None:
        from clientplatform.application import activity as application

        expected = SimpleNamespace(customer_id=str(uuid4()))

        @contextmanager
        def fake_db():
            yield object()

        repository = unittest.mock.MagicMock()
        repository.claim_customer_invite_identity.return_value = expected
        with (
            patch.object(application, "get_db", fake_db),
            patch.object(
                application,
                "resolve_account_for_identity",
                return_value=5136927077509609556,
            ) as account_resolver,
            patch.object(application, "ActivityRepository", return_value=repository),
        ):
            result = application.claim_customer_invite_identity(
                token="C" * 32,
                platform="vk",
                external_subject="700001",
                username="anna",
                display_name="Анна",
                expected_business_id="9a9b0ad1-01ab-41bf-9f89-7a60ad56d6a3",
            )

        self.assertIs(expected, result)
        account_resolver.assert_called_once_with(
            "vk",
            "700001",
            username="anna",
            display_name="Анна",
            allow_create=False,
        )
        self.assertEqual(
            5136927077509609556,
            repository.claim_customer_invite_identity.call_args.kwargs[
                "claiming_account_id"
            ],
        )



class InviteTextParserTests(unittest.TestCase):
    def test_parser_recognizes_native_and_start_forms(self) -> None:
        token = "A" * 32
        self.assertEqual(token, extract_customer_invite_token(f"cpj_{token}"))
        self.assertEqual(token, extract_customer_invite_token(f"/start cpj_{token}"))
        self.assertEqual(token, extract_customer_invite_token(f"start cpj_{token}"))
        self.assertIsNone(extract_customer_invite_token("Хочу узнать цену"))


class _FakeRequest:
    def __init__(self, payload: dict[str, object], *, route_id: str) -> None:
        self._raw = json.dumps(payload).encode("utf-8")
        self.match_info = {"route_id": route_id}
        self.headers = {"X-Max-Bot-Api-Secret": "route-secret"}

    async def read(self) -> bytes:
        return self._raw


@unittest.skipUnless(
    _AIOHTTP_AVAILABLE,
    "aiohttp runtime dependency is not installed in dependency-light Canon",
)
class NativeInviteIngressTests(unittest.IsolatedAsyncioTestCase):
    def _route(self) -> MessengerIngressRoute:
        return MessengerIngressRoute(
            id=str(uuid4()),
            business_id=str(uuid4()),
            connection_id=str(uuid4()),
            platform=ConnectionPlatform.MAX,
            external_route_id="551001",
            webhook_secret_reference="secret://env/CLIENTPLATFORM_SECRET_MAX_WEBHOOK_TEST",
            confirmation_code_reference=None,
            status="active",
            created_by_member_id=str(uuid4()),
            created_at=_STAMP,
            updated_at=_STAMP,
        )

    def _request(self, route: MessengerIngressRoute, text: str) -> _FakeRequest:
        return _FakeRequest(
            {
                "update_type": "message_created",
                "update_id": 99001,
                "timestamp": 1787299200000,
                "message": {
                    "body": {"mid": "invite-mid", "text": text},
                    "sender": {"user_id": 700001, "first_name": "Анна"},
                },
            },
            route_id=route.id,
        )

    async def test_max_invite_claim_opens_customer_menu_without_sales_capture(self) -> None:
        from clientplatform.runtime.messenger_channel_ingress import canonical_max_webhook

        route = self._route()
        customer_id = str(uuid4())
        identity = SimpleNamespace(
            id=str(uuid4()), customer_id=customer_id, external_subject="700001"
        )
        token = "A" * 32
        with (
            patch(
                "clientplatform.runtime.messenger_channel_ingress.resolve_messenger_ingress_route",
                return_value=route,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.EnvironmentCredentialProvider.resolve",
                return_value="route-secret",
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.claim_inbound_event",
                return_value=True,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.resolve_native_member",
                return_value=None,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.claim_customer_invite_identity",
                return_value=SimpleNamespace(customer_id=customer_id),
            ) as claim,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.ensure_channel_customer",
                return_value=identity,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.record_customer_contact",
                return_value=True,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.process_native_customer_interaction",
            ) as customer_ui,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.record_customer_channel_message"
            ) as sales,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.complete_inbound_event"
            ),
        ):
            response = await canonical_max_webhook(self._request(route, f"cpj_{token}"))  # type: ignore[arg-type]

        self.assertEqual(200, response.status)
        sales.assert_not_called()
        claim.assert_called_once()
        self.assertEqual(route.business_id, claim.call_args.kwargs["expected_business_id"])
        self.assertEqual("max", claim.call_args.kwargs["platform"])
        customer_ui.assert_called_once()
        self.assertEqual("cpi:menu", customer_ui.call_args.kwargs["raw_text"])
        self.assertTrue(customer_ui.call_args.kwargs["linked"])

    async def test_rejected_invite_does_not_create_customer_or_enter_sales(self) -> None:
        from clientplatform.runtime.messenger_channel_ingress import canonical_max_webhook

        route = self._route()
        token = "B" * 32
        with (
            patch(
                "clientplatform.runtime.messenger_channel_ingress.resolve_messenger_ingress_route",
                return_value=route,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.EnvironmentCredentialProvider.resolve",
                return_value="route-secret",
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.claim_inbound_event",
                return_value=True,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.resolve_native_member",
                return_value=None,
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.claim_customer_invite_identity",
                side_effect=ActivityInvariantViolation("rejected"),
            ),
            patch(
                "clientplatform.runtime.messenger_channel_ingress.ensure_channel_customer"
            ) as ensure_customer,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.record_customer_channel_message"
            ) as sales,
            patch(
                "clientplatform.runtime.messenger_channel_ingress.fail_claimed_inbound_event"
            ) as failed,
        ):
            response = await canonical_max_webhook(self._request(route, f"cpj_{token}"))  # type: ignore[arg-type]

        self.assertEqual(200, response.status)
        self.assertEqual("ok", response.text)
        ensure_customer.assert_not_called()
        sales.assert_not_called()
        failed.assert_called_once()
        self.assertEqual("customer_invite_rejected", failed.call_args.args[3])
        self.assertTrue(failed.call_args.kwargs["permanent"])


if __name__ == "__main__":
    unittest.main()
