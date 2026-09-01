from __future__ import annotations

import importlib

import pytest


def _fresh_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIENTPLATFORM_DB_PATH", str(tmp_path / "account_identity_atomic.db"))
    monkeypatch.setenv("CLIENTPLATFORM_DB_ENGINE", "sqlite")

    for name in [
        "core.paths",
        "services.db.core",
        "services.schema_core",
        "services.schema",
        "services.accounts.identity",
    ]:
        module = importlib.import_module(name)
        importlib.reload(module)

    schema = importlib.import_module("services.schema")
    identity = importlib.import_module("services.accounts.identity")
    schema.init_db()
    return identity


def test_replace_existing_identity_rolls_back_original_owner_on_failure(tmp_path, monkeypatch):
    identity = _fresh_identity(tmp_path, monkeypatch)
    identity.link_channel_to_account(10001, "vk", "vk-20002")

    def fail_account_creation(conn, account_id, **kwargs):
        raise RuntimeError("synthetic replacement failure")

    monkeypatch.setattr(identity, "_ensure_account_in_conn", fail_account_creation)

    with pytest.raises(RuntimeError, match="synthetic replacement failure"):
        identity.link_channel_to_account(
            30003,
            "vk",
            "vk-20002",
            replace_existing=True,
        )

    original = identity.get_account_snapshot(10001)
    replacement = identity.get_account_snapshot(30003)
    assert [(row["platform"], row["external_user_id"]) for row in original["identities"]] == [
        ("vk", "vk-20002")
    ]
    assert replacement["identities"] == []
