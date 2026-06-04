"""Wave 8 K-1: real two-process race tests for promotion_rollback_applier.

Pre-W8 the K-1 hardening was only tested by
``test_concurrent_apply_in_progress_yields_clean_noop`` — which simulates
the contention by acquiring ``fcntl.flock`` within the SAME process and
calling the applier (POSIX ``fcntl.flock`` is process-scoped, so the
applier's separate FD sees the parent test's lock and bounces).

That same-process test is sufficient for the *behavior under contention*,
but it does NOT verify cross-process semantics. A refactor that swapped
``fcntl.flock`` for an FD-scoped primitive (e.g. ``fcntl.lockf`` per FD on
some platforms, or any in-memory lock) could pass the same-process test
while silently breaking the double-cron / double-launchd defense.

This module closes that gap by spawning **two real subprocesses** via
``multiprocessing.get_context("spawn")`` and racing them into
``apply_rollback_decision`` with the same manifest path. The expected
race semantics:

- Exactly one process applies (writes manifest + ``rollback_started`` +
  ``rollback_completed`` audit rows).
- The other process EITHER:
  - returns ``concurrent_apply_in_progress`` (lost the lock race), OR
  - returns ``already_rolled_back`` (got the lock after the winner
    finished and now sees the manifest already in ``rolled_back`` state).
- Manifest on disk reflects ``rolled_back`` exactly once.
- ``ops_audit_log`` contains exactly one ``rollback_started`` +
  ``rollback_completed`` pair (no double application).

The autouse paused-state sidecar fixture is per-process pytest state and
won't carry over to spawn children, so the worker manually redirects the
sidecar path. The DB is opened by the parent (init_schema) at a fixed
``tmp_path`` location and both children point ``backend.db.DB_PATH`` at
the same file (SQLite's own file locks handle write serialization).
"""

from __future__ import annotations

import json
import multiprocessing
import time
from pathlib import Path
from typing import Any

import pytest

from backend import db
from backend.services.promotion_rollback_applier import (
    AUDIT_OUTCOME_ABORTED,
    AUDIT_OUTCOME_COMPLETED,
    AUDIT_OUTCOME_STARTED,
    REASON_ALREADY_ROLLED_BACK,
    REASON_APPLIED,
    REASON_CONCURRENT_APPLY,
    ROLLED_BACK_STATE,
)

from backend.tests._race_workers import _race_apply_worker

_VALID_BINDINGS = {
    "vlm_calibration_hash": "sha256:aaaa",
    "production_gate_hash": "sha256:bbbb",
    "ab_report_hash": "sha256:cccc",
    "render_quality_baseline_hash": "sha256:dddd",
}


def _make_manifest(*, promotion_state: str = "p50") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "version": "v1.0.0",
        "scope": "production",
        "approver": "alice@example.com",
        "approved_at": "2026-05-01T00:00:00+00:00",
        "expires_at": None,
        "promotion_state": promotion_state,
        "bindings": dict(_VALID_BINDINGS),
    }


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _spawn_race(
    db_path: Path,
    manifest_path: Path,
    barrier_path: Path,
    request_ids: tuple[str, str],
    timeout_seconds: float = 30.0,
) -> list[dict[str, Any]]:
    """Spawn two children, release the barrier, collect both results."""
    ctx = multiprocessing.get_context("spawn")
    result_queue: Any = ctx.Queue()

    procs = [
        ctx.Process(
            target=_race_apply_worker,
            args=(
                str(db_path),
                str(manifest_path),
                rid,
                str(barrier_path),
                result_queue,
            ),
            name=f"race-worker-{rid}",
        )
        for rid in request_ids
    ]
    for p in procs:
        p.start()

    # Brief settle so both children are spinning on the barrier before we
    # release. 50 ms is generous on CI; even a stuck child will time out
    # via the in-worker 10 s deadline.
    time.sleep(0.05)
    barrier_path.write_text("go", encoding="utf-8")

    results: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout_seconds
    for _ in procs:
        remaining = max(0.1, deadline - time.monotonic())
        results.append(result_queue.get(timeout=remaining))
    for p in procs:
        p.join(timeout=5.0)
        if p.is_alive():  # pragma: no cover  (CI watchdog only)
            p.terminate()
            p.join(timeout=2.0)
    return results


