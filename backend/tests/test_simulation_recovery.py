"""Tests for `simulation_recovery.recover_stale_simulation_jobs()`.

simulation_jobs rows go 'running' inside an in-memory `_job_pool` worker; a
process crash strands them forever (real incident: jobs stuck 'running' for
31/44 days). The sweep marks stale running/queued rows 'failed' — it must
NEVER re-run them (submit closure not persisted + re-run burns provider
credits without user intent) — and closes their linked ai_runs rows
(link direction: ai_runs.subject_kind='simulation_job', subject_id=job id).

Timestamps are seeded in Python ISO format (production `_now_iso()` style,
'T'-separated with '+00:00'), NOT via SQLite `datetime()` — the two formats
are not lex-comparable and the sweep must go through the julianday() path
production hits. See the bug_001 note in test_queue_recovery.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.services.simulation_recovery import recover_stale_simulation_jobs


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_minutes_ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _insert_sim_job(conn, case_id: int, status: str, updated_minutes_ago: int = 0) -> int:
    cur = conn.execute(
        """
        INSERT INTO simulation_jobs (case_id, status, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (case_id, status, _now_iso(), _iso_minutes_ago(updated_minutes_ago)),
    )
    return cur.lastrowid


def _insert_ai_run(conn, job_id: int, status: str, subject_kind: str = "simulation_job") -> int:
    cur = conn.execute(
        """
        INSERT INTO ai_runs
            (subject_kind, subject_id, model_role, status, started_at)
        VALUES (?, ?, 'image_generation', ?, ?)
        """,
        (subject_kind, job_id, status, _now_iso()),
    )
    return cur.lastrowid


def test_stale_running_marked_failed_and_ai_run_closed(temp_db, seed_case):
    from backend import db

    case_id = seed_case()
    with db.connect() as conn:
        job_id = _insert_sim_job(conn, case_id, "running", updated_minutes_ago=60)
        run_id = _insert_ai_run(conn, job_id, "running")

    counts = recover_stale_simulation_jobs()
    assert counts == {"reaped": 1, "ai_runs_closed": 1}

    with db.connect() as conn:
        job = conn.execute(
            "SELECT status, error_message FROM simulation_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        run = conn.execute(
            "SELECT status, error_message, finished_at FROM ai_runs WHERE id = ?", (run_id,)
        ).fetchone()
    assert job["status"] == "failed"
    assert "stale" in job["error_message"]
    assert "(was running)" in job["error_message"]
    assert run["status"] == "failed"
    assert "stale" in run["error_message"]
    assert run["finished_at"] is not None


def test_fresh_running_untouched(temp_db, seed_case):
    from backend import db

    case_id = seed_case()
    with db.connect() as conn:
        job_id = _insert_sim_job(conn, case_id, "running", updated_minutes_ago=5)
        run_id = _insert_ai_run(conn, job_id, "running")

    counts = recover_stale_simulation_jobs()
    assert counts == {"reaped": 0, "ai_runs_closed": 0}

    with db.connect() as conn:
        job = conn.execute(
            "SELECT status, error_message FROM simulation_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        run = conn.execute("SELECT status FROM ai_runs WHERE id = ?", (run_id,)).fetchone()
    assert job["status"] == "running"
    assert job["error_message"] is None
    assert run["status"] == "running"


def test_stale_queued_swept_too(temp_db, seed_case):
    """queued rows exist only as in-memory pool submissions — after a restart
    a stale queued row is just as dead as a stale running one (owner-approved:
    plan §六.3, sweep both)."""
    from backend import db

    case_id = seed_case()
    with db.connect() as conn:
        job_id = _insert_sim_job(conn, case_id, "queued", updated_minutes_ago=45)
        run_id = _insert_ai_run(conn, job_id, "queued")

    counts = recover_stale_simulation_jobs()
    assert counts == {"reaped": 1, "ai_runs_closed": 1}

    with db.connect() as conn:
        job = conn.execute(
            "SELECT status, error_message FROM simulation_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        run = conn.execute("SELECT status FROM ai_runs WHERE id = ?", (run_id,)).fetchone()
    assert job["status"] == "failed"
    assert "(was queued)" in job["error_message"]
    assert run["status"] == "failed"


def test_terminal_rows_untouched_even_when_old(temp_db, seed_case):
    from backend import db

    case_id = seed_case()
    with db.connect() as conn:
        ids = {
            status: _insert_sim_job(conn, case_id, status, updated_minutes_ago=999)
            for status in ("done", "done_with_issues", "failed")
        }

    counts = recover_stale_simulation_jobs()
    assert counts == {"reaped": 0, "ai_runs_closed": 0}

    with db.connect() as conn:
        for status, job_id in ids.items():
            row = conn.execute(
                "SELECT status, error_message FROM simulation_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            assert row["status"] == status
            assert row["error_message"] is None


def test_foreign_subject_kind_ai_run_untouched(temp_db, seed_case):
    """An ai_runs row of another subject_kind that happens to share the
    numeric subject_id must not be closed by the sweep."""
    from backend import db

    case_id = seed_case()
    with db.connect() as conn:
        job_id = _insert_sim_job(conn, case_id, "running", updated_minutes_ago=60)
        foreign_run_id = _insert_ai_run(conn, job_id, "running", subject_kind="render")

    counts = recover_stale_simulation_jobs()
    assert counts == {"reaped": 1, "ai_runs_closed": 0}

    with db.connect() as conn:
        run = conn.execute(
            "SELECT status FROM ai_runs WHERE id = ?", (foreign_run_id,)
        ).fetchone()
    assert run["status"] == "running"
