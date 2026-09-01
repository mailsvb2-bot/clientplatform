from __future__ import annotations

import gzip
import json
import stat
from types import SimpleNamespace

import pytest

from services import privacy_controls
from services.db import db


def test_streaming_export_writes_valid_gzip_json(tmp_path) -> None:
    uid = 987654399
    with db() as conn:
        conn.execute("DELETE FROM events WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE user_id=?", (uid,))
        conn.execute(
            """
            INSERT INTO users(user_id, joined_at, username, first_name)
            VALUES(?,?,?,?)
            """.strip(),
            (uid, "2026-07-17", "stream_user", "Stream"),
        )
        for index in range(5):
            conn.execute(
                "INSERT INTO events(user_id, event, ts, meta) VALUES(?,?,?,?)",
                (uid, f"stream_event_{index}", f"2026-07-17T00:00:0{index}+00:00", json.dumps({"n": index})),
            )

    output_path = tmp_path / "user-export.json.gz"
    result = privacy_controls.write_user_data_export_gzip(uid, output_path, batch_size=2)

    assert result.path == output_path
    assert result.compressed_size_bytes == output_path.stat().st_size
    assert result.compressed_size_bytes > 0
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    assert result.table_rows["users"] == 1
    assert result.table_rows["events"] == 5
    assert result.total_rows >= 6

    with gzip.open(output_path, mode="rt", encoding="utf-8") as stream:
        payload = json.load(stream)

    assert payload["user_id"] == uid
    assert payload["privacy_manifest_version"] == privacy_controls.MANIFEST_VERSION
    assert payload["tables"]["users"][0]["username"] == "stream_user"
    assert sorted(row["event"] for row in payload["tables"]["events"]) == [
        f"stream_event_{index}" for index in range(5)
    ]

    with db() as conn:
        conn.execute("DELETE FROM events WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE user_id=?", (uid,))


def test_owned_rows_are_fetched_in_bounded_batches(monkeypatch) -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.rows = [
                {"id": 1},
                {"id": 2},
                {"id": 3},
                {"id": 4},
                {"id": 5},
            ]
            self.calls: list[int] = []

        def fetchmany(self, size: int):
            self.calls.append(size)
            batch = self.rows[:size]
            self.rows = self.rows[size:]
            return batch

    cursor = FakeCursor()
    monkeypatch.setattr(
        privacy_controls,
        "_owned_rows_cursor",
        lambda _conn, _policy, _user_id: cursor,
    )

    rows = list(
        privacy_controls._iter_owned_rows(
            object(),
            SimpleNamespace(table="events"),
            1,
            batch_size=2,
        )
    )

    assert rows == [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}]
    assert cursor.calls == [2, 2, 2, 2]


def test_partial_export_is_removed_on_failure(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "partial.json.gz"

    def fail_rows(*_args, **_kwargs):
        raise RuntimeError("synthetic export failure")

    monkeypatch.setattr(privacy_controls, "_iter_owned_rows", fail_rows)

    with pytest.raises(RuntimeError, match="synthetic export failure"):
        privacy_controls.write_user_data_export_gzip(1, output_path)

    assert not output_path.exists()
