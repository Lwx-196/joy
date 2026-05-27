"""Thin wrapper for POST /api/render/ops/repair-queue.

Replaces owner-style T145 v10 identity/tone repair batch fire. CLI preserved
for operators; the actual work goes through the audited ops endpoint. dry_run
is default (operator records what they intend to repair; real fire is
pending owner ai_generation_adapter integration).

Usage:
    python -m backend.scripts.repair_queue_runner \\
        --case-ids 123,456 --repair-type identity \\
        --reviewer operator@example.com --reason "identity drift batch"
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
    p.add_argument("--case-ids", required=True,
                   help="comma-separated case ids to repair")
    p.add_argument("--repair-type", required=True,
                   choices=["identity", "tone", "both"])
    p.add_argument("--fire", action="store_true",
                   help="actual fire (currently returns 501; dry_run is default)")
    p.add_argument("--reviewer", required=True)
    p.add_argument("--reason", default=None)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--request-id", default=None)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    case_ids = [int(x.strip()) for x in args.case_ids.split(",") if x.strip()]
    if not case_ids:
        print("error: --case-ids empty after parsing", file=sys.stderr)
        return 2
    payload: dict = {
        "case_ids": case_ids,
        "repair_type": args.repair_type,
        "dry_run": not args.fire,
        "reviewer": args.reviewer,
    }
    if args.reason:
        payload["reason"] = args.reason
    headers = {}
    if args.request_id:
        headers["X-Request-Id"] = args.request_id
    resp = requests.post(
        f"{args.base_url}/api/render/ops/repair-queue",
        json=payload,
        headers=headers,
        timeout=60,
    )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text
    print(json.dumps({"http_status": resp.status_code, "body": body}, ensure_ascii=False, indent=2))
    return 0 if resp.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
