from __future__ import annotations

import stat
from pathlib import Path

import pytest

from scripts import migrate_privacy_export_env as migration


DEFAULT_URL = "https://clientplatform-bot.clientplatform.ru"
SYNTHETIC_TOKEN = "not-a-live-telegram-token"


def _assignments(path: Path) -> dict[str, tuple[int, str]]:
    return migration._active_assignments(path.read_text(encoding="utf-8").splitlines(keepends=True))


def test_migration_adds_only_managed_keys_and_keeps_exact_backup(tmp_path: Path) -> None:
    env_file = tmp_path / "clientplatform.env"
    original = (
        "# production secrets\n"
        f"BOT_TOKEN={SYNTHETIC_TOKEN}\n"
        "DATABASE_URL='postgresql://db-user:db-pass@db/clientplatform'\n"
        "TELEGRAM_TRANSPORT=polling\n"
    ).encode()
    env_file.write_bytes(original)
    env_file.chmod(0o640)

    result = migration.migrate_env_file(
        env_file,
        fallback_public_base_url=DEFAULT_URL,
        fallback_ttl_minutes=10,
    )

    assert result.changed is True
    assert result.backup_path is not None
    assert result.backup_path.read_bytes() == original
    assert stat.S_IMODE(result.backup_path.stat().st_mode) == 0o640
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o640
    updated = env_file.read_text(encoding="utf-8")
    assert f"BOT_TOKEN={SYNTHETIC_TOKEN}" in updated
    assert "DATABASE_URL='postgresql://db-user:db-pass@db/clientplatform'" in updated
    active = _assignments(env_file)
    assert active["PRIVACY_EXPORT_HTTP_ENABLED"][1] == "1"
    assert active["PRIVACY_EXPORT_PUBLIC_BASE_URL"][1] == DEFAULT_URL
    assert active["PRIVACY_EXPORT_TOKEN_TTL_MINUTES"][1] == "10"

    first_bytes = env_file.read_bytes()
    second = migration.migrate_env_file(
        env_file,
        fallback_public_base_url=DEFAULT_URL,
        fallback_ttl_minutes=10,
    )
    assert second.changed is False
    assert second.backup_path is None
    assert env_file.read_bytes() == first_bytes
    for key in migration.MANAGED_KEYS:
        assert updated.count(f"{key}=") == 1


def test_migration_preserves_valid_custom_url_ttl_and_export_prefix(tmp_path: Path) -> None:
    env_file = tmp_path / "clientplatform.env"
    env_file.write_text(
        "export PRIVACY_EXPORT_HTTP_ENABLED=0\n"
        "PRIVACY_EXPORT_PUBLIC_BASE_URL='https://custom.example/clientplatform'\n"
        "PRIVACY_EXPORT_TOKEN_TTL_MINUTES=20\n",
        encoding="utf-8",
    )

    result = migration.migrate_env_file(
        env_file,
        fallback_public_base_url=DEFAULT_URL,
        fallback_ttl_minutes=10,
    )

    assert result.public_base_url == "https://custom.example/clientplatform"
    assert result.ttl_minutes == 20
    text = env_file.read_text(encoding="utf-8")
    assert "export PRIVACY_EXPORT_HTTP_ENABLED=1" in text
    assert "PRIVACY_EXPORT_PUBLIC_BASE_URL=https://custom.example/clientplatform" in text
    assert "PRIVACY_EXPORT_TOKEN_TTL_MINUTES=20" in text


def test_migration_repairs_invalid_managed_values_with_safe_fallbacks(tmp_path: Path) -> None:
    env_file = tmp_path / "clientplatform.env"
    env_file.write_text(
        "PRIVACY_EXPORT_HTTP_ENABLED=maybe\n"
        "PRIVACY_EXPORT_PUBLIC_BASE_URL=http://insecure.example\n"
        "PRIVACY_EXPORT_TOKEN_TTL_MINUTES=999\n",
        encoding="utf-8",
    )

    result = migration.migrate_env_file(
        env_file,
        fallback_public_base_url=DEFAULT_URL,
        fallback_ttl_minutes=10,
    )

    assert result.changed is True
    active = _assignments(env_file)
    assert active["PRIVACY_EXPORT_HTTP_ENABLED"][1] == "1"
    assert active["PRIVACY_EXPORT_PUBLIC_BASE_URL"][1] == DEFAULT_URL
    assert active["PRIVACY_EXPORT_TOKEN_TTL_MINUTES"][1] == "10"


