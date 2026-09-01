from __future__ import annotations

import sqlite3

from services.db import get_connection
from services.db.runtime import CONFIG

READY_TABLES = {
    # Shared operational substrate.
    "users",
    "events",
    "jobs",
    "engine_state",
    "idempotency",
    "schema_migrations",
    # Canonical identity / tenancy.
    "accounts",
    "account_channel_identities",
    "businesses",
    "business_members",
    "customers",
    "customer_identities",
    # Canonical money and customer value.
    "business_payments",
    "booking_slots",
    "programs",
    "lessons",
    "enrollments",
    # Canonical messenger ingress / dispatch.
    "connections",
    "messenger_ingress_routes",
    "messenger_webhook_events",
    "messenger_delivery_outbox",
    "provider_dispatch_outbox",
}



def required_readiness_tables() -> list[str]:
    return sorted(READY_TABLES)


def schema_readiness() -> tuple[bool, str | None]:
    required_tables = set(READY_TABLES)
    try:
        with get_connection() as conn:
            if CONFIG.uses_postgres:
                rows = conn.execute(
                    "SELECT table_name AS name FROM information_schema.tables "
                    "WHERE table_schema=current_schema()",
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'",
                ).fetchall()
        names: set[str] = set()
        for row in rows:
            if isinstance(row, dict):
                value = row.get('table_name') or row.get('name')
            else:
                try:
                    value = row[0]
                except (IndexError, KeyError, TypeError):
                    value = None
            if value:
                names.add(str(value))
        missing = sorted(required_tables - names)
        if missing:
            return False, 'schema_missing:' + ','.join(missing)
        return True, None
    except sqlite3.Error as exc:
        return False, f'schema:{exc}'
    except OSError as exc:
        return False, f'schema:{exc}'
    except RuntimeError as exc:
        return False, f'schema:{exc}'
    except TypeError as exc:
        return False, f'schema:{exc}'
    except ValueError as exc:
        return False, f'schema:{exc}'
    except AttributeError as exc:
        return False, f'schema:{exc}'
