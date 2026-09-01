from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor

import pytest


def _fresh_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIENTPLATFORM_DB_PATH", str(tmp_path / "bridge_atomic.db"))
    monkeypatch.setenv("CLIENTPLATFORM_DB_ENGINE", "sqlite")
    modules = {}
    for name in [
        "core.paths",
        "services.db.core",
        "services.schema_core",
        "services.schema",
        "services.accounts.identity",
        "services.messenger.bridge",
    ]:
        module = importlib.import_module(name)
        modules[name] = importlib.reload(module)
    modules["services.schema"].init_db()
    return modules


def test_bridge_token_rolls_back_when_identity_link_conflicts(tmp_path, monkeypatch) -> None:
    modules = _fresh_modules(tmp_path, monkeypatch)
    bridge = modules["services.messenger.bridge"]
    identity = modules["services.accounts.identity"]

    identity.link_channel_to_account(810001, "telegram", "tg-owner")
    identity.link_channel_to_account(810002, "vk", "vk-already-owned")
    token = bridge.issue_bridge_token(810001, target_platform="vk")

    with pytest.raises(identity.AccountIdentityConflict):
        bridge.consume_bridge_token_and_link(
            token,
            platform="vk",
            external_user_id="vk-already-owned",
        )

    assert bridge.resolve_bridge_token(token).consumed is False
    retry = bridge.consume_bridge_token_and_link(
        token,
        platform="vk",
        external_user_id="vk-retry-ok",
    )
    assert retry is not None
    assert retry.consumed is True
    linked = identity.get_account_snapshot(810001)
    vk = next(item for item in linked["identities"] if item["platform"] == "vk")
    assert vk["external_user_id"] == "vk-retry-ok"


def test_concurrent_bridge_link_has_one_identity_winner(tmp_path, monkeypatch) -> None:
    modules = _fresh_modules(tmp_path, monkeypatch)
    bridge = modules["services.messenger.bridge"]
    identity = modules["services.accounts.identity"]

    identity.link_channel_to_account(810011, "telegram", "tg-owner-2")
    token = bridge.issue_bridge_token(810011, target_platform="max")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                bridge.consume_bridge_token_and_link,
                token,
                platform="max",
                external_user_id=external_id,
            )
            for external_id in ("max-a", "max-b")
        ]
        results = [future.result(timeout=10) for future in futures]

    assert sum(result is not None for result in results) == 1
    snapshot = identity.get_account_snapshot(810011)
    max_identities = [item for item in snapshot["identities"] if item["platform"] == "max"]
    assert len(max_identities) == 1
    assert max_identities[0]["external_user_id"] in {"max-a", "max-b"}
