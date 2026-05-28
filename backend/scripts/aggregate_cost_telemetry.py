"""CLI: aggregate cost-related telemetry for C5.3 cost model backfill.

Phase C5.3 deliverable (Stream C). Read-only SELECT aggregator over
`vlm_usage_log` + `render_jobs` + `simulation_jobs` + `candidate_lineage`.
Outputs a JSON snapshot used by `delivery/c5-cost-model.md` to replace the
`<<*_PENDING>>` placeholders.

**Boundaries (Stream C contract)**
- Read-only: no INSERT / UPDATE / DELETE / DDL anywhere in this file
- No DB schema changes; no schema migrations
- No mutation of `case-workbench.db`; honour `CASE_WORKBENCH_DB_PATH` env override
- Output is JSON to stdout (default) or `--output <path>` (atomic write)
- Drill / staging exclusion uses `promotion_audit_log` JOIN once C3.0.4 lands
  (planned post-this-PR); current skeleton leaves the join site marked with
  `# TODO(C3.0.4 join)` and filters out simulation_jobs in obvious drill states
  via `status` + `audit_json.drill_marker` best-effort only

Usage:
    # Aggregate last 7 days, print JSON to stdout (default)
    python -m backend.scripts.aggregate_cost_telemetry --window 7

    # Aggregate last 30 days, write atomically to delivery file
    python -m backend.scripts.aggregate_cost_telemetry --window 30 \\
        --output delivery/c5-cost-telemetry-$(date +%F).json

    # Custom DB path (testing / staging snapshot)
    CASE_WORKBENCH_DB_PATH=/tmp/staging.db \\
        python -m backend.scripts.aggregate_cost_telemetry --window 7

Exit codes:
    0  success
    2  invalid argument / runtime error / read failure

Output schema (v1):
    {
      "schema_version": 1,
      "window_days": <int>,
      "cutoff_iso": "<UTC ISO-8601>",
      "generated_at_iso": "<UTC ISO-8601>",
      "vlm_usage": {
        "total_calls": <int>,
        "total_cost_usd": <float>,
        "cost_usd_per_call_avg": <float>,
        "latency_ms_avg": <float>,
        "latency_ms_p50": <float>,
        "latency_ms_p95": <float>,
        "by_purpose": {<purpose>: {calls, cost_usd, latency_ms_avg}, ...},
        "by_provider_model": [...]
      },
      "render_jobs": {
        "total_finished": <int>,
        "duration_ms_avg": <float>,
        "duration_ms_p50": <float>,
        "duration_ms_p95": <float>,
        "by_status": {<status>: <count>, ...}
      },
      "simulation_jobs": {
        "total": <int>,
        "by_status": {<status>: <count>, ...},
        "drill_excluded": <int>
      },
      "candidate_lineage": {
        "total_attempts": <int>,
        "attempts_per_case_avg": <float>,
        "failure_reasons": {<reason>: <count>, ...}
      },
      "cost_per_case": {
        "vlm_api_cost_usd": <float>,         # VLM cost / unique case
        "estimated_eligible_cases": <int>,    # finished render_jobs uniqued by case_id
        "_note": "GPU amortized cost + fixed cost are added by finance, not derivable from DB"
      },
      "limitations": [
        "promotion_audit_log JOIN not yet wired (C3.0.4 deliverable)",
        "GPU rental / electricity / hardware cost lives outside DB",
        "Drill exclusion is best-effort via simulation_jobs.audit_json; will tighten post-C3.0.4"
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_WINDOW_DAYS = 7
DEFAULT_PURPOSE_BREAKDOWN_LIMIT = 20
DEFAULT_PROVIDER_BREAKDOWN_LIMIT = 20


def _cutoff_iso(window_days: int, *, now: datetime | None = None) -> str:
    """UTC ISO-8601 cutoff = now - window_days. timezone-aware (W13 lesson)."""
    base = now or datetime.now(timezone.utc)
    return (base - timedelta(days=window_days)).isoformat()


def _percentile(values: list[float], pct: float) -> float:
    """Best-effort percentile (linear interpolation). Returns 0.0 on empty."""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    frac = k - lo
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


def _aggregate_vlm_usage(conn: sqlite3.Connection, cutoff: str) -> dict[str, Any]:
    """Aggregate vlm_usage_log over the window. Pure SELECT."""
    rows = conn.execute(
        """
        SELECT purpose, provider, model, cost_usd, latency_ms, status
          FROM vlm_usage_log
         WHERE created_at >= ?
        """,
        (cutoff,),
    ).fetchall()

    total_calls = len(rows)
    total_cost = sum(float(r["cost_usd"] or 0.0) for r in rows)
    latencies = [float(r["latency_ms"] or 0) for r in rows]

    by_purpose: dict[str, dict[str, float]] = defaultdict(
        lambda: {"calls": 0, "cost_usd": 0.0, "latency_ms_sum": 0.0}
    )
    by_provider_model: Counter[tuple[str, str]] = Counter()
    by_provider_model_cost: dict[tuple[str, str], float] = defaultdict(float)

    for r in rows:
        purpose = r["purpose"] or "unknown"
        by_purpose[purpose]["calls"] += 1
        by_purpose[purpose]["cost_usd"] += float(r["cost_usd"] or 0.0)
        by_purpose[purpose]["latency_ms_sum"] += float(r["latency_ms"] or 0)

        key = (r["provider"] or "unknown", r["model"] or "unknown")
        by_provider_model[key] += 1
        by_provider_model_cost[key] += float(r["cost_usd"] or 0.0)

    purpose_out = {
        p: {
            "calls": d["calls"],
            "cost_usd": round(d["cost_usd"], 6),
            "latency_ms_avg": round(d["latency_ms_sum"] / d["calls"], 2) if d["calls"] else 0.0,
        }
        for p, d in by_purpose.items()
    }

    provider_model_out = [
        {
            "provider": prov,
            "model": model,
            "calls": calls,
            "cost_usd": round(by_provider_model_cost[(prov, model)], 6),
        }
        for (prov, model), calls in by_provider_model.most_common(DEFAULT_PROVIDER_BREAKDOWN_LIMIT)
    ]

    return {
        "total_calls": total_calls,
        "total_cost_usd": round(total_cost, 6),
        "cost_usd_per_call_avg": round(total_cost / total_calls, 6) if total_calls else 0.0,
        "latency_ms_avg": round(sum(latencies) / total_calls, 2) if total_calls else 0.0,
        "latency_ms_p50": round(_percentile(latencies, 50), 2),
        "latency_ms_p95": round(_percentile(latencies, 95), 2),
        "by_purpose": purpose_out,
        "by_provider_model": provider_model_out,
    }


def _aggregate_render_jobs(conn: sqlite3.Connection, cutoff: str) -> dict[str, Any]:
    """Aggregate render_jobs over the window. Pure SELECT.

    Duration uses SQLite julianday() to convert TIMESTAMP strings to ms diff;
    rows with NULL finished_at are dropped from duration stats but kept in count.
    """
    rows = conn.execute(
        """
        SELECT case_id, status, started_at, finished_at,
               CAST((julianday(finished_at) - julianday(started_at)) * 86400000 AS INTEGER)
                   AS duration_ms
          FROM render_jobs
         WHERE enqueued_at >= ?
        """,
        (cutoff,),
    ).fetchall()

    by_status: Counter[str] = Counter()
    durations: list[float] = []
    finished_case_ids: set[int] = set()
    finished_states = ("done", "done_with_issues")

    for r in rows:
        by_status[r["status"] or "unknown"] += 1
        if (
            r["status"] in finished_states
            and r["duration_ms"] is not None
            and r["duration_ms"] > 0
        ):
            durations.append(float(r["duration_ms"]))
            if r["case_id"] is not None:
                finished_case_ids.add(int(r["case_id"]))

    return {
        "total_finished": sum(by_status.get(s, 0) for s in finished_states),
        "duration_ms_avg": round(sum(durations) / len(durations), 2) if durations else 0.0,
        "duration_ms_p50": round(_percentile(durations, 50), 2),
        "duration_ms_p95": round(_percentile(durations, 95), 2),
        "by_status": dict(by_status),
        "_finished_case_ids_size": len(finished_case_ids),
    }


def _aggregate_simulation_jobs(conn: sqlite3.Connection, cutoff: str) -> dict[str, Any]:
    """Aggregate simulation_jobs (ComfyUI sim path proxy). Pure SELECT.

    Drill exclusion: best-effort. Looks for `audit_json` containing
    `"drill_marker"` substring. Will be replaced by promotion_audit_log JOIN
    when C3.0.4 lands.
    """
    rows = conn.execute(
        """
        SELECT status, audit_json
          FROM simulation_jobs
         WHERE created_at >= ?
        """,
        (cutoff,),
    ).fetchall()

    by_status: Counter[str] = Counter()
    drill_excluded = 0

    for r in rows:
        audit = r["audit_json"] or "{}"
        # TODO(C3.0.4 join): replace substring sniff with proper LEFT JOIN
        # against promotion_audit_log WHERE drill_id IS NOT NULL.
        if '"drill_marker"' in audit or '"drill_id"' in audit:
            drill_excluded += 1
            continue
        by_status[r["status"] or "unknown"] += 1

    return {
        "total": sum(by_status.values()),
        "by_status": dict(by_status),
        "drill_excluded": drill_excluded,
    }


def _aggregate_candidate_lineage(conn: sqlite3.Connection, cutoff: str) -> dict[str, Any]:
    """Aggregate candidate_lineage attempts + failure reasons. Pure SELECT."""
    rows = conn.execute(
        """
        SELECT case_id, attempt, failure_reason
          FROM candidate_lineage
         WHERE created_at >= ?
        """,
        (cutoff,),
    ).fetchall()

    total_attempts = len(rows)
    unique_cases = {int(r["case_id"]) for r in rows if r["case_id"] is not None}
    failure_reasons: Counter[str] = Counter()
    for r in rows:
        reason = r["failure_reason"]
        if reason:
            failure_reasons[reason] += 1

    return {
        "total_attempts": total_attempts,
        "unique_cases": len(unique_cases),
        "attempts_per_case_avg": (
            round(total_attempts / len(unique_cases), 3) if unique_cases else 0.0
        ),
        "failure_reasons": dict(failure_reasons.most_common(20)),
    }


def _compute_cost_per_case(
    vlm_usage: dict[str, Any], render_jobs: dict[str, Any]
) -> dict[str, Any]:
    """Derive a first-cut VLM cost-per-eligible-case. Conservative.

    Eligible cases = unique case_ids with a finished render_job in window.
    Excludes GPU amortized / electricity / hardware / human review (out of DB).
    """
    eligible = max(int(render_jobs.get("_finished_case_ids_size") or 0), 0)
    total_vlm_cost = float(vlm_usage.get("total_cost_usd") or 0.0)
    return {
        "vlm_api_cost_usd": round(total_vlm_cost / eligible, 6) if eligible else 0.0,
        "estimated_eligible_cases": eligible,
        "total_vlm_api_cost_usd": round(total_vlm_cost, 6),
        "_note": (
            "GPU amortized / electricity / hardware / human review cost lives "
            "outside DB; finance must add to derive end-to-end per-case cost"
        ),
    }


def _aggregate(conn: sqlite3.Connection, window_days: int) -> dict[str, Any]:
    cutoff = _cutoff_iso(window_days)
    vlm = _aggregate_vlm_usage(conn, cutoff)
    renders = _aggregate_render_jobs(conn, cutoff)
    sims = _aggregate_simulation_jobs(conn, cutoff)
    lineage = _aggregate_candidate_lineage(conn, cutoff)
    cost_per_case = _compute_cost_per_case(vlm, renders)

    # Strip internal scratch field before serializing
    renders.pop("_finished_case_ids_size", None)

    return {
        "schema_version": SCHEMA_VERSION,
        "window_days": window_days,
        "cutoff_iso": cutoff,
        "generated_at_iso": datetime.now(timezone.utc).isoformat(),
        "vlm_usage": vlm,
        "render_jobs": renders,
        "simulation_jobs": sims,
        "candidate_lineage": lineage,
        "cost_per_case": cost_per_case,
        "limitations": [
            "promotion_audit_log JOIN not yet wired (C3.0.4 deliverable)",
            "GPU rental / electricity / hardware cost lives outside DB",
            "Drill exclusion is best-effort via simulation_jobs.audit_json substring sniff; will tighten post-C3.0.4",
            "render_jobs.duration computed via julianday diff (ms granularity); does not include queue wait time",
        ],
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON via tmp + os.replace (same pattern as calibrate_slo_baseline)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate VLM cost + render duration + simulation telemetry over "
            "a sliding window. Read-only SELECT; output JSON for C5.3 cost model."
        )
    )
    parser.add_argument(
        "--window",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=f"Window size in days (default {DEFAULT_WINDOW_DAYS})",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output file path; if omitted, JSON is printed to stdout",
    )
    args = parser.parse_args(argv)

    if args.window <= 0:
        sys.stderr.write("--window must be positive integer\n")
        return 2

    try:
        # Import here so module-level import doesn't fail if backend pkg is
        # only partially available (e.g. running snapshot script standalone).
        from backend import db as _db

        conn = _db.get_conn()
        try:
            payload = _aggregate(conn, args.window)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        sys.stderr.write(f"sqlite error: {exc}\n")
        return 2
    except Exception as exc:  # pragma: no cover  (defensive)
        sys.stderr.write(f"aggregation failed: {exc}\n")
        return 2

    if args.output:
        target = Path(args.output)
        _atomic_write_json(target, payload)
        sys.stdout.write(
            f"wrote cost telemetry snapshot to {target} "
            f"(window_days={args.window}, vlm_calls={payload['vlm_usage']['total_calls']}, "
            f"render_finished={payload['render_jobs']['total_finished']})\n"
        )
        return 0

    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
