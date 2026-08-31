from __future__ import annotations

import inspect

from services import migrations
from services.db.core import translate_sql_for_postgres
from services.migrations.postgres_account_bigint_v2 import (
    _ACCOUNT_ID_COLUMN,
    _account_integer_columns,
)
from services.migrations.postgres_identity_bigint_v1 import _IDENTITY_COLUMN


def test_postgres_ddl_promotes_telegram_identity_columns_to_bigint() -> None:
    translated = translate_sql_for_postgres(
        """
        CREATE TABLE sample(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER PRIMARY KEY,
            buyer_user_id INTEGER NOT NULL,
            recipient_user_id INT,
            chat_id INTEGER,
            requested_by INTEGER,
            amount INTEGER
        )
        """
    )

    assert "id BIGSERIAL PRIMARY KEY" in translated
    assert "user_id BIGINT PRIMARY KEY" in translated
    assert "buyer_user_id BIGINT NOT NULL" in translated
    assert "recipient_user_id BIGINT" in translated
    assert "chat_id BIGINT" in translated
    assert "requested_by BIGINT" in translated
    assert "amount INTEGER" in translated


def test_identity_migration_column_filter_is_narrow() -> None:
    assert _IDENTITY_COLUMN.fullmatch("user_id")
    assert _IDENTITY_COLUMN.fullmatch("buyer_user_id")
    assert _IDENTITY_COLUMN.fullmatch("telegram_chat_id")
    assert _IDENTITY_COLUMN.fullmatch("admin_id")
    assert _IDENTITY_COLUMN.fullmatch("requested_by")
    assert not _IDENTITY_COLUMN.fullmatch("payment_id")
    assert not _IDENTITY_COLUMN.fullmatch("amount")


def test_account_bigint_migration_column_filter_is_narrow() -> None:
    assert _ACCOUNT_ID_COLUMN.fullmatch("account_id")
    assert not _ACCOUNT_ID_COLUMN.fullmatch("primary_user_id")
    assert not _ACCOUNT_ID_COLUMN.fullmatch("payment_id")
    assert not _ACCOUNT_ID_COLUMN.fullmatch("business_account_id")


def test_account_bigint_migration_selects_all_integer_account_id_columns() -> None:
    class _Cursor:
        def fetchall(self):
            return [
                {"table_name": "account_channel_identities", "column_name": "account_id"},
                {"table_name": "account_audio_progress", "column_name": "account_id"},
                {"table_name": "payments", "column_name": "payment_id"},
            ]

    class _Connection:
        def execute(self, _sql: str):
            return _Cursor()

    assert _account_integer_columns(_Connection()) == [
        ("account_channel_identities", "account_id"),
        ("account_audio_progress", "account_id"),
    ]


def test_account_bigint_migration_is_registered_after_identity_bigint_v1() -> None:
    source = inspect.getsource(migrations.apply_all_migrations)
    identity = source.index("_apply_postgres_identity_bigint_v1(conn)")
    account = source.index("_apply_postgres_account_bigint_v2(conn)")
    assert identity < account
