"""Tier 3 steady-heartbeat: running-row ownership + heartbeat + reap.

recover() used to demote EVERY 'running' row at process start; with multiple
processes sharing the DB (backend + worktree backend + batch scripts) a
late-starting process stole jobs another process was actively executing
(double run = double provider spend + artifact clobber). These tests pin the
new protocol:
- claim stamps RUN_OWNER_TOKEN + fresh recovery_claimed_at
- heartbeat_once() refreshes only rows this process owns
- recover()/_reap_stale_running() skip fresh-heartbeat rows, reclaim stale
  ones, and keep legacy behavior (immediate reclaim) for NULL claimed_at rows
  (that path is pinned by the pre-existing tests in test_queue_recovery.py)
- terminal paths clear the owner token (no more token residue on done rows)

Staleness seeding uses Python ISO format timestamps (production `_now_iso()`
style) — NOT SQLite `datetime()` — so the julianday() compare path production
hits is exercised. See the bug_001 note in test_queue_recovery.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend import _job_ownership


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_seconds_ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _insert_render_row(
    conn,
    case_id: int,
    status: str,
    recovery_token: str | None = None,
    recovery_claimed_at: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO render_jobs
            (case_id, brand, template, status, enqueued_at, semantic_judge,
             recovery_token, recovery_claimed_at)
        VALUES (?, 'fumei', 'tri-compare', ?, ?, 'off', ?, ?)
        """,
        (case_id, status, _now_iso(), recovery_token, recovery_claimed_at),
    )
    return cur.lastrowid


