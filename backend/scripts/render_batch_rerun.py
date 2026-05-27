"""Thin wrapper for POST /api/render/ops/batch-rerun.

Replaces the old `force_render_*.py` / `formal_render_repair_execution.py`
scripts. CLI is preserved for backward compatibility with operators; the actual
work goes through the audited ops endpoint (writes ops_audit_log).

Usage:
    python -m backend.scripts.render_batch_rerun \\
        --case-ids 123,456,789 \\
        --scope render \\
        --reviewer operator@example.com \\
        --reason "re-fire after v3 upscale fix"

    # Dry-run scope=simulation returns planned simulation_jobs
    python -m backend.scripts.render_batch_rerun \\
        --case-ids 123,456 --scope simulation --dry-run \\
        --workflow-filter v3-sdxl --reviewer ops@x.com --reason audit
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
                   help="comma-separated case ids, e.g. 1,2,3")
    p.add_argument("--scope", default="render", choices=["render", "simulation"])
    p.add_argument("--workflow-filter", default=None,
                   help="(scope=simulation only) filter planned reruns by workflow name")
    p.add_argument("--dry-run", action="store_true",
                   help="don't actually enqueue; return what would happen")
    p.add_argument("--brand", default="fumei")
    p.add_argument("--template", default="tri-compare")
    p.add_argument("--semantic-judge", default="auto", choices=["off", "auto"])
    p.add_argument("--reviewer", required=True, help="operator email or login (audited)")
    p.add_argument("--reason", default=None, help="reason for the batch (audited)")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--request-id", default=None,
                   help="optional X-Request-Id; server generates if absent")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    case_ids = [int(x.strip()) for x in args.case_ids.split(",") if x.strip()]
    if not case_ids:
        print("error: --case-ids empty after parsing", file=sys.stderr)
        return 2
    payload = {
        "case_ids": case_ids,
        "scope": args.scope,
        "dry_run": args.dry_run,
        "brand": args.brand,
        "template": args.template,
        "semantic_judge": args.semantic_judge,
        "reviewer": args.reviewer,
    }
    if args.workflow_filter:
        payload["workflow_filter"] = args.workflow_filter
    if args.reason:
        payload["reason"] = args.reason
    headers = {}
    if args.request_id:
        headers["X-Request-Id"] = args.request_id
    resp = requests.post(
        f"{args.base_url}/api/render/ops/batch-rerun",
        json=payload,
        headers=headers,
        timeout=60,
    )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text
    print(json.dumps({"http_status": resp.status_code, "body": body}, ensure_ascii=False, indent=2))
    return 0 if resp.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
