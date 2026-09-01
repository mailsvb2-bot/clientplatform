from __future__ import annotations

import sys
from pathlib import Path

ROOT_PATH = Path(__file__).resolve().parents[1]
if str(ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(ROOT_PATH))

import sqlite3
import tempfile

from scripts import backup_db, restore_db
from services.db.runtime import is_postgres_enabled, redacted_db_target


def main() -> int:
    if is_postgres_enabled():
        print(
            'SKIP: CLIENTPLATFORM_DB_ENGINE=postgres uses pg_dump/psql restore drills, not SQLite restore_drill.py. '
            f'Target={redacted_db_target()}'
        )
        return 0

    backup = restore_db._latest_backup()
    if backup is None or not backup.exists():
        backup_db.main()
        backup = restore_db._latest_backup()
    if backup is None or not backup.exists():
        raise SystemExit('No backup available for restore drill')

    restore_db._integrity_check(backup)
    with tempfile.TemporaryDirectory(prefix='clientplatform_restore_drill_') as tmp:
        target = Path(tmp) / 'restore_check.db'
        restore_db._restore(backup, target)
        restore_db._integrity_check(target)
        conn = sqlite3.connect(target)
        try:
            conn.execute('SELECT 1').fetchone()
        finally:
            conn.close()
    print('OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
