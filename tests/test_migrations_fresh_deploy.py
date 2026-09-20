from __future__ import annotations

from services.db import db
from services.migrations import apply_all_migrations


def test_fresh_deploy_does_not_recreate_retired_payment_schema():
    with db() as conn:
        conn.execute("DROP TABLE IF EXISTS payments")
        apply_all_migrations(conn)
        applied = {r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "payments_decision_attribution_v1" not in applied
    assert "payments" not in tables
    assert "clientplatform_business_payment_outcomes_v1" in applied
    assert "clientplatform_external_observation_lifecycle_v1" in applied
    assert "business_payments" in tables
    with db() as conn:
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(external_product_event_receipts)"
            ).fetchall()
        }
    assert "observation_revision" in columns
    assert "observation_state" in columns
