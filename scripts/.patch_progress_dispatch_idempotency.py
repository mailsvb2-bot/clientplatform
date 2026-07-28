from pathlib import Path

path = Path("clientplatform/infrastructure/customer_progress_repository.py")
source = path.read_text(encoding="utf-8")
old = '''            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL,
                     NULL, NULL, ?, ?, NULL, NULL)
            """,
'''
new = '''            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL,
                     NULL, NULL, ?, ?, NULL, NULL)
            ON CONFLICT(
                business_id, logical_delivery_id, connection_id, customer_identity_id
            ) DO NOTHING
            """,
'''
if source.count(old) != 1:
    raise SystemExit("progress dispatch idempotency anchor mismatch")
path.write_text(source.replace(old, new), encoding="utf-8")
