"""Thin wrapper for POST /api/render/ops/ab-sample.

Replaces the legacy "野蛮跑批" `comfyui_ab_runner.py`. CLI preserved; the
actual work goes through the audited ops endpoint (writes ops_audit_log).

Usage:
    python -m backend.scripts.comfyui_ab_runner \\
        --workflow-a v2-baseline --workflow-b v3-sdxl \\
        --sample-size 20 --source-pool candidate_layer \\
        --reviewer operator@example.com --reason "weekly A/B"

    # explicit case_ids
    python -m backend.scripts.comfyui_ab_runner \\
        --workflow-a a --workflow-b b --sample-size 5 \\
        --source-pool case_ids --case-ids 1,2,3,4,5 \\
        --reviewer ops@x.com
"""
from __future__ import annotations

import argparse
import json
import os

import requests

DEFAULT_BASE_URL = os.environ.get("CASE_WORKBENCH_API_BASE", "http://127.0.0.1:8000")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workflow-a", required=True)
    p.add_argument("--workflow-b", required=True)
    p.add_argument("--sample-size", type=int, required=True)
    p.add_argument("--source-pool", default="recent_done",
                   choices=["recent_done", "candidate_layer", "case_ids"])
    p.add_argument("--case-ids", default=None,
                   help="(source_pool=case_ids only) comma-separated list")
    p.add_argument("--reviewer", required=True)
    p.add_argument("--reason", default=None)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--request-id", default=None)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    payload: dict = {
        "workflow_a": args.workflow_a,
        "workflow_b": args.workflow_b,
        "sample_size": args.sample_size,
        "source_pool": args.source_pool,
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
        f"{args.base_url}/api/render/ops/ab-sample",
        json=payload,
        headers=headers,
        timeout=60,
    )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text
    print(json.dumps({"http_status": resp.status_code, "body": body}, ensure_ascii=False, indent=2))
    return 0 if resp.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
