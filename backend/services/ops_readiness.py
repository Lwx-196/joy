"""C3.0.1 Ops Readiness — promotion status aggregation.

Adds the 11 new fields specified by plan §C3.0.1 to the
`/api/render/ops/vlm-comfyui/status` endpoint. **Backward compatible**: the
existing top-level sections (`vlm` / `comfyui` / `gate` / `days`) are NOT
touched here. The endpoint wires a new top-level key ``promotion`` whose
value is the dict returned by :func:`compute_promotion_status`.

Field source-of-truth (every field traces to one upstream module — no
fabrication, no inference):

  ===================================  ========================================
  Field                                Source
  ===================================  ========================================
  manifest_state                       promotion_manifest_loader.get_promotion_state
  bucket_exposure                      derived from manifest_state (deterministic)
  slo_recommendation                   promotion_slo_monitor.evaluate_window().recommendation
  sample_size                          SLOReport.sample_size
  minimum_sample_size                  SLOReport.evidence['minimum_sample_size']
  violations                           SLOReport.violations  (latest window)
  baseline_freshness                   manifest.bindings + rollback_baseline.captured_at
  comfyui_live_probe                   HTTP GET COMFYUI_BASE_URL/queue (1.5s timeout)
  render_latency_p50_p95               render_jobs.finished_at-started_at last 24h
  silent_fail_count                    render_jobs status=done + output_path NULL (24h)
  rollback_applier_last                ops_audit_log latest AUDIT_OUTCOME_*
  ===================================  ========================================

Failure modes: every field falls back to a structured ``{"error": "..."}``
sentinel rather than raising — the dashboard must keep rendering even when a
single section's source is down. Tests pin each fallback explicitly.

This module is **read-only** w.r.t. manifest / SLO / applier modules; it
never writes to disk. Stream A retains exclusive ownership of every write
path (manifest, applier, slo_monitor sidecar).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error as _urllib_error
from urllib import request as _urllib_request

from . import promotion_manifest_loader as _manifest
from . import promotion_slo_monitor as _slo


__all__ = [
    "BUCKET_EXPOSURE_BY_STATE",
    "compute_promotion_status",
]


# Deterministic % exposure per documented promotion state (plan §P2.4).
# 'rolled_back' is 0 — the rollback applier reverts to shadow semantics
# (see promotion_manifest_loader.should_promote decision matrix). Unknown
# states map to 0 (fail-closed) to avoid over-reporting exposure.
BUCKET_EXPOSURE_BY_STATE: dict[str, int] = {
    "shadow": 0,
    "p10": 10,
    "p25": 25,
    "p50": 50,
    "p100": 100,
    "rolled_back": 0,
}


# Audit outcomes considered part of the rollback applier surface. Mirrors
# promotion_rollback_applier AUDIT_OUTCOME_* tokens; we deliberately inline
# the literal strings to avoid an import cycle and to keep this query stable
# even if the applier module reshuffles its constants.
_ROLLBACK_AUDIT_OUTCOMES: tuple[str, ...] = (
    "rollback_started",
    "rollback_completed",
    "rollback_aborted",
    "stop_loss_halt_alert",
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # fromisoformat handles offset suffix in 3.11+; explicit UTC fallback
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_days(when: datetime | None, *, now: datetime | None = None) -> float | None:
    if when is None:
        return None
    ref = now or datetime.now(timezone.utc)
    delta = ref - when
    return round(delta.total_seconds() / 86400.0, 4)


# ---------------------------------------------------------------------------
# Field 1-2: manifest_state + bucket_exposure
# ---------------------------------------------------------------------------


def _read_manifest(path: Path | None) -> dict[str, Any] | None:
    return _manifest.load_manifest(path)


def _resolve_manifest_state(manifest: dict[str, Any] | None) -> str:
    return _manifest.get_promotion_state(manifest)


def _bucket_exposure(state: str) -> int:
    return BUCKET_EXPOSURE_BY_STATE.get(state, 0)


# ---------------------------------------------------------------------------
# Field 3-6: SLO recommendation + sample_size + violations + baseline freshness
# ---------------------------------------------------------------------------


def _evaluate_slo(
    conn: sqlite3.Connection,
    *,
    window_hours: int,
    promotion_state: str,
) -> dict[str, Any]:
    """Wrap evaluate_window with a graceful failure path so the dashboard
    keeps rendering when (e.g.) thresholds JSON is missing.

    Returns dict with keys: recommendation, sample_size, minimum_sample_size,
    violations, within_slo, notes, error (only when failed).
    """
    try:
        report = _slo.evaluate_window(
            window_hours=window_hours,
            conn=conn,
            promotion_state=promotion_state,
        )
    except Exception as exc:  # pragma: no cover  — defensive surface
        return {
            "recommendation": None,
            "sample_size": 0,
            "minimum_sample_size": _slo.DEFAULT_MINIMUM_SAMPLE_SIZE,
            "violations": [],
            "within_slo": None,
            "notes": "",
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    evidence = report.evidence or {}
    return {
        "recommendation": report.recommendation,
        "sample_size": int(report.sample_size),
        "minimum_sample_size": int(evidence.get("minimum_sample_size") or 0),
        "violations": list(report.violations or []),
        "within_slo": report.within_slo,
        "notes": report.notes or "",
        "generated_at": report.generated_at,
        "window_hours": report.window_hours,
    }


def _baseline_freshness(manifest: dict[str, Any] | None) -> dict[str, Any]:
    """Combine ``bindings`` (current approved hashes) with
    ``rollback_baseline.captured_at`` so operators can see how stale the
    revert target is.
    """
    if manifest is None:
        return {
            "bindings_present": False,
            "rollback_baseline_captured_at": None,
            "rollback_baseline_age_days": None,
        }
    bindings = manifest.get("bindings")
    bindings_present = (
        isinstance(bindings, dict)
        and any(v not in (None, "", {}, []) for v in bindings.values())
    )
    rb = manifest.get("rollback_baseline") if isinstance(manifest, dict) else None
    captured_at_raw = (
        rb.get("captured_at") if isinstance(rb, dict) else None
    )
    captured_at = _parse_iso(captured_at_raw)
    return {
        "bindings_present": bool(bindings_present),
        "approver": manifest.get("approver"),
        "approved_at": manifest.get("approved_at"),
        "expires_at": manifest.get("expires_at"),
        "rollback_baseline_captured_at": captured_at_raw,
        "rollback_baseline_age_days": _age_days(captured_at),
    }


# ---------------------------------------------------------------------------
# Field 7: ComfyUI live probe
# ---------------------------------------------------------------------------


def _probe_comfyui(base_url: str, *, timeout: float = 1.5) -> dict[str, Any]:
    """GET ``{base_url}/queue`` to read live queue state.

    ComfyUI's `/queue` endpoint returns ``{queue_running: [...], queue_pending: [...]}``;
    we don't validate the structure deeply — reachability + depth is enough.
    Any error returns ``{reachable: False, error: "..."}`` so the panel can
    render the issue without breaking the whole status response.
    """
    url = base_url.rstrip("/") + "/queue"
    try:
        with _urllib_request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            status = resp.status
            raw = resp.read(64 * 1024)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        running = (
            len(payload.get("queue_running") or [])
            if isinstance(payload, dict)
            else None
        )
        pending = (
            len(payload.get("queue_pending") or [])
            if isinstance(payload, dict)
            else None
        )
        return {
            "reachable": status == 200,
            "http_status": status,
            "queue_running": running,
            "queue_pending": pending,
            "probed_at": _iso_now(),
            "base_url": base_url,
        }
    except (_urllib_error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {
            "reachable": False,
            "error": f"{exc.__class__.__name__}: {exc}",
            "probed_at": _iso_now(),
            "base_url": base_url,
        }


# ---------------------------------------------------------------------------
# Field 8: render_latency_p50_p95 (per workflow)
# ---------------------------------------------------------------------------


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolation percentile. Empty → 0.0."""
    if not values:
        return 0.0
    if len(values) == 1:
        return round(values[0], 3)
    sorted_v = sorted(values)
    # statistics.quantiles requires n >= 2; map q∈[0,1] to method='inclusive'
    idx = q * (len(sorted_v) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_v) - 1)
    frac = idx - lo
    return round(sorted_v[lo] * (1 - frac) + sorted_v[hi] * frac, 3)


