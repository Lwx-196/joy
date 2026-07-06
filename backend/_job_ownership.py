"""Cross-process ownership + heartbeat for 'running' job rows (Tier 3 steady-heartbeat).

Multiple processes can share the SQLite job tables (backend + worktree
backend + batch scripts driving the queues). Before this module, `recover()`
demoted *every* 'running' row to 'queued' at process start — a late-starting
process would steal jobs another process was actively executing (double run
= double provider spend + artifact clobber).

Protocol (zero new columns — reuses recovery_token/recovery_claimed_at,
extending the queued-claim protocol to the running lifecycle):
- claim: the worker CAS-claims queued→running and stamps the row with this
  process's RUN_OWNER_TOKEN + fresh recovery_claimed_at (`claim_running`,
  shared by render_queue/upgrade_queue so the stamp cannot drift).
- heartbeat: a per-process daemon thread refreshes recovery_claimed_at every
  tick for rows this process owns. The executing worker thread itself is
  blocked in subprocess.run() and has no chance to stamp progress.
- reap: recover() (startup) demotes only running rows whose heartbeat went
  stale (> TTL) or that predate the protocol (recovery_claimed_at IS NULL →
  legacy behavior: immediate reclaim). The daemon thread additionally runs
  registered reapers every _REAP_EVERY_TICKS ticks — without this, a dead
  process's running jobs would wait for the *next restart* to be reclaimed
  (recover() only runs once at startup).

env:
- CASE_WORKBENCH_JOB_HEARTBEAT_S  heartbeat tick seconds; <=0 disables the
  daemon thread entirely (running rows then look stale to other processes
  after the TTL — pre-Tier-3 demote behavior). Default 30.
- CASE_WORKBENCH_JOB_STALE_TTL_S  running-row heartbeat TTL seconds.
  Default 120 (= 4 missed beats).
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from . import db

LOGGER = logging.getLogger(__name__)

# One token per process lifetime: pid alone is not enough (pids recycle).
PROCESS_TOKEN = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
RUN_OWNER_TOKEN = f"run:{PROCESS_TOKEN}"

_DEFAULT_HEARTBEAT_S = 30.0
_DEFAULT_STALE_TTL_S = 120
_REAP_EVERY_TICKS = 2  # reap at ~2x the heartbeat interval (60s at default 30s)

_JOB_TABLES = ("render_jobs", "upgrade_jobs")

_REAPERS: list[Callable[[], Any]] = []
_THREAD_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        LOGGER.warning("%s=%r is not a number, using default %s", name, raw, default)
        return default


def heartbeat_interval_seconds() -> float:
    return _env_float("CASE_WORKBENCH_JOB_HEARTBEAT_S", _DEFAULT_HEARTBEAT_S)


def stale_ttl_seconds() -> int:
    return int(_env_float("CASE_WORKBENCH_JOB_STALE_TTL_S", _DEFAULT_STALE_TTL_S))


def stale_cutoff_modifier() -> str:
    """SQLite date modifier for the staleness cutoff, to be used as
    `julianday(recovery_claimed_at) < julianday('now', ?)`. julianday()
    (not lex TEXT compare) because recovery_claimed_at is Python ISO format
    while SQLite's datetime() emits space-separated format — see the
    recover() comment in render_queue.py."""
    return f"-{stale_ttl_seconds()} seconds"


def claim_running(conn, table: str, job_id: int) -> int:
    """CAS queued→running with ownership stamp. Returns rowcount (0 = lost race
    against cancel()/another worker — caller must give up the job)."""
    if table not in _JOB_TABLES:
        raise ValueError(f"unknown job table: {table}")
    now = _now_iso()
    return conn.execute(
        f"""
        UPDATE {table}
        SET status = 'running',
            started_at = ?,
            recovery_token = ?,
            recovery_claimed_at = ?
        WHERE id = ? AND status = 'queued'
        """,
        (now, RUN_OWNER_TOKEN, now, job_id),
    ).rowcount


def heartbeat_once() -> int:
    """Refresh recovery_claimed_at for running rows this process owns.
    Returns rows touched. Cheap when idle: an autocommit probe skips the
    write txn when this process owns nothing."""
    with db.connect() as conn:
        owns = conn.execute(
            """
            SELECT 1 FROM render_jobs WHERE status = 'running' AND recovery_token = ?
            UNION ALL
            SELECT 1 FROM upgrade_jobs WHERE status = 'running' AND recovery_token = ?
            LIMIT 1
            """,
            (RUN_OWNER_TOKEN, RUN_OWNER_TOKEN),
        ).fetchone()
        if not owns:
            return 0
        touched = 0
        for table in _JOB_TABLES:
            touched += conn.execute(
                f"""
                UPDATE {table}
                SET recovery_claimed_at = ?
                WHERE status = 'running' AND recovery_token = ?
                """,
                (_now_iso(), RUN_OWNER_TOKEN),
            ).rowcount
    return touched


def register_reaper(fn: Callable[[], Any]) -> None:
    """Register a periodic stale-job reaper (idempotent per function)."""
    with _THREAD_LOCK:
        if fn not in _REAPERS:
            _REAPERS.append(fn)


def run_reapers_once() -> None:
    for fn in list(_REAPERS):
        try:
            fn()
        except Exception:  # noqa: BLE001 — one broken reaper must not kill the tick
            LOGGER.warning("job reaper %s failed", getattr(fn, "__qualname__", fn), exc_info=True)


def _heartbeat_loop(interval: float) -> None:
    tick = 0
    while True:
        time.sleep(interval)
        tick += 1
        try:
            heartbeat_once()
        except Exception:  # noqa: BLE001 — heartbeat must survive transient DB errors
            LOGGER.warning("job heartbeat tick failed", exc_info=True)
        if tick % _REAP_EVERY_TICKS == 0:
            run_reapers_once()


def ensure_heartbeat_thread() -> bool:
    """Start the per-process heartbeat/reaper daemon (idempotent). Called from
    main.py startup and from the queue claim paths (so batch scripts driving
    the queues get heartbeats too). Returns False when disabled via
    CASE_WORKBENCH_JOB_HEARTBEAT_S<=0 — tests do this and drive
    heartbeat_once()/reapers directly for determinism."""
    global _THREAD
    interval = heartbeat_interval_seconds()
    if interval <= 0:
        return False
    with _THREAD_LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return True
        _THREAD = threading.Thread(
            target=_heartbeat_loop, args=(interval,), name="job-heartbeat", daemon=True
        )
        _THREAD.start()
    return True
