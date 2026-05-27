"""CLI: calibrate SLO baseline from real shadow-period data (Wave 3 Agent C).

Eval-auditor C-2: SLO threshold + baseline numbers in `slo_thresholds.json`
were hand-seeded at Wave 2 ship time without provenance. This script measures
real metric distributions from `simulation_jobs` / `candidate_lineage` /
`ops_audit_log` over the shadow window and emits a calibrated patch
(thresholds + baseline + baseline_provenance) so operators can review/apply.

Usage:
    # Dry-run (default) — print JSON patch on stdout, do not touch file
    python -m backend.scripts.calibrate_slo_baseline --window 24

    # Apply — write back atomically (tmp + os.replace), update provenance
    python -m backend.scripts.calibrate_slo_baseline --window 24 --apply

    # Use custom output path (testing / staging)
    python -m backend.scripts.calibrate_slo_baseline --apply \\
           --thresholds-path /tmp/staging_slo_thresholds.json

Exit codes:
    0  success (dry_run or apply succeeded)
    2  invalid argument / runtime error

Baseline algorithm (intentionally simple — single-window observed + safety
floor; provenance trace matters more than sophistication. Future revs can
swap in higher-quantile P95 or Bayesian methods over multiple sliding
windows; today we score a single calibration window and floor it):
    - comfyui_failure_rate_max:    max(observed_rate, 0.05)          single window
    - vlm_disagreement_rate_max:   max(observed_mean, 0.10)          single window
    - vlm_judge_missing_rate_max:  max(observed_missing * 1.1, 0.10) single window
    - delivery_gate_rejection_rate:  observed_rate                   raw baseline
    - pre_render_gate_blocker_count: observed_count                  raw baseline
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.services.promotion_slo_monitor import (
    DEFAULT_WINDOW_HOURS,
    _compute_comfyui_failure_rate,
    _compute_delivery_gate_rejection_rate,
    _compute_pre_render_gate_blocker_count,
    _compute_vlm_disagreement_rate,
    _cutoff_iso,
    load_default_thresholds,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Floors prevent calibration from accidentally relaxing SLO below seed values.
_COMFYUI_FAILURE_RATE_FLOOR = 0.05
_VLM_DISAGREEMENT_RATE_FLOOR = 0.10
# Wave 3 P0.5 H-1: missing/malformed VLM judge payload rate floor.
_VLM_JUDGE_MISSING_RATE_FLOOR = 0.10
# Safety multiplier on observed missing_rate so transient spikes don't trip the
# threshold immediately after calibration. Same semantics as other "observed × k"
# baselines (delivery / pre_render multipliers use 1.05 / 1.10 respectively).
_VLM_JUDGE_MISSING_RATE_MULTIPLIER = 1.10

# Multiplier defaults (eval auditor: keep multiplier seeds + recalibrate raw baselines)
_DELIVERY_GATE_MULT_DEFAULT = 1.05
_PRE_RENDER_GATE_MULT_DEFAULT = 1.10

_DEFAULT_THRESHOLDS_FILE = (
    Path(__file__).resolve().parent.parent.parent
    / "case-workbench-ai"
    / "promotion"
    / "slo_thresholds.json"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_main_sha() -> str:
    """Resolve current HEAD short sha for provenance trace. Best-effort; on
    failure returns 'unknown'. Never raises (CLI must keep running)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return "unknown"


def _measure(
    conn: sqlite3.Connection,
    window_hours: int,
) -> dict[str, Any]:
    """Run the same 4 dimension queries as evaluate_window, but for *calibration*
    not violation detection. Returns raw observed numbers."""
    cutoff = _cutoff_iso(window_hours)
    comfyui = _compute_comfyui_failure_rate(conn, cutoff)
    vlm = _compute_vlm_disagreement_rate(conn, cutoff)
    delivery = _compute_delivery_gate_rejection_rate(conn, cutoff)
    gate = _compute_pre_render_gate_blocker_count(conn, cutoff)
    sample_size = (
        comfyui["terminal_total"]
        + vlm["sample_count"]
        + delivery["counted"]
        + gate["blocker_count"]
    )
    return {
        "comfyui_failure_rate_observed": comfyui["rate"],
        "vlm_disagreement_mean_observed": vlm["mean_disagreement"],
        # Wave 3 P0.5 H-1: missing_rate piggy-backs on the same
        # _compute_vlm_disagreement_rate call that produces the disagreement
        # signal (B重写后 vlm dict 含 total_rows / missing_count / missing_rate).
        # We surface it here so _build_patch can emit a calibrated
        # vlm_judge_missing_rate_max threshold alongside the disagreement one.
        "vlm_judge_missing_rate_observed": vlm["missing_rate"],
        "delivery_rejection_rate_observed": delivery["rate"],
        "pre_render_gate_blocker_count_observed": gate["blocker_count"],
        "sample_size": sample_size,
        "evidence": {
            "comfyui": comfyui,
            "vlm": vlm,
            "delivery": delivery,
            "pre_render_gate": gate,
        },
    }