def _render_latency_p50_p95(
    conn: sqlite3.Connection, *, window_hours: int = 24
) -> dict[str, Any]:
    """Group render_jobs by render_mode (proxy for workflow at the
    render_jobs layer; simulation_jobs carries the ComfyUI workflow_name and
    is reported separately).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    try:
        rows = conn.execute(
            """
            SELECT
              COALESCE(render_mode, 'unknown') AS rmode,
              julianday(finished_at) - julianday(started_at) AS jd_diff
            FROM render_jobs
            WHERE status = 'done'
              AND started_at IS NOT NULL
              AND finished_at IS NOT NULL
              AND julianday(finished_at) >= julianday(?)
            """,
            (cutoff,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        # Older test schemas may omit render_mode column; report gracefully.
        return {"error": f"OperationalError: {exc}", "by_render_mode": {}}
    by_mode: dict[str, list[float]] = {}
    for row in rows:
        jd = row["jd_diff"]
        if jd is None or jd < 0:
            continue
        seconds = float(jd) * 86400.0
        by_mode.setdefault(str(row["rmode"]), []).append(seconds)
    return {
        "window_hours": window_hours,
        "by_render_mode": {
            mode: {
                "count": len(values),
                "p50_seconds": _percentile(values, 0.50),
                "p95_seconds": _percentile(values, 0.95),
            }
            for mode, values in by_mode.items()
        },
    }


# ---------------------------------------------------------------------------
# Field 9: silent_fail_count (last 24h)
# ---------------------------------------------------------------------------


def _silent_fail_count(
    conn: sqlite3.Connection, *, window_hours: int = 24
) -> dict[str, Any]:
    """Heuristic: render_jobs status='done' with output_path IS NULL — the
    worker reported success but produced no artifact (the W11 dynamic-timeout
    /resize work explicitly noted this as the canonical silent-fail surface).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    try:
        cnt = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM render_jobs
            WHERE status = 'done'
              AND (output_path IS NULL OR output_path = '')
              AND julianday(finished_at) >= julianday(?)
            """,
            (cutoff,),
        ).fetchone()["n"]
    except sqlite3.OperationalError as exc:
        return {"error": f"OperationalError: {exc}", "count": 0}
    return {"window_hours": window_hours, "count": int(cnt or 0)}


# ---------------------------------------------------------------------------
# Field 10: rollback_applier_last
# ---------------------------------------------------------------------------


def _rollback_applier_last(conn: sqlite3.Connection) -> dict[str, Any]:
    """Latest ops_audit_log row matching the applier's outcome tokens, ranked
    by id DESC (id is INTEGER PRIMARY KEY AUTOINCREMENT — strictly monotonic).
    """
    placeholders = ",".join("?" * len(_ROLLBACK_AUDIT_OUTCOMES))
    try:
        row = conn.execute(
            f"""
            SELECT id, request_id, outcome, http_status, reason, created_at
            FROM ops_audit_log
            WHERE outcome IN ({placeholders})
            ORDER BY id DESC
            LIMIT 1
            """,
            _ROLLBACK_AUDIT_OUTCOMES,
        ).fetchone()
    except sqlite3.OperationalError as exc:
        return {"error": f"OperationalError: {exc}"}
    if row is None:
        return {
            "last_outcome": None,
            "last_run_at": None,
            "last_request_id": None,
            "last_reason": None,
            "last_http_status": None,
        }
    return {
        "last_outcome": row["outcome"],
        "last_run_at": row["created_at"],
        "last_request_id": row["request_id"],
        "last_reason": row["reason"],
        "last_http_status": row["http_status"],
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_promotion_status(
    conn: sqlite3.Connection,
    *,
    manifest_path: Path | None = None,
    slo_window_hours: int = 24,
    latency_window_hours: int = 24,
    silent_fail_window_hours: int = 24,
    probe_comfyui: bool = True,
    comfyui_base_url: str | None = None,
) -> dict[str, Any]:
    """Build the 11-field promotion status block.

    All params are kwargs so call sites are explicit. Defaults match plan
    §C3.0.1: 24h SLO window, 24h latency / silent-fail window, ComfyUI live
    probe ON (tests disable via ``probe_comfyui=False``).
    """
    manifest = _read_manifest(manifest_path)
    state = _resolve_manifest_state(manifest)
    exposure = _bucket_exposure(state)
    slo_block = _evaluate_slo(
        conn, window_hours=slo_window_hours, promotion_state=state
    )
    baseline = _baseline_freshness(manifest)
    if probe_comfyui:
        # Lazy resolve so tests can pin a stub URL without importing the
        # adapter (which would pull in ComfyUI workflow paths).
        if comfyui_base_url is None:
            from .. import ai_generation_adapter as _aga

            comfyui_base_url = _aga.COMFYUI_BASE_URL
        probe = _probe_comfyui(comfyui_base_url)
    else:
        probe = {"reachable": None, "skipped": True, "base_url": comfyui_base_url}
    latency = _render_latency_p50_p95(conn, window_hours=latency_window_hours)
    silent = _silent_fail_count(conn, window_hours=silent_fail_window_hours)
    applier_last = _rollback_applier_last(conn)

    return {
        "manifest_state": state,
        "bucket_exposure_pct": exposure,
        "slo_recommendation": slo_block.get("recommendation"),
        "sample_size": slo_block.get("sample_size", 0),
        "minimum_sample_size": slo_block.get("minimum_sample_size", 0),
        "violations": slo_block.get("violations", []),
        "slo_within": slo_block.get("within_slo"),
        "slo_notes": slo_block.get("notes", ""),
        "slo_generated_at": slo_block.get("generated_at"),
        "slo_window_hours": slo_block.get("window_hours", slo_window_hours),
        "slo_error": slo_block.get("error"),
        "baseline_freshness": baseline,
        "comfyui_live_probe": probe,
        "render_latency": latency,
        "silent_fail": silent,
        "rollback_applier_last": applier_last,
        "computed_at": _iso_now(),
        "schema_version": 1,
    }
