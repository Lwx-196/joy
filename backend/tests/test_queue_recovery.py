"""Tests for `RENDER_QUEUE.recover()` and `UPGRADE_QUEUE.recover()`.

`recover()` is called once at process start (from `backend.main` import) to:
  1. Demote residual 'running' rows back to 'queued' (a previous crash left
     them stuck — no worker is actually executing them).
  2. Re-submit every queued row to the thread pool.

Tests insert raw rows to simulate a stale state, run recover() with the pool
shielded by `no_job_pool`, and assert the row-status transitions + the
returned counts.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_render_row(
    conn,
    case_id: int,
    status: str,
    started_at: str | None = None,
    brand: str = "fumei",
    template: str = "tri-compare",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO render_jobs
            (case_id, brand, template, status, enqueued_at, started_at, semantic_judge)
        VALUES (?, ?, ?, ?, ?, ?, 'off')
        """,
        (case_id, brand, template, status, _now_iso(), started_at),
    )
    return cur.lastrowid


def _insert_upgrade_row(conn, case_id: int, status: str, brand: str = "fumei") -> int:
    cur = conn.execute(
        """
        INSERT INTO upgrade_jobs (case_id, brand, status, enqueued_at)
        VALUES (?, ?, ?, ?)
        """,
        (case_id, brand, status, _now_iso()),
    )
    return cur.lastrowid


# ----------------------------------------------------------------------
# render_queue.recover()
# ----------------------------------------------------------------------


def test_render_recover_demotes_running_to_queued(temp_db, seed_case, no_job_pool):
    from backend import db
    from backend.render_queue import RENDER_QUEUE

    case_id = seed_case()
    with db.connect() as conn:
        running_id = _insert_render_row(
            conn, case_id, "running", started_at=_now_iso()
        )

    counts = RENDER_QUEUE.recover()
    assert counts["requeued_running"] == 1
    assert counts["resubmitted_queued"] == 1  # the demoted-now-queued row

    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, started_at FROM render_jobs WHERE id = ?", (running_id,)
        ).fetchone()
    assert row["status"] == "queued"
    assert row["started_at"] is None


def test_render_recover_resubmits_queued(temp_db, seed_case, no_job_pool):
    from backend import db
    from backend.render_queue import RENDER_QUEUE

    case_id = seed_case()
    with db.connect() as conn:
        _insert_render_row(conn, case_id, "queued")
        _insert_render_row(conn, case_id, "queued")

    counts = RENDER_QUEUE.recover()
    assert counts["requeued_running"] == 0
    assert counts["resubmitted_queued"] == 2


def test_render_recover_skips_terminal_states(temp_db, seed_case, no_job_pool):
    """`done` / issue-done / `blocked` / `failed` / `cancelled` / `undone` are terminal — recover()
    must not touch them or count them as resubmits.
    """
    from backend import db
    from backend.render_queue import RENDER_QUEUE

    case_id = seed_case()
    with db.connect() as conn:
        for status in ("done", "done_with_issues", "blocked", "failed", "cancelled", "undone"):
            _insert_render_row(conn, case_id, status)

    counts = RENDER_QUEUE.recover()
    assert counts == {"requeued_running": 0, "resubmitted_queued": 0}

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT status FROM render_jobs ORDER BY id"
        ).fetchall()
    statuses = [r["status"] for r in rows]
    assert statuses == ["done", "done_with_issues", "blocked", "failed", "cancelled", "undone"]


def test_render_recover_empty_db_returns_zero_counts(temp_db, no_job_pool):
    from backend.render_queue import RENDER_QUEUE

    counts = RENDER_QUEUE.recover()
    assert counts == {"requeued_running": 0, "resubmitted_queued": 0}


# ----------------------------------------------------------------------
# upgrade_queue.recover()
# ----------------------------------------------------------------------


def test_upgrade_recover_demotes_running_to_queued(temp_db, seed_case, no_job_pool):
    from backend import db
    from backend.upgrade_queue import UPGRADE_QUEUE

    case_id = seed_case()
    with db.connect() as conn:
        running_id = _insert_upgrade_row(conn, case_id, "running")

    counts = UPGRADE_QUEUE.recover()
    assert counts["requeued_running"] == 1
    assert counts["resubmitted_queued"] == 1

    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, started_at FROM upgrade_jobs WHERE id = ?", (running_id,)
        ).fetchone()
    assert row["status"] == "queued"
    assert row["started_at"] is None


def test_upgrade_recover_resubmits_queued(temp_db, seed_case, no_job_pool):
    from backend import db
    from backend.upgrade_queue import UPGRADE_QUEUE

    case_id = seed_case()
    with db.connect() as conn:
        _insert_upgrade_row(conn, case_id, "queued")
        _insert_upgrade_row(conn, case_id, "queued")
        _insert_upgrade_row(conn, case_id, "queued")

    counts = UPGRADE_QUEUE.recover()
    assert counts["resubmitted_queued"] == 3


def test_upgrade_recover_skips_terminal_states(temp_db, seed_case, no_job_pool):
    from backend import db
    from backend.upgrade_queue import UPGRADE_QUEUE

    case_id = seed_case()
    with db.connect() as conn:
        for status in ("done", "failed", "cancelled", "undone"):
            _insert_upgrade_row(conn, case_id, status)

    counts = UPGRADE_QUEUE.recover()
    assert counts == {"requeued_running": 0, "resubmitted_queued": 0}


def test_upgrade_recover_empty_db_returns_zero(temp_db, no_job_pool):
    from backend.upgrade_queue import UPGRADE_QUEUE

    counts = UPGRADE_QUEUE.recover()
    assert counts == {"requeued_running": 0, "resubmitted_queued": 0}
