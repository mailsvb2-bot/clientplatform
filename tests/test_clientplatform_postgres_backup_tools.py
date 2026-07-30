from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import clientplatform_postgres_backup as backup


class ClientPlatformPostgresBackupToolTests(unittest.TestCase):
    def test_client_major_defaults_to_postgresql_16_and_is_bounded(self) -> None:
        self.assertEqual(backup._configured_client_major({}), 16)
        self.assertEqual(
            backup._configured_client_major(
                {"CLIENTPLATFORM_POSTGRES_CLIENT_MAJOR": "17"}
            ),
            17,
        )
        for value in ("not-a-number", "11", "21"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    backup._configured_client_major(
                        {"CLIENTPLATFORM_POSTGRES_CLIENT_MAJOR": value}
                    )

    def test_tool_override_must_be_absolute(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be an absolute path"):
            backup._resolve_postgres_tool(
                "pg_dump",
                env={
                    "CLIENTPLATFORM_POSTGRES_CLIENT_MAJOR": "16",
                    "CLIENTPLATFORM_PG_DUMP_PATH": "bin/pg_dump",
                },
            )

    def test_tool_override_accepts_exact_required_major(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tool = Path(temp) / "pg_dump"
            tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            tool.chmod(0o700)
            with patch.object(backup, "_postgres_tool_major", return_value=16):
                selected = backup._resolve_postgres_tool(
                    "pg_dump",
                    env={
                        "CLIENTPLATFORM_POSTGRES_CLIENT_MAJOR": "16",
                        "CLIENTPLATFORM_PG_DUMP_PATH": str(tool),
                    },
                )
            self.assertEqual(selected, str(tool.resolve()))

    def test_tool_override_rejects_major_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tool = Path(temp) / "pg_restore"
            tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            tool.chmod(0o700)
            with patch.object(backup, "_postgres_tool_major", return_value=14):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "required 16, found 14",
                ):
                    backup._resolve_postgres_tool(
                        "pg_restore",
                        env={
                            "CLIENTPLATFORM_POSTGRES_CLIENT_MAJOR": "16",
                            "CLIENTPLATFORM_PG_RESTORE_PATH": str(tool),
                        },
                    )

    def test_unknown_postgres_tool_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported"):
            backup._resolve_postgres_tool("createdb", env={})

    def test_create_backup_uses_resolved_pg_dump_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            commands: list[list[str]] = []

            def fake_run(
                command: list[str],
                *,
                env: dict[str, str],
                capture: bool = False,
            ) -> str:
                del env, capture
                commands.append(command)
                target = Path(command[command.index("--file") + 1])
                target.write_bytes(b"custom-dump")
                return ""

            with (
                patch.object(
                    backup,
                    "_resolve_postgres_tool",
                    return_value="/usr/lib/postgresql/16/bin/pg_dump",
                ),
                patch.object(backup, "_run", side_effect=fake_run),
            ):
                target = backup.create_backup(
                    database_url=(
                        "postgresql://clientplatform_app:secret@postgres:5432/"
                        "clientplatform"
                    ),
                    backup_dir=root,
                    retention_days=30,
                    now=1_785_402_138.0,
                )

            self.assertTrue(target.is_file())
            self.assertEqual(
                commands[0][0],
                "/usr/lib/postgresql/16/bin/pg_dump",
            )


if __name__ == "__main__":
    unittest.main()