@pytest.fixture
def race_db(tmp_path: Path) -> Path:
    """DB initialized in PARENT process at a stable tmp path so both
    spawn children can point their ``backend.db.DB_PATH`` at the same
    file. We do NOT use the ``temp_db`` autouse fixture because its
    ``monkeypatch`` is per-process and wouldn't carry into children.
    """
    db_path = tmp_path / "race-test.db"
    # Repoint the in-parent DB_PATH so init_schema writes to our file.
    original = db.DB_PATH
    db.DB_PATH = db_path
    try:
        db.init_schema()
    finally:
        db.DB_PATH = original
    return db_path


def test_w8_k1_real_2process_race_yields_exactly_one_winner(
    race_db: Path, tmp_path: Path
) -> None:
    """W8 K-1 core: two real subprocesses contending for the same manifest
    must produce exactly one ``applied=True`` outcome. The loser must
    cleanly defer via either ``concurrent_apply_in_progress`` (lost lock
    race) or ``already_rolled_back`` (acquired lock after winner finished
    and saw the manifest already in rolled_back state). No torn manifest,
    no duplicate audit rows."""
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _make_manifest(promotion_state="p100"))
    barrier_path = tmp_path / "race_start"

    results = _spawn_race(
        race_db,
        manifest_path,
        barrier_path,
        request_ids=("w8-race-A", "w8-race-B"),
    )

    assert len(results) == 2, f"expected 2 results, got {len(results)}: {results}"
    # NB: ``apply_rollback_decision`` populates an informational ``error``
    # field on the deferred path (e.g. "another process holds <lock>") —
    # that's not a real failure, just a human-readable explanation. We
    # only require ``error`` to be absent on the WINNER's row (where any
    # error would indicate the applied=True path itself raised). Deferral
    # rows are allowed to carry the informational lock-holder string.
    applied = [r for r in results if r.get("applied")]
    deferred = [r for r in results if not r.get("applied")]
    for r in applied:
        assert r.get("error") in (None, ""), (
            f"WINNER {r.get('request_id')} must have no error; got "
            f"{r.get('error')!r}"
        )

    # Race invariant: exactly one wins.
    assert len(applied) == 1, (
        f"expected exactly 1 winner, got {len(applied)} applied + "
        f"{len(deferred)} deferred. Results: {results}"
    )
    assert applied[0]["reason"] == REASON_APPLIED

    # Loser must defer cleanly with one of the two acceptable reasons.
    loser_reason = deferred[0].get("reason")
    assert loser_reason in {REASON_CONCURRENT_APPLY, REASON_ALREADY_ROLLED_BACK}, (
        f"loser must defer cleanly, got reason={loser_reason!r}; "
        f"full loser result: {deferred[0]}"
    )

    # Manifest reflects rolled_back exactly once (atomic write).
    final_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert final_manifest["promotion_state"] == ROLLED_BACK_STATE, (
        f"manifest must be in rolled_back state; got "
        f"promotion_state={final_manifest.get('promotion_state')!r}"
    )

    # Audit log: exactly one started + one completed pair for the winner;
    # NO audit row for the loser (concurrent_apply path writes no audit;
    # already_rolled_back path also writes no audit since work is already done).
    db.DB_PATH = race_db
    try:
        with db.connect() as conn:
            winner_rid = applied[0]["request_id"]
            loser_rid = deferred[0]["request_id"]

            started = conn.execute(
                "SELECT COUNT(*) AS n FROM ops_audit_log "
                "WHERE request_id = ? AND outcome = ?",
                (winner_rid, AUDIT_OUTCOME_STARTED),
            ).fetchone()["n"]
            completed = conn.execute(
                "SELECT COUNT(*) AS n FROM ops_audit_log "
                "WHERE request_id = ? AND outcome = ?",
                (winner_rid, AUDIT_OUTCOME_COMPLETED),
            ).fetchone()["n"]
            aborted = conn.execute(
                "SELECT COUNT(*) AS n FROM ops_audit_log "
                "WHERE request_id = ? AND outcome = ?",
                (winner_rid, AUDIT_OUTCOME_ABORTED),
            ).fetchone()["n"]
            loser_rows = conn.execute(
                "SELECT COUNT(*) AS n FROM ops_audit_log WHERE request_id = ?",
                (loser_rid,),
            ).fetchone()["n"]

        assert started == 1, f"winner must have exactly 1 rollback_started, got {started}"
        assert completed == 1, (
            f"winner must have exactly 1 rollback_completed, got {completed}"
        )
        assert aborted == 0, f"winner must have 0 rollback_aborted, got {aborted}"
        assert loser_rows == 0, (
            f"loser must write 0 audit rows under its request_id "
            f"(holder owns audit trail); got {loser_rows}"
        )
    finally:
        # Restore parent's DB_PATH for any downstream introspection.
        pass


