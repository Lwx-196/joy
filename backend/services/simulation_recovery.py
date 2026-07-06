"""Stale simulation job sweep — mark failed, never re-run.

simulation_jobs rows are driven by in-memory `_job_pool` submissions
(routes/cases_simulation_jobs.py): a process crash strands 'running' (and
never-submitted 'queued') rows forever, because unlike render/upgrade jobs
this table has no recovery-token protocol and the submit closure (resolved
paths, focus regions, style refs) is not fully persisted. Re-running would
also burn provider credits without user intent, so the sweep only flips
stale rows to 'failed' — the operator re-triggers from the UI at will.

Mounted at process start (backend/main.py, after the queue recover() calls).
TTL 30min is far above real single-simulation duration, so false reaping of
a live job is effectively impossible.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .. import db

LOGGER = logging.getLogger(__name__)

DEFAULT_TTL_MINUTES = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def recover_stale_simulation_jobs(ttl_minutes: int = DEFAULT_TTL_MINUTES) -> dict[str, int]:
    """Flip stale 'running'/'queued' simulation jobs to 'failed' and close
    their linked ai_runs (subject_kind='simulation_job' — the link direction
    is ai_runs→job; simulation_jobs has no ai_run_id column).

    julianday() comparison (not lex TEXT compare) because updated_at is
    written as Python ISO format while SQLite's datetime() emits the
    space-separated format — same pitfall documented in render_queue.recover().
    """
    cutoff = f"-{int(ttl_minutes)} minutes"
    with db.connect() as conn:
        # Cheap autocommit probe first: the common case (nothing stale) must
        # not take the write lock.
        stale_rows = conn.execute(
            """
            SELECT id FROM simulation_jobs
            WHERE status IN ('running', 'queued')
              AND julianday(updated_at) < julianday('now', ?)
            """,
            (cutoff,),
        ).fetchall()
        if not stale_rows:
            return {"reaped": 0, "ai_runs_closed": 0}
        ids = [int(r["id"]) for r in stale_rows]
        placeholders = ",".join("?" for _ in ids)
        now = _now_iso()
        conn.execute("BEGIN IMMEDIATE")
        # Status/TTL re-checked inside the write txn: a job that finished
        # between the probe and here must not be clobbered.
        reaped = conn.execute(
            f"""
            UPDATE simulation_jobs
            SET status = 'failed',
                error_message = 'stale: reaped after process restart (was ' || status || ')',
                updated_at = ?
            WHERE id IN ({placeholders})
              AND status IN ('running', 'queued')
              AND julianday(updated_at) < julianday('now', ?)
            """,
            (now, *ids, cutoff),
        ).rowcount
        ai_runs_closed = conn.execute(
            f"""
            UPDATE ai_runs
            SET status = 'failed',
                error_message = 'stale: simulation job reaped after process restart',
                finished_at = ?
            WHERE subject_kind = 'simulation_job'
              AND subject_id IN ({placeholders})
              AND status IN ('running', 'queued')
            """,
            (now, *ids),
        ).rowcount
    if reaped:
        LOGGER.warning(
            "simulation stale sweep: %s job(s) -> failed, %s ai_run(s) closed (ttl=%smin)",
            reaped,
            ai_runs_closed,
            ttl_minutes,
        )
    return {"reaped": reaped, "ai_runs_closed": ai_runs_closed}
