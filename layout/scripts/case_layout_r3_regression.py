#!/usr/bin/env python3
"""Fixed r3 regression helpers for case-layout-classify.

This script replaces one-off inline Python used during P12. It builds the r3
baseline from real classify outputs and compares live runs against that
baseline.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_RUN = Path(
    "/private/tmp/case-layout-classify-p9-cache-regression-20260426/20260426T152222_0800"
)
DEFAULT_BASELINE_OUT = Path("/private/tmp/case-layout-classify-r3-baseline-20260426")
DEFAULT_BASELINE_PATH = DEFAULT_BASELINE_OUT / "classify-summary-r3.json"

CASE_LEVEL_FIELDS = (
    "bucket",
    "primary_category",
    "reason_group",
    "recommended_action",
)

EXPECTED_R3 = {
    "case_count": 119,
    "image_count": 1480,
    "bucket_counts": {
        "manual-curation": 104,
        "ready_tri_compare": 5,
        "ready_single_compare": 4,
        "ready_bi_compare": 5,
        "ready_body_dual_compare": 1,
    },
    "category_counts": {
        "manual-curation": 50,
        "ready_tri_compare": 5,
        "other_blocked": 45,
        "ready_single_compare": 4,
        "ready_bi_compare": 5,
        "front_only": 2,
        "no_labeled_sources": 1,
        "missing_nonfront": 5,
        "ambiguous_candidates": 1,
        "ready_body_dual_compare": 1,
    },
    "reason_group_counts": {
        "body_case": 18,
        "manual_curation": 37,
        "ready_tri_compare": 5,
        "missing_front": 33,
        "inspect_blocked": 16,
        "ready_single_compare": 4,
        "ready_bi_compare": 5,
        "ready_body_dual_compare": 1,
    },
    "action_counts": {
        "organize": 60,
        "pick": 15,
        "body_followup": 4,
        "reselect_pair": 2,
        "reshoot_front": 22,
        "reshoot_quality": 8,
        "review_candidates": 4,
        "reshoot_nonfront": 4,
    },
}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_classify_module():
    module_path = SCRIPT_DIR / "case_layout_classify.py"
    spec = importlib.util.spec_from_file_location("case_layout_classify_r3", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 classify 模块: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_run_payload(run_dir: Path) -> tuple[dict, list[dict]]:
    summary_path = run_dir / "classify-summary.json"
    images_path = run_dir / "classify-images.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"缺少 classify-summary.json: {summary_path}")
    if not images_path.exists():
        raise FileNotFoundError(f"缺少 classify-images.json: {images_path}")
    summary = load_json(summary_path)
    images_payload = load_json(images_path)
    images = images_payload if isinstance(images_payload, list) else images_payload.get("records", [])
    if not isinstance(images, list):
        raise ValueError(f"classify-images.json 格式不支持: {images_path}")
    return summary, images


def records_by_case_dir(records: list[dict]) -> dict[str, dict]:
    return {record.get("case_dir"): record for record in records}


def images_by_case_dir(images: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for image in images:
        grouped.setdefault(image.get("case_dir"), []).append(image)
    return grouped


def count_records(records: list[dict], key: str) -> dict[str, int]:
    return dict(Counter(record.get(key) for record in records))


def build_count_payload(records: list[dict], image_count: int) -> dict:
    return {
        "case_count": len(records),
        "image_count": image_count,
        "bucket_counts": count_records(records, "bucket"),
        "category_counts": count_records(records, "primary_category"),
        "reason_group_counts": count_records(records, "reason_group"),
        "action_counts": count_records(records, "recommended_action"),
    }


def diff_expected_counts(actual: dict, expected: dict = EXPECTED_R3) -> dict:
    diffs = {}
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if actual_value != expected_value:
            diffs[key] = {
                "expected": expected_value,
                "actual": actual_value,
            }
    return diffs


def enrich_baseline_records(summary: dict, images: list[dict]) -> tuple[list[dict], list[dict]]:
    classify = load_classify_module()
    grouped_images = images_by_case_dir(images)
    enriched_records = []
    changed_records = []
    for record in summary.get("records", []):
        before = {field: record.get(field) for field in CASE_LEVEL_FIELDS}
        case_images = grouped_images.get(record.get("case_dir"), [])
        enriched = classify.enrich_case_record(copy.deepcopy(record), copy.deepcopy(case_images))
        after = {field: enriched.get(field) for field in CASE_LEVEL_FIELDS}
        if before != after:
            changed_records.append(
                {
                    "customer": enriched.get("customer"),
                    "case_name": enriched.get("case_name"),
                    "case_dir": enriched.get("case_dir"),
                    "before": before,
                    "after": after,
                }
            )
        enriched_records.append(enriched)
    return enriched_records, changed_records


def build_baseline(source_run: Path, out_dir: Path, check_expected: bool = True) -> dict:
    source_run = source_run.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    summary, images = load_run_payload(source_run)
    records, changed_records = enrich_baseline_records(summary, images)
    counts = build_count_payload(records, len(images))
    expected_diff = diff_expected_counts(counts) if check_expected else {}

    classify = load_classify_module()
    baseline_summary = copy.deepcopy(summary)
    baseline_summary["created_at"] = classify.CASE_LAYOUT.now_iso()
    baseline_summary["baseline_version"] = "r3"
    baseline_summary["baseline_note"] = (
        "P11 body subject threshold applied by current enrich_case_record over real classify outputs."
    )
    baseline_summary["source_run_dir"] = str(source_run)
    baseline_summary["out_dir"] = str(out_dir.resolve())
    baseline_summary["records"] = records
    baseline_summary.update(counts)
    baseline_summary["changed_records_from_source"] = changed_records

    counts_payload = {
        "baseline_version": "r3",
        "source_run_dir": str(source_run),
        **counts,
        "changed_record_count": len(changed_records),
        "changed_records_from_source": changed_records,
        "expected_counts": EXPECTED_R3,
        "expected_diff": expected_diff,
        "expected_match": not expected_diff,
    }

    summary_path = out_dir / "classify-summary-r3.json"
    counts_path = out_dir / "r3-counts.json"
    images_path = out_dir / "classify-images-r3-source.json"
    write_json(summary_path, baseline_summary)
    write_json(counts_path, counts_payload)
    write_json(images_path, images)

    result = {
        "ok": not expected_diff,
        "summary_path": str(summary_path.resolve()),
        "counts_path": str(counts_path.resolve()),
        "images_path": str(images_path.resolve()),
        **counts,
        "changed_record_count": len(changed_records),
        "expected_match": not expected_diff,
        "expected_diff": expected_diff,
    }
    return result


def load_baseline_payload(baseline_path: Path) -> tuple[dict, list[dict]]:
    if not baseline_path.exists():
        raise FileNotFoundError(f"缺少 r3 baseline: {baseline_path}")
    baseline = load_json(baseline_path)
    records = baseline.get("records", [])
    if not isinstance(records, list):
        raise ValueError(f"baseline records 格式不支持: {baseline_path}")
    return baseline, records


def diff_case_records(baseline_records: list[dict], live_records: list[dict]) -> list[dict]:
    baseline_by_dir = records_by_case_dir(baseline_records)
    live_by_dir = records_by_case_dir(live_records)
    diffs = []
    for case_dir in sorted(set(baseline_by_dir) | set(live_by_dir)):
        baseline = baseline_by_dir.get(case_dir)
        live = live_by_dir.get(case_dir)
        if baseline is None or live is None:
            diffs.append(
                {
                    "case_dir": case_dir,
                    "baseline_present": baseline is not None,
                    "live_present": live is not None,
                }
            )
            continue
        field_diff = {}
        for field in CASE_LEVEL_FIELDS:
            if baseline.get(field) != live.get(field):
                field_diff[field] = {
                    "baseline": baseline.get(field),
                    "live": live.get(field),
                }
        if field_diff:
            diffs.append(
                {
                    "customer": live.get("customer") or baseline.get("customer"),
                    "case_name": live.get("case_name") or baseline.get("case_name"),
                    "case_dir": case_dir,
                    "diff": field_diff,
                }
            )
    return diffs


def summarize_timing(timing: dict) -> dict:
    hit = int(timing.get("screen_cache_hit_count") or 0)
    miss = int(timing.get("screen_cache_miss_count") or 0)
    return {
        "total_ms": timing.get("total_ms"),
        "case_discovery_ms": timing.get("case_discovery_ms"),
        "case_processing_ms": timing.get("case_processing_ms"),
        "screen_total_ms": timing.get("screen_total_ms"),
        "screen_call_total_ms": timing.get("screen_call_total_ms"),
        "screen_cache_enabled": timing.get("screen_cache_enabled"),
        "screen_cache_hit_count": hit,
        "screen_cache_miss_count": miss,
        "screen_cache_save_count": timing.get("screen_cache_save_count"),
        "screen_cache_error_count": timing.get("screen_cache_error_count"),
        "screen_cache_lookup_ms": timing.get("screen_cache_lookup_ms"),
        "cache_hit_rate": (hit / (hit + miss)) if (hit + miss) else None,
    }


def compare_runs(baseline_path: Path, live_run: Path, out_path: Path | None, check_expected: bool = True) -> dict:
    baseline, baseline_records = load_baseline_payload(baseline_path)
    live_summary, live_images = load_run_payload(live_run)
    live_records = live_summary.get("records", [])
    if not isinstance(live_records, list):
        raise ValueError(f"live records 格式不支持: {live_run}")

    baseline_counts = build_count_payload(baseline_records, int(baseline.get("image_count") or 0))
    live_counts = build_count_payload(live_records, len(live_images))
    case_diffs = diff_case_records(baseline_records, live_records)

    compare = {
        "baseline_summary_path": str(baseline_path.resolve()),
        "live_summary_path": str((live_run / "classify-summary.json").resolve()),
        "live_images_path": str((live_run / "classify-images.json").resolve()),
        "case_count": {
            "baseline": baseline_counts["case_count"],
            "live": live_counts["case_count"],
            "match": baseline_counts["case_count"] == live_counts["case_count"],
        },
        "image_count": {
            "baseline": baseline_counts["image_count"],
            "live": live_counts["image_count"],
            "match": baseline_counts["image_count"] == live_counts["image_count"],
        },
        "bucket_counts": {
            "baseline": baseline_counts["bucket_counts"],
            "live": live_counts["bucket_counts"],
            "match": baseline_counts["bucket_counts"] == live_counts["bucket_counts"],
        },
        "category_counts": {
            "baseline": baseline_counts["category_counts"],
            "live": live_counts["category_counts"],
            "match": baseline_counts["category_counts"] == live_counts["category_counts"],
        },
        "reason_group_counts": {
            "baseline": baseline_counts["reason_group_counts"],
            "live": live_counts["reason_group_counts"],
            "match": baseline_counts["reason_group_counts"] == live_counts["reason_group_counts"],
        },
        "action_counts": {
            "baseline": baseline_counts["action_counts"],
            "live": live_counts["action_counts"],
            "match": baseline_counts["action_counts"] == live_counts["action_counts"],
        },
        "case_level_diff_count": len(case_diffs),
        "case_level_diffs": case_diffs,
        "timing": live_summary.get("timing", {}),
        "timing_summary": summarize_timing(live_summary.get("timing", {})),
    }
    count_keys = (
        "case_count",
        "image_count",
        "bucket_counts",
        "category_counts",
        "reason_group_counts",
        "action_counts",
    )
    compare["all_counts_match"] = all(compare[key]["match"] for key in count_keys)
    compare["case_level_match"] = len(case_diffs) == 0

    if check_expected:
        compare["baseline_expected_diff"] = diff_expected_counts(baseline_counts)
        compare["live_expected_diff"] = diff_expected_counts(live_counts)
    else:
        compare["baseline_expected_diff"] = {}
        compare["live_expected_diff"] = {}
    compare["expected_match"] = (
        not compare["baseline_expected_diff"] and not compare["live_expected_diff"]
    )
    compare["ok"] = (
        compare["all_counts_match"]
        and compare["case_level_match"]
        and compare["expected_match"]
    )

    if out_path is None:
        out_path = live_run / "r3-baseline-compare.json"
    write_json(out_path, compare)
    compare["compare_path"] = str(out_path.resolve())
    write_json(out_path, compare)
    return compare


def compact_compare_result(result: dict) -> dict:
    return {
        "ok": result.get("ok"),
        "compare_path": result.get("compare_path"),
        "all_counts_match": result.get("all_counts_match"),
        "case_level_match": result.get("case_level_match"),
        "case_level_diff_count": result.get("case_level_diff_count"),
        "expected_match": result.get("expected_match"),
        "baseline_expected_diff": result.get("baseline_expected_diff"),
        "live_expected_diff": result.get("live_expected_diff"),
        "case_count": result.get("case_count"),
        "image_count": result.get("image_count"),
        "bucket_counts": result.get("bucket_counts"),
        "category_counts": result.get("category_counts"),
        "reason_group_counts": result.get("reason_group_counts"),
        "action_counts": result.get("action_counts"),
        "timing_summary": result.get("timing_summary"),
    }


def add_common_expected_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-check-expected",
        action="store_true",
        help="跳过 P12 固化的 r3 expected counts 校验。",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and compare the fixed r3 case-layout-classify regression baseline."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    baseline_parser = subparsers.add_parser("baseline", help="生成 r3 baseline artifact")
    baseline_parser.add_argument("--source-run", type=Path, default=DEFAULT_SOURCE_RUN)
    baseline_parser.add_argument("--out", type=Path, default=DEFAULT_BASELINE_OUT)
    add_common_expected_flag(baseline_parser)

    compare_parser = subparsers.add_parser("compare", help="比较 live run 与 r3 baseline")
    compare_parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE_PATH)
    compare_parser.add_argument("--live-run", type=Path, required=True)
    compare_parser.add_argument("--out", type=Path)
    add_common_expected_flag(compare_parser)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "baseline":
            result = build_baseline(
                source_run=args.source_run,
                out_dir=args.out,
                check_expected=not args.no_check_expected,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 2
        if args.command == "compare":
            result = compare_runs(
                baseline_path=args.baseline,
                live_run=args.live_run,
                out_path=args.out,
                check_expected=not args.no_check_expected,
            )
            print(json.dumps(compact_compare_result(result), ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
