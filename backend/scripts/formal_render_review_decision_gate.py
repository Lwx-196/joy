"""Validate T65 formal render human-review decisions and build repair queue.

This script is intentionally non-mutating: it never updates render_quality and
never publishes artifacts. It converts real reviewer decisions into a concrete
repair/import queue.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/Users/a1234/Desktop/案例生成器/case-workbench")
DEFAULT_MANIFEST = ROOT / "tasks" / "t65_formal_review_packet" / "manifest.json"
DEFAULT_DECISIONS = ROOT / "tasks" / "t65_formal_review_packet" / "review_decisions_draft.json"
DEFAULT_VALIDATION_OUTPUT = ROOT / "tasks" / "t65_formal_review_decision_validation.json"
DEFAULT_REPAIR_OUTPUT = ROOT / "tasks" / "t65_formal_review_repair_queue.json"
DEFAULT_REPAIR_MARKDOWN_OUTPUT = ROOT / "tasks" / "t65_formal_review_repair_queue.md"
UNVERIFIED = "未验证/无法获取"
VALID_DECISIONS = {
    "accept_template_downgrade",
    "needs_slot_fill",
    "needs_reselect",
    "needs_rerender",
    "reject",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _decisions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("decisions") or payload.get("review_decisions") or payload.get("items") or []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _manifest_units(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for unit in _as_list(manifest.get("review_units")):
        if not isinstance(unit, dict):
            continue
        unit_id = str(unit.get("unit_id") or "").strip()
        if unit_id:
            out[unit_id] = unit
    return out


def _decision_unit_id(decision: dict[str, Any]) -> str:
    return str(decision.get("unit_id") or decision.get("review_unit_id") or "").strip()


def _reviewer(decision: dict[str, Any]) -> str:
    return str(decision.get("reviewer") or "").strip()


def _decision_value(decision: dict[str, Any]) -> str:
    return str(decision.get("decision") or decision.get("verdict") or "").strip()


def _reject(decision: dict[str, Any], reason_code: str, reason: str) -> dict[str, Any]:
    return {
        "unit_id": _decision_unit_id(decision) or None,
        "case_id": decision.get("case_id"),
        "job_id": decision.get("job_id"),
        "reviewer": _reviewer(decision) or None,
        "decision": _decision_value(decision) or None,
        "reason_code": reason_code,
        "reason": reason,
    }


def _sanitize(decision: dict[str, Any], unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "unit_id": str(unit.get("unit_id") or ""),
        "case_id": int(unit.get("case_id") or decision.get("case_id") or 0),
        "job_id": int(unit.get("job_id") or decision.get("job_id") or 0),
        "reviewer": _reviewer(decision),
        "decision": _decision_value(decision),
        "review_note": decision.get("review_note"),
        "case_path": unit.get("case_path"),
        "customer_raw": unit.get("customer_raw"),
        "template": unit.get("template"),
        "quality_score": unit.get("quality_score"),
        "warning_samples": _as_list(unit.get("warning_samples")),
        "source_final_board_path": unit.get("source_final_board_path"),
        "source_manifest_path": unit.get("source_manifest_path"),
    }


def validate_review_decisions(decision_payload: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    units = _manifest_units(manifest)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for decision in _decisions(decision_payload):
        unit_id = _decision_unit_id(decision)
        if not unit_id:
            rejected.append(_reject(decision, "missing_unit_id", "缺少 unit_id，无法导入人工复核决策。"))
            continue
        if unit_id in seen:
            rejected.append(_reject(decision, "duplicate_unit_id", "同一个 unit_id 只能导入一条人工复核决策。"))
            continue
        unit = units.get(unit_id)
        if not unit:
            rejected.append(_reject(decision, "unknown_unit_id", "unit_id 不在当前 T65 review packet manifest 中。"))
            continue
        if not unit.get("ready_for_review"):
            rejected.append(_reject(decision, "unit_not_review_ready", "该 unit 缺少真实 final-board 或 manifest，不能导入决策。"))
            continue
        if not _reviewer(decision):
            rejected.append(_reject(decision, "missing_reviewer", "缺少 reviewer，不能作为真实人工复核证据。"))
            continue
        value = _decision_value(decision)
        if value not in VALID_DECISIONS:
            rejected.append(_reject(decision, "invalid_decision", f"decision 必须是 {sorted(VALID_DECISIONS)} 之一。"))
            continue
        seen.add(unit_id)
        accepted.append(_sanitize(decision, unit))

    review_ready_count = sum(1 for unit in units.values() if unit.get("ready_for_review"))
    accepted_count = len(accepted)
    if accepted_count == 0:
        status = "unverified_missing_human_decisions"
        decision_text = f"{UNVERIFIED}：当前没有可导入的真实人工复核决策。"
    elif accepted_count < review_ready_count:
        status = "partial_human_decisions_fail_closed"
        decision_text = f"未完成：可导入人工复核决策 {accepted_count} < 待复核 unit {review_ready_count}，保持 fail-closed。"
    elif rejected:
        status = "rejected_decisions_need_fix"
        decision_text = "未完成：存在被拒绝的人工复核决策，保持 fail-closed。"
    else:
        status = "ready_for_repair_queue"
        decision_text = "T65 人工复核决策已覆盖全部待复核 unit，可生成反向修复清单；仍不会自动发布。"

    return {
        "generated_at": _now(),
        "scope": "t65_formal_render_review_decision_validation_v1",
        "validation_status": status,
        "ready_for_repair_queue": status == "ready_for_repair_queue",
        "decision": decision_text,
        "manifest_review_unit_count": len(units),
        "manifest_ready_review_unit_count": review_ready_count,
        "submitted_decision_count": len(_decisions(decision_payload)),
        "accepted_decision_count": accepted_count,
        "rejected_decision_count": len(rejected),
        "accepted_decisions": accepted,
        "rejected_decisions": rejected,
        "used_mock_data": False,
    }


def _recommended_action(decision: str) -> tuple[str, bool]:
    if decision == "accept_template_downgrade":
        return "quality_review_accept_template_downgrade", False
    if decision == "needs_slot_fill":
        return "fill_missing_slots", True
    if decision == "needs_reselect":
        return "reselect_source_pair", True
    if decision == "needs_rerender":
        return "rerun_formal_render", True
    return "reject_artifact", False


def build_repair_queue(validation: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    accepted = [item for item in _as_list(validation.get("accepted_decisions")) if isinstance(item, dict)]
    if not accepted:
        return {
            "generated_at": _now(),
            "scope": "t65_formal_render_review_repair_queue_v1",
            "run_status": "blocked_missing_real_human_review_decisions",
            "decision": f"{UNVERIFIED}：没有真实人工复核决策，不能生成可执行修复/放行清单。",
            "used_mock_data": False,
            "summary": {
                "accepted_decision_count": 0,
                "repair_item_count": 0,
                "requires_rerun_count": 0,
                "accept_template_downgrade_count": 0,
                "can_publish_now_count": 0,
            },
            "repair_items": [],
            "pending_units": list(_manifest_units(manifest)),
        }

    repair_items: list[dict[str, Any]] = []
    for item in accepted:
        decision = str(item.get("decision") or "")
        action, requires_rerun = _recommended_action(decision)
        repair_items.append(
            {
                "unit_id": item.get("unit_id"),
                "case_id": item.get("case_id"),
                "job_id": item.get("job_id"),
                "reviewer": item.get("reviewer"),
                "decision": decision,
                "recommended_action": action,
                "requires_rerun": requires_rerun,
                "can_publish_now": False,
                "ready_for_quality_review_import": decision == "accept_template_downgrade",
                "review_note": item.get("review_note"),
                "warning_samples": _as_list(item.get("warning_samples")),
                "source_final_board_path": item.get("source_final_board_path"),
                "source_manifest_path": item.get("source_manifest_path"),
            }
        )
    counts = Counter(str(item.get("decision") or "") for item in repair_items)
    return {
        "generated_at": _now(),
        "scope": "t65_formal_render_review_repair_queue_v1",
        "run_status": "completed_real_human_review_repair_queue",
        "decision": "已从真实人工复核决策生成反向修复清单；本报告不自动发布，不自动改库。",
        "used_mock_data": False,
        "validation_status": validation.get("validation_status"),
        "ready_for_repair_queue": bool(validation.get("ready_for_repair_queue")),
        "summary": {
            "accepted_decision_count": len(accepted),
            "repair_item_count": len(repair_items),
            "requires_rerun_count": sum(1 for item in repair_items if item.get("requires_rerun")),
            "accept_template_downgrade_count": counts.get("accept_template_downgrade", 0),
            "needs_slot_fill_count": counts.get("needs_slot_fill", 0),
            "needs_reselect_count": counts.get("needs_reselect", 0),
            "needs_rerender_count": counts.get("needs_rerender", 0),
            "reject_count": counts.get("reject", 0),
            "can_publish_now_count": 0,
        },
        "repair_items": repair_items,
        "pending_units": [
            unit_id
            for unit_id in _manifest_units(manifest)
            if unit_id not in {str(item.get("unit_id") or "") for item in accepted}
        ],
    }


def render_repair_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# T65 Formal Render Review Repair Queue",
        "",
        f"- run_status: `{report.get('run_status')}`",
        f"- decision: {report.get('decision')}",
    ]
    for key, value in (report.get("summary") or {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.append("")
    if report.get("repair_items"):
        lines.append("## Repair Items")
        lines.append("")
        for item in report.get("repair_items") or []:
            lines.extend(
                [
                    f"### Case {item.get('case_id')} / Job {item.get('job_id')} / `{item.get('decision')}`",
                    "",
                    f"- reviewer: `{item.get('reviewer')}`",
                    f"- recommended_action: `{item.get('recommended_action')}`",
                    f"- requires_rerun: `{item.get('requires_rerun')}`",
                    f"- can_publish_now: `{item.get('can_publish_now')}`",
                    f"- review_note: {item.get('review_note') or ''}",
                    "",
                ]
            )
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate T65 human decisions and build repair queue.")
    parser.add_argument("--manifest-json", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--review-decisions-json", type=Path, default=DEFAULT_DECISIONS)
    parser.add_argument("--validation-output", type=Path, default=DEFAULT_VALIDATION_OUTPUT)
    parser.add_argument("--repair-output", type=Path, default=DEFAULT_REPAIR_OUTPUT)
    parser.add_argument("--repair-markdown-output", type=Path, default=DEFAULT_REPAIR_MARKDOWN_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    manifest = _load_json(args.manifest_json)
    validation = validate_review_decisions(_load_json(args.review_decisions_json), manifest)
    repair = build_repair_queue(validation, manifest)
    _write_json(args.validation_output, validation)
    _write_json(args.repair_output, repair)
    args.repair_markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.repair_markdown_output.write_text(render_repair_markdown(repair), encoding="utf-8")
    print(json.dumps({"validation": validation, "repair_summary": repair.get("summary")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
