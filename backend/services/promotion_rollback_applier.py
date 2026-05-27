"""Promotion auto-rollback applier — plan §P2.3/§P2.4 收尾.

Consumes :func:`backend.services.promotion_slo_monitor.evaluate_window` output
and **applies** the rollback decision atomically when ``recommendation ==
'rollback'``:

1. Load the current promotion manifest via
   :mod:`backend.services.promotion_manifest_loader` (read-only import).
2. **Acquire** an exclusive non-blocking ``fcntl.flock`` on a sentinel lock
   file (``.promotion_rollback.lock``) in the manifest directory — defends
   against double-cron / double-launchd races (Wave 3 P0.5 H-4).
3. Insert an ``ops_audit_log`` row with ``outcome='rollback_started'``
   **before** mutating the manifest (audit-first invariant; Wave 3 P0.5 H-3).
4. Snapshot the manifest's active ``bindings`` into a fresh
   ``rollback_forensics.failed_snapshot`` block (Wave 3 P0.5 H-2 — semantic
   rename, since ``rollback_baseline.bindings`` is reserved for the
   revert-target / prior-approved-healthy version).
5. Set ``promotion_state = 'rolled_back'``.
6. Write the new manifest via **tmp file + os.replace** (POSIX atomic rename).
7. Insert a second ``ops_audit_log`` row with ``outcome='rollback_completed'``
   (sharing ``request_id`` as correlation id with the ``rollback_started``
   row). On manifest write failure we instead insert a
   ``rollback_aborted`` correlation row and raise.

Recommendation handling:
- ``rollback``           → real apply (or dry-run plan)
- ``continue``           → no-op, reason=``no_rollback_needed``
- ``monitoring_paused``  → no-op (P2.4 methodology; Agent B emits this)
- ``insufficient_data``  → no-op, warning attached
- already ``rolled_back``→ no-op, reason=``already_rolled_back``
- missing baseline       → **fail-closed** reject, reason=``missing_baseline_bindings``
- concurrent lock holder → **fail-closed** noop, reason=``concurrent_apply_in_progress``

The applier never raises on a happy-path decision branch; it returns a result
dict so the CLI / cron caller can branch on ``applied`` / ``reason``.
Unexpected I/O or DB errors surface via raises (caller turns them into exit
code 1).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import promotion_manifest_loader as _manifest
from .promotion_slo_monitor import (
    RECOMMENDATION_CONTINUE as REC_CONTINUE,
    RECOMMENDATION_INSUFFICIENT_DATA as REC_INSUFFICIENT,
    RECOMMENDATION_MONITORING_PAUSED as REC_MONITORING_PAUSED,
    RECOMMENDATION_ROLLBACK as REC_ROLLBACK,
    RECOMMENDATION_STOP_LOSS_HALT as REC_STOP_LOSS_HALT,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (no magic strings — every state name / field key resolved here)
# ---------------------------------------------------------------------------

ROLLED_BACK_STATE = "rolled_back"
REVIEWER = "system/slo_monitor"
ENDPOINT = "promotion_rollback_applier.apply"

# Audit-first transaction (Wave 3 P0.5 H-3) — three structured outcome tokens
# share a single ``request_id`` as correlation id (existing ops_audit_log
# schema supports this without DDL change; request_id is INDEX'd).
AUDIT_OUTCOME_STARTED = "rollback_started"
AUDIT_OUTCOME_COMPLETED = "rollback_completed"
AUDIT_OUTCOME_ABORTED = "rollback_aborted"
# K-5: stop-loss alert is an audit-only event (no manifest mutation). Distinct
# token so on-call can filter it apart from real rollbacks.
AUDIT_OUTCOME_STOP_LOSS_HALT_ALERT = "stop_loss_halt_alert"

# Manifest field-name constants (Wave 3 P0.5 H-2 — semantic split between
# `rollback_baseline` = prior-approved-healthy version (revert target,
# schema preserved & untouched here) and `rollback_forensics.failed_snapshot`
# = forensic capture of the bindings that triggered this rollback).
ROLLBACK_BASELINE_KEY = "rollback_baseline"
ROLLBACK_FORENSICS_KEY = "rollback_forensics"
FAILED_SNAPSHOT_KEY = "failed_snapshot"
FS_BINDINGS_KEY = "bindings"
FS_FROM_STATE_KEY = "from_state"
FS_ROLLED_BACK_AT_KEY = "rolled_back_at"
FS_MANIFEST_REF_KEY = "manifest_ref"
FS_CAPTURED_AT_KEY = "captured_at"

# Sentinel lock filename (lives in manifest_path.parent so the lock and the
# file it protects share the same FS / directory — protects POSIX atomic
# rename guarantee + survives `os.replace` since the lock targets a *separate*
# file the applier owns).
ROLLBACK_LOCK_FILENAME = ".promotion_rollback.lock"

NOOP_RECOMMENDATIONS: frozenset[str] = frozenset(
    {REC_CONTINUE, REC_INSUFFICIENT, REC_MONITORING_PAUSED}
)

# Reason tokens (machine-readable; surface in result dict).
REASON_NO_ROLLBACK_NEEDED = "no_rollback_needed"
REASON_INSUFFICIENT_DATA = "insufficient_data"
REASON_MONITORING_PAUSED = "monitoring_paused"
REASON_ALREADY_ROLLED_BACK = "already_rolled_back"
REASON_MISSING_BASELINE_BINDINGS = "missing_baseline_bindings"
REASON_NO_MANIFEST = "no_manifest"
REASON_APPLIED = "rollback_applied"
REASON_DRY_RUN = "dry_run"
REASON_CONCURRENT_APPLY = "concurrent_apply_in_progress"
# K-5: stop-loss alert — audit row written, manifest untouched, operator
# review required.
REASON_STOP_LOSS_HALT_ALERT = "stop_loss_halt_alert"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_bindings_populated(bindings: Mapping[str, Any] | None) -> bool:
    """Return True iff the bindings map has at least one non-empty, non-null
    value. Empty dict, all-None, or None all count as *not* populated.
    """
    if not isinstance(bindings, Mapping):
        return False
    if not bindings:
        return False
    return any(v not in (None, "", {}, []) for v in bindings.values())


def _summarize_violations(violations: list[dict[str, Any]] | None) -> str:
    """Build a 1-line human-readable reason from the violations list. Used
    as ``ops_audit_log.reason`` so on-call can scan the audit feed."""
    if not violations:
        return "rollback recommended (no violations payload)"
    parts: list[str] = []
    for v in violations:
        dim = str(v.get("dimension") or "unknown")
        actual = v.get("actual")
        threshold = v.get("threshold")
        parts.append(f"{dim}={actual} > {threshold}")
    return "; ".join(parts)


def _resolve_decision(decision: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Accept either a plain dict (CLI / JSON ingest) or an ``SLOReport``
    dataclass instance (in-process call). Returns a plain dict."""
    if hasattr(decision, "to_dict") and callable(decision.to_dict):
        out = decision.to_dict()
        if isinstance(out, dict):
            return out
    if isinstance(decision, Mapping):
        return dict(decision)
    raise TypeError(
        f"decision must be Mapping or SLOReport-like; got {type(decision).__name__}"
    )


