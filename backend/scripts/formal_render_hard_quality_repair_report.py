"""Build a T59.4 hard quality blocker repair report from real render reports."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/Users/a1234/Desktop/案例生成器/case-workbench")
DEFAULT_QUEUE = ROOT / "tasks" / "t593_quality_warning_repair_queue.json"
DEFAULT_MATRIX = ROOT / "tasks" / "t594_hard_quality_targeted_matrix.json"
DEFAULT_JSON_OUTPUT = ROOT / "tasks" / "t594_hard_quality_repair_report.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "tasks" / "t594_hard_quality_repair_report.md"
TARGET_CASE_IDS = (21, 54, 69)
UNVERIFIED = "未验证/无法获取"
STATUS_GATE = "render_quality.quality_status:done_with_issues"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _source_hash(path: Path | None) -> str | None:
    if not path:
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _case_id(record: dict[str, Any]) -> int:
    case = record.get("case") if isinstance(record.get("case"), dict) else {}
    return int(record.get("case_id") or case.get("case_id") or 0)


def _artifact_ok(record: dict[str, Any]) -> bool:
    artifact = record.get("artifact_integrity") if isinstance(record.get("artifact_integrity"), dict) else {}
    final_board = artifact.get("final_board") if isinstance(artifact.get("final_board"), dict) else {}
    manifest = artifact.get("manifest") if isinstance(artifact.get("manifest"), dict) else {}
    return artifact.get("status") == "ok" and bool(final_board.get("exists")) and bool(manifest.get("exists"))


def _quality(record: dict[str, Any]) -> dict[str, Any]:
    quality = record.get("quality") if isinstance(record.get("quality"), dict) else {}
    return quality if isinstance(quality, dict) else {}


def _metrics(record: dict[str, Any]) -> dict[str, Any]:
    metrics = _quality(record).get("metrics")
    return metrics if isinstance(metrics, dict) else {}


def _hard_blockers(record: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    for item in _as_list(record.get("hard_blockers")):
        text = str(item or "")
        if text:
            blockers.append(text)
    for item in _as_list(_metrics(record).get("blocking_issues")):
        text = str(item or "")
        if text:
            blockers.append(text)
    return list(dict.fromkeys(blockers))


def _concrete_hard_blockers(record: dict[str, Any]) -> list[str]:
    return [item for item in _hard_blockers(record) if item != STATUS_GATE]


def _semantic_unavailable(record: dict[str, Any]) -> bool:
    texts: list[str] = []
    metrics = _metrics(record)
    for key in ("warnings", "display_warnings", "audit_warnings"):
        texts.extend(str(item or "") for item in _as_list(metrics.get(key)))
    joined = "\n".join(texts)
    return (
        "insufficient_user_quota" in joined
        or "API 403" in joined
        or "预扣费额度失败" in joined
        or "quota" in joined.lower()
    )


def _before_items(queue: dict[str, Any], target_case_ids: tuple[int, ...]) -> list[dict[str, Any]]:
    items = []
    for item in _as_list(queue.get("action_items")):
        if not isinstance(item, dict):
            continue
        if int(item.get("case_id") or 0) in target_case_ids:
            items.append(item)
    return sorted(items, key=lambda item: int(item.get("case_id") or 0))


def _after_records(matrix: dict[str, Any], target_case_ids: tuple[int, ...]) -> list[dict[str, Any]]:
    records = []
    for record in _as_list(matrix.get("records")):
        if isinstance(record, dict) and _case_id(record) in target_case_ids:
            records.append(record)
    return sorted(records, key=_case_id)


def build_report(
    *,
    queue_path: Path,
    matrix_path: Path,
    target_case_ids: tuple[int, ...] = TARGET_CASE_IDS,
) -> dict[str, Any]:
    if not queue_path.exists() or not matrix_path.exists():
        missing = [str(path) for path in (queue_path, matrix_path) if not path.exists()]
        return {
            "generated_at": _now(),
            "run_status": "blocked_missing_real_report",
            "used_mock_data": False,
            "decision": f"{UNVERIFIED}：缺少真实 report，不能生成硬阻断修复结论。",
            "missing_paths": missing,
        }

    queue = _load_json(queue_path)
    matrix = _load_json(matrix_path)
    before = _before_items(queue, target_case_ids)
    after = _after_records(matrix, target_case_ids)
    after_by_case = {_case_id(record): record for record in after}

    case_results = []
    for case_id in target_case_ids:
        record = after_by_case.get(case_id)
        if not record:
            case_results.append({
                "case_id": case_id,
                "status": "missing_after_record",
                "concrete_hard_blockers": [UNVERIFIED],
                "hard_blocker_cleared": False,
                "artifact_ok": False,
            })
            continue
        quality = _quality(record)
        concrete = _concrete_hard_blockers(record)
        case_results.append({
            "case_id": case_id,
            "job_id": int(record.get("job_id") or 0),
            "job_status": record.get("status"),
            "quality_status": quality.get("quality_status"),
            "quality_score": quality.get("quality_score"),
            "artifact_ok": _artifact_ok(record),
            "manifest_status": quality.get("manifest_status"),
            "blocking_count": int(quality.get("blocking_count") or 0),
            "concrete_hard_blockers": concrete,
            "hard_blocker_cleared": len(concrete) == 0,
            "semantic_judge_unavailable": _semantic_unavailable(record),
            "delivery_envelope_class": (record.get("delivery_envelope") or {}).get("class")
            if isinstance(record.get("delivery_envelope"), dict)
            else None,
        })

    before_hard_count = sum(1 for item in before if item.get("primary_category") == "hard_blocker_in_done_with_issues")
    after_concrete_count = sum(len(item["concrete_hard_blockers"]) for item in case_results)
    cleared_count = sum(1 for item in case_results if item.get("hard_blocker_cleared"))
    semantic_count = sum(1 for item in case_results if item.get("semantic_judge_unavailable"))
    formal_ready_count = sum(1 for item in case_results if item.get("delivery_envelope_class") == "formal_ready")
    all_target_records_present = len(after) == len(target_case_ids)
    hard_blockers_cleared = all_target_records_present and after_concrete_count == 0
    if hard_blockers_cleared and formal_ready_count == len(target_case_ids):
        decision = "case 21/54/69 的具体姿态/清晰度/manifest 硬阻断已在真实 targeted render 中清除，且 3/3 已进入 formal_ready。"
    elif hard_blockers_cleared:
        decision = (
            "case 21/54/69 的具体姿态/清晰度/manifest 硬阻断已在真实 targeted render 中清除；"
            "但仍存在正式质量 warning，不能交付。"
        )
    else:
        decision = f"{UNVERIFIED}：仍有目标 case 缺失或存在具体 hard blocker。"

    return {
        "generated_at": _now(),
        "run_status": "completed_real_hard_quality_repair_report",
        "used_mock_data": False,
        "decision": decision,
        "policy": {
            "source_queue_path": str(queue_path),
            "source_queue_sha256": _source_hash(queue_path),
            "source_matrix_path": str(matrix_path),
            "source_matrix_sha256": _source_hash(matrix_path),
            "does_not_modify_db": True,
            "does_not_enqueue_render": True,
            "done_with_issues_is_not_publishable": True,
        },
        "summary": {
            "target_case_ids": list(target_case_ids),
            "before_hard_blocker_item_count": before_hard_count,
            "after_record_count": len(after),
            "after_concrete_hard_blocker_count": after_concrete_count,
            "hard_blocker_cleared_case_count": cleared_count,
            "artifact_ok_count": sum(1 for item in case_results if item.get("artifact_ok")),
            "semantic_judge_unavailable_count": semantic_count,
            "formal_ready_count": formal_ready_count,
            "still_not_deliverable_count": len([item for item in case_results if item.get("delivery_envelope_class") != "formal_ready"]),
        },
        "case_results": case_results,
    }


def write_markdown(report: dict[str, Any], output_path: Path) -> None:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# T59.4 硬质量阻断修复报告",
        "",
        f"- run_status: `{report.get('run_status')}`",
        f"- decision: {report.get('decision')}",
        f"- target_case_ids: `{summary.get('target_case_ids')}`",
        f"- after_record_count: `{summary.get('after_record_count')}`",
        f"- after_concrete_hard_blocker_count: `{summary.get('after_concrete_hard_blocker_count')}`",
        f"- semantic_judge_unavailable_count: `{summary.get('semantic_judge_unavailable_count')}`",
        f"- formal_ready_count: `{summary.get('formal_ready_count')}`",
        "",
        "## Case Results",
        "",
    ]
    for item in _as_list(report.get("case_results")):
        blockers = item.get("concrete_hard_blockers") or []
        lines.append(
            f"- case `{item.get('case_id')}` job `{item.get('job_id')}`: "
            f"artifact_ok=`{item.get('artifact_ok')}`, hard_blocker_cleared=`{item.get('hard_blocker_cleared')}`, "
            f"quality=`{item.get('quality_status')}`, delivery=`{item.get('delivery_envelope_class')}`"
        )
        if blockers:
            lines.append(f"  - blockers: `{blockers}`")
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN_OUTPUT)
    args = parser.parse_args()

    report = build_report(queue_path=args.queue, matrix_path=args.matrix)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(report, args.markdown_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
