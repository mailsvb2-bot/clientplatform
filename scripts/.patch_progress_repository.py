from pathlib import Path

path = Path("clientplatform/infrastructure/customer_progress_repository.py")
source = path.read_text(encoding="utf-8")
old = '''        self._conn.execute(
            """
            INSERT OR IGNORE INTO lesson_progress(
                id, business_id, program_id, enrollment_id, lesson_id,
                status, delivered_at, opened_at, completed_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL, ?)
            """,
            (str(uuid4()), business_id, program_id, enrollment_id, lesson_id, now),
        )
        return delivery_id
'''
new = '''        progress = self._conn.execute(
            "SELECT id FROM lesson_progress WHERE business_id=? AND enrollment_id=? AND lesson_id=? LIMIT 1",
            (business_id, enrollment_id, lesson_id),
        ).fetchone()
        if progress is None:
            try:
                self._conn.execute(
                    """
                    INSERT INTO lesson_progress(
                        id, business_id, program_id, enrollment_id, lesson_id,
                        status, delivered_at, opened_at, completed_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL, ?)
                    """,
                    (str(uuid4()), business_id, program_id, enrollment_id, lesson_id, now),
                )
            except sqlite3.IntegrityError:
                concurrent = self._conn.execute(
                    "SELECT id FROM lesson_progress WHERE business_id=? AND enrollment_id=? AND lesson_id=? LIMIT 1",
                    (business_id, enrollment_id, lesson_id),
                ).fetchone()
                if concurrent is None:
                    raise
        return delivery_id
'''
if source.count(old) != 1:
    raise SystemExit("progress repository patch anchor mismatch")
path.write_text(source.replace(old, new), encoding="utf-8")
