"""C3.0.4 — Tests for the ops_audit_log archive exporter."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.scripts.export_ops_audit_log import (
    COLUMNS,
    RESTORED_PREFIX,
    export_rows,
    restore_rows,
)


def _create_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE ops_audit_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id      TEXT,
            endpoint        TEXT NOT NULL,
            reviewer        TEXT NOT NULL,
            reason          TEXT,
            payload_json    TEXT,
            response_json   TEXT,
            outcome         TEXT NOT NULL,
            http_status     INTEGER NOT NULL,
            created_at      TIMESTAMP NOT NULL
        )
        """
    )
    conn.commit()


def _seed_row(
    conn: sqlite3.Connection,
    *,
    when: datetime,
    request_id: str = "req-test",
    reviewer: str = "operator-1",
    reason: str | None = None,
    outcome: str = "ok",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO ops_audit_log
          (request_id, endpoint, reviewer, reason,
           payload_json, response_json, outcome, http_status, created_at)
        VALUES (?, 'POST /api/render/ops/test', ?, ?, '{}', '{}', ?, 200, ?)
        """,
        (request_id, reviewer, reason, outcome, when.isoformat()),
    )
    return int(cur.lastrowid or 0)


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "audit.db"
    cx = sqlite3.connect(db_path)
    cx.row_factory = sqlite3.Row
    _create_table(cx)
    yield cx
    cx.close()


def test_export_buckets_rows_into_monthly_shards(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    now = datetime(2027, 1, 15, tzinfo=timezone.utc)
    # Old enough → exported
    _seed_row(conn, when=datetime(2026, 9, 5, tzinfo=timezone.utc), request_id="req-old-1")
    _seed_row(conn, when=datetime(2026, 9, 20, tzinfo=timezone.utc), request_id="req-old-2")
    _seed_row(conn, when=datetime(2026, 10, 1, tzinfo=timezone.utc), request_id="req-old-3")
    # Too young → not exported (now - 30 days)
    _seed_row(conn, when=now - timedelta(days=30), request_id="req-young")
    conn.commit()

    result = export_rows(
        conn=conn,
        output_dir=tmp_path / "out",
        min_age_days=90,
        overwrite=False,
        now=now,
    )
    # 3 old rows (Sep 2026 + Oct 2026), 1 young row excluded.
    assert result["rows_written"] == 3
    assert len(result["shards"]) == 2  # 2026-09 + 2026-10
    sep = tmp_path / "out" / "2026" / "09.jsonl"
    oct_ = tmp_path / "out" / "2026" / "10.jsonl"
    assert sep.exists() and oct_.exists()
    sep_rows = [json.loads(line) for line in sep.read_text(encoding="utf-8").splitlines()]
    assert {r["request_id"] for r in sep_rows} == {"req-old-1", "req-old-2"}


def test_export_refuses_overwrite_without_flag(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    now = datetime(2027, 1, 15, tzinfo=timezone.utc)
    _seed_row(conn, when=datetime(2026, 9, 5, tzinfo=timezone.utc))
    conn.commit()

    out = tmp_path / "out"
    export_rows(conn=conn, output_dir=out, min_age_days=90, overwrite=False, now=now)
    # Second run without --overwrite must FAIL on the existing shard.
    with pytest.raises(FileExistsError):
        export_rows(conn=conn, output_dir=out, min_age_days=90, overwrite=False, now=now)
    # With --overwrite it succeeds.
    export_rows(conn=conn, output_dir=out, min_age_days=90, overwrite=True, now=now)


def test_export_byte_identical_on_repeated_run_with_overwrite(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    now = datetime(2027, 1, 15, tzinfo=timezone.utc)
    _seed_row(conn, when=datetime(2026, 9, 5, tzinfo=timezone.utc), request_id="req-1")
    _seed_row(conn, when=datetime(2026, 9, 6, tzinfo=timezone.utc), request_id="req-2")
    conn.commit()

    out = tmp_path / "out"
    export_rows(conn=conn, output_dir=out, min_age_days=90, overwrite=False, now=now)
    shard = out / "2026" / "09.jsonl"
    first = shard.read_bytes()
    export_rows(conn=conn, output_dir=out, min_age_days=90, overwrite=True, now=now)
    second = shard.read_bytes()
    assert first == second


def test_restore_inserts_rows_with_prefixed_reason(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    now = datetime(2027, 1, 15, tzinfo=timezone.utc)
    _seed_row(
        conn,
        when=datetime(2026, 9, 5, tzinfo=timezone.utc),
        request_id="req-restore",
        reason="original_reason",
    )
    conn.commit()
    out = tmp_path / "out"
    export_rows(conn=conn, output_dir=out, min_age_days=90, overwrite=False, now=now)

    # Drop the live row to simulate it being purged.
    conn.execute("DELETE FROM ops_audit_log WHERE request_id = 'req-restore'")
    conn.commit()

    shard = out / "2026" / "09.jsonl"
    result = restore_rows(conn=conn, archive=shard, where='request_id="req-restore"')
    assert result["restored_rows"] == 1
    row = conn.execute(
        "SELECT reason, request_id FROM ops_audit_log WHERE request_id = 'req-restore'"
    ).fetchone()
    assert row is not None
    assert row["reason"].startswith(RESTORED_PREFIX)


def test_columns_are_stable_contract() -> None:
    """Schema contract: if a new column is added, this test fails so the
    exporter is updated in lockstep.
    """
    expected = {
        "id",
        "request_id",
        "endpoint",
        "reviewer",
        "reason",
        "payload_json",
        "response_json",
        "outcome",
        "http_status",
        "created_at",
    }
    assert set(COLUMNS) == expected
