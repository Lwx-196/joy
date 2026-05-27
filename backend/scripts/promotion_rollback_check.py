"""CLI: promotion auto-rollback applier — plan §P2.3/§P2.4 收尾.

Wraps :func:`backend.services.promotion_rollback_applier.apply_rollback_decision`
in a cron-friendly entrypoint:

Usage::

    # Plan only (default — DOES NOT WRITE):
    python -m backend.scripts.promotion_rollback_check
    python -m backend.scripts.promotion_rollback_check --dry-run

    # Real apply (rewrites manifest.json + writes ops_audit_log):
    python -m backend.scripts.promotion_rollback_check --apply

    # Pin a custom manifest / window / thresholds:
    python -m backend.scripts.promotion_rollback_check --apply \\
        --window 24 --manifest /path/to/manifest.json \\
        --thresholds /path/to/custom_slo.json

Exit codes (cron-friendly: stdout = JSON result, stderr = human log):

    0   no rollback needed (within SLO / insufficient_data / monitoring_paused /
        already rolled_back) — caller continues normal operation.
    2   rollback triggered (planned in --dry-run, applied with --apply) —
        caller can scrape stdout JSON to send the alert.
    1   error (invalid argument, manifest unreadable, DB write failure, etc.)

Example crontab::

    */15 * * * * /opt/cwb/.venv/bin/python \\
        -m backend.scripts.promotion_rollback_check --apply \\
        --format json --window 48
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from backend.services.promotion_rollback_applier import (
    REASON_APPLIED,
    REASON_DRY_RUN,
    apply_rollback_decision,
)
from backend.services.promotion_slo_monitor import (
    DEFAULT_WINDOW_HOURS,
    evaluate_window,
    load_default_thresholds,
)

# Cron-friendly: human log to stderr; structured payload to stdout.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("promotion_rollback_check")


EXIT_OK = 0
EXIT_ROLLBACK = 2
EXIT_ERROR = 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="promotion_rollback_check",
        description=(
            "Run SLO monitor and apply (or plan) the auto-rollback decision. "
            "Default mode is --dry-run (no manifest write)."
        ),
    )
    parser.add_argument(
        "--window",
        type=int,
        default=DEFAULT_WINDOW_HOURS,
        help=f"SLO eval window in hours (default: {DEFAULT_WINDOW_HOURS})",
    )
    parser.add_argument(
        "--manifest",
        type=str,
        default=None,
        help="manifest path (default: case-workbench-ai/promotion/manifest.json)",
    )
    parser.add_argument(
        "--thresholds",
        type=str,
        default=None,
        help="custom SLO thresholds JSON path",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        dest="apply_changes",
        action="store_true",
        help="Apply the rollback decision (writes manifest + ops_audit_log).",
    )
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Plan only — DO NOT write manifest or audit log (default).",
    )
    return parser


def _run(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.window <= 0:
        logger.error("--window must be a positive integer (got %s)", args.window)
        return EXIT_ERROR

    # Default to dry-run unless --apply was explicitly set.
    dry_run = not args.apply_changes
    manifest_path = Path(args.manifest) if args.manifest else None

    try:
        threshold_override = None
        if args.thresholds:
            threshold_override = load_default_thresholds(Path(args.thresholds))
        report = evaluate_window(
            window_hours=args.window,
            thresholds=threshold_override,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.exception("SLO eval failed")
        sys.stdout.write(
            json.dumps({"error": "slo_eval_failed", "detail": str(exc)}) + "\n"
        )
        return EXIT_ERROR

    logger.info(
        "SLO recommendation=%s sample_size=%s window_hours=%s violations=%d",
        report.recommendation,
        report.sample_size,
        report.window_hours,
        len(report.violations),
    )

    try:
        result = apply_rollback_decision(
            report,
            dry_run=dry_run,
            manifest_path=manifest_path,
        )
    except OSError as exc:
        logger.exception("rollback applier failed with OSError")
        sys.stdout.write(
            json.dumps({"error": "applier_io_failure", "detail": str(exc)}) + "\n"
        )
        return EXIT_ERROR
    except Exception as exc:  # pragma: no cover  (defensive)
        logger.exception("rollback applier failed unexpectedly")
        sys.stdout.write(
            json.dumps({"error": "applier_failure", "detail": str(exc)}) + "\n"
        )
        return EXIT_ERROR

    # Compose payload for stdout (cron / launchd / pipe-into-alerting).
    payload = {
        "slo": {
            "recommendation": report.recommendation,
            "within_slo": report.within_slo,
            "sample_size": report.sample_size,
            "window_hours": report.window_hours,
            "violations": report.violations,
            "generated_at": report.generated_at,
        },
        "applier": result,
        "mode": "apply" if not dry_run else "dry_run",
    }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")

    # Exit code policy:
    #   - rollback applied  → 2 (alerting signal)
    #   - dry-run plan w/ rollback → 2 (still signals "would apply")
    #   - no-op (continue / insufficient_data / already_rolled_back) → 0
    if result.get("reason") == REASON_APPLIED:
        return EXIT_ROLLBACK
    if result.get("reason") == REASON_DRY_RUN and result.get("would_apply"):
        return EXIT_ROLLBACK
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    return _run(argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
