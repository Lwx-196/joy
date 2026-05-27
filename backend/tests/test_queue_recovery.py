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

from datetime import datetime, timedelta, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_minutes_ago(minutes: int) -> str:
    """Mirror production `_now_iso()` format (ISO with `T`, not SQLite
    space-separated). Used to seed `recovery_claimed_at` in orphan tests so
    they exercise the real production write path — see bug_001 (round-2
    ultrareview): seeding via SQLite `datetime('now', '-10 minutes')` silently
    bypasses the lex/julianday compare path that production actually hits.
    """
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


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


# ----------------------------------------------------------------------
# orphan recovery (bug_003): queued rows tagged by a prior recover() that
# crashed before _job_pool.submit() must be reclaimed on next recover()
# ----------------------------------------------------------------------


def test_render_recover_reclaims_orphan_with_stale_token(
    temp_db, seed_case, no_job_pool
):
    """Simulates: prior process committed recovery_token then died before
    submitting to _job_pool. Without orphan reclaim, next recover()'s SELECT
    `AND recovery_token IS NULL` would skip the row forever.
    """
    from backend import db
    from backend.render_queue import RENDER_QUEUE

    case_id = seed_case()
    with db.connect() as conn:
        queued_id = _insert_render_row(conn, case_id, "queued")
        # Stale tag: claimed > 5 minutes ago by a dead process.
        # IMPORTANT: seed via Python ISO format (matches production
        # `_now_iso()`), NOT SQLite `datetime('now', '-10 minutes')`. The
        # two formats are NOT lex-comparable within a UTC day — the round-1
        # version of this test used SQLite datetime() and passed against a
        # broken reclaim that lex-compared formats. See bug_001 (round-2
        # ultrareview).
        conn.execute(
            """
            UPDATE render_jobs
            SET recovery_token = ?,
                recovery_claimed_at = ?
            WHERE id = ?
            """,
            ("render-recover:99999:deadbeef", _iso_minutes_ago(10), queued_id),
        )

    counts = RENDER_QUEUE.recover()
    assert counts["resubmitted_queued"] == 1, "orphan must be reclaimed"

    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, recovery_token FROM render_jobs WHERE id = ?",
            (queued_id,),
        ).fetchone()
    assert row["status"] == "queued"
    # New recover() re-tagged with this process's token (non-NULL but fresh)
    assert row["recovery_token"] is not None
    assert "deadbeef" not in row["recovery_token"], "stale token must be replaced"
