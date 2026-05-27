"""Thin wrapper for POST /api/render/ops/vlm-shadow (plan §P1.4 daily cron).

Replaces `vlm_classify_batch.py --live-no-apply` direct fire with an audited
ops endpoint call. CLI preserved for the launchd plist / crontab job that
fires this nightly.

Usage (daily cron):
    python -m backend.scripts.vlm_daily_shadow \\
        --all-low-confidence \\
        --max-items 100 \\
        --reviewer cron@case-workbench \\
        --reason "nightly shadow audit"

    # spot-check specific cases
    python -m backend.scripts.vlm_daily_shadow \\
        --case-ids 12,34,56 --reviewer operator@x.com
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import requests

DEFAULT_BASE_URL = os.environ.get("CASE_WORKBENCH_API_BASE", "http://127.0.0.1:8000")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--all-low-confidence", action="store_true",
                   help="select all image_observations with confidence < threshold")
    p.add_argument("--case-ids", default=None,
                   help="explicit comma-separated case ids to shadow")
    p.add_argument("--max-items", type=int, default=50)
    p.add_argument("--confidence-threshold", type=float, default=0.85)
    p.add_argument("--dry-run", action="store_true", default=True,
                   help="(default) generate candidate list; do not fire VLM (real fire 501)")
    p.add_argument("--reviewer", required=True)
    p.add_argument("--reason", default=None)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--request-id", default=None)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.all_low_confidence and not args.case_ids:
        print("error: must supply --all-low-confidence or --case-ids", file=sys.stderr)
        return 2
    payload: dict = {
        "all_low_confidence": bool(args.all_low_confidence),
        "max_items": args.max_items,
        "confidence_threshold": args.confidence_threshold,
        "dry_run": bool(args.dry_run),
        "reviewer": args.reviewer,
    }
    if args.case_ids:
        payload["case_ids"] = [int(x.strip()) for x in args.case_ids.split(",") if x.strip()]
    if args.reason:
        payload["reason"] = args.reason
    headers = {}
    if args.request_id:
        headers["X-Request-Id"] = args.request_id
    resp = requests.post(
        f"{args.base_url}/api/render/ops/vlm-shadow",
        json=payload,
        headers=headers,
        timeout=60,
    )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text
    print(json.dumps({"http_status": resp.status_code, "body": body}, ensure_ascii=False, indent=2))
    return 0 if resp.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
