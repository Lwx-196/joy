"""Execute T59.1 formal render repair actions against the live workspace DB.

This script only performs evidence-preserving cleanup:
- backfill trace metadata for existing manual overrides without changing labels;
- exclude source images already identified as unusable/unclosed for formal render;
- record audited template downgrade when a real prior selection plan shows that
  only a lower-slot template is possible.

It does not invent phase/view labels and does not create source images.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/Users/a1234/Desktop/案例生成器/case-workbench")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import source_images  # noqa: E402

DEFAULT_DB_PATH = ROOT / "case-workbench.db"
DEFAULT_QUEUE_PATH = ROOT / "tasks" / "t591_formal_render_repair_queue.json"
DEFAULT_MATRIX_PATH = ROOT / "tasks" / "t59_formal_render_quality_matrix.json"
DEFAULT_JSON_OUTPUT = ROOT / "tasks" / "t592_repair_execution_report.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "tasks" / "t592_repair_execution_report.md"
UNVERIFIED = "未验证/无法获取"
REVIEW_STATE_KEY = "image_review_states"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json_file(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return default


def _json_load(raw: str | None, default: Any) -> Any:
    try:
        parsed = json.loads(raw or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def _json_dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _case_row(conn: sqlite3.Connection, case_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cases WHERE id = ? AND trashed_at IS NULL", (case_id,)).fetchone()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _audit_before(conn: sqlite3.Connection, case_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not case_ids or not {"case_revisions", "cases"}.issubset(
        {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    ):
        return {}
    try:
        from backend import audit  # noqa: PLC0415

        return audit.snapshot_before(conn, case_ids)
    except Exception:  # noqa: BLE001 - audit is best-effort for script/test DBs.
        return {}


def _audit_after(
    conn: sqlite3.Connection,
    case_ids: list[int],
    befores: dict[int, dict[str, Any]],
    *,
    op: str,
    source_route: str,
) -> list[int]:
    if not befores:
        return []
    try:
        from backend import audit  # noqa: PLC0415

        return audit.record_after(conn, case_ids, befores, op=op, source_route=source_route, actor="codex-t592")
    except Exception:  # noqa: BLE001
        return []


def _reason_payload(*, reason: str, reviewer: str, source: str = "t592_formal_render_repair") -> str:
    return _json_dump(
        {
            "reason": reason,
            "source": source,
            "reviewer": reviewer,
            "recorded_at": _now(),
        }
    )


def _backfill_manual_override_trace(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    *,
    reviewer: str,
) -> list[dict[str, Any]]:
    case_id = int(item.get("case_id") or 0)
    fixed: list[dict[str, Any]] = []
    if case_id <= 0:
        return fixed
    befores = _audit_before(conn, [case_id])
    for gap in item.get("override_trace_gaps") or []:
        if not isinstance(gap, dict):
            continue
        filename = str(gap.get("filename") or "").strip()
        if not filename:
            continue
        row = conn.execute(
            """
            SELECT manual_phase, manual_view, manual_transform_json, reason_json, reviewer
            FROM case_image_overrides
            WHERE case_id = ? AND filename = ?
            """,
            (case_id, filename),
        ).fetchone()
        if not row:
            fixed.append({"case_id": case_id, "filename": filename, "status": "unresolved_missing_override"})
            continue
        reason_json = row["reason_json"]
        row_reviewer = row["reviewer"]
        if str(reason_json or "").strip() and str(row_reviewer or "").strip():
            fixed.append({"case_id": case_id, "filename": filename, "status": "already_traceable"})
            continue
        reason = (
            "t592_trace_backfill: preserved existing manual override; "
            "no phase/view/classification value was changed."
        )
        conn.execute(
            """
            UPDATE case_image_overrides
            SET reason_json = COALESCE(NULLIF(reason_json, ''), ?),
                reviewer = COALESCE(NULLIF(reviewer, ''), ?),
                updated_at = ?
            WHERE case_id = ? AND filename = ?
            """,
            (_reason_payload(reason=reason, reviewer=reviewer, source="t592_trace_backfill"), reviewer, _now(), case_id, filename),
        )
        fixed.append(
            {
                "case_id": case_id,
                "filename": filename,
                "status": "trace_backfilled",
                "manual_phase_preserved": row["manual_phase"],
                "manual_view_preserved": row["manual_view"],
            }
        )
    _audit_after(
        conn,
        [case_id],
        befores,
        op="t592_manual_override_trace_backfill",
        source_route="backend/scripts/formal_render_repair_execution.py",
    )
    return fixed


def _skill_by_file(row: sqlite3.Row) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in _json_load(row["skill_image_metadata_json"] if "skill_image_metadata_json" in row.keys() else None, []):
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "").strip()
        if not filename:
            continue
        out[filename] = item
        out[Path(filename).name] = item
    return out


def _image_review_states(meta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    states = meta.get(REVIEW_STATE_KEY)
    return dict(states) if isinstance(states, dict) else {}


def _phase_for_file(case_dir: Path, filename: str) -> str | None:
    case_name = case_dir.name
    contextual = str(Path(case_name) / filename) if case_name else filename
    return source_images._phase_from_filename(filename) or source_images._phase_from_filename(contextual)


def _has_before_after_after_excluding(case_dir: Path, image_files: list[str], states: dict[str, Any], exclude_name: str) -> bool:
    phases: set[str] = set()
    for filename in image_files:
        if filename == exclude_name or Path(filename).name == Path(exclude_name).name:
            continue
        state = states.get(filename) or states.get(Path(filename).name)
        if isinstance(state, dict) and (state.get("render_excluded") or state.get("verdict") == "excluded"):
            continue
        if not source_images.is_source_image_file(filename):
            continue
        if not (case_dir / filename).is_file():
            continue
        phase = _phase_for_file(case_dir, filename)
        if phase in {"before", "after"}:
            phases.add(phase)
    return {"before", "after"}.issubset(phases)


def _classification_file_is_safe_to_exclude(
    *,
    row: sqlite3.Row,
    filename: str,
    meta: dict[str, Any],
    states: dict[str, dict[str, Any]],
    skill: dict[str, dict[str, Any]],
) -> tuple[bool, str]:
    case_dir = Path(str(row["abs_path"]))
    image_files = [str(item) for item in (meta.get("image_files") or []) if item]
    if filename not in image_files and Path(filename).name not in {Path(item).name for item in image_files}:
        return False, "file_not_in_case_meta"
    if not (case_dir / filename).is_file():
        return False, "file_missing_on_disk"
    if not _has_before_after_after_excluding(case_dir, image_files, states, filename):
        return False, "would_remove_before_after_evidence"
    item = skill.get(filename) or skill.get(Path(filename).name) or {}
    rejection = str(item.get("rejection_reason") or "").strip()
    phase = item.get("phase")
    angle = item.get("view_bucket") or item.get("angle")
    if rejection in {"face_detection_failure", "phase_missing", "blurry_image"}:
        return True, f"skill_rejection:{rejection}"
    if phase not in {"before", "after"}:
        return True, "phase_missing"
    if angle not in {"front", "oblique", "side"}:
        return True, "view_missing"
    return False, "classification_has_phase_and_view"


def _exclude_classification_files(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    *,
    reviewer: str,
) -> list[dict[str, Any]]:
    case_id = int(item.get("case_id") or 0)
    files = [str(value) for value in (item.get("files_to_review") or []) if str(value)]
    if case_id <= 0 or not files:
        return []
    row = _case_row(conn, case_id)
    if not row:
        return [{"case_id": case_id, "status": "unresolved_missing_case"}]
    meta = _json_load(row["meta_json"], {})
    if not isinstance(meta, dict):
        meta = {}
    states = _image_review_states(meta)
    skill = _skill_by_file(row)
    befores = _audit_before(conn, [case_id])
    results: list[dict[str, Any]] = []
    changed = False
    for filename in files:
        current = states.get(filename) or states.get(Path(filename).name)
        if isinstance(current, dict) and (current.get("render_excluded") or current.get("verdict") == "excluded"):
            results.append({"case_id": case_id, "filename": filename, "status": "already_excluded"})
            continue
        safe, reason = _classification_file_is_safe_to_exclude(
            row=row,
            filename=filename,
            meta=meta,
            states=states,
            skill=skill,
        )
        if not safe:
            results.append({"case_id": case_id, "filename": filename, "status": "unresolved", "reason": reason})
            continue
        states[filename] = {
            "verdict": "excluded",
            "label": "已排除出图",
            "reviewer": reviewer,
            "note": f"T59.2 classification closure: {reason}; no phase/view label was fabricated.",
            "reason": f"t592_classification_closure:{reason}",
            "layer": "t592_classification_closure",
            "render_excluded": True,
            "reviewed_at": _now(),
        }
        changed = True
        results.append({"case_id": case_id, "filename": filename, "status": "excluded", "reason": reason})
    if changed:
        meta[REVIEW_STATE_KEY] = states
        conn.execute("UPDATE cases SET meta_json = ? WHERE id = ?", (_json_dump(meta), case_id))
        _audit_after(
            conn,
            [case_id],
            befores,
            op="t592_classification_closure",
            source_route="backend/scripts/formal_render_repair_execution.py",
        )
    return results


def _template_tier_from_hint(value: Any) -> str | None:
    text = str(value or "").strip()
    return {
        "single": "single",
        "single-compare": "single",
        "bi": "bi",
        "bi-compare": "bi",
        "tri": "tri",
        "tri-compare": "tri",
    }.get(text)


def _selection_plan_for_item(conn: sqlite3.Connection, item: dict[str, Any]) -> dict[str, Any]:
    source_record = item.get("source_record") if isinstance(item.get("source_record"), dict) else {}
    audit_payload = source_record.get("render_selection_audit") if isinstance(source_record, dict) else None
    if isinstance(audit_payload, dict) and isinstance(audit_payload.get("selection_plan"), dict):
        return audit_payload["selection_plan"]
    job_id = int(item.get("job_id") or 0)
    if job_id <= 0:
        return {}
    row = conn.execute("SELECT meta_json FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return {}
    meta = _json_load(row["meta_json"], {})
    audit_payload = meta.get("render_selection_audit") if isinstance(meta, dict) else None
    if isinstance(audit_payload, dict) and isinstance(audit_payload.get("selection_plan"), dict):
        return audit_payload["selection_plan"]
    return {}


def _repair_slot_completeness(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    *,
    reviewer: str,
) -> list[dict[str, Any]]:
    case_id = int(item.get("case_id") or 0)
    if case_id <= 0:
        return []
    row = _case_row(conn, case_id)
    if not row:
        return [{"case_id": case_id, "status": "unresolved_missing_case"}]
    current_tier = _template_tier_from_hint(row["manual_template_tier"] if "manual_template_tier" in row.keys() else None)
    current_tier = current_tier or _template_tier_from_hint(row["template_tier"] if "template_tier" in row.keys() else None)
    plan = _selection_plan_for_item(conn, item)
    target_tier = _template_tier_from_hint(plan.get("effective_template_hint") if isinstance(plan, dict) else None)
    renderable = [str(value) for value in (plan.get("renderable_slots") or [])] if isinstance(plan, dict) else []
    if current_tier in {"single", "bi"}:
        return [{"case_id": case_id, "status": "already_downgraded", "template_tier": current_tier}]
    if target_tier not in {"single", "bi"} or "front" not in renderable:
        return [{"case_id": case_id, "status": "unresolved_no_audited_template_downgrade", "target_tier": target_tier}]
    befores = _audit_before(conn, [case_id])
    previous_notes = str(row["notes"] if "notes" in row.keys() and row["notes"] else "")
    note_line = (
        f"[T59.2 {_now()}] {reviewer} audited slot downgrade to {target_tier}: "
        "prior real render selection plan lacked full tri-compare slots; no source images were fabricated."
    )
    notes = f"{previous_notes}\n{note_line}".strip() if previous_notes else note_line
    conn.execute(
        "UPDATE cases SET manual_template_tier = ?, notes = ? WHERE id = ?",
        (target_tier, notes, case_id),
    )
    _audit_after(
        conn,
        [case_id],
        befores,
        op="t592_template_downgrade",
        source_route="backend/scripts/formal_render_repair_execution.py",
    )
    return [{"case_id": case_id, "status": "template_downgraded", "template_tier": target_tier}]


def _records_by_job(matrix_report: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    if not isinstance(matrix_report, dict):
        return out
    for record in matrix_report.get("records") or []:
        if not isinstance(record, dict):
            continue
        try:
            job_id = int(record.get("job_id") or 0)
        except (TypeError, ValueError):
            continue
        if job_id > 0:
            out[job_id] = record
    return out


def filter_action_items(
    actions: list[dict[str, Any]],
    *,
    categories: set[str] | None = None,
    case_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    category_filter = {str(item) for item in (categories or set()) if str(item)}
    case_filter = {int(item) for item in (case_ids or set()) if int(item) > 0}
    for item in actions:
        if not isinstance(item, dict):
            continue
        if category_filter and str(item.get("category") or "") not in category_filter:
            continue
        try:
            case_id = int(item.get("case_id") or 0)
        except (TypeError, ValueError):
            case_id = 0
        if case_filter and case_id not in case_filter:
            continue
        filtered.append(item)
    return filtered


def execute_repairs(
    queue_report: dict[str, Any],
    *,
    db_path: Path,
    reviewer: str,
    matrix_report: dict[str, Any] | None = None,
    categories: set[str] | None = None,
    case_ids: set[int] | None = None,
) -> dict[str, Any]:
    db_path = Path(db_path).expanduser().resolve()
    records = _records_by_job(matrix_report)
    all_actions = [item for item in (queue_report.get("action_items") or []) if isinstance(item, dict)]
    actions = filter_action_items(all_actions, categories=categories, case_ids=case_ids)
    results: list[dict[str, Any]] = []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for raw_item in actions:
            item = dict(raw_item)
            job_id = int(item.get("job_id") or 0)
            if "source_record" not in item and job_id in records:
                item["source_record"] = records[job_id]
            category = str(item.get("category") or "")
            if category == "manual_override_trace":
                detail = _backfill_manual_override_trace(conn, item, reviewer=reviewer)
            elif category == "classification_closure":
                detail = _exclude_classification_files(conn, item, reviewer=reviewer)
            elif category == "slot_completeness":
                detail = _repair_slot_completeness(conn, item, reviewer=reviewer)
            else:
                detail = [{"status": "skipped_category", "category": category}]
            results.append(
                {
                    "case_id": item.get("case_id"),
                    "job_id": item.get("job_id"),
                    "category": category,
                    "detail": detail,
                }
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    flat_statuses = [
        str(detail.get("status") or "")
        for result in results
        for detail in (result.get("detail") or [])
        if isinstance(detail, dict)
    ]
    status_counts = dict(sorted(Counter(flat_statuses).items()))
    summary = {
        "input_action_count": len(actions),
        "result_count": len(results),
        "manual_override_trace_fixed_count": status_counts.get("trace_backfilled", 0),
        "classification_excluded_count": status_counts.get("excluded", 0),
        "template_downgrade_count": status_counts.get("template_downgraded", 0),
        "already_downgraded_count": status_counts.get("already_downgraded", 0),
        "unresolved_count": sum(count for status, count in status_counts.items() if status.startswith("unresolved")),
        "status_counts": status_counts,
    }
    decision = (
        "T59.2 真实修复已执行；需要复跑 T59 矩阵验证 blocked 是否下降。"
        if summary["unresolved_count"] == 0
        else f"{UNVERIFIED}: 存在未自动修复项，不能声称全部阻断已解除。"
    )
    return {
        "generated_at": _now(),
        "run_status": "completed_real_repair_execution",
        "used_mock_data": False,
        "decision": decision,
        "reviewer": reviewer,
        "policy": {
            "manual_override_trace": "backfill trace only; preserve manual_phase/manual_view",
            "classification_closure": "exclude only unusable/unclosed files with real metadata evidence; do not fabricate labels",
            "slot_completeness": "record audited template downgrade only from existing case tier or prior real selection plan",
        },
        "summary": summary,
        "results": results,
        "filters": {
            "categories": sorted(categories or []),
            "case_ids": sorted(case_ids or []),
            "unfiltered_action_count": len(all_actions),
        },
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    lines = [
        "# T59.2 正式出图阻断修复执行报告",
        "",
        f"- run_status: `{report.get('run_status')}`",
        f"- decision: {report.get('decision')}",
        f"- reviewer: `{report.get('reviewer')}`",
        f"- manual_override_trace_fixed_count: {summary.get('manual_override_trace_fixed_count')}",
        f"- classification_excluded_count: {summary.get('classification_excluded_count')}",
        f"- template_downgrade_count: {summary.get('template_downgrade_count')}",
        f"- already_downgraded_count: {summary.get('already_downgraded_count')}",
        f"- unresolved_count: {summary.get('unresolved_count')}",
        "",
        "## Status Counts",
        "",
    ]
    for key, value in (summary.get("status_counts") or {}).items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Results", ""])
    for result in report.get("results") or []:
        lines.append(f"### Case {result.get('case_id')} / Job {result.get('job_id')} / `{result.get('category')}`")
        for detail in result.get("detail") or []:
            if isinstance(detail, dict):
                lines.append(f"- `{detail.get('status')}`: {json.dumps(detail, ensure_ascii=False)}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute T59.2 repair actions from the T59.1 queue.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE_PATH)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX_PATH)
    parser.add_argument("--reviewer", default="codex-t592")
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN_OUTPUT)
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--case-ids", default="")
    return parser


def _parse_case_ids(value: str) -> set[int]:
    out: set[int] = set()
    for raw in str(value or "").replace(" ", "").split(","):
        if not raw:
            continue
        try:
            parsed = int(raw)
        except ValueError:
            continue
        if parsed > 0:
            out.add(parsed)
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    queue_report = _load_json_file(Path(args.queue), {})
    matrix_report = _load_json_file(Path(args.matrix), {})
    report = execute_repairs(
        queue_report,
        db_path=Path(args.db),
        reviewer=str(args.reviewer),
        matrix_report=matrix_report,
        categories={str(item) for item in args.category if str(item)},
        case_ids=_parse_case_ids(str(args.case_ids)),
    )
    write_json(Path(args.output), report)
    write_markdown(Path(args.markdown_output), report)
    print(json.dumps({"run_status": report["run_status"], "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