def _insert_upgrade_row(
    conn,
    case_id: int,
    status: str,
    recovery_token: str | None = None,
    recovery_claimed_at: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO upgrade_jobs
            (case_id, brand, status, enqueued_at, recovery_token, recovery_claimed_at)
        VALUES (?, 'fumei', ?, ?, ?, ?)
        """,
        (case_id, status, _now_iso(), recovery_token, recovery_claimed_at),
    )
    return cur.lastrowid


def _render_row(conn, job_id: int):
    return conn.execute(
        "SELECT status, recovery_token, recovery_claimed_at FROM render_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()


def _upgrade_row(conn, job_id: int):
    return conn.execute(
        "SELECT status, recovery_token, recovery_claimed_at FROM upgrade_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()


# ----------------------------------------------------------------------
# claim_running
# ----------------------------------------------------------------------


def test_claim_running_stamps_owner_token(temp_db, seed_case):
    from backend import db

    case_id = seed_case()
    with db.connect() as conn:
        render_id = _insert_render_row(conn, case_id, "queued")
        upgrade_id = _insert_upgrade_row(conn, case_id, "queued")

    with db.connect() as conn:
        assert _job_ownership.claim_running(conn, "render_jobs", render_id) == 1
        assert _job_ownership.claim_running(conn, "upgrade_jobs", upgrade_id) == 1

    with db.connect() as conn:
        for row in (_render_row(conn, render_id), _upgrade_row(conn, upgrade_id)):
            assert row["status"] == "running"
            assert row["recovery_token"] == _job_ownership.RUN_OWNER_TOKEN
            assert row["recovery_claimed_at"] is not None


def test_claim_running_lost_race_returns_zero(temp_db, seed_case):
    """CAS: a row already claimed (or cancelled) must not be claimed again."""
    from backend import db

    case_id = seed_case()
    with db.connect() as conn:
        job_id = _insert_render_row(conn, case_id, "running", recovery_token="run:other:aa")

    with db.connect() as conn:
        assert _job_ownership.claim_running(conn, "render_jobs", job_id) == 0
        row = _render_row(conn, job_id)
    assert row["recovery_token"] == "run:other:aa", "losing claim must not overwrite owner"


# ----------------------------------------------------------------------
# heartbeat_once
# ----------------------------------------------------------------------


def test_heartbeat_refreshes_only_own_running_rows(temp_db, seed_case):
    from backend import db

    stale = _iso_seconds_ago(300)
    case_id = seed_case()
    with db.connect() as conn:
        own_render = _insert_render_row(
            conn, case_id, "running",
            recovery_token=_job_ownership.RUN_OWNER_TOKEN, recovery_claimed_at=stale,
        )
        own_upgrade = _insert_upgrade_row(
            conn, case_id, "running",
            recovery_token=_job_ownership.RUN_OWNER_TOKEN, recovery_claimed_at=stale,
        )
        foreign = _insert_render_row(
            conn, case_id, "running",
            recovery_token="run:99999:deadbeef", recovery_claimed_at=stale,
        )
        own_but_queued = _insert_render_row(
            conn, case_id, "queued",
            recovery_token=_job_ownership.RUN_OWNER_TOKEN, recovery_claimed_at=stale,
        )

    assert _job_ownership.heartbeat_once() == 2

    with db.connect() as conn:
        assert _render_row(conn, own_render)["recovery_claimed_at"] > stale
        assert _upgrade_row(conn, own_upgrade)["recovery_claimed_at"] > stale
        assert _render_row(conn, foreign)["recovery_claimed_at"] == stale
        assert _render_row(conn, own_but_queued)["recovery_claimed_at"] == stale


def test_heartbeat_noop_when_owning_nothing(temp_db, seed_case):
    from backend import db

    case_id = seed_case()
    with db.connect() as conn:
        _insert_render_row(
            conn, case_id, "running",
            recovery_token="run:99999:deadbeef", recovery_claimed_at=_iso_seconds_ago(300),
        )

    assert _job_ownership.heartbeat_once() == 0


# ----------------------------------------------------------------------
# recover(): fresh heartbeat is skipped, stale is reclaimed
# (NULL claimed_at legacy demote is pinned by test_queue_recovery.py)
# ----------------------------------------------------------------------


def test_render_recover_skips_running_with_fresh_heartbeat(temp_db, seed_case, no_job_pool):
    """THE core Tier 3 semantic: a running job actively heartbeaten by another
    live process must NOT be stolen at our startup."""
    from backend import db
    from backend.render_queue import RENDER_QUEUE

    case_id = seed_case()
    with db.connect() as conn:
        live_id = _insert_render_row(
            conn, case_id, "running",
            recovery_token="run:99999:deadbeef", recovery_claimed_at=_now_iso(),
        )

    counts = RENDER_QUEUE.recover()
    assert counts["requeued_running"] == 0
    assert counts["resubmitted_queued"] == 0

    with db.connect() as conn:
        row = _render_row(conn, live_id)
    assert row["status"] == "running"
    assert row["recovery_token"] == "run:99999:deadbeef"


def test_render_recover_demotes_stale_heartbeat_running(temp_db, seed_case, no_job_pool):
    from backend import db
    from backend.render_queue import RENDER_QUEUE

    case_id = seed_case()
    with db.connect() as conn:
        dead_id = _insert_render_row(
            conn, case_id, "running",
            recovery_token="run:99999:deadbeef",
            recovery_claimed_at=_iso_seconds_ago(600),  # >> TTL 120s
        )

    counts = RENDER_QUEUE.recover()
    assert counts["requeued_running"] == 1
    assert counts["resubmitted_queued"] == 1  # demoted row re-enters the queued flow

    with db.connect() as conn:
        row = _render_row(conn, dead_id)
    assert row["status"] == "queued"


def test_upgrade_recover_ttl_symmetry(temp_db, seed_case, no_job_pool):
    from backend import db
    from backend.upgrade_queue import UPGRADE_QUEUE

    case_id = seed_case()
    with db.connect() as conn:
        live_id = _insert_upgrade_row(
            conn, case_id, "running",
            recovery_token="run:99999:deadbeef", recovery_claimed_at=_now_iso(),
        )
        dead_id = _insert_upgrade_row(
            conn, case_id, "running",
            recovery_token="run:88888:cafecafe", recovery_claimed_at=_iso_seconds_ago(600),
        )

    counts = UPGRADE_QUEUE.recover()
    assert counts["requeued_running"] == 1

    with db.connect() as conn:
        assert _upgrade_row(conn, live_id)["status"] == "running"
        assert _upgrade_row(conn, dead_id)["status"] == "queued"


# ----------------------------------------------------------------------
# _reap_stale_running: periodic reclaim between restarts
# ----------------------------------------------------------------------


def _capture_submissions(monkeypatch) -> list:
    from backend import _job_pool

    submitted: list = []
    monkeypatch.setattr(
        _job_pool, "submit", lambda fn, *args, **kwargs: submitted.append(args)
    )
    return submitted


def test_render_reap_demotes_stale_and_resubmits(temp_db, seed_case, monkeypatch):
    from backend import db
    from backend.render_queue import RENDER_QUEUE

    submitted = _capture_submissions(monkeypatch)
    case_id = seed_case()
    with db.connect() as conn:
        dead_id = _insert_render_row(
            conn, case_id, "running",
            recovery_token="run:99999:deadbeef", recovery_claimed_at=_iso_seconds_ago(600),
        )
        live_id = _insert_render_row(
            conn, case_id, "running",
            recovery_token=_job_ownership.RUN_OWNER_TOKEN, recovery_claimed_at=_now_iso(),
        )

    counts = RENDER_QUEUE._reap_stale_running()
    assert counts == {"requeued_running": 1}
    assert [args[0] for args in submitted] == [dead_id]

    with db.connect() as conn:
        dead = _render_row(conn, dead_id)
        live = _render_row(conn, live_id)
    assert dead["status"] == "queued"
    # Tagged with a reap token before submit — a crash between commit and
    # submit is picked up by the next recover()'s 5-minute orphan reclaim.
    assert dead["recovery_token"].startswith("render-reap:")
    assert live["status"] == "running"
    assert live["recovery_token"] == _job_ownership.RUN_OWNER_TOKEN


def test_render_reap_noop_when_nothing_stale(temp_db, seed_case, monkeypatch):
    from backend import db
    from backend.render_queue import RENDER_QUEUE

    submitted = _capture_submissions(monkeypatch)
    case_id = seed_case()
    with db.connect() as conn:
        _insert_render_row(
            conn, case_id, "running",
            recovery_token="run:99999:deadbeef", recovery_claimed_at=_now_iso(),
        )

    counts = RENDER_QUEUE._reap_stale_running()
    assert counts == {"requeued_running": 0}
    assert submitted == []


def test_upgrade_reap_symmetry(temp_db, seed_case, monkeypatch):
    from backend import db
    from backend.upgrade_queue import UPGRADE_QUEUE

    submitted = _capture_submissions(monkeypatch)
    case_id = seed_case()
    with db.connect() as conn:
        dead_id = _insert_upgrade_row(
            conn, case_id, "running",
            recovery_token="run:99999:deadbeef", recovery_claimed_at=_iso_seconds_ago(600),
        )

    counts = UPGRADE_QUEUE._reap_stale_running()
    assert counts == {"requeued_running": 1}
    assert [args[0] for args in submitted] == [dead_id]

    with db.connect() as conn:
        assert _upgrade_row(conn, dead_id)["status"] == "queued"


# ----------------------------------------------------------------------
# terminal paths clear the owner token
# ----------------------------------------------------------------------


def test_mark_failed_clears_owner_token(temp_db, seed_case):
    from backend import db
    from backend.render_queue import RENDER_QUEUE
    from backend.upgrade_queue import UPGRADE_QUEUE

    case_id = seed_case()
    with db.connect() as conn:
        render_id = _insert_render_row(
            conn, case_id, "running",
            recovery_token=_job_ownership.RUN_OWNER_TOKEN, recovery_claimed_at=_now_iso(),
        )
        upgrade_id = _insert_upgrade_row(
            conn, case_id, "running",
            recovery_token=_job_ownership.RUN_OWNER_TOKEN, recovery_claimed_at=_now_iso(),
        )

    RENDER_QUEUE._mark_failed(render_id, "boom")
    UPGRADE_QUEUE._mark_failed(upgrade_id, "boom")

    with db.connect() as conn:
        for row in (_render_row(conn, render_id), _upgrade_row(conn, upgrade_id)):
            assert row["status"] == "failed"
            assert row["recovery_token"] is None
            assert row["recovery_claimed_at"] is None


def test_finish_result_clears_owner_token(temp_db, seed_case):
    from backend import db
    from backend.render_queue import RENDER_QUEUE

    case_id = seed_case()
    with db.connect() as conn:
        job_id = _insert_render_row(
            conn, case_id, "running",
            recovery_token=_job_ownership.RUN_OWNER_TOKEN, recovery_claimed_at=_now_iso(),
        )

    RENDER_QUEUE._finish_result(
        job_id,
        case_id=case_id,
        batch_id=None,
        brand="fumei",
        template="tri-compare",
        result={"status": "error", "render_error": "boom"},
    )

    with db.connect() as conn:
        row = _render_row(conn, job_id)
    assert row["status"] != "running"
    assert row["recovery_token"] is None
    assert row["recovery_claimed_at"] is None


# ----------------------------------------------------------------------
# reaper registry resilience
# ----------------------------------------------------------------------


def test_run_reapers_once_survives_broken_reaper(monkeypatch):
    calls: list[str] = []

    def _boom() -> None:
        calls.append("boom")
        raise RuntimeError("reaper exploded")

    def _fine() -> None:
        calls.append("fine")

    monkeypatch.setattr(_job_ownership, "_REAPERS", [_boom, _fine])
    _job_ownership.run_reapers_once()
    assert calls == ["boom", "fine"], "a broken reaper must not kill the tick"


def test_heartbeat_thread_disabled_by_env(monkeypatch):
    """conftest sets CASE_WORKBENCH_JOB_HEARTBEAT_S=0 session-wide; pin that
    the disable switch actually short-circuits before spawning a thread."""
    monkeypatch.setenv("CASE_WORKBENCH_JOB_HEARTBEAT_S", "0")
    assert _job_ownership.ensure_heartbeat_thread() is False
    assert _job_ownership._THREAD is None
