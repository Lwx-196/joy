"""Build a T59.3 repair queue from real done_with_issues quality warnings.

This script is read-only against the live DB. It reads the real T59 matrix and
turns current formal render warnings into an operator-facing repair queue.
It never marks done_with_issues as publishable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/Users/a1234/Desktop/案例生成器/case-workbench")
DEFAULT_INPUT = ROOT / "tasks" / "t59_formal_render_quality_matrix.json"
DEFAULT_JSON_OUTPUT = ROOT / "tasks" / "t593_quality_warning_repair_queue.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "tasks" / "t593_quality_warning_repair_queue.md"
UNVERIFIED = "未验证/无法获取"

CATEGORY_PRIORITY = {
    "hard_blocker_in_done_with_issues": 10,
    "material_loop_required": 20,
    "composition_review": 30,
    "template_downgrade_review": 40,
    "semantic_judge_unavailable": 50,
    "manual_quality_review": 60,
    "unknown_warning_review": 90,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _source_hash(path: Path | None) -> str | None:
    if not path:
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _record_metrics(record: dict[str, Any]) -> dict[str, Any]:
    quality = record.get("quality") if isinstance(record.get("quality"), dict) else {}
    metrics = quality.get("metrics") if isinstance(quality.get("metrics"), dict) else {}
    return metrics if isinstance(metrics, dict) else {}


def _quality_payload(record: dict[str, Any]) -> dict[str, Any]:
    quality = record.get("quality") if isinstance(record.get("quality"), dict) else {}
    return quality if isinstance(quality, dict) else {}


def _artifact_payload(record: dict[str, Any]) -> dict[str, Any]:
    artifact = record.get("artifact_integrity") if isinstance(record.get("artifact_integrity"), dict) else {}
    return artifact if isinstance(artifact, dict) else {}


def _envelope_payload(record: dict[str, Any]) -> dict[str, Any]:
    envelope = record.get("delivery_envelope") if isinstance(record.get("delivery_envelope"), dict) else {}
    return envelope if isinstance(envelope, dict) else {}


def _suggestion_code(suggestion: Any) -> str:
    if isinstance(suggestion, dict):
        return str(suggestion.get("code") or "")
    return ""


def _suggestion_source(suggestion: Any) -> str:
    if isinstance(suggestion, dict):
        return str(suggestion.get("source") or "")
    return ""


def _suggestion_blocks_publish(suggestion: Any) -> bool:
    if not isinstance(suggestion, dict):
        return False
    gate = suggestion.get("publish_gate") if isinstance(suggestion.get("publish_gate"), dict) else {}
    return gate.get("can_publish_after_acceptance") is False or gate.get("requires_new_material_for_publish") is True


def _warning_texts(record: dict[str, Any]) -> list[str]:
    metrics = _record_metrics(record)
    texts: list[str] = []
    for key in ("warnings", "display_warnings", "audit_warnings"):
        for item in _as_list(metrics.get(key)):
            if item:
                texts.append(str(item))
    return list(dict.fromkeys(texts))


def _blocking_texts(record: dict[str, Any]) -> list[str]:
    metrics = _record_metrics(record)
    texts: list[str] = []
    for item in _as_list(metrics.get("blocking_issues")):
        if item:
            texts.append(str(item))
    for item in _as_list(record.get("hard_blockers")):
        text = str(item or "")
        if not text:
            continue
        if text == "render_quality.quality_status:done_with_issues":
            continue
        texts.append(text)
    return list(dict.fromkeys(texts))


def _classify_categories(record: dict[str, Any]) -> list[str]:
    metrics = _record_metrics(record)
    warnings = _warning_texts(record)
    blockers = _blocking_texts(record)
    suggestions = _as_list(metrics.get("action_suggestions"))
    composition_alerts = _as_list(metrics.get("composition_alerts"))
    joined = "\n".join([*warnings, *blockers])
    categories: set[str] = set()

    if blockers or "render_quality.blocking_count" in "\n".join(_as_list(_envelope_payload(record).get("reasons"))):
        categories.add("hard_blocker_in_done_with_issues")
    if any(_suggestion_blocks_publish(item) for item in suggestions):
        categories.add("material_loop_required")
    if any(_suggestion_source(item) in {"material_loop", "pair_quality"} for item in suggestions):
        categories.add("material_loop_required")
    if any(_suggestion_code(item) in {"reselect_pair"} or _suggestion_code(item).startswith("add_") for item in suggestions):
        categories.add("material_loop_required")
    if composition_alerts or any(_suggestion_code(item) == "review_composition" for item in suggestions):
        categories.add("composition_review")
    if "自动降级已排除" in joined:
        categories.add("template_downgrade_review")
    if (
        "视觉补判仅供参考" in joined
        or "insufficient_user_quota" in joined
        or "预扣费额度失败" in joined
        or "API 403" in joined
        or "quota" in joined.lower()
    ):
        categories.add("semantic_judge_unavailable")
    if any(_suggestion_code(item) == "manual_quality_review" for item in suggestions):
        categories.add("manual_quality_review")
    if not categories:
        categories.add("unknown_warning_review")
    return sorted(categories, key=lambda item: CATEGORY_PRIORITY.get(item, 90))


def _recommended_next_step(categories: list[str]) -> str:
    if "hard_blocker_in_done_with_issues" in categories:
        return "先回到源图/配对层修复硬阻断；该 job 不能通过 warning review 放行。"
    if "material_loop_required" in categories:
        return "回到源组候选重选或补拍素材，再复跑正式 render。"
    if "composition_review" in categories:
        return "人工复核 final-board 构图和对应槽位，必要时换片后复跑。"
    if "template_downgrade_review" in categories:
        return "复核自动降级是否符合交付模板预期；不满足则补齐槽位。"
    if "semantic_judge_unavailable" in categories:
        return "恢复视觉补判 provider/key/quota 或走人工复核；未复核前保持不可发布。"
    if "manual_quality_review" in categories:
        return "人工打开 final-board 和 manifest 复核 warning 后再决定修复动作。"
    return "人工查看 warning 原文后归类处理。"


def _requires_new_material_or_reselect(categories: list[str]) -> bool:
    return bool({"hard_blocker_in_done_with_issues", "material_loop_required"} & set(categories))


def _build_action_item(record: dict[str, Any]) -> dict[str, Any]:
    case = record.get("case") if isinstance(record.get("case"), dict) else {}
    quality = _quality_payload(record)
    metrics = _record_metrics(record)
    artifact = _artifact_payload(record)
    envelope = _envelope_payload(record)
    categories = _classify_categories(record)
    primary_category = categories[0]
    warnings = _warning_texts(record)
    blockers = _blocking_texts(record)
    suggestions = [item for item in _as_list(metrics.get("action_suggestions")) if isinstance(item, dict)]
    final_board = artifact.get("final_board") if isinstance(artifact.get("final_board"), dict) else {}
    manifest = artifact.get("manifest") if isinstance(artifact.get("manifest"), dict) else {}
    return {
        "priority": CATEGORY_PRIORITY.get(primary_category, 90),
        "primary_category": primary_category,
        "categories": categories,
        "job_id": int(record.get("job_id") or 0),
        "case_id": int(record.get("case_id") or case.get("case_id") or 0),
        "case_path": case.get("abs_path"),
        "customer_raw": case.get("customer_raw"),
        "job_status": record.get("status"),
        "quality_status": quality.get("quality_status"),
        "quality_score": quality.get("quality_score"),
        "template": record.get("template"),
        "artifact_integrity": artifact.get("status"),
        "final_board_path": final_board.get("path"),
        "manifest_path": manifest.get("path"),
        "warning_count": len(warnings),
        "blocking_issue_count": len(blockers),
        "composition_alert_count": len(_as_list(metrics.get("composition_alerts"))),
        "warning_samples": warnings[:6],
        "blocking_issue_samples": blockers[:6],
        "action_suggestions": suggestions,
        "recommended_next_step": _recommended_next_step(categories),
        "requires_new_material_or_reselect": _requires_new_material_or_reselect(categories),
        "blocks_publish": True,
        "publishable_after_warning_review": False,
        "delivery_envelope_class": envelope.get("class") or "experimental_blocked",
        "delivery_can_deliver": bool(envelope.get("can_deliver")),
        "delivery_reasons": _as_list(envelope.get("reasons"))[:8],
    }


def _counter(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _case_rollup(action_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for item in action_items:
        grouped.setdefault(int(item.get("case_id") or 0), []).append(item)
    out: list[dict[str, Any]] = []
    for case_id, items in sorted(grouped.items()):
        first = items[0]
        out.append(
            {
                "case_id": case_id,
                "case_path": first.get("case_path"),
                "customer_raw": first.get("customer_raw"),
                "job_ids": [int(item.get("job_id") or 0) for item in items],
                "primary_categories": sorted({str(item.get("primary_category")) for item in items}),
                "categories": sorted({str(category) for item in items for category in _as_list(item.get("categories"))}),
                "requires_new_material_or_reselect": any(
                    bool(item.get("requires_new_material_or_reselect")) for item in items
                ),
                "blocks_publish": any(bool(item.get("blocks_publish")) for item in items),
            }
        )
    return out


def build_warning_queue(
    matrix_report: dict[str, Any],
    *,
    source_report_path: Path | None = None,
) -> dict[str, Any]:
    records = [
        item for item in _as_list(matrix_report.get("records"))
        if isinstance(item, dict) and str(item.get("status") or "") == "done_with_issues"
    ]
    if not isinstance(matrix_report, dict) or not _as_list(matrix_report.get("records")):
        return blocked_report("T59 matrix report has no real records")
    action_items = [_build_action_item(record) for record in records]
    action_items.sort(key=lambda item: (int(item["priority"]), int(item["case_id"]), int(item["job_id"])))
    all_categories = [str(category) for item in action_items for category in _as_list(item.get("categories"))]
    primary_categories = [str(item.get("primary_category")) for item in action_items]
    return {
        "generated_at": _now(),
        "run_status": "completed_real_warning_repair_queue",
        "used_mock_data": False,
        "decision": "已从真实 T59 done_with_issues 生成质量 warning 修复清单；不自动放行，formal_ready 仍以 delivery_envelope/formal gate 为准。",
        "policy": {
            "source": "T59 real formal render matrix",
            "source_report_path": str(source_report_path) if source_report_path else None,
            "source_report_sha256": _source_hash(source_report_path),
            "does_not_modify_db": True,
            "does_not_enqueue_render": True,
            "done_with_issues_is_not_publishable": True,
        },
        "source_matrix": {
            "run_status": matrix_report.get("run_status"),
            "selected_case_count": (matrix_report.get("summary") or {}).get("selected_case_count"),
            "record_count": (matrix_report.get("summary") or {}).get("record_count"),
            "by_job_status": (matrix_report.get("summary") or {}).get("by_job_status"),
            "formal_ready_count": (matrix_report.get("summary") or {}).get("formal_ready_count"),
        },
        "summary": {
            "source_done_with_issues_count": len(records),
            "action_item_count": len(action_items),
            "blocks_publish_count": sum(1 for item in action_items if item.get("blocks_publish")),
            "requires_new_material_or_reselect_count": sum(
                1 for item in action_items if item.get("requires_new_material_or_reselect")
            ),
            "semantic_judge_unavailable_count": sum(
                1 for item in action_items if "semantic_judge_unavailable" in _as_list(item.get("categories"))
            ),
            "manual_review_count": sum(
                1 for item in action_items if "manual_quality_review" in _as_list(item.get("categories"))
            ),
            "by_category": _counter(all_categories),
            "by_primary_category": _counter(primary_categories),
        },
        "action_items": action_items,
        "case_rollup": _case_rollup(action_items),
    }


def blocked_report(reason: str) -> dict[str, Any]:
    return {
        "generated_at": _now(),
        "run_status": "blocked_missing_real_t59_matrix",
        "used_mock_data": False,
        "decision": f"{UNVERIFIED}: {reason}",
        "policy": {
            "source": "T59 real formal render matrix",
            "does_not_modify_db": True,
            "does_not_enqueue_render": True,
            "done_with_issues_is_not_publishable": True,
        },
        "summary": {
            "source_done_with_issues_count": 0,
            "action_item_count": 0,
            "blocks_publish_count": 0,
            "requires_new_material_or_reselect_count": 0,
            "semantic_judge_unavailable_count": 0,
            "manual_review_count": 0,
            "by_category": {},
            "by_primary_category": {},
        },
        "action_items": [],
        "case_rollup": [],
        "blocked_reason": reason,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    lines = [
        "# T59.3 done_with_issues 质量 warning 修复清单",
        "",
        f"- run_status: `{report.get('run_status')}`",
        f"- decision: {report.get('decision')}",
        f"- source_done_with_issues_count: {summary.get('source_done_with_issues_count')}",
        f"- action_item_count: {summary.get('action_item_count')}",
        f"- blocks_publish_count: {summary.get('blocks_publish_count')}",
        f"- requires_new_material_or_reselect_count: {summary.get('requires_new_material_or_reselect_count')}",
        f"- semantic_judge_unavailable_count: {summary.get('semantic_judge_unavailable_count')}",
        "",
        "## Categories",
        "",
    ]
    for category, count in (summary.get("by_category") or {}).items():
        lines.append(f"- `{category}`: {count}")
    lines.extend(["", "## Action Items", ""])
    for item in report.get("action_items") or []:
        lines.extend(
            [
                f"### Case {item.get('case_id')} / Job {item.get('job_id')} / `{item.get('primary_category')}`",
                "",
                f"- categories: {', '.join(item.get('categories') or [])}",
                f"- quality_score: {item.get('quality_score')}",
                f"- artifact_integrity: `{item.get('artifact_integrity')}`",
                f"- delivery_envelope_class: `{item.get('delivery_envelope_class')}`",
                f"- blocks_publish: `{item.get('blocks_publish')}`",
                f"- publishable_after_warning_review: `{item.get('publishable_after_warning_review')}`",
                f"- requires_new_material_or_reselect: `{item.get('requires_new_material_or_reselect')}`",
                f"- final_board: {item.get('final_board_path') or '-'}",
                f"- manifest: {item.get('manifest_path') or '-'}",
                f"- next_step: {item.get('recommended_next_step')}",
                "",
            ]
        )
        for warning in (item.get("warning_samples") or [])[:4]:
            lines.append(f"  - warning: {warning}")
        for blocker in (item.get("blocking_issue_samples") or [])[:4]:
            lines.append(f"  - blocker: {blocker}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build T59.3 warning repair queue from a real T59 matrix.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.input.exists():
        report = blocked_report(f"T59 matrix report not found: {args.input}")
    else:
        try:
            matrix_report = _load_json(args.input)
            report = build_warning_queue(matrix_report, source_report_path=args.input)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            report = blocked_report(f"cannot read T59 matrix report: {exc}")
    write_json(args.output, report)
    write_markdown(args.markdown_output, report)
    print(json.dumps({"run_status": report.get("run_status"), "summary": report.get("summary")}, ensure_ascii=False, indent=2))
    return 0 if report.get("run_status") == "completed_real_warning_repair_queue" else 2


if __name__ == "__main__":
    raise SystemExit(main())
