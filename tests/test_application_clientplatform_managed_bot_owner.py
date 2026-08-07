from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from clientplatform.application import managed_bot_owner as application
from clientplatform.domain.connections import (
    ConnectionInvariantViolation,
    ConnectionStatus,
    ManagedBotStatus,
)
from clientplatform.domain.managed_bot_owner import (
    ManagedBotOwnerSnapshot,
    ManagedBotWebhookMaterial,
    ManagedBotWebhookOperationFailed,
)

_BUSINESS_ID = "00000000-0000-0000-0000-000000000201"
_CONNECTION_ID = "00000000-0000-0000-0000-000000000202"
_BOT_ID = "00000000-0000-0000-0000-000000000203"


def _material() -> ManagedBotWebhookMaterial:
    return ManagedBotWebhookMaterial(
        managed_bot_id=_BOT_ID,
        business_id=_BUSINESS_ID,
        connection_id=_CONNECTION_ID,
        external_bot_id="700001",
        username="practice_helper_bot",
        credential_reference=(
            "secret://env/CLIENTPLATFORM_SECRET_TELEGRAM_OWNER_LIFECYCLE"
        ),
        webhook_secret_reference=(
            "secret://env/CLIENTPLATFORM_SECRET_WEBHOOK_OWNER_LIFECYCLE"
        ),
    )


def _snapshot(status: ManagedBotStatus) -> ManagedBotOwnerSnapshot:
    connection_status = (
        ConnectionStatus.ACTIVE
        if status == ManagedBotStatus.ACTIVE
        else ConnectionStatus.DISABLED
        if status == ManagedBotStatus.DISABLED
        else ConnectionStatus.REVOKED
    )
    return ManagedBotOwnerSnapshot(
        managed_bot_id=_BOT_ID,
        business_id=_BUSINESS_ID,
        connection_id=_CONNECTION_ID,
        external_bot_id="700001",
        username="practice_helper_bot",
        display_name="Помощник практики",
        bot_status=status,
        connection_status=connection_status,
        pending_events=0,
        processing_events=0,
        retry_events=0,
        processed_events=3,
        dead_events=1,
        bot_updated_at="2026-07-29T10:00:00+00:00",
        connection_updated_at="2026-07-29T10:00:00+00:00",
    )


class _FakeController:
    def __init__(self, *, fail_attach: bool = False, fail_detach: bool = False) -> None:
        self.fail_attach = fail_attach
        self.fail_detach = fail_detach
        self.attach_calls = 0
        self.detach_calls = 0

    async def attach(self, material: ManagedBotWebhookMaterial) -> None:
        self.attach_calls += 1
        if self.fail_attach:
            raise ManagedBotWebhookOperationFailed("attach failed")

    async def detach(self, material: ManagedBotWebhookMaterial) -> None:
        self.detach_calls += 1
        if self.fail_detach:
            raise ManagedBotWebhookOperationFailed("detach failed")


class ClientPlatformManagedBotOwnerApplicationTests(
    unittest.IsolatedAsyncioTestCase
):
    def setUp(self) -> None:
        self.actor = SimpleNamespace(business_id=_BUSINESS_ID, user_id=101)

    async def test_disable_is_locally_committed_even_when_webhook_detach_fails(self) -> None:
        controller = _FakeController(fail_detach=True)
        disabled = _snapshot(ManagedBotStatus.DISABLED)
        with (
            patch.object(application, "_get_webhook_material", return_value=_material()),
            patch.object(application, "disable_managed_bot") as disable,
            patch.object(
                application,
                "_snapshot_async",
                new=AsyncMock(return_value=disabled),
            ),
        ):
            result = await application.disable_managed_bot_for_owner(
                actor=self.actor,
                managed_bot_id=_BOT_ID,
                controller=controller,
            )
        disable.assert_called_once_with(actor=self.actor, managed_bot_id=_BOT_ID)
        self.assertEqual(controller.detach_calls, 1)
        self.assertEqual(result.snapshot.bot_status, ManagedBotStatus.DISABLED)
        self.assertFalse(result.webhook_synchronized)
        self.assertEqual(result.warning_code, "webhook_detach_failed")

    async def test_activation_configures_webhook_before_enabling_local_route(self) -> None:
        controller = _FakeController()
        active = _snapshot(ManagedBotStatus.ACTIVE)
        order: list[str] = []

        async def attach(material: ManagedBotWebhookMaterial) -> None:
            order.append("attach")
            controller.attach_calls += 1

        def activate(*, actor, managed_bot_id):
            order.append("activate")

        controller.attach = attach
        with (
            patch.object(application, "_get_webhook_material", return_value=_material()),
            patch.object(application, "activate_managed_bot", side_effect=activate),
            patch.object(
                application,
                "_snapshot_async",
                new=AsyncMock(return_value=active),
            ),
        ):
            result = await application.activate_managed_bot_for_owner(
                actor=self.actor,
                managed_bot_id=_BOT_ID,
                controller=controller,
            )
        self.assertEqual(order, ["attach", "activate"])
        self.assertTrue(result.webhook_synchronized)
        self.assertEqual(result.snapshot.bot_status, ManagedBotStatus.ACTIVE)

    async def test_activation_conflict_rolls_back_new_webhook(self) -> None:
        controller = _FakeController()
        with (
            patch.object(application, "_get_webhook_material", return_value=_material()),
            patch.object(
                application,
                "activate_managed_bot",
                side_effect=ConnectionInvariantViolation("another bot is active"),
            ),
        ):
            with self.assertRaises(ConnectionInvariantViolation):
                await application.activate_managed_bot_for_owner(
                    actor=self.actor,
                    managed_bot_id=_BOT_ID,
                    controller=controller,
                )
        self.assertEqual(controller.attach_calls, 1)
        self.assertEqual(controller.detach_calls, 1)

    async def test_attach_failure_never_enables_local_route(self) -> None:
        controller = _FakeController(fail_attach=True)
        with (
            patch.object(application, "_get_webhook_material", return_value=_material()),
            patch.object(application, "activate_managed_bot") as activate,
        ):
            with self.assertRaises(ManagedBotWebhookOperationFailed):
                await application.activate_managed_bot_for_owner(
                    actor=self.actor,
                    managed_bot_id=_BOT_ID,
                    controller=controller,
                )
        activate.assert_not_called()
        self.assertEqual(controller.detach_calls, 0)

    async def test_revoke_is_permanent_even_when_webhook_detach_fails(self) -> None:
        controller = _FakeController(fail_detach=True)
        revoked = _snapshot(ManagedBotStatus.REVOKED)
        with (
            patch.object(application, "_get_webhook_material", return_value=_material()),
            patch.object(application, "_revoke_managed_bot_and_credential") as revoke,
            patch.object(
                application,
                "_snapshot_async",
                new=AsyncMock(return_value=revoked),
            ),
        ):
            result = await application.revoke_managed_bot_for_owner(
                actor=self.actor,
                managed_bot_id=_BOT_ID,
                controller=controller,
            )
        revoke.assert_called_once_with(
            actor=self.actor,
            managed_bot_id=_BOT_ID,
            material=_material(),
        )
        self.assertEqual(controller.detach_calls, 1)
        self.assertEqual(result.snapshot.bot_status, ManagedBotStatus.REVOKED)
        self.assertEqual(result.warning_code, "webhook_detach_failed")


if __name__ == "__main__":
    unittest.main()