def test_migration_rejects_duplicate_keys_without_touching_file(tmp_path: Path) -> None:
    env_file = tmp_path / "clientplatform.env"
    original = (
        "PRIVACY_EXPORT_HTTP_ENABLED=0\n"
        "PRIVACY_EXPORT_HTTP_ENABLED=1\n"
    ).encode()
    env_file.write_bytes(original)

    with pytest.raises(migration.MigrationError, match="duplicate managed"):
        migration.migrate_env_file(
            env_file,
            fallback_public_base_url=DEFAULT_URL,
            fallback_ttl_minutes=10,
        )

    assert env_file.read_bytes() == original
    assert list(tmp_path.glob("clientplatform.env.bak.privacy-export.*")) == []


def test_migration_rejects_symlink_and_world_writable_env(tmp_path: Path) -> None:
    real_env = tmp_path / "real.env"
    real_env.write_text("BOT_TOKEN=synthetic\n", encoding="utf-8")
    linked_env = tmp_path / "linked.env"
    linked_env.symlink_to(real_env)

    with pytest.raises(migration.MigrationError, match="symbolic link"):
        migration.migrate_env_file(
            linked_env,
            fallback_public_base_url=DEFAULT_URL,
        )

    real_env.chmod(0o666)
    with pytest.raises(migration.MigrationError, match="world-writable"):
        migration.migrate_env_file(
            real_env,
            fallback_public_base_url=DEFAULT_URL,
        )


def test_migration_rejects_invalid_cli_fallbacks_before_writing(tmp_path: Path) -> None:
    env_file = tmp_path / "clientplatform.env"
    original = b"BOT_TOKEN=synthetic\n"
    env_file.write_bytes(original)

    with pytest.raises(migration.MigrationError, match="HTTPS"):
        migration.migrate_env_file(
            env_file,
            fallback_public_base_url="http://insecure.example",
        )
    with pytest.raises(migration.MigrationError, match="between 2 and 30"):
        migration.migrate_env_file(
            env_file,
            fallback_public_base_url=DEFAULT_URL,
            fallback_ttl_minutes=31,
        )

    assert env_file.read_bytes() == original


def test_post_write_verification_failure_restores_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "clientplatform.env"
    original = b"BOT_TOKEN=synthetic\n"
    env_file.write_bytes(original)
    real_atomic_replace = migration._atomic_replace
    calls = 0

    def corrupt_first_replace(path: Path, data: bytes, *, mode: int, uid: int, gid: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            real_atomic_replace(
                path,
                b"BOT_TOKEN=synthetic\nPRIVACY_EXPORT_HTTP_ENABLED=0\n",
                mode=mode,
                uid=uid,
                gid=gid,
            )
            return
        real_atomic_replace(path, data, mode=mode, uid=uid, gid=gid)

    monkeypatch.setattr(migration, "_atomic_replace", corrupt_first_replace)

    with pytest.raises(migration.MigrationError, match="post-write verification"):
        migration.migrate_env_file(
            env_file,
            fallback_public_base_url=DEFAULT_URL,
        )

    assert calls == 2
    assert env_file.read_bytes() == original


def test_cli_output_never_prints_existing_secrets(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env_file = tmp_path / "clientplatform.env"
    secret = "super-private-synthetic-token"
    env_file.write_text(f"BOT_TOKEN={secret}\n", encoding="utf-8")

    code = migration.main(
        [
            "--env-file",
            str(env_file),
            "--public-base-url",
            DEFAULT_URL,
            "--ttl-minutes",
            "10",
        ]
    )

    output = capsys.readouterr()
    assert code == 0
    assert "PRIVACY_EXPORT_ENV_MIGRATION_OK" in output.out
    assert secret not in output.out
    assert secret not in output.err
