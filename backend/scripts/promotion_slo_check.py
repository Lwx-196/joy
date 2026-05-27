"""CLI: promotion SLO monitor — plan §P2.4 auto-rollback signal source.

Usage:
    python -m backend.scripts.promotion_slo_check
    python -m backend.scripts.promotion_slo_check --window 24
    python -m backend.scripts.promotion_slo_check --format markdown
    python -m backend.scripts.promotion_slo_check --thresholds path/to/custom.json

Exit codes:
    0  within_slo OR insufficient_data (safe to keep current promotion_state)
    1  violation found (caller — cron / launchd — should trigger rollback flow)
    2  invalid argument / runtime error

Suitable for cron / launchd; example crontab line:
    */15 * * * * /path/to/.venv/bin/python -m backend.scripts.promotion_slo_check \\
         --format json --window 48 || /path/to/rollback_runner.sh
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from backend.services.promotion_slo_monitor import (
    DEFAULT_WINDOW_HOURS,
    SLOReport,
    evaluate_window,
    load_default_thresholds,
)


def _format_markdown(report: SLOReport) -> str:
    lines: list[str] = []
    lines.append(f"# Promotion SLO report — {report.generated_at}")
    lines.append("")
    lines.append(f"- window_hours: **{report.window_hours}**")
    lines.append(f"- sample_size: **{report.sample_size}**")
    lines.append(f"- within_slo: **{report.within_slo}**")
    lines.append(f"- recommendation: **{report.recommendation}**")
    lines.append("")
    lines.append("## Evidence")
    for dim_key, dim_data in report.evidence.items():
        if dim_key in {"thresholds", "baseline", "cutoff_iso", "minimum_sample_size"}:
            continue
        lines.append(f"### {dim_key}")
        lines.append("```json")
        lines.append(json.dumps(dim_data, ensure_ascii=False, indent=2, default=str))
        lines.append("```")
    if report.violations:
        lines.append("")
        lines.append("## Violations")
        for v in report.violations:
            lines.append(f"- **{v['dimension']}** — actual={v['actual']} "
                         f"threshold={v['threshold']} (≤)")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="promotion_slo_check",
        description="Evaluate promotion SLO window and emit report.",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=DEFAULT_WINDOW_HOURS,
        help=f"window in hours (default: {DEFAULT_WINDOW_HOURS})",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="output format (default: json)",
    )
    parser.add_argument(
        "--thresholds",
        type=str,
        default=None,
        help="path to custom thresholds JSON (overrides slo_thresholds.json)",
    )
    args = parser.parse_args(argv)

    if args.window <= 0:
        sys.stderr.write("--window must be positive integer\n")
        return 2

    try:
        threshold_override = None
        if args.thresholds:
            threshold_override = load_default_thresholds(Path(args.thresholds))
        report = evaluate_window(
            window_hours=args.window,
            thresholds=threshold_override,
        )
    except Exception as exc:  # pragma: no cover  (defensive — runtime errors)
        sys.stderr.write(f"SLO check failed: {exc}\n")
        return 2

    if args.format == "markdown":
        sys.stdout.write(_format_markdown(report))
    else:
        json.dump(report.to_dict(), sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")

    # Exit code policy: only true violation → 1; insufficient_data → 0 (safe)
    if report.recommendation == "rollback":
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