def _build_patch(
    observed: dict[str, Any],
    *,
    window_hours: int,
    main_sha: str,
    now: datetime,
) -> dict[str, Any]:
    """Construct the JSON patch (full thresholds doc shape) from observed
    numbers + provenance trace."""
    # Threshold floors — calibration never relaxes safety bar.
    comfyui_threshold = max(
        float(observed["comfyui_failure_rate_observed"]),
        _COMFYUI_FAILURE_RATE_FLOOR,
    )
    vlm_threshold = max(
        float(observed["vlm_disagreement_mean_observed"]),
        _VLM_DISAGREEMENT_RATE_FLOOR,
    )
    # Wave 3 P0.5 H-1: missing_rate threshold = observed * safety multiplier,
    # floored at the seed value so a calm shadow window never relaxes the bar.
    missing_rate_threshold = max(
        float(observed["vlm_judge_missing_rate_observed"])
        * _VLM_JUDGE_MISSING_RATE_MULTIPLIER,
        _VLM_JUDGE_MISSING_RATE_FLOOR,
    )
    delivery_baseline = float(observed["delivery_rejection_rate_observed"])
    pre_render_baseline = float(observed["pre_render_gate_blocker_count_observed"])

    return {
        "schema_version": 1,
        "thresholds": {
            "comfyui_failure_rate_max": round(comfyui_threshold, 6),
            "vlm_disagreement_rate_max": round(vlm_threshold, 6),
            "vlm_judge_missing_rate_max": round(missing_rate_threshold, 6),
            "delivery_gate_rejection_rate_multiplier_max": _DELIVERY_GATE_MULT_DEFAULT,
            "pre_render_gate_blocker_multiplier_max": _PRE_RENDER_GATE_MULT_DEFAULT,
        },
        "baseline": {
            "delivery_gate_rejection_rate": round(delivery_baseline, 6),
            "pre_render_gate_blocker_count": pre_render_baseline,
        },
        "baseline_provenance": {
            "measured_at": now.isoformat(),
            "window_hours": int(window_hours),
            "sample_size": int(observed["sample_size"]),
            "computed_by": "calibrate_cli",
            "computed_at_main_sha": main_sha,
        },
        "minimum_sample_size": 30,
        "default_window_hours": 48,
        "notes": (
            "Auto-generated by backend.scripts.calibrate_slo_baseline. "
            "Review provenance before treating as production baseline."
        ),
    }


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    """tmp + os.replace for atomic write. Keeps existing file intact on crash."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(serialized, encoding="utf-8")
    os.replace(tmp, target)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="calibrate_slo_baseline",
        description=(
            "Measure shadow-period SLO metrics and emit a calibrated "
            "thresholds patch with full provenance (eval-auditor C-2)."
        ),
    )
    parser.add_argument(
        "--window",
        type=int,
        default=DEFAULT_WINDOW_HOURS,
        help=f"calibration window in hours (default: {DEFAULT_WINDOW_HOURS})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write back to thresholds JSON; default is dry-run (stdout only)",
    )
    parser.add_argument(
        "--thresholds-path",
        type=str,
        default=None,
        help="thresholds JSON path (default: case-workbench-ai/promotion/slo_thresholds.json)",
    )
    args = parser.parse_args(argv)

    if args.window <= 0:
        sys.stderr.write("--window must be positive integer\n")
        return 2

    target = Path(args.thresholds_path) if args.thresholds_path else _DEFAULT_THRESHOLDS_FILE

    try:
        # Sanity check current thresholds parse (helps operator see what's
        # being replaced; does not affect the calibration result).
        try:
            existing = load_default_thresholds(target) if target.exists() else None
        except ValueError as exc:  # pragma: no cover  (informational only)
            existing = None
            sys.stderr.write(f"warning: existing thresholds invalid: {exc}\n")

        from backend import db as _db

        conn = _db.get_conn()
        try:
            observed = _measure(conn, args.window)
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover  (defensive — runtime errors)
        sys.stderr.write(f"calibration failed: {exc}\n")
        return 2

    patch = _build_patch(
        observed,
        window_hours=args.window,
        main_sha=_get_main_sha(),
        now=datetime.now(timezone.utc),
    )

    if args.apply:
        _atomic_write_json(target, patch)
        sys.stdout.write(
            f"applied: wrote calibrated SLO baseline to {target} "
            f"(sample_size={observed['sample_size']}, window_hours={args.window})\n"
        )
        return 0

    # Dry-run: emit machine-readable patch on stdout
    json.dump(
        {
            "dry_run": True,
            "target_path": str(target),
            "existing_provenance": (
                (existing or {}).get("baseline_provenance") if existing else None
            ),
            "observed": observed,
            "patch": patch,
        },
        sys.stdout,
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
