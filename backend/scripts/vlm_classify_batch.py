"""CLI for VLM source image classification.

三态 mode（互斥）：
- --dry-run        只列队列，不调模型（default，最安全）
- --live-no-apply  真实调模型 + 写 vlm_usage_log + 不写 image_observations
- --apply          真实调模型 + 写 vlm_usage_log + 写 image_observations（满足阈值）
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from backend import db
from backend.services.vlm_provider import VLMProvider
from backend.services.vlm_source_classifier import run_classification


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case-id", type=int)
    parser.add_argument("--all-low-confidence", action="store_true")
    parser.add_argument("--max-items", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--dry-run", action="store_true", help="只列队列，不调模型")
    mode_group.add_argument("--live-no-apply", action="store_true", help="调模型记 usage 但不写 image_observations")
    mode_group.add_argument("--apply", action="store_true", help="调模型 + 满足阈值写 image_observations")
    parser.add_argument("--output-json", type=Path)
    return parser


def _resolve_mode(args: argparse.Namespace) -> str:
    if args.apply:
        return "apply"
    if args.live_no_apply:
        return "live-no-apply"
    return "dry-run"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode = _resolve_mode(args)
    provider: VLMProvider | None = VLMProvider(env=dict(os.environ)) if mode != "dry-run" else None
    with db.connect() as conn:
        report = run_classification(
            conn,
            provider=provider,
            case_id=args.case_id,
            all_low_confidence=bool(args.all_low_confidence),
            max_items=args.max_items,
            mode=mode,
            concurrency=args.concurrency,
            timeout=args.timeout_seconds,
        )
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
