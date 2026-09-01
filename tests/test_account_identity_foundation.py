from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor

import pytest


def _fresh_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIENTPLATFORM_DB_PATH", str(tmp_path / "account_identity.db"))
    monkeypatch.setenv("CLIENTPLATFORM_DB_ENGINE", "sqlite")

    modules = {}
    for name in [
        "core.paths",
        "services.db.core",
        "services.schema_core",
        "services.schema",
        "services.accounts.identity",
        "services.messenger.bridge",
        "services.messenger.entrypoints",
        "services.messenger.preferences",
        "services.messenger.outbound",
    ]:
        module = importlib.import_module(name)
        modules[name] = importlib.reload(module)

    modules["services.schema"].init_db()
    return modules


def test_bridge_links_second_messenger_to_same_account(tmp_path, monkeypatch):
    modules = _fresh_modules(tmp_path, monkeypatch)
    entrypoints = modules["services.messenger.entrypoints"]
    bridge = modules["services.messenger.bridge"]
    identity = modules["services.accounts.identity"]
    prefs = modules["services.messenger.preferences"]

    first = entrypoints.register_user_entry(
        10001,
        platform="telegram",
        external_user_id="tg-10001",
        username="test-user",
        display_name="Test User",
    )
    assert first.user_id == 10001

    token = bridge.issue_bridge_token(
        10001,
        target_platform="vk",
        created_from_platform="telegram",
        created_from_external_user_id="tg-10001",
    )
    linked = entrypoints.register_user_entry(
        20002,
        platform="vk",
        external_user_id="vk-20002",
        start_payload=f"bridge_{token}",
    )

    assert linked.user_id == 10001
    assert linked.linked_via_bridge is True

    snapshot = identity.get_account_snapshot(10001)
    assert {row["platform"] for row in snapshot["identities"]} == {"telegram", "vk"}

    legacy_snapshot = prefs.get_channel_snapshot(10001)
    assert {row["platform"] for row in legacy_snapshot["identities"]} == {"telegram", "vk"}


def test_bridge_token_is_single_use_and_cannot_replace_linked_identity(tmp_path, monkeypatch):
    modules = _fresh_modules(tmp_path, monkeypatch)
    entrypoints = modules["services.messenger.entrypoints"]
    bridge = modules["services.messenger.bridge"]
    identity = modules["services.accounts.identity"]

    entrypoints.register_user_entry(10001, platform="telegram", external_user_id="tg-10001")
    token = bridge.issue_bridge_token(10001, target_platform="vk")

    first = entrypoints.register_user_entry(
        20002,
        platform="vk",
        external_user_id="vk-first",
        start_payload=f"bridge_{token}",
    )
    replay = entrypoints.register_user_entry(
        30003,
        platform="vk",
        external_user_id="vk-replay",
        start_payload=f"bridge_{token}",
    )

    assert first.user_id == 10001
    assert first.linked_via_bridge is True
    assert replay.user_id != 10001
    assert replay.linked_via_bridge is False

    original = identity.get_account_snapshot(10001)
    vk_identity = next(row for row in original["identities"] if row["platform"] == "vk")
    assert vk_identity["external_user_id"] == "vk-first"

    replay_account = identity.get_account_snapshot(replay.user_id)
    assert replay_account["identities"][0]["external_user_id"] == "vk-replay"


def test_bridge_token_concurrent_consumption_has_one_winner(tmp_path, monkeypatch):
    modules = _fresh_modules(tmp_path, monkeypatch)
    bridge = modules["services.messenger.bridge"]

    bridge.issue_bridge_token(10001, target_platform="vk")
    token = bridge.issue_bridge_token(10001, target_platform="vk")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(bridge.consume_bridge_token, token, platform="vk", external_user_id="vk-a"),
            executor.submit(bridge.consume_bridge_token, token, platform="vk", external_user_id="vk-b"),
        ]
        results = [future.result(timeout=10) for future in futures]

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].consumed is True
    assert bridge.resolve_bridge_token(token).consumed is True


def test_plain_returning_messenger_resolves_existing_account(tmp_path, monkeypatch):
    modules = _fresh_modules(tmp_path, monkeypatch)
    entrypoints = modules["services.messenger.entrypoints"]
    bridge = modules["services.messenger.bridge"]

    entrypoints.register_user_entry(10001, platform="telegram", external_user_id="tg-10001")
    token = bridge.issue_bridge_token(10001, target_platform="vk")
    entrypoints.register_user_entry(
        20002,
        platform="vk",
        external_user_id="vk-20002",
        start_payload=f"bridge_{token}",
    )

    returning = entrypoints.register_user_entry(
        20002,
        platform="vk",
        external_user_id="vk-20002",
    )

    assert returning.user_id == 10001
    assert returning.linked_via_bridge is False