def test_w8_k1_serial_apply_after_race_winner_returns_already_rolled_back(
    race_db: Path, tmp_path: Path
) -> None:
    """W8 K-1 follow-on: after the race winner releases the lock and
    finishes, a fresh subprocess invocation against the same manifest
    must return ``already_rolled_back`` (idempotent). This guards against
    a regression where the lock would be released but the idempotency
    check (promotion_state == rolled_back short-circuit) gets bypassed.
    """
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _make_manifest(promotion_state="p100"))
    barrier_path = tmp_path / "race_start_round1"

    # Round 1: single solo worker applies cleanly (parent waits for it).
    results = _spawn_race(
        race_db,
        manifest_path,
        barrier_path,
        request_ids=("w8-solo-A", "w8-solo-B"),
    )
    winner = next(r for r in results if r.get("applied"))
    assert winner["reason"] == REASON_APPLIED

    # Round 2: spawn two fresh subprocesses AGAINST the already-rolled-back
    # manifest. Both should see ``promotion_state == rolled_back`` and
    # short-circuit. Neither should rewrite the manifest, write a new
    # audit row, or surface an error.
    pre_manifest_bytes = manifest_path.read_bytes()
    barrier_path_2 = tmp_path / "race_start_round2"

    results_2 = _spawn_race(
        race_db,
        manifest_path,
        barrier_path_2,
        request_ids=("w8-post-rollback-A", "w8-post-rollback-B"),
    )

    assert len(results_2) == 2
    for r in results_2:
        assert r.get("applied") is False, (
            f"post-rollback worker must NOT re-apply; got {r}"
        )
        # No barrier/import error in worker — informational deferral
        # messages are allowed (see comment in primary test above).
        # Either concurrent_apply (one process beat the other to the lock)
        # OR already_rolled_back (both saw rolled_back state). Both are
        # safe outcomes — manifest stays bit-identical.
        assert r["reason"] in {REASON_CONCURRENT_APPLY, REASON_ALREADY_ROLLED_BACK}

    # Manifest bit-identical → no torn / re-written file.
    assert manifest_path.read_bytes() == pre_manifest_bytes, (
        "manifest must be byte-identical after post-rollback race"
    )


def test_w8_k1_race_against_already_rolled_back_manifest_both_short_circuit(
    race_db: Path, tmp_path: Path
) -> None:
    """W8 K-1 idempotency under race: if the manifest is ALREADY in
    ``rolled_back`` state when two workers race, the applier's
    idempotency check should short-circuit at least one of them with
    ``already_rolled_back`` (the lock-winner) while the other gets
    ``concurrent_apply_in_progress`` (lost the lock and never reached
    the idempotency check). Neither rewrites the manifest; no new audit
    rows under the racers' request_ids."""
    manifest_path = tmp_path / "manifest.json"
    # Manifest already in rolled_back state — the rollback target.
    _write_manifest(manifest_path, _make_manifest(promotion_state=ROLLED_BACK_STATE))
    pre_bytes = manifest_path.read_bytes()
    barrier_path = tmp_path / "race_start_already_rb"

    results = _spawn_race(
        race_db,
        manifest_path,
        barrier_path,
        request_ids=("w8-already-rb-A", "w8-already-rb-B"),
    )

    assert len(results) == 2
    for r in results:
        assert r.get("applied") is False, (
            f"already-rolled-back state must produce applied=False; got {r}"
        )
        assert r["reason"] in {REASON_ALREADY_ROLLED_BACK, REASON_CONCURRENT_APPLY}, (
            f"reason must be already_rolled_back or concurrent_apply; got {r}"
        )

    # Manifest bit-identical → idempotency held under contention.
    assert manifest_path.read_bytes() == pre_bytes, (
        "manifest must be byte-identical when no rollback work was done"
    )

    # No audit rows under either racer's request_id (idempotent short-circuit
    # writes nothing; concurrent_apply_in_progress also writes nothing).
    db.DB_PATH = race_db
    with db.connect() as conn:
        for rid in ("w8-already-rb-A", "w8-already-rb-B"):
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM ops_audit_log WHERE request_id = ?",
                (rid,),
            ).fetchone()["n"]
            assert n == 0, (
                f"already-rolled-back path must write 0 audit rows for {rid}; got {n}"
            )
