"""Build a repair queue from the real T59 formal render matrix.

This script does not enqueue renders or fabricate records. It turns the latest
real formal render blockers into an actionable cleanup queue for classification,
manual override traceability, source slot completeness, and quality review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/Users/a1234/Desktop/案例生成器/case-workbench")
DEFAULT_DB_PATH = ROOT / "case-workbench.db"
DEFAULT_INPUT = ROOT / "tasks" / "t59_formal_render_quality_matrix.json"
DEFAULT_JSON_OUTPUT = ROOT / "tasks" / "t591_formal_render_repair_queue.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "tasks" / "t591_formal_render_repair_queue.md"
UNVERIFIED = "未验证/无法获取"

CATEGORY_PRIORITY = {
    "manual_override_trace": 10,
    "classification_closure": 20,
    "slot_completeness": 30,
    "quality_review": 40,
    "unknown_blocker": 90,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _source_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _record_metrics(record: dict[str, Any]) -> dict[str, Any]:
    quality = record.get("quality") if isinstance(record.get("quality"), dict) else {}
    metrics = quality.get("metrics") if isinstance(quality.get("metrics"), dict) else {}
    return metrics if isinstance(metrics, dict) else {}


def _texts_for_record(record: dict[str, Any]) -> list[str]:
    metrics = _record_metrics(record)
    texts: list[str] = []
    for key in ("error_message",):
        value = record.get(key)
        if value:
            texts.append(str(value))
    for key in ("blocking_issues", "warnings", "display_warnings"):
        for item in _as_list(metrics.get(key)):
            if item:
                texts.append(str(item))
    for item in _as_list(record.get("hard_blockers")):
        if item:
            texts.append(str(item))
    return texts


def _extract_files_to_review(texts: list[str]) -> list[str]:
    files: list[str] = []
    marker = "未闭环图片："
    for text in texts:
        if marker not in text:
            continue
        filename = text.split(marker, 1)[1].strip()
        if filename:
            files.append(filename)
    return list(dict.fromkeys(files))


def _extract_missing_slots(texts: list[str]) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    marker = "缺槽位："
    for text in texts:
        if marker not in text:
            continue
        value = text.split(marker, 1)[1].strip()
        if not value:
            continue
        if " " in value:
            view, phases_raw = value.split(" ", 1)
        else:
            view, phases_raw = value, ""
        phases = [
            item.strip()
            for item in phases_raw.replace("/", ",").split(",")
            if item.strip()
        ]
        key = (view.strip(), tuple(phases))
        if key in seen:
            continue
        seen.add(key)
        slots.append({"view": key[0], "phases": phases})
    return slots


def _override_trace_gaps(
    *,
    db_path: Path | None,
    case_id: int,
    files_to_review: list[str],
) -> tuple[list[dict[str, Any]], str]:
    if not db_path:
        return [], "not_requested"
    if not db_path.exists():
        return [], "db_missing"
    if not files_to_review:
        return [], "no_files_to_review"
    placeholders = ",".join("?" * len(files_to_review))
    sql = f"""
        SELECT filename, manual_phase, manual_view, reason_json, reviewer
        FROM case_image_overrides
        WHERE case_id = ? AND filename IN ({placeholders})
        ORDER BY filename
    """
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (case_id, *files_to_review)).fetchall()
    except sqlite3.Error:
        return [], "query_failed"
    finally:
        try:
            conn.close()
        except Exception:
            pass

    gaps: list[dict[str, Any]] = []
    for row in rows:
        missing: list[str] = []
        if not str(row["reason_json"] or "").strip():
            missing.append("reason_json")
        if not str(row["reviewer"] or "").strip():
            missing.append("reviewer")
        if missing:
            gaps.append(
                {
                    "filename": row["filename"],
                    "manual_phase": row["manual_phase"],
                    "manual_view": row["manual_view"],
                    "missing": missing,
                }
            )
    return gaps, "ok"


def _category_for_record(
    record: dict[str, Any],
    texts: list[str],
    missing_slots: list[dict[str, Any]],
    override_trace_gaps: list[dict[str, Any]],
) -> str:
    joined = "\n".join(texts)
    if override_trace_gaps or "人工覆盖缺少原因" in joined:
        return "manual_override_trace"
    if missing_slots or "槽位未配齐" in joined:
        return "slot_completeness"
    if "未闭环" in joined or "低置信" in joined or "待补充" in joined:
        return "classification_closure"
    if str(record.get("status") or "") == "done_with_issues":
        return "quality_review"
    return "unknown_blocker"


def _recommended_next_step(category: str) -> str:
    if category == "manual_override_trace":
        return "在照片分类工作台补齐人工覆盖 reviewer 和 reason，再重新跑正式 render。"
    if category == "classification_closure":
        return "在照片分类工作台确认阶段、角度、可用性；不可用图片标记为不用于出图。"
    if category == "slot_completeness":
        return "补齐三联正面、45°、侧面术前/术后槽位；无法补齐时改为可审计降级模板。"
    if category == "quality_review":
        return "打开 final-board 和 manifest，人工复核 warning；通过后再决定是否需要重跑。"
    return "人工查看 T59 blocker 原文后归类。"


def _requires_rerun(category: str) -> bool:
    return category in {"manual_override_trace", "classification_closure", "slot_completeness", "unknown_blocker"}


def build_action_item(
    record: dict[str, Any],
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    case = record.get("case") if isinstance(record.get("case"), dict) else {}
    case_id = int(record.get("case_id") or case.get("case_id") or 0)
    texts = _texts_for_record(record)
    files_to_review = _extract_files_to_review(texts)
    missing_slots = _extract_missing_slots(texts)
    override_trace_gaps, override_trace_status = _override_trace_gaps(
        db_path=db_path,
        case_id=case_id,
        files_to_review=files_to_review,
    )
    category = _category_for_record(record, texts, missing_slots, override_trace_gaps)
    metrics = _record_metrics(record)
    warnings = [str(item) for item in _as_list(metrics.get("display_warnings") or metrics.get("warnings"))]
    return {
        "priority": CATEGORY_PRIORITY.get(category, 90),
        "category": category,
        "job_id": int(record.get("job_id") or 0),
        "case_id": case_id,
        "case_path": case.get("abs_path"),
        "customer_raw": case.get("customer_raw"),
        "job_status": record.get("status"),
        "quality_status": (record.get("quality") or {}).get("quality_status") if isinstance(record.get("quality"), dict) else None,
        "artifact_integrity": (record.get("artifact_integrity") or {}).get("status") if isinstance(record.get("artifact_integrity"), dict) else None,
        "files_to_review": files_to_review,
        "missing_slots": missing_slots,
        "override_trace_status": override_trace_status,
        "override_trace_gaps": override_trace_gaps,
        "warning_count": len(warnings),
        "warning_samples": warnings[:5],
        "blocker_samples": texts[:8],
        "recommended_next_step": _recommended_next_step(category),
        "requires_render_rerun_after_fix": _requires_rerun(category),
    }


def _counter(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def build_repair_queue(
    matrix_report: dict[str, Any],
    *,
    db_path: Path | None = None,
    source_report_path: Path | None = None,
) -> dict[str, Any]:
    records = [
        item for item in _as_list(matrix_report.get("records"))
        if isinstance(item, dict)
    ]
    if not records:
        return blocked_report("T59 matrix report has no real records")
    action_items = [build_action_item(record, db_path=db_path) for record in records]
    action_items.sort(key=lambda item: (int(item["priority"]), int(item["case_id"]), int(item["job_id"])))
    categories = [str(item["category"]) for item in action_items]
    blocked_action_count = sum(1 for item in action_items if item.get("job_status") == "blocked")
    quality_review_count = sum(1 for item in action_items if item.get("category") == "quality_review")
    return {
        "generated_at": _now(),
        "run_status": "completed_real_repair_queue",
        "used_mock_data": False,
        "decision": "已从 T59 真实正式 render 矩阵生成整理/槽位修复清单；优先处理 blocked job，再复核 done_with_issues。",
        "policy": {
            "source": "T59 real formal render matrix",
            "source_report_path": str(source_report_path) if source_report_path else None,
            "source_report_sha256": _source_hash(source_report_path) if source_report_path else None,
            "live_db_path": str(db_path) if db_path else None,
            "does_not_enqueue_render": True,
            "comfyui_included": False,
            "repair_goal": "reduce formal render blocked jobs before tuning render algorithms",
        },
        "source_matrix": {
            "run_status": matrix_report.get("run_status"),
            "selected_case_count": (matrix_report.get("summary") or {}).get("selected_case_count"),
            "record_count": (matrix_report.get("summary") or {}).get("record_count"),
            "formal_ready_count": (matrix_report.get("summary") or {}).get("formal_ready_count"),
            "by_job_status": (matrix_report.get("summary") or {}).get("by_job_status"),
        },
        "summary": {
            "source_record_count": len(records),
            "action_item_count": len(action_items),
            "blocked_action_count": blocked_action_count,
            "quality_review_action_count": quality_review_count,
            "requires_render_rerun_after_fix_count": sum(
                1 for item in action_items if item.get("requires_render_rerun_after_fix")
            ),
            "by_category": _counter(categories),
            "by_job_status": _counter([str(item.get("job_status") or "unknown") for item in action_items]),
        },
        "action_items": action_items,
        "case_rollup": _case_rollup(action_items),
    }


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
                "categories": sorted({str(item.get("category")) for item in items}),
                "job_ids": [int(item.get("job_id") or 0) for item in items],
                "files_to_review": sorted(
                    {
                        str(filename)
                        for item in items
                        for filename in _as_list(item.get("files_to_review"))
                    }
                ),
                "missing_slots": [
                    slot
                    for item in items
                    for slot in _as_list(item.get("missing_slots"))
                    if isinstance(slot, dict)
                ],
                "requires_render_rerun_after_fix": any(
                    bool(item.get("requires_render_rerun_after_fix")) for item in items
                ),
            }
        )
    return out


def blocked_report(reason: str) -> dict[str, Any]:
    return {
        "generated_at": _now(),
        "run_status": "blocked_missing_real_t59_matrix",
        "used_mock_data": False,
        "decision": f"{UNVERIFIED}: {reason}",
        "policy": {
            "source": "T59 real formal render matrix",
            "does_not_enqueue_render": True,
            "comfyui_included": False,
        },
        "summary": {
            "source_record_count": 0,
            "action_item_count": 0,
            "blocked_action_count": 0,
            "quality_review_action_count": 0,
            "requires_render_rerun_after_fix_count": 0,
            "by_category": {},
            "by_job_status": {},
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
        "# T59.1 正式出图阻断反向修复清单",
        "",
        f"- run_status: `{report.get('run_status')}`",
        f"- decision: {report.get('decision')}",
        f"- source_record_count: {summary.get('source_record_count')}",
        f"- action_item_count: {summary.get('action_item_count')}",
        f"- blocked_action_count: {summary.get('blocked_action_count')}",
        f"- quality_review_action_count: {summary.get('quality_review_action_count')}",
        f"- requires_render_rerun_after_fix_count: {summary.get('requires_render_rerun_after_fix_count')}",
        "",
        "## Categories",
        "",
    ]
    for category, count in (summary.get("by_category") or {}).items():
        lines.append(f"- `{category}`: {count}")
    lines.extend(["", "## Action Items", ""])
    for item in report.get("action_items") or []:
        files = ", ".join(item.get("files_to_review") or []) or "-"
        slots = "; ".join(
            f"{slot.get('view')}:{','.join(slot.get('phases') or [])}"
            for slot in (item.get("missing_slots") or [])
            if isinstance(slot, dict)
        ) or "-"
        gaps = "; ".join(
            f"{gap.get('filename')}({','.join(gap.get('missing') or [])})"
            for gap in (item.get("override_trace_gaps") or [])
            if isinstance(gap, dict)
        ) or "-"
        lines.extend(
            [
                f"### Case {item.get('case_id')} / Job {item.get('job_id')} / `{item.get('category')}`",
                "",
                f"- priority: {item.get('priority')}",
                f"- job_status: `{item.get('job_status')}`",
                f"- artifact_integrity: `{item.get('artifact_integrity')}`",
                f"- case_path: {item.get('case_path') or '-'}",
                f"- files_to_review: {files}",
                f"- missing_slots: {slots}",
                f"- override_trace_gaps: {gaps}",
                f"- warning_count: {item.get('warning_count')}",
                f"- next_step: {item.get('recommended_next_step')}",
                f"- rerun_after_fix: `{item.get('requires_render_rerun_after_fix')}`",
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build T59.1 repair queue from a real T59 formal render matrix.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
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
            report = build_repair_queue(matrix_report, db_path=args.db, source_report_path=args.input)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            report = blocked_report(f"cannot read T59 matrix report: {exc}")
    write_json(args.output, report)
    write_markdown(args.markdown_output, report)
    print(json.dumps({"run_status": report.get("run_status"), "summary": report.get("summary")}, ensure_ascii=False, indent=2))
    return 0 if report.get("run_status") == "completed_real_repair_queue" else 2


if __name__ == "__main__":
    raise SystemExit(main())