def _atomic_write_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    """Write JSON atomically via tmp file + ``os.replace`` (POSIX atomic).

    If the destination dir is unwritable we raise — caller treats as exit
    code 1. We never leave a half-written manifest behind (the tmp lives in
    the same dir to guarantee ``os.replace`` stays atomic on the same FS).
    """
    parent = manifest_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_path = parent / f".{manifest_path.name}.tmp"
    try:
        # JSON dump first, then atomic rename. Trailing newline matches
        # compute_manifest_hashes.write_manifest_bindings convention.
        tmp_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, manifest_path)
    except Exception:
        # Best-effort cleanup of the tmp; never mask the original error.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _insert_audit_row(
    *,
    conn: sqlite3.Connection,
    request_id: str,
    reason: str,
    payload: dict[str, Any],
    response: dict[str, Any],
    outcome: str,
    http_status: int,
) -> int:
    """Insert one row into ``ops_audit_log``. Mirrors the schema established
    by ``backend.routes.render._write_ops_audit_log`` (P1.1 deliverable).

    We do NOT cross-import the route helper to keep the applier importable
    without FastAPI / Pydantic side effects. Returns the row's autoincrement
    ``id``.
    """
    cur = conn.execute(
        """
        INSERT INTO ops_audit_log
          (request_id, endpoint, reviewer, reason,
           payload_json, response_json, outcome, http_status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            ENDPOINT,
            REVIEWER,
            reason,
            json.dumps(payload, ensure_ascii=False, default=str),
            json.dumps(response, ensure_ascii=False, default=str),
            outcome,
            int(http_status),
            _utc_now_iso(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _result(
    *,
    applied: bool,
    reason: str,
    recommendation: str,
    dry_run: bool = False,
    would_apply: bool | None = None,
    plan: dict[str, Any] | None = None,
    warning: str | None = None,
    error: str | None = None,
    audit_log_id: int | None = None,
    audit_log_ids: dict[str, int] | None = None,
    request_id: str | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Uniform result dict (shape contract for callers).

    Wave 3 P0.5 H-3: real apply now produces a *pair* of audit rows
    (``rollback_started`` + ``rollback_completed``), so we expose both via
    ``audit_log_ids``. ``audit_log_id`` is preserved as a back-compat alias
    pointing at the **completed** row id (the public success record).
    """
    out: dict[str, Any] = {
        "applied": applied,
        "reason": reason,
        "recommendation": recommendation,
        "dry_run": dry_run,
    }
    if would_apply is not None:
        out["would_apply"] = would_apply
    if plan is not None:
        out["plan"] = plan
    if warning is not None:
        out["warning"] = warning
    if error is not None:
        out["error"] = error
    if audit_log_id is not None:
        out["audit_log_id"] = audit_log_id
    if audit_log_ids is not None:
        out["audit_log_ids"] = audit_log_ids
    if request_id is not None:
        out["request_id"] = request_id
    if manifest_path is not None:
        out["manifest_path"] = str(manifest_path)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_rollback_decision(
    decision: Mapping[str, Any] | Any,
    *,
    dry_run: bool = False,
    manifest_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Apply the SLO monitor's recommendation to the promotion manifest.

    :param decision: ``promotion_slo_monitor.evaluate_window()`` output (a
        ``SLOReport`` dataclass or its ``to_dict()`` form).
    :param dry_run: When True, compute the apply plan without writing the
        manifest or audit log.
    :param manifest_path: Override manifest location (defaults to
        :data:`promotion_manifest_loader.DEFAULT_MANIFEST_PATH`).
    :param conn: Optional SQLite connection for audit log writes. If None
        we lazily open one via ``backend.db.get_conn()`` (and close it).
    :param request_id: Override audit ``request_id`` (auto-generated UTC
        timestamp + recommendation when omitted). Used as correlation id
        across the two-INSERT audit-first transaction.
    :returns: Result dict (see ``_result``).
    """
    dec = _resolve_decision(decision)
    rec = str(dec.get("recommendation") or "")
    target_path = Path(manifest_path) if manifest_path else _manifest.DEFAULT_MANIFEST_PATH
    req_id = request_id or f"slo-rollback-{_utc_now_iso()}"

    # 1) Cheap no-op branches first (don't even open DB / load manifest).
    if rec == REC_CONTINUE:
        return _result(
            applied=False,
            reason=REASON_NO_ROLLBACK_NEEDED,
            recommendation=rec,
            dry_run=dry_run,
        )
    if rec == REC_MONITORING_PAUSED:
        return _result(
            applied=False,
            reason=REASON_MONITORING_PAUSED,
            recommendation=rec,
            dry_run=dry_run,
            warning="sample size below minimum; monitoring paused per P2.4",
        )
    if rec == REC_INSUFFICIENT:
        return _result(
            applied=False,
            reason=REASON_INSUFFICIENT_DATA,
            recommendation=rec,
            dry_run=dry_run,
            warning="insufficient sample for SLO eval — preserving current state",
        )
    if rec == REC_STOP_LOSS_HALT:
        # K-5: stop-loss alert — write an audit row tagged
        # ``stop_loss_halt_alert`` but do NOT touch the manifest. The
        # semantics are "灰度流量 > 7d 不足，operator review required"
        # rather than a true SLO violation. Manifest stays as-is so the
        # operator decides next step (rollback / extend window / promote
        # off the stuck bucket). Dry-run respects the audit-write rule too.
        return _handle_stop_loss_halt(
            decision=dec,
            target_path=target_path,
            dry_run=dry_run,
            conn=conn,
            request_id=req_id,
        )
    if rec != REC_ROLLBACK:
        # Unknown recommendation — fail-closed (don't touch the file).
        return _result(
            applied=False,
            reason=REASON_NO_ROLLBACK_NEEDED,
            recommendation=rec,
            dry_run=dry_run,
            warning=f"unrecognized recommendation {rec!r}; treating as no-op",
        )

    # 2) rec=='rollback' — load manifest and inspect current state.
    manifest = _manifest.load_manifest(target_path)
    if manifest is None:
        # No manifest to roll back. Surface explicitly — fail-closed.
        return _result(
            applied=False,
            reason=REASON_NO_MANIFEST,
            recommendation=rec,
            dry_run=dry_run,
            error=f"manifest not found at {target_path}",
            manifest_path=target_path,
        )

    current_state = manifest.get("promotion_state")
    if current_state == ROLLED_BACK_STATE:
        # Already rolled back — never re-apply (audit feed would lie about
        # who triggered the second rollback).
        return _result(
            applied=False,
            reason=REASON_ALREADY_ROLLED_BACK,
            recommendation=rec,
            dry_run=dry_run,
            manifest_path=target_path,
        )

    active_bindings = manifest.get("bindings")
    if not _is_bindings_populated(
        active_bindings if isinstance(active_bindings, Mapping) else None
    ):
        # Fail-closed: we cannot record a meaningful forensic snapshot from
        # an empty/null bindings block, so we refuse to apply rather than
        # write a half-baked rollback record. Field name preserved for
        # back-compat (legacy reason code) even though Wave 3 H-2 renamed
        # the destination snapshot field.
        return _result(
            applied=False,
            reason=REASON_MISSING_BASELINE_BINDINGS,
            recommendation=rec,
            dry_run=dry_run,
            error=(
                "manifest.bindings is empty or all-null; cannot snapshot "
                "rollback_forensics.failed_snapshot"
            ),
            manifest_path=target_path,
        )

    # 3) Build the new manifest payload — snapshot active bindings into
    #    rollback_forensics.failed_snapshot (NOT rollback_baseline; see H-2).
    rolled_back_at = _utc_now_iso()
    bindings_snapshot = dict(active_bindings)  # shallow copy is sufficient (str values)

    prior_forensics_raw = manifest.get(ROLLBACK_FORENSICS_KEY)
    prior_forensics: dict[str, Any] = (
        dict(prior_forensics_raw) if isinstance(prior_forensics_raw, dict) else {}
    )

    failed_snapshot: dict[str, Any] = {
        FS_BINDINGS_KEY: bindings_snapshot,
        FS_FROM_STATE_KEY: current_state if isinstance(current_state, str) else None,
        FS_ROLLED_BACK_AT_KEY: rolled_back_at,
        FS_MANIFEST_REF_KEY: manifest.get("manifest_ref") or None,
        FS_CAPTURED_AT_KEY: manifest.get("captured_at") or None,
    }
    new_forensics: dict[str, Any] = dict(prior_forensics)
    new_forensics[FAILED_SNAPSHOT_KEY] = failed_snapshot

    new_manifest: dict[str, Any] = dict(manifest)
    new_manifest["promotion_state"] = ROLLED_BACK_STATE
    new_manifest[ROLLBACK_FORENSICS_KEY] = new_forensics
    # NOTE: rollback_baseline left untouched on purpose — it carries the
    # prior-approved-healthy "revert target" (manifest_ref + bindings to
    # restore). The applier never mutates it here; revert tooling owns it.

    plan: dict[str, Any] = {
        "manifest_path": str(target_path),
        "from_state": current_state,
        "to_state": ROLLED_BACK_STATE,
        "rolled_back_at": rolled_back_at,
        "bindings_snapshot": bindings_snapshot,
        "violations_summary": _summarize_violations(dec.get("violations")),
        "sample_size": dec.get("sample_size"),
        "window_hours": dec.get("window_hours"),
    }

    # 4) dry_run short-circuits BEFORE any DB / FS mutation.
    if dry_run:
        return _result(
            applied=False,
            reason=REASON_DRY_RUN,
            recommendation=rec,
            dry_run=True,
            would_apply=True,
            plan=plan,
            manifest_path=target_path,
            request_id=req_id,
        )

    # 5) Real apply — audit-first + lock + atomic manifest write + audit-end.
    return _apply_with_lock_and_audit(
        target_path=target_path,
        new_manifest=new_manifest,
        plan=plan,
        decision=dec,
        rec=rec,
        current_state=current_state,
        bindings_snapshot=bindings_snapshot,
        rolled_back_at=rolled_back_at,
        conn=conn,
        request_id=req_id,
    )


def _apply_with_lock_and_audit(
    *,
    target_path: Path,
    new_manifest: dict[str, Any],
    plan: dict[str, Any],
    decision: dict[str, Any],
    rec: str,
    current_state: Any,
    bindings_snapshot: dict[str, Any],
    rolled_back_at: str,
    conn: sqlite3.Connection | None,
    request_id: str,
) -> dict[str, Any]:
    """Real-apply path: fcntl lock + audit-first + atomic manifest write +
    audit-completed (or audit-aborted on failure).

    Ordering invariant (Wave 3 P0.5 H-3 / H-4):

    1. Acquire ``fcntl.flock(LOCK_EX | LOCK_NB)`` on sentinel lock file.
       Failure → return ``concurrent_apply_in_progress`` (no manifest write,
       no audit row — another cron is mid-apply with its own audit trail).
    2. ``INSERT outcome='rollback_started'`` (audit-first). If THIS insert
       fails we raise without touching the manifest.
    3. Atomic write the new manifest. On failure: ``INSERT
       outcome='rollback_aborted'`` (best-effort, sharing ``request_id``
       correlation id) then raise.
    4. ``INSERT outcome='rollback_completed'`` — the public success record.
    """
    audit_payload: dict[str, Any] = {
        "decision_recommendation": rec,
        "violations": decision.get("violations"),
        "sample_size": decision.get("sample_size"),
        "window_hours": decision.get("window_hours"),
        "from_state": current_state,
        "evidence_excerpt": _evidence_excerpt(decision.get("evidence")),
    }
    started_response: dict[str, Any] = {
        "phase": AUDIT_OUTCOME_STARTED,
        "to_state": ROLLED_BACK_STATE,
        "manifest_path": str(target_path),
        "rolled_back_at": rolled_back_at,
        "bindings_snapshot": bindings_snapshot,
    }
    violation_reason = _summarize_violations(decision.get("violations"))

    # 1) Sentinel lock — defends against double-cron / double-launchd.
    lock_path = target_path.parent / ROLLBACK_LOCK_FILENAME
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(
            f"failed to ensure lock dir at {lock_path.parent}: {exc}"
        ) from exc

    lock_fd: int | None = None
    owns_conn = False
    try:
        try:
            lock_fd = os.open(
                str(lock_path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644
            )
        except OSError as exc:
            raise OSError(
                f"failed to open rollback lock file {lock_path}: {exc}"
            ) from exc

        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another process holds the lock → defer cleanly. We deliberately
            # write NO audit row (the holder's own started/completed/aborted
            # rows will tell the real story; an empty deferral row would
            # only confuse forensics).
            logger.warning(
                "promotion_rollback_applier: lock held by another process at %s; "
                "deferring apply",
                lock_path,
            )
            return _result(
                applied=False,
                reason=REASON_CONCURRENT_APPLY,
                recommendation=rec,
                dry_run=False,
                error=f"another process holds {lock_path}",
                manifest_path=target_path,
                request_id=request_id,
            )

        # 2) DB connection (lazy + cleanup-tracked).
        if conn is None:
            from .. import db as _db

            conn = _db.get_conn()
            owns_conn = True

        # 3) Audit-first INSERT (rollback_started). Failure here is fatal —
        #    we have NOT yet touched the manifest, so a raise is safe.
        try:
            started_id = _insert_audit_row(
                conn=conn,
                request_id=request_id,
                reason=violation_reason,
                payload=audit_payload,
                response=started_response,
                outcome=AUDIT_OUTCOME_STARTED,
                http_status=202,  # 'Accepted' — apply in progress
            )
        except sqlite3.Error as exc:
            logger.exception(
                "promotion_rollback_applier: audit-first INSERT failed; "
                "refusing to touch manifest"
            )
            raise sqlite3.DatabaseError(
                "audit-first rollback_started insert failed; manifest untouched"
            ) from exc

        # 4) Atomic manifest write. Failure → insert aborted row + raise.
        try:
            _atomic_write_manifest(target_path, new_manifest)
        except OSError as exc:
            logger.exception("promotion_rollback_applier: manifest write failed")
            aborted_response = {
                "phase": AUDIT_OUTCOME_ABORTED,
                "manifest_path": str(target_path),
                "error": str(exc),
                "correlation_started_id": started_id,
            }
            # Best-effort aborted-row insert — never mask the original error.
            try:
                _insert_audit_row(
                    conn=conn,
                    request_id=request_id,
                    reason=f"manifest write failed: {exc}",
                    payload=audit_payload,
                    response=aborted_response,
                    outcome=AUDIT_OUTCOME_ABORTED,
                    http_status=500,
                )
            except sqlite3.Error:  # pragma: no cover  (best-effort)
                logger.exception(
                    "promotion_rollback_applier: failed to write aborted audit row"
                )
            raise OSError(
                f"failed to write rollback manifest at {target_path}: {exc}"
            ) from exc

        # 5) Audit-completed INSERT — the public success record.
        completed_response = {
            "phase": AUDIT_OUTCOME_COMPLETED,
            "applied": True,
            "to_state": ROLLED_BACK_STATE,
            "rolled_back_at": rolled_back_at,
            "manifest_path": str(target_path),
            "bindings_snapshot": bindings_snapshot,
            "correlation_started_id": started_id,
        }
        try:
            completed_id = _insert_audit_row(
                conn=conn,
                request_id=request_id,
                reason=violation_reason,
                payload=audit_payload,
                response=completed_response,
                outcome=AUDIT_OUTCOME_COMPLETED,
                http_status=200,
            )
        except sqlite3.Error as exc:
            # Manifest was successfully rewritten, but the public success
            # audit row failed to land. We DO raise — operators must
            # investigate the orphan started row, and the rollback is
            # already on-disk so promotion_state=rolled_back is canonical.
            logger.exception(
                "promotion_rollback_applier: rollback_completed audit insert failed "
                "after manifest write; manifest on disk reflects rolled_back"
            )
            raise sqlite3.DatabaseError(
                "rollback_completed audit insert failed; manifest state "
                "rolled_back is canonical, started_id={}".format(started_id)
            ) from exc

        return _result(
            applied=True,
            reason=REASON_APPLIED,
            recommendation=rec,
            dry_run=False,
            plan=plan,
            audit_log_id=completed_id,  # back-compat alias
            audit_log_ids={
                AUDIT_OUTCOME_STARTED: started_id,
                AUDIT_OUTCOME_COMPLETED: completed_id,
            },
            request_id=request_id,
            manifest_path=target_path,
        )
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover
                logger.warning("flock LOCK_UN failed (non-fatal)", exc_info=True)
            try:
                os.close(lock_fd)
            except OSError:  # pragma: no cover
                logger.warning("lock fd close failed (non-fatal)", exc_info=True)
        if owns_conn and conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                logger.warning("audit conn close failed (non-fatal)", exc_info=True)


def _handle_stop_loss_halt(
    *,
    decision: dict[str, Any],
    target_path: Path,
    dry_run: bool,
    conn: sqlite3.Connection | None,
    request_id: str,
) -> dict[str, Any]:
    """K-5: stop-loss alert path. Writes an ``ops_audit_log`` row with
    outcome ``stop_loss_halt_alert``, preserves the manifest unchanged.

    Dry-run still writes no audit row (mirrors ``rec=rollback`` dry_run
    convention) — caller gets ``applied=False`` + ``would_apply=True`` so
    they can preview the alert.
    """
    rec = REC_STOP_LOSS_HALT
    violation_reason = _summarize_violations(decision.get("violations"))
    audit_payload: dict[str, Any] = {
        "decision_recommendation": rec,
        "violations": decision.get("violations"),
        "sample_size": decision.get("sample_size"),
        "window_hours": decision.get("window_hours"),
        "evidence_excerpt": _evidence_excerpt(decision.get("evidence")),
        "notes": (
            "灰度流量长时间不足（paused window > 7d），无法判定 SLO；"
            "operator review required — manifest unchanged"
        ),
    }
    response_payload: dict[str, Any] = {
        "phase": AUDIT_OUTCOME_STOP_LOSS_HALT_ALERT,
        "manifest_path": str(target_path),
        "violations_summary": violation_reason,
    }

    if dry_run:
        return _result(
            applied=False,
            reason=REASON_STOP_LOSS_HALT_ALERT,
            recommendation=rec,
            dry_run=True,
            would_apply=True,
            warning=(
                "stop-loss halt alert (灰度流量长时间不足); operator review "
                "required — manifest will NOT be mutated"
            ),
            plan=audit_payload,
            manifest_path=target_path,
            request_id=request_id,
        )

    owns_conn = False
    if conn is None:
        from .. import db as _db

        conn = _db.get_conn()
        owns_conn = True
    try:
        audit_id = _insert_audit_row(
            conn=conn,
            request_id=request_id,
            reason=violation_reason or "stop_loss_halt alert",
            payload=audit_payload,
            response=response_payload,
            outcome=AUDIT_OUTCOME_STOP_LOSS_HALT_ALERT,
            http_status=200,
        )
    finally:
        if owns_conn:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover
                logger.warning("audit conn close failed (non-fatal)", exc_info=True)

    return _result(
        applied=False,
        reason=REASON_STOP_LOSS_HALT_ALERT,
        recommendation=rec,
        dry_run=False,
        warning=(
            "stop-loss halt alert (灰度流量长时间不足); operator review "
            "required — manifest NOT mutated"
        ),
        audit_log_id=audit_id,
        request_id=request_id,
        manifest_path=target_path,
    )


def _evidence_excerpt(evidence: Any) -> dict[str, Any]:
    """Trim the SLO evidence dict to a compact subset suitable for audit log
    storage (full evidence can be megabytes of status breakdown). We keep
    only the per-dimension rate / counts and thresholds."""
    if not isinstance(evidence, Mapping):
        return {}
    out: dict[str, Any] = {}
    for k in (
        "comfyui_failure",
        "vlm_disagreement",
        "delivery_gate_rejection",
        "pre_render_gate_blocker",
        "minimum_sample_size",
        "cutoff_iso",
    ):
        if k in evidence:
            out[k] = evidence[k]
    return out


__all__ = [
    "apply_rollback_decision",
    "ROLLED_BACK_STATE",
    "REVIEWER",
    "ENDPOINT",
    "AUDIT_OUTCOME_STARTED",
    "AUDIT_OUTCOME_COMPLETED",
    "AUDIT_OUTCOME_ABORTED",
    "AUDIT_OUTCOME_STOP_LOSS_HALT_ALERT",
    "ROLLBACK_BASELINE_KEY",
    "ROLLBACK_FORENSICS_KEY",
    "FAILED_SNAPSHOT_KEY",
    "ROLLBACK_LOCK_FILENAME",
    "REC_ROLLBACK",
    "REC_CONTINUE",
    "REC_INSUFFICIENT",
    "REC_MONITORING_PAUSED",
    "REC_STOP_LOSS_HALT",
    "REASON_NO_ROLLBACK_NEEDED",
    "REASON_INSUFFICIENT_DATA",
    "REASON_MONITORING_PAUSED",
    "REASON_ALREADY_ROLLED_BACK",
    "REASON_MISSING_BASELINE_BINDINGS",
    "REASON_NO_MANIFEST",
    "REASON_APPLIED",
    "REASON_DRY_RUN",
    "REASON_CONCURRENT_APPLY",
    "REASON_STOP_LOSS_HALT_ALERT",
]
