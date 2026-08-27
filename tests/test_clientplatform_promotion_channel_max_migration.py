from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from services.migrations import clientplatform_promotion_channel_max_v1 as migration


class ClientPlatformPromotionChannelMaxMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(
            """
            CREATE TABLE promotion_campaigns(
                id TEXT PRIMARY KEY,
                business_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                UNIQUE(id, business_id),
                CHECK(channel IN ('telegram', 'vk', 'whatsapp', 'website', 'offline'))
            );
            CREATE TABLE promotion_children(
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                business_id TEXT NOT NULL,
                FOREIGN KEY(campaign_id, business_id)
                    REFERENCES promotion_campaigns(id, business_id) ON DELETE CASCADE
            );
            INSERT INTO promotion_campaigns(id,business_id,channel)
            VALUES('campaign-old','business-1','vk');
            INSERT INTO promotion_children(id,campaign_id,business_id)
            VALUES('child-1','campaign-old','business-1');
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_upgrade_preserves_data_and_foreign_keys_and_accepts_max(self) -> None:
        with patch.object(migration, "is_postgres_enabled", return_value=False):
            migration.apply(self.conn)
            migration.apply(self.conn)

        sql = str(
            self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='promotion_campaigns'"
            ).fetchone()["sql"]
        )
        self.assertIn("'max'", sql)
        self.assertEqual(
            self.conn.execute(
                "SELECT channel FROM promotion_campaigns WHERE id='campaign-old'"
            ).fetchone()["channel"],
            "vk",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT campaign_id FROM promotion_children WHERE id='child-1'"
            ).fetchone()["campaign_id"],
            "campaign-old",
        )
        self.conn.execute(
            "INSERT INTO promotion_campaigns(id,business_id,channel) VALUES(?,?,?)",
            ("campaign-max", "business-1", "max"),
        )
        self.conn.execute(
            "INSERT INTO promotion_children(id,campaign_id,business_id) VALUES(?,?,?)",
            ("child-max", "campaign-max", "business-1"),
        )
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        applied = self.conn.execute(
            "SELECT COUNT(*) AS c FROM schema_migrations WHERE name=?",
            (migration.NAME,),
        ).fetchone()["c"]
        self.assertEqual(applied, 1)


    def test_postgres_constraint_replacement_targets_channel_check_only(self) -> None:
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            {"conname": "legacy_promotion_channel_check", "definition": "CHECK (channel IN ('telegram', 'vk'))"},
            {"conname": "promotion_status_check", "definition": "CHECK (status IN ('active', 'closed'))"},
        ]
        migration._update_postgres_constraint(conn)
        statements = [str(call.args[0]) for call in conn.execute.call_args_list]
        self.assertTrue(any('DROP CONSTRAINT "legacy_promotion_channel_check"' in item for item in statements))
        self.assertFalse(any('DROP CONSTRAINT "promotion_status_check"' in item for item in statements))
        self.assertTrue(any("cp_promotion_campaigns_channel_max_v1" in item for item in statements))

    def test_postgres_constraint_replacement_rejects_unsafe_name(self) -> None:
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [
            {"conname": 'bad"constraint', "definition": "CHECK (channel IN ('telegram'))"}
        ]
        with self.assertRaisesRegex(RuntimeError, "unsafe promotion campaign constraint"):
            migration._update_postgres_constraint(conn)

    def test_apply_routes_postgres_through_backend_specific_update(self) -> None:
        conn = MagicMock()
        with (
            patch.object(migration, "migration_applied", return_value=False),
            patch.object(migration, "is_postgres_enabled", return_value=True),
            patch.object(migration, "_update_postgres_constraint") as update,
            patch.object(migration, "mark_migration") as mark,
        ):
            migration.apply(conn)
        update.assert_called_once_with(conn)
        mark.assert_called_once_with(conn, migration.NAME)


if __name__ == "__main__":
    unittest.main()
