"""Helper module for cross-process race tests (Wave 8 K-1).

Worker functions live in this module rather than inline in the test file so
``multiprocessing.get_context("spawn")`` children can import them cleanly
without pulling in the entire pytest fixture machinery.

Currently exercises ``backend.services.promotion_rollback_applier`` under
real two-process contention to verify ``fcntl.flock(LOCK_EX | LOCK_NB)``
defends against double-cron / double-launchd. Pre-W8 the existing K-1 test
(``test_concurrent_apply_in_progress_yields_clean_noop``) only simulated the
contention within a single process — a refactor that switched ``fcntl.flock``
(process-scoped on POSIX) to e.g. an FD-scoped primitive could pass the
same-process test while breaking real cross-process semantics. This module
closes that gap.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any


def _race_apply_worker(
    db_path: str,
    manifest_path: str,
    request_id: str,
    start_barrier_path: str,
    result_queue: Any,
) -> None:
    """Top-level worker (pickleable for spawn context).

    Synchronizes start with a sibling worker by polling for a barrier file
    written by the parent. Once the barrier exists both workers race into
    ``apply_rollback_decision``. The first one to acquire ``fcntl.flock``
    on the sentinel lock file wins; the other should bounce off with
    ``BlockingIOError`` and return ``concurrent_apply_in_progress``.

    Results are returned via a ``multiprocessing.Queue`` (passed as
    ``result_queue``) as a flat dict so the parent can correlate without
    pickling the full applier response.
    """
    # 1. Repoint DB_PATH in the child (each spawn child has its own process
    #    state — no monkeypatch carries over from pytest).
    import backend.db as _db

    _db.DB_PATH = Path(db_path)

    # 2. Redirect the paused-state sidecar so the autouse fixture's tmp
    #    redirection survives into the child process. We point both workers
    #    at the same per-test tmp path (parent passes it via env if needed),
    #    but for the rollback race this side-channel isn't touched so a
    #    process-local placeholder under ``/tmp`` is fine.
    import backend.services.promotion_slo_monitor as _psm

    _psm._DEFAULT_PAUSED_STATE_FILE = Path(db_path).parent / "_paused.json"

    # 3. Wait for the parent's go-signal (barrier file) so both workers
    #    contend simultaneously rather than racing serially against startup
    #    skew. Bounded wait so a stuck test fails fast rather than hanging CI.
    barrier = Path(start_barrier_path)
    deadline = time.monotonic() + 10.0
    while not barrier.exists():
        if time.monotonic() > deadline:
            result_queue.put(
                {
                    "request_id": request_id,
                    "error": "barrier_timeout",
                    "applied": False,
                    "reason": None,
                }
            )
            return
        time.sleep(0.001)

    # 4. Import + call the applier. We do this AFTER barrier sync so any
    #    one-time import side effects don't skew the race window.
    from backend.services.promotion_rollback_applier import apply_rollback_decision

    decision: dict[str, Any] = {
        "recommendation": "rollback",
        "within_slo": False,
        "violations": [
            {
                "dimension": "comfyui_failure_rate",
                "actual": 0.20,
                "threshold": 0.05,
                "comparator": "<=",
            }
        ],
        "evidence": {
            "comfyui_failure": {"rate": 0.20, "terminal_total": 80},
            "minimum_sample_size": 30,
            "cutoff_iso": "2026-05-28T00:00:00+00:00",
        },
        "window_hours": 48,
        "sample_size": 100,
        "generated_at": "2026-05-28T00:00:00+00:00",
    }

    try:
        result = apply_rollback_decision(
            decision,
            dry_run=False,
            manifest_path=Path(manifest_path),
            request_id=request_id,
        )
        result_queue.put(
            {
                "request_id": request_id,
                "applied": bool(result.get("applied")),
                "reason": result.get("reason"),
                "audit_log_id": result.get("audit_log_id"),
                "error": result.get("error"),
                "pid": os.getpid(),
            }
        )
    except Exception as exc:  # surface any unexpected exception to the parent
        result_queue.put(
            {
                "request_id": request_id,
                "error": f"{type(exc).__name__}: {exc}",
                "applied": False,
                "reason": None,
            }
        )
