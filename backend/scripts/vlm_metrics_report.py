"""CLI: print VLM usage + classifier output metrics (PLAN P2 wave-1).

Usage:
    python -m backend.scripts.vlm_metrics_report
    python -m backend.scripts.vlm_metrics_report --since 2026-05-26
    python -m backend.scripts.vlm_metrics_report --purpose classifier --format json
"""
from __future__ import annotations

import argparse
import json
import sys

from backend import db
from backend.services.vlm_usage_metrics import (
    summarize_classifier_outputs,
    summarize_usage,
)


def _format_text(usage: dict, classifier: dict) -> str:
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("VLM usage metrics (vlm_usage_log)")
    lines.append("=" * 60)
    f = usage["filters"]
    f_str = ", ".join(f"{k}={v}" for k, v in f.items() if v) or "all rows"
    lines.append(f"filters: {f_str}")
    lines.append(f"total calls: {usage['total_calls']}")
    if usage["total_calls"]:
        lines.append(f"status:    {usage['status_breakdown']}")
        lines.append(f"purpose:   {usage['purpose_breakdown']}")
        lines.append(f"provider:  {usage['provider_breakdown']}")
        lat = usage["latency_ms"]
        lines.append(
            f"latency_ms: p50={lat['p50']} p95={lat['p95']} p99={lat['p99']} "
            f"max={lat['max']} mean={lat['mean']}"
        )
        tk = usage["tokens"]
        lines.append(
            f"tokens:    in_total={tk['input_total']} out_total={tk['output_total']} "
            f"in_mean={tk['input_mean']} out_mean={tk['output_mean']}"
        )
        c = usage["cost_usd"]
        lines.append(f"cost_usd:  total={c['total']} mean={c['mean']}")
        lines.append("per-provider:")
        for pr, b in usage["per_provider"].items():
            lines.append(
                f"  {pr}: count={b['count']} p50={b['latency_p50_ms']}ms "
                f"p95={b['latency_p95_ms']}ms cost=${b['cost_usd_total']} "
                f"tok_in={b['tokens_input_total']} tok_out={b['tokens_output_total']}"
            )
    lines.append("")
    lines.append("=" * 60)
    lines.append(f"Classifier output distribution (image_observations source={classifier['source']})")
    lines.append("=" * 60)
    lines.append(f"total entries: {classifier['total']}")
    if classifier["total"]:
        lines.append(f"phase:        {classifier['phase']}")
        lines.append(f"view:         {classifier['view']}")
        lines.append(f"body_part:    {classifier['body_part']}")
        lines.append(f"confidence:   buckets={classifier['confidence_buckets']} mean={classifier['confidence_mean']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report VLM usage + classifier metrics")
    parser.add_argument("--since", help="ISO timestamp, e.g. 2026-05-26 (UTC)")
    parser.add_argument("--until", help="ISO timestamp upper bound (exclusive)")
    parser.add_argument("--purpose", help="filter purpose (classifier/judge)")
    parser.add_argument("--provider", help="filter provider")
    parser.add_argument("--status", help="filter status (success/error/live_no_apply)")
    parser.add_argument(
        "--source",
        default="vlm_classifier",
        help="image_observations source (default vlm_classifier)",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
    )
    args = parser.parse_args(argv)

    with db.connect() as conn:
        usage = summarize_usage(
            conn,
            since=args.since,
            until=args.until,
            purpose=args.purpose,
            provider=args.provider,
            status=args.status,
        )
        classifier = summarize_classifier_outputs(conn, source=args.source)

    if args.format == "json":
        print(json.dumps({"usage": usage, "classifier": classifier}, ensure_ascii=False, indent=2))
    else:
        print(_format_text(usage, classifier))
    return 0


if __name__ == "__main__":
    sys.exit(main())