def test_delivery_plan_uses_linked_channel_identity(tmp_path, monkeypatch):
    modules = _fresh_modules(tmp_path, monkeypatch)
    entrypoints = modules["services.messenger.entrypoints"]
    bridge = modules["services.messenger.bridge"]
    outbound = modules["services.messenger.outbound"]

    entrypoints.register_user_entry(10001, platform="telegram", external_user_id="tg-10001")
    token = bridge.issue_bridge_token(10001, target_platform="vk")
    entrypoints.register_user_entry(
        20002,
        platform="vk",
        external_user_id="vk-20002",
        start_payload=f"bridge_{token}",
    )

    plan = outbound.build_delivery_plan(10001, preferred_platform="vk")

    assert plan.user_id == 10001
    assert plan.platform == "vk"
    assert plan.external_user_id == "vk-20002"


def test_bridge_token_rejects_wrong_target_platform(tmp_path, monkeypatch):
    modules = _fresh_modules(tmp_path, monkeypatch)
    entrypoints = modules["services.messenger.entrypoints"]
    bridge = modules["services.messenger.bridge"]
    identity = modules["services.accounts.identity"]

    entrypoints.register_user_entry(10001, platform="telegram", external_user_id="tg-10001")
    token = bridge.issue_bridge_token(10001, target_platform="vk")

    result = entrypoints.register_user_entry(
        30003,
        platform="max",
        external_user_id="max-30003",
        start_payload=f"bridge_{token}",
    )

    assert result.user_id != 30003
    assert result.user_id >= (1 << 62)
    assert result.linked_via_bridge is False
    assert {row["platform"] for row in identity.get_account_snapshot(10001)["identities"]} == {"telegram"}
    assert {row["platform"] for row in identity.get_account_snapshot(result.user_id)["identities"]} == {"max"}


def test_identity_conflict_blocks_unconfirmed_account_merge(tmp_path, monkeypatch):
    modules = _fresh_modules(tmp_path, monkeypatch)
    identity = modules["services.accounts.identity"]

    identity.link_channel_to_account(10001, "vk", "vk-20002")

    with pytest.raises(identity.AccountIdentityConflict):
        identity.link_channel_to_account(30003, "vk", "vk-20002")

    assert {row["platform"] for row in identity.get_account_snapshot(10001)["identities"]} == {"vk"}
    assert identity.get_account_snapshot(30003)["identities"] == []


def test_legacy_channel_identity_writes_through_to_account_layer(tmp_path, monkeypatch):
    modules = _fresh_modules(tmp_path, monkeypatch)
    identity = modules["services.accounts.identity"]
    prefs = modules["services.messenger.preferences"]

    prefs.record_channel_identity(10001, "telegram", "tg-10001", username="test-user")
    prefs.record_channel_identity(10001, "vk", "vk-20002")

    snapshot = identity.get_account_snapshot(10001)
    assert {row["platform"] for row in snapshot["identities"]} == {"telegram", "vk"}

    with pytest.raises(identity.AccountIdentityConflict):
        prefs.record_channel_identity(30003, "vk", "vk-20002")

    assert {row["platform"] for row in prefs.get_channel_snapshot(10001)["identities"]} == {"telegram", "vk"}
    assert prefs.get_channel_snapshot(30003)["identities"] == []


def test_same_numeric_id_on_three_platforms_creates_three_accounts(tmp_path, monkeypatch):
    modules = _fresh_modules(tmp_path, monkeypatch)
    entrypoints = modules["services.messenger.entrypoints"]

    telegram = entrypoints.register_user_entry(777, platform="telegram", external_user_id="777")
    vk = entrypoints.register_user_entry(777, platform="vk", external_user_id="777")
    max_user = entrypoints.register_user_entry(777, platform="max", external_user_id="777")

    assert telegram.user_id == 777
    assert vk.user_id != telegram.user_id
    assert max_user.user_id not in {telegram.user_id, vk.user_id}
    assert vk.user_id >= (1 << 62)
    assert max_user.user_id >= (1 << 62)


def test_same_numeric_id_is_separate_when_non_telegram_arrives_first(tmp_path, monkeypatch):
    modules = _fresh_modules(tmp_path, monkeypatch)
    entrypoints = modules["services.messenger.entrypoints"]

    vk = entrypoints.register_user_entry(888, platform="vk", external_user_id="888")
    telegram = entrypoints.register_user_entry(888, platform="telegram", external_user_id="888")

    assert vk.user_id >= (1 << 62)
    assert telegram.user_id == 888
    assert telegram.user_id != vk.user_id
