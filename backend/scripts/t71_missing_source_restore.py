"""T71 missing-source restore planner.

This tool repairs only a narrow, evidence-backed state:
- an active case row points at a missing source directory;
- every source filename in DB metadata exists in exactly one matching directory
  under the parent case trash tree;
- the active destination directory does not already exist.

The apply mode copies from trash back to the active path and rescans the case.
It never points an active case at a .case-workbench-trash path.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/Users/a1234/Desktop/案例生成器/case-workbench")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import scanner, source_images  # noqa: E402

DEFAULT_DB_PATH = ROOT / "case-workbench.db"
DEFAULT_JSON_OUTPUT = ROOT / "tasks" / "t71_missing_source_restore_plan.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "tasks" / "t71_missing_source_restore_plan.md"
DEFAULT_APPLY_OUTPUT = ROOT / "tasks" / "t71_missing_source_restore_apply_report.json"
TRASH_DIR_NAME = ".case-workbench-trash"
CASE_TRASH_SUBDIR = "cases"
UNVERIFIED = "未验证/无法获取"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_load(raw: str | None, default: Any) -> Any:
    try:
        parsed = json.loads(raw or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def _case_image_files(row: sqlite3.Row) -> list[str]:
    meta = _json_load(row["meta_json"] if "meta_json" in row.keys() else None, {})
    if not isinstance(meta, dict):
        return []
    return source_images.filter_source_image_files([str(item) for item in meta.get("image_files") or [] if str(item)])


def _nearest_existing_ancestor(path: Path) -> Path | None:
    for current in [path, *path.parents]:
        if current.exists() and current.is_dir():
            return current
    return None


def _expected_files_exist(candidate_dir: Path, filenames: list[str]) -> tuple[bool, list[str]]:
    missing = [filename for filename in filenames if not (candidate_dir / filename).is_file()]
    return not missing, missing


def _candidate_dirs_for_missing_case(case_dir: Path, filenames: list[str]) -> list[dict[str, Any]]:
    nearest = _nearest_existing_ancestor(case_dir.parent)
    if nearest is None:
        return []

    trash_root = nearest / TRASH_DIR_NAME / CASE_TRASH_SUBDIR
    if not trash_root.is_dir():
        return []

    parent_name = case_dir.parent.name
    case_name = case_dir.name
    candidates: list[dict[str, Any]] = []
    try:
        direct_patterns = [
            trash_root.glob(f"*-{parent_name}/{case_name}"),
            trash_root.glob(f"*{parent_name}*/{case_name}"),
        ]
        seen: set[Path] = set()
        for pattern in direct_patterns:
            for candidate in pattern:
                resolved = candidate.resolve()
                if resolved in seen or not resolved.is_dir():
                    continue
                seen.add(resolved)
                complete, missing = _expected_files_exist(resolved, filenames)
                candidates.append(
                    {
                        "path": str(resolved),
                        "source": "case_trash_direct_parent_match",
                        "file_match_count": len(filenames) - len(missing),
                        "expected_count": len(filenames),
                        "missing_expected_files": missing[:20],
                        "complete_match": complete,
                    }
                )
    except OSError:
        return candidates

    return sorted(candidates, key=lambda item: (not bool(item["complete_match"]), str(item["path"])))


def _build_item(row: sqlite3.Row) -> dict[str, Any] | None:
    case_id = int(row["id"])
    case_dir = Path(str(row["abs_path"] or "")).resolve()
    filenames = _case_image_files(row)
    if not filenames:
        return None

    missing = [filename for filename in filenames if not (case_dir / filename).is_file()]
    if not missing:
        return None

    candidates = _candidate_dirs_for_missing_case(case_dir, filenames)
    complete_candidates = [item for item in candidates if item.get("complete_match")]
    if case_dir.exists():
        status = "blocked_destination_exists_but_files_missing"
        apply_eligible = False
        recommended_action = "manual_inspect_existing_destination"
    elif len(complete_candidates) == 1 and len(missing) == len(filenames):
        status = "safe_restore_from_case_trash"
        apply_eligible = True
        recommended_action = "copy_matching_trash_dir_to_active_path_and_rescan"
    elif not complete_candidates:
        status = "manual_required_no_complete_candidate"
        apply_eligible = False
        recommended_action = "manual_restore_or_rescan"
    else:
        status = "manual_required_ambiguous_candidates"
        apply_eligible = False
        recommended_action = "manual_choose_source_dir"

    return {
        "case_id": case_id,
        "active_case_abs_path": str(case_dir),
        "expected_file_count": len(filenames),
        "missing_file_count": len(missing),
        "missing_files": missing,
        "candidate_count": len(candidates),
        "complete_candidate_count": len(complete_candidates),
        "candidates": candidates,
        "status": status,
        "apply_eligible": apply_eligible,
        "recommended_action": recommended_action,
        "readiness": "ready_to_apply" if apply_eligible else UNVERIFIED,
    }


def build_restore_plan(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
    db_path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, abs_path, meta_json, source_count, trashed_at
            FROM cases
            WHERE trashed_at IS NULL
            ORDER BY id
            """
        ).fetchall()
        items = [item for row in rows if (item := _build_item(row)) is not None]
    finally:
        conn.close()

    status_counts = dict(sorted(Counter(str(item["status"]) for item in items).items()))
    safe_count = sum(1 for item in items if item.get("apply_eligible"))
    missing_file_count = sum(int(item.get("missing_file_count") or 0) for item in items)
    if not items:
        plan_status = "clear"
        decision = "缺失源文件已清零。"
    elif safe_count == len(items):
        plan_status = "ready_to_apply"
        decision = "所有缺失源文件均可从 case trash 中一一对应恢复；apply 将 copy 回 active path 并 rescan。"
    elif safe_count:
        plan_status = "partially_ready"
        decision = f"{UNVERIFIED}: 只有部分缺失 case 可安全恢复，其余需要人工处理。"
    else:
        plan_status = "manual_required"
        decision = f"{UNVERIFIED}: 未找到可自动恢复的一一对应真实目录。"

    return {
        "generated_at": _now(),
        "run_status": "readonly_t71_missing_source_restore_plan",
        "plan_status": plan_status,
        "decision": decision,
        "used_mock_data": False,
        "db_path": str(db_path),
        "summary": {
            "case_count_with_missing_sources": len(items),
            "missing_file_count": missing_file_count,
            "safe_restore_case_count": safe_count,
            "status_counts": status_counts,
        },
        "items": items,
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone()
    return bool(row)


def _audit_before(conn: sqlite3.Connection, case_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not case_ids or not (_table_exists(conn, "case_revisions") and _table_exists(conn, "cases")):
        return {}
    try:
        from backend import audit  # noqa: PLC0415

        return audit.snapshot_before(conn, case_ids)
    except Exception:  # noqa: BLE001
        return {}


def _audit_after(conn: sqlite3.Connection, case_ids: list[int], before: dict[int, dict[str, Any]]) -> list[int]:
    if not before:
        return []
    try:
        from backend import audit  # noqa: PLC0415

        return audit.record_after(
            conn,
            case_ids,
            before,
            op="t71_missing_source_restore",
            source_route="backend/scripts/t71_missing_source_restore.py",
            actor="codex-t71",
        )
    except Exception:  # noqa: BLE001
        return []


def apply_safe_restores(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
    db_path = Path(db_path).expanduser().resolve()
    plan = build_restore_plan(db_path)
    results: list[dict[str, Any]] = []
    eligible = [item for item in plan.get("items") or [] if item.get("apply_eligible")]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for item in eligible:
            case_id = int(item["case_id"])
            dest = Path(str(item["active_case_abs_path"])).resolve()
            candidates = [candidate for candidate in item["candidates"] if candidate.get("complete_match")]
            if len(candidates) != 1:
                results.append({"case_id": case_id, "status": "skipped_not_unique_candidate"})
                continue
            source = Path(str(candidates[0]["path"])).resolve()
            if TRASH_DIR_NAME not in source.parts:
                results.append({"case_id": case_id, "status": "skipped_candidate_not_in_case_trash", "source": str(source)})
                continue
            if dest.exists():
                results.append({"case_id": case_id, "status": "skipped_destination_exists", "destination": str(dest)})
                continue
            complete, missing = _expected_files_exist(source, [str(x) for x in item.get("missing_files") or []])
            if not complete:
                results.append(
                    {
                        "case_id": case_id,
                        "status": "skipped_candidate_no_longer_complete",
                        "source": str(source),
                        "missing": missing[:20],
                    }
                )
                continue

            before = _audit_before(conn, [case_id])
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, dest, copy_function=shutil.copy2)
            scanner.rescan_one(conn, case_id)
            revisions = _audit_after(conn, [case_id], before)
            results.append(
                {
                    "case_id": case_id,
                    "status": "restored_from_case_trash_copy",
                    "source": str(source),
                    "destination": str(dest),
                    "restored_file_count": len(item.get("missing_files") or []),
                    "revision_ids": revisions,
                }
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    status_counts = dict(sorted(Counter(str(item["status"]) for item in results).items()))
    return {
        "generated_at": _now(),
        "run_status": "applied_t71_missing_source_restore",
        "used_mock_data": False,
        "db_path": str(db_path),
        "summary": {
            "eligible_case_count": len(eligible),
            "result_count": len(results),
            "restored_case_count": status_counts.get("restored_from_case_trash_copy", 0),
            "status_counts": status_counts,
        },
        "results": results,
        "plan_before_apply": plan,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    lines = [
        "# T71 Missing Source Restore Plan",
        "",
        f"- run_status: `{report.get('run_status')}`",
        f"- plan_status: `{report.get('plan_status')}`",
        f"- decision: {report.get('decision')}",
        f"- used_mock_data: `{report.get('used_mock_data')}`",
        f"- case_count_with_missing_sources: {summary.get('case_count_with_missing_sources')}",
        f"- missing_file_count: {summary.get('missing_file_count')}",
        f"- safe_restore_case_count: {summary.get('safe_restore_case_count')}",
        f"- status_counts: `{json.dumps(summary.get('status_counts') or {}, ensure_ascii=False)}`",
        "",
        "## Items",
        "",
    ]
    for item in report.get("items") or []:
        candidates = [candidate.get("path") for candidate in item.get("candidates") or [] if candidate.get("complete_match")]
        lines.extend(
            [
                f"### Case {item.get('case_id')}",
                f"- status: `{item.get('status')}`",
                f"- active_case_abs_path: `{item.get('active_case_abs_path')}`",
                f"- missing_file_count: {item.get('missing_file_count')}",
                f"- complete_candidate_count: {item.get('complete_candidate_count')}",
                f"- apply_eligible: `{item.get('apply_eligible')}`",
                f"- recommended_action: `{item.get('recommended_action')}`",
                f"- readiness: {item.get('readiness')}",
            ]
        )
        for candidate in candidates:
            lines.append(f"- complete_candidate: `{candidate}`")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan/apply T71 missing source restore from case trash.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN_OUTPUT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--apply-output", type=Path, default=DEFAULT_APPLY_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    apply_report = None
    if args.apply:
        apply_report = apply_safe_restores(args.db)
        write_json(args.apply_output, apply_report)
    report = build_restore_plan(args.db)
    if apply_report is not None:
        report["applied_restore_report"] = apply_report
    write_json(args.output, report)
    write_markdown(args.markdown_output, report)
    print(json.dumps({"plan_status": report["plan_status"], "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
