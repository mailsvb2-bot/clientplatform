from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from clientplatform.infrastructure.activity_repository import ActivityRepository
from clientplatform.infrastructure.tenancy_repository import TenancyRepository
from services.db import core as db_core
from services.db.schema import clientplatform_activity, clientplatform_tenancy

from clientplatform.application import cockpit_connections, cockpit_settings
from clientplatform.application.capability_parity import CapabilityAvailability
from clientplatform.domain.connections import ConnectionPlatform
from clientplatform.domain.tenancy import PlatformRole, TenantContext, TenantPermissionDenied

_BUSINESS = "11111111-1111-4111-8111-111111111111"
_MEMBER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _actor(role: PlatformRole = PlatformRole.OWNER) -> TenantContext:
    return TenantContext(
        business_id=_BUSINESS,
        user_id=101,
        membership_id=_MEMBER,
        role=role,
    )


class CockpitConnectionsSettingsM7005Tests(unittest.TestCase):
    def test_connections_projects_only_user_safe_runtime_state(self) -> None:
        actor = _actor()
        projection = SimpleNamespace(
            messengers=(
                SimpleNamespace(
                    platform=ConnectionPlatform.TELEGRAM,
                    availability=CapabilityAvailability.ACTIVE,
                    active=True,
                    can_connect=False,
                    runtime_ready=True,
                    credential_reference="secret://must-not-leak",
                ),
                SimpleNamespace(
                    platform=ConnectionPlatform.VK,
                    availability=CapabilityAvailability.CONNECTABLE,
                    active=False,
                    can_connect=True,
                    runtime_ready=True,
                ),
                SimpleNamespace(
                    platform=ConnectionPlatform.MAX,
                    availability=CapabilityAvailability.UNAVAILABLE,
                    active=False,
                    can_connect=False,
                    runtime_ready=False,
                ),
            )
        )
        with (
            patch.object(cockpit_connections, "resolve_tenant_context", return_value=actor),
            patch.object(cockpit_connections, "get_business_capability_projection", return_value=projection),
        ):
            snapshot = cockpit_connections.build_cockpit_connections(actor=actor, business_name="Практика")
        self.assertEqual([item.platform for item in snapshot.items], ["telegram", "vk", "max"])
        self.assertTrue(snapshot.items[0].active)
        self.assertTrue(snapshot.items[1].can_connect)
        self.assertEqual(snapshot.items[2].state_label, "Сейчас недоступен")
        rendered = repr(snapshot.as_dict())
        self.assertNotIn("credential_reference", rendered)
        self.assertNotIn("secret://", rendered)

    def test_connections_rechecks_manage_business_permission(self) -> None:
        actor = _actor(PlatformRole.MANAGER)
        called = False

        def loader(**_kwargs: object):
            nonlocal called
            called = True
            return SimpleNamespace(messengers=())

        with (
            patch.object(cockpit_connections, "resolve_tenant_context", return_value=actor),
            patch.object(cockpit_connections, "get_business_capability_projection", side_effect=loader),
        ):
            with self.assertRaises(TenantPermissionDenied):
                cockpit_connections.build_cockpit_connections(actor=actor, business_name="Практика")
        self.assertFalse(called)

    def test_settings_projects_canonical_profile_without_browser_authority(self) -> None:
        actor = _actor()
        with (
            patch.object(cockpit_settings, "resolve_tenant_context", return_value=actor),
            patch.object(
                cockpit_settings,
                "get_business_profile",
                return_value=SimpleNamespace(activity_description="Психологическая практика", timezone="Europe/Moscow"),
            ),
        ):
            snapshot = cockpit_settings.build_cockpit_settings(actor=actor, business_name="Практика")
        self.assertEqual(snapshot.business_name, "Практика")
        self.assertEqual(snapshot.activity_description, "Психологическая практика")
        self.assertEqual(snapshot.timezone_name, "Europe/Moscow")

    def test_setup_issue_reuses_canonical_capability_and_single_use_owner(self) -> None:
        actor = _actor()
        projection = SimpleNamespace(
            messenger=lambda platform: SimpleNamespace(can_connect=platform == ConnectionPlatform.VK)
        )
        issued = SimpleNamespace(token="opaque", platform=ConnectionPlatform.VK)
        with (
            patch.object(cockpit_connections, "_resolve_actor", return_value=(actor, "Практика")),
            patch.object(cockpit_connections, "get_business_capability_projection", return_value=projection),
            patch.object(cockpit_connections, "issue_native_messenger_setup", return_value=issued) as issue,
        ):
            result = cockpit_connections.issue_cockpit_messenger_setup(
                telegram_user_id=101,
                requested_business_id=_BUSINESS,
                platform="vk",
            )
        self.assertIs(result, issued)
        issue.assert_called_once_with(actor=actor, platform=ConnectionPlatform.VK, ttl_seconds=600)

        with (
            patch.object(cockpit_connections, "_resolve_actor", return_value=(actor, "Практика")),
            patch.object(cockpit_connections, "get_business_capability_projection", return_value=projection),
        ):
            with self.assertRaises(RuntimeError):
                cockpit_connections.issue_cockpit_messenger_setup(
                    telegram_user_id=101,
                    requested_business_id=_BUSINESS,
                    platform="max",
                )
            with self.assertRaises(ValueError):
                cockpit_connections.issue_cockpit_messenger_setup(
                    telegram_user_id=101,
                    requested_business_id=_BUSINESS,
                    platform="email",
                )

    def test_settings_update_is_atomic_across_existing_application_owners(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "settings.db"

            def connect() -> sqlite3.Connection:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
                return conn

            seed = connect()
            clientplatform_tenancy.ensure(seed)
            clientplatform_activity.ensure(seed)
            tenancy = TenancyRepository(seed)
            access = tenancy.create_business(owner_user_id=101, name="Практика")
            actor = tenancy.resolve_context(user_id=101, business_id=access.business.id)
            ActivityRepository(seed).upsert_profile(
                actor=actor,
                activity_description="Старое описание",
                timezone_name="Europe/Moscow",
            )
            seed.commit()
            seed.close()

            with (
                patch.object(cockpit_settings, "_resolve_actor", return_value=(actor, "Практика")),
                patch.object(db_core, "get_connection", side_effect=connect),
            ):
                updated = cockpit_settings.update_cockpit_settings(
                    telegram_user_id=101,
                    requested_business_id=access.business.id,
                    business_name="Новая практика",
                    activity_description="Новое описание",
                    timezone_name="Europe/Amsterdam",
                )
                self.assertEqual(updated.business_name, "Новая практика")
                self.assertEqual(updated.timezone_name, "Europe/Amsterdam")

                with self.assertRaises(ValueError):
                    cockpit_settings.update_cockpit_settings(
                        telegram_user_id=101,
                        requested_business_id=access.business.id,
                        business_name="Не должно сохраниться",
                        activity_description="Ещё описание",
                        timezone_name="Mars/Olympus",
                    )

            check = connect()
            business_name = check.execute(
                "SELECT name FROM businesses WHERE id=?", (access.business.id,)
            ).fetchone()[0]
            profile = check.execute(
                "SELECT activity_description, timezone FROM business_profiles WHERE business_id=?",
                (access.business.id,),
            ).fetchone()
            self.assertEqual(business_name, "Новая практика")
            self.assertEqual(profile[0], "Новое описание")
            self.assertEqual(profile[1], "Europe/Amsterdam")
            check.close()

    def test_native_assets_have_no_client_side_authority_or_secret_store(self) -> None:
        root = Path(__file__).resolve().parents[1]
        transport = (root / "clientplatform" / "runtime" / "cockpit_http.py").read_text(encoding="utf-8")
        connections = (root / "clientplatform" / "runtime" / "cockpit_connections.js").read_text(encoding="utf-8")
        settings = (root / "clientplatform" / "runtime" / "cockpit_settings.js").read_text(encoding="utf-8")
        self.assertIn("'connections','settings'", transport)
        self.assertIn('/clientplatform/cockpit/connections', connections)
        self.assertIn('/clientplatform/cockpit/settings/update', settings)
        self.assertIn('syncBusinessName', transport)
        self.assertIn('api.syncBusinessName(payload.business_name)', settings)
        for script in (connections, settings):
            self.assertNotIn("localStorage", script)
            self.assertNotIn("sessionStorage", script)
            self.assertNotIn("URLSearchParams", script)
            self.assertNotIn("innerHTML", script)
            self.assertNotIn("credential_reference", script)
            self.assertNotIn("provider_token", script)


if __name__ == "__main__":
    unittest.main()
