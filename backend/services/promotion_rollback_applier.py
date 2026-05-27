"""Promotion auto-rollback applier — plan §P2.3/§P2.4 收尾.

Consumes :func:`backend.services.promotion_slo_monitor.evaluate_window` output
and **applies** the rollback decision atomically when ``recommendation ==
'rollback'``:

1. Load the current promotion manifest via
   :mod:`backend.services.promotion_manifest_loader` (read-only import).
2. Snapshot the manifest's active ``bindings`` into
   ``rollback_baseline.bindings`` (with ``rolled_back_at`` UTC timestamp) so the
   prior hash set is recorded for forensics / future revert.
3. Set ``promotion_state = 'rolled_back'``.
4. Write the new manifest via **tmp file + os.replace** (POSIX atomic rename).
5. Persist an ``ops_audit_log`` row (reviewer=``system/slo_monitor``,
   reason = violations summary, payload/response = decision evidence).

Recommendation handling:
- ``rollback``           → real apply (or dry-run plan)
- ``continue``           → no-op, reason=``no_rollback_needed``
- ``monitoring_paused``  → no-op (P2.4 methodology; Agent B may emit this)
- ``insufficient_data``  → no-op, warning attached
- already ``rolled_back``→ no-op, reason=``already_rolled_back``
- missing baseline       → **fail-closed** reject, reason=``missing_baseline_bindings``

The applier never raises on a happy-path decision branch; it returns a result
dict so the CLI / cron caller can branch on ``applied`` / ``reason``.
Unexpected I/O or DB errors surface via raises (caller turns them into exit
code 1).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import promotion_manifest_loader as _manifest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (no magic strings — every state name resolved here)
# ---------------------------------------------------------------------------

ROLLED_BACK_STATE = "rolled_back"
REVIEWER = "system/slo_monitor"
ENDPOINT = "promotion_rollback_applier.apply"
AUDIT_OUTCOME_APPLIED = "ok"
AUDIT_OUTCOME_NOOP = "dry_run"
AUDIT_OUTCOME_REJECTED = "error"

# Recommendation tokens emitted by promotion_slo_monitor.evaluate_window().
# Mirrors the SLOReport.recommendation literal set; we duplicate locally so
# this module has zero runtime coupling to the monitor (read-only import is
# only for static / IDE navigation).
REC_ROLLBACK = "rollback"
REC_CONTINUE = "continue"
REC_INSUFFICIENT = "insufficient_data"
REC_MONITORING_PAUSED = "monitoring_paused"

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


def _write_audit_log(
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
    without FastAPI / Pydantic side effects.
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
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Uniform result dict (shape contract for callers)."""
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
        we lazily open one via ``backend.db.connect()`` (and close it).
    :param request_id: Override audit ``request_id`` (auto-generated UTC
        timestamp + recommendation when omitted).
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
    if not _is_bindings_populated(active_bindings if isinstance(active_bindings, Mapping) else None):
        # Fail-closed: we cannot record a meaningful baseline snapshot from
        # an empty/null bindings block, so we refuse to apply rather than
        # write a half-baked rollback record.
        return _result(
            applied=False,
            reason=REASON_MISSING_BASELINE_BINDINGS,
            recommendation=rec,
            dry_run=dry_run,
            error="manifest.bindings is empty or all-null; cannot snapshot baseline",
            manifest_path=target_path,
        )

    # 3) Build the new manifest payload (snapshot active → rollback_baseline).
    rolled_back_at = _utc_now_iso()
    bindings_snapshot = dict(active_bindings)  # shallow copy is sufficient (str values)
    prior_baseline = manifest.get("rollback_baseline") if isinstance(
        manifest.get("rollback_baseline"), dict
    ) else {}
    new_baseline: dict[str, Any] = dict(prior_baseline)
    new_baseline["bindings"] = bindings_snapshot
    new_baseline["rolled_back_at"] = rolled_back_at
    # Preserve / set ``rolled_back_from_state`` so forensics can see what
    # state we left (p10/p25/p50/p100/shadow).
    new_baseline["rolled_back_from_state"] = current_state if isinstance(current_state, str) else None
    # Preserve manifest_ref / captured_at if previously set; otherwise leave
    # nulls so an operator can backfill from git.
    new_baseline.setdefault("manifest_ref", None)
    new_baseline.setdefault("captured_at", prior_baseline.get("captured_at"))

    new_manifest: dict[str, Any] = dict(manifest)
    new_manifest["promotion_state"] = ROLLED_BACK_STATE
    new_manifest["rollback_baseline"] = new_baseline

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
        )

    # 5) Real apply: write manifest atomically THEN write audit log. We
    #    deliberately write the manifest first — the audit log records the
    #    completed action, and a DB failure should not leave the manifest
    #    unchanged after a real production rollback (the on-disk state is
    #    the source of truth for runtime promotion decisions).
    try:
        _atomic_write_manifest(target_path, new_manifest)
    except OSError as exc:
        logger.exception("promotion_rollback_applier: manifest write failed")
        raise OSError(
            f"failed to write rollback manifest at {target_path}: {exc}"
        ) from exc

    audit_payload = {
        "decision_recommendation": rec,
        "violations": dec.get("violations"),
        "sample_size": dec.get("sample_size"),
        "window_hours": dec.get("window_hours"),
        "from_state": current_state,
        "evidence_excerpt": _evidence_excerpt(dec.get("evidence")),
    }
    audit_response = {
        "applied": True,
        "to_state": ROLLED_BACK_STATE,
        "rolled_back_at": rolled_back_at,
        "manifest_path": str(target_path),
        "bindings_snapshot": bindings_snapshot,
    }

    owns_conn = False
    if conn is None:
        # Lazy import to keep the service importable in pure-unit contexts.
        # We use get_conn() (not the context-manager connect()) because we
        # need a long-lived handle whose lifetime we control across the
        # commit boundary inside _write_audit_log.
        from .. import db as _db

        conn = _db.get_conn()
        owns_conn = True

    try:
        audit_id = _write_audit_log(
            conn=conn,
            request_id=req_id,
            reason=_summarize_violations(dec.get("violations")),
            payload=audit_payload,
            response=audit_response,
            outcome=AUDIT_OUTCOME_APPLIED,
            http_status=200,
        )
    finally:
        if owns_conn:
            # commit was already performed inside _write_audit_log; close.
            try:
                conn.close()
            except sqlite3.Error:
                logger.warning("audit conn close failed (non-fatal)", exc_info=True)

    return _result(
        applied=True,
        reason=REASON_APPLIED,
        recommendation=rec,
        dry_run=False,
        plan=plan,
        audit_log_id=audit_id,
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
    "REC_ROLLBACK",
    "REC_CONTINUE",
    "REC_INSUFFICIENT",
    "REC_MONITORING_PAUSED",
    "REASON_NO_ROLLBACK_NEEDED",
    "REASON_INSUFFICIENT_DATA",
    "REASON_MONITORING_PAUSED",
    "REASON_ALREADY_ROLLED_BACK",
    "REASON_MISSING_BASELINE_BINDINGS",
    "REASON_NO_MANIFEST",
    "REASON_APPLIED",
    "REASON_DRY_RUN",
]
