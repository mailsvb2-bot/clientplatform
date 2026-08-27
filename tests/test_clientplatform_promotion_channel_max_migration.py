from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
