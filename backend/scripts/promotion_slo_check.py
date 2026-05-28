"""CLI: promotion SLO monitor — plan §P2.4 auto-rollback signal source.

Usage:
    python -m backend.scripts.promotion_slo_check
    python -m backend.scripts.promotion_slo_check --window 24
    python -m backend.scripts.promotion_slo_check --format markdown
    python -m backend.scripts.promotion_slo_check --thresholds path/to/custom.json
    python -m backend.scripts.promotion_slo_check --baseline-stale-days 90
    python -m backend.scripts.promotion_slo_check --paused-stale-days 14

Two kwarg-semantic CLI overrides mirror their JSON-tunable counterparts:

* ``--baseline-stale-days N`` (Wave 6 followup A-1) → ``baseline_stale_days``
  JSON field (added in Wave 5 followup #2).
* ``--paused-stale-days N`` (Wave 7 followup A-1) → ``paused_stale_days``
  JSON field (added in Wave 5 followup #1).

Operator priority (see :mod:`backend.services.promotion_slo_monitor` docstring):
``kwarg > JSON > module constant`` — passing ``--baseline-stale-days 90`` /
``--paused-stale-days 14`` from a cron line wins over ``slo_thresholds.json``
without requiring a JSON edit + redeploy. Both flags route through
:func:`_inject_structured_field` so the structured-branch invariant in
``_merge_thresholds`` is honored uniformly.

Exit codes (Wave 3 P0.5 H-5: aligned with the four-token recommendation
vocabulary that ``promotion_slo_monitor.evaluate_window`` now emits):

    0  ``continue``           — within SLO, keep current promotion_state.
    0  ``insufficient_data``  — too few samples (shadow / rolled_back state),
                               safe to keep current state and gather more data.
    0  ``monitoring_paused``  — small sample under PROMOTED state (p10/p25/
                               p50/p100); per P2.4 methodology we *do not*
                               regress the state on the basis of <30 samples.
    1  ``rollback``           — violation; caller (cron / launchd) should
                               trigger the auto-rollback flow.
    2  invalid argument / runtime error.

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
    RECOMMENDATION_CONTINUE,
    RECOMMENDATION_INSUFFICIENT_DATA,
    RECOMMENDATION_MONITORING_PAUSED,
    RECOMMENDATION_ROLLBACK,
    RECOMMENDATION_STOP_LOSS_HALT,
    SLOReport,
    evaluate_window,
    load_default_thresholds,
)

EXIT_OK = 0
EXIT_ROLLBACK = 1
EXIT_USAGE_ERROR = 2
# K-5 / K-7: stop-loss halt is operator-actionable but NOT a rollback signal.
# Cron `||` chains should not auto-trigger rollback_runner.sh on this exit.
# Use a distinct non-zero exit code so launchd / monitoring can route it
# separately.
EXIT_STOP_LOSS_HALT = 3


def _format_within_slo(report: SLOReport) -> str:
    """Wave 3 P0.5 H-5: ``within_slo`` is now ``None`` when recommendation
    is ``monitoring_paused`` (small sample under promoted state — neither
    pass nor fail). Render it explicitly so on-call doesn't read 'None' as
    'False'."""
    if report.within_slo is None:
        return f"**N/A (monitoring_paused)** — note: {report.notes or 'small sample'}"
    return f"**{report.within_slo}**"


def _render_violation_row(v: dict) -> str:
    """K-7: schema-adaptive single-violation renderer.

    Pre-K-7 the renderer hard-coded ``v['actual']`` and ``v['threshold']``,
    which crashed on Wave 4 violations that name those fields differently
    (or omit them entirely):

      * ``monitoring_paused_stale`` uses ``actual_days`` / ``threshold_days``
      * ``baseline_unmeasured``     has no actual/threshold (categorical)
      * ``baseline_undersampled``   (K-4 new) has ``sample_size`` /
        ``minimum_sample_size`` in context
      * ``paused_state_unreadable`` (K-6 new) has ``actual=None`` (synthetic)

    Post-K-7: probe multiple field-name aliases, fall back to "N/A" placeholder
    rather than KeyError-ing the whole markdown render. Comparator defaults to
    "≤" for back-compat with legacy violation shapes.
    """
    dimension = v.get("dimension", "unknown")
    actual = v.get("actual")
    if actual is None:
        actual = v.get("actual_days", "N/A")
    threshold = v.get("threshold")
    if threshold is None:
        threshold = v.get("threshold_days", "N/A")
    comparator = v.get("comparator", "≤")
    ctx = v.get("context") or {}
    hint = ctx.get("hint") if isinstance(ctx, dict) else None
    suffix = f" — hint: {hint}" if hint else ""
    return (
        f"- **{dimension}** — actual={actual} threshold={threshold} "
        f"({comparator}){suffix}"
    )


def _format_markdown(report: SLOReport) -> str:
    lines: list[str] = []
    lines.append(f"# Promotion SLO report — {report.generated_at}")
    lines.append("")
    lines.append(f"- window_hours: **{report.window_hours}**")
    lines.append(f"- sample_size: **{report.sample_size}**")
    lines.append(f"- within_slo: {_format_within_slo(report)}")
    lines.append(f"- recommendation: **{report.recommendation}**")
    if report.notes:
        lines.append(f"- notes: {report.notes}")
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
            lines.append(_render_violation_row(v))
    return "\n".join(lines) + "\n"


def _exit_code_for(report: SLOReport) -> int:
    """Map the SLO monitor's recommendation token to a cron-friendly exit
    code. Vocabulary mapping:

        0 ``continue`` / ``insufficient_data`` / ``monitoring_paused``
        1 ``rollback``          — auto-rollback signal
        3 ``stop_loss_halt``    — K-5 audit alert (灰度流量长时间不足);
                                  operator review required; NOT a rollback
        0 unknown               — defensive (don't trigger spurious rollback)
    """
    if report.recommendation == RECOMMENDATION_ROLLBACK:
        return EXIT_ROLLBACK
    if report.recommendation == RECOMMENDATION_STOP_LOSS_HALT:
        return EXIT_STOP_LOSS_HALT
    if report.recommendation in (
        RECOMMENDATION_CONTINUE,
        RECOMMENDATION_INSUFFICIENT_DATA,
        RECOMMENDATION_MONITORING_PAUSED,
    ):
        return EXIT_OK
    # Defensive: unknown token → treat as OK (don't trigger spurious
    # rollback), but log via stderr so on-call notices the drift.
    sys.stderr.write(
        f"unknown recommendation {report.recommendation!r}; treating as exit 0\n"
    )
    return EXIT_OK


def _inject_structured_field(
    threshold_override: dict | None,
    field: str,
    value: int,
) -> dict:
    """Wave 7 followup A-1 (extracts the helper W6-A in-lined):

    Inject a top-level ``field=value`` into ``threshold_override`` and ensure
    the result is shaped so :func:`backend.services.promotion_slo_monitor._merge_thresholds`
    routes through its **structured branch** (which honors top-level fields
    like ``baseline_stale_days`` / ``paused_stale_days``) rather than its
    **flat branch** (which would misroute the key into the ``thresholds``
    sub-dict and effectively no-op the CLI override).

    Routing invariant: either ``"thresholds"`` OR ``"baseline"`` key in the
    override dict is enough to trip the structured branch (see
    ``_merge_thresholds`` source — the check is an OR, not an AND). So:

    * ``override is None`` → create ``{"thresholds": {}}`` (empty marker).
    * Override already has ``"thresholds"`` or ``"baseline"`` (e.g. supplied
      via ``--thresholds <file>``) → no marker injection needed.
    * Override exists but lacks both markers (rare — typically a flat
      thresholds-only dict from a hand-built test) → inject empty
      ``"thresholds": {}`` so the structured branch takes precedence.

    This helper replaces the duplicated routing dance that lived inline for
    each CLI flag pre-W7. Adding a future ``--minimum-sample-size N`` flag
    (or any other JSON tunable that surfaces at the structured-branch top
    level) would call this helper rather than re-implementing the pattern.

    Returns the (possibly newly-created) override dict so callers can chain.
    """
    if threshold_override is None:
        threshold_override = {"thresholds": {}}
    elif (
        "thresholds" not in threshold_override
        and "baseline" not in threshold_override
    ):
        # Either marker would suffice; we pick "thresholds" by convention.
        # See W6 reviewer Info #1 (Wave 6 followup) for the OR-vs-AND audit.
        threshold_override["thresholds"] = {}
    threshold_override[field] = value
    return threshold_override


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
    parser.add_argument(
        "--baseline-stale-days",
        dest="baseline_stale_days",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Wave 6 followup A-1: override baseline_stale_days (default: read "
            "from slo_thresholds.json or BASELINE_STALE_DAYS=60). Priority "
            "kwarg > JSON > module constant. Must be positive int."
        ),
    )
    parser.add_argument(
        "--paused-stale-days",
        dest="paused_stale_days",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Wave 7 followup A-1: override paused_stale_days (default: read "
            "from slo_thresholds.json or PAUSED_STALE_DAYS=7). Priority "
            "kwarg > JSON > module constant. Must be positive int. Symmetric "
            "to --baseline-stale-days (W6-A)."
        ),
    )
    args = parser.parse_args(argv)

    if args.window <= 0:
        sys.stderr.write("--window must be positive integer\n")
        return EXIT_USAGE_ERROR
    # CLI-layer positive-int gate: surface the constraint with a friendly
    # stderr line BEFORE values tunnel into `_validate_positive_int` →
    # ValueError → generic "SLO check failed" wrap. The deeper validator
    # still protects production callers that bypass the CLI.
    if args.baseline_stale_days is not None and args.baseline_stale_days <= 0:
        sys.stderr.write("--baseline-stale-days must be positive integer\n")
        return EXIT_USAGE_ERROR
    if args.paused_stale_days is not None and args.paused_stale_days <= 0:
        sys.stderr.write("--paused-stale-days must be positive integer\n")
        return EXIT_USAGE_ERROR

    try:
        threshold_override: dict | None = None
        if args.thresholds:
            threshold_override = load_default_thresholds(Path(args.thresholds))
        if args.baseline_stale_days is not None:
            threshold_override = _inject_structured_field(
                threshold_override,
                "baseline_stale_days",
                args.baseline_stale_days,
            )
        if args.paused_stale_days is not None:
            threshold_override = _inject_structured_field(
                threshold_override,
                "paused_stale_days",
                args.paused_stale_days,
            )
        report = evaluate_window(
            window_hours=args.window,
            thresholds=threshold_override,
        )
    except Exception as exc:  # pragma: no cover  (defensive — runtime errors)
        sys.stderr.write(f"SLO check failed: {exc}\n")
        return EXIT_USAGE_ERROR

    if args.format == "markdown":
        sys.stdout.write(_format_markdown(report))
    else:
        json.dump(report.to_dict(), sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")

    return _exit_code_for(report)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
