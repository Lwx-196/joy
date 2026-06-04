"""Extract existing real simulation job records for a ComfyUI A/B plan."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.scripts.comfyui_ab_runner import BASELINE_PROVIDER, BASELINE_VARIANT, CANDIDATE_PROVIDER, _candidate_variant

ROOT = Path("/Users/a1234/Desktop/案例生成器/case-workbench")
DEFAULT_DB_PATH = ROOT / "case-workbench.db"

PREFERRED_OUTPUT_KINDS = {"generated_raw", "ai_after_simulation"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return default


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _case_relative_refs(input_refs: list[Any]) -> tuple[str | None, str | None]:
    before = None
    after = None
    for ref in input_refs:
        if not isinstance(ref, dict):
            continue
        role = str(ref.get("role") or "")
        if role == "before_pose_reference":
            before = str(ref.get("case_relative_path") or "").strip() or None
        elif role == "after_source":
            after = str(ref.get("case_relative_path") or "").strip() or None
    return before, after


def _usable_output_refs(output_refs: list[Any]) -> list[dict[str, Any]]:
    refs = [ref for ref in output_refs if isinstance(ref, dict)]
    usable = []
    for ref in refs:
        kind = str(ref.get("kind") or "")
        if kind in PREFERRED_OUTPUT_KINDS and Path(str(ref.get("path") or "")).is_file():
            usable.append(ref)
    return usable


def _role_and_variant(model_plan: dict[str, Any], workflow: str) -> tuple[str | None, str | None]:
    provider = str(model_plan.get("provider") or "").strip()
    model_name = str(model_plan.get("model_name") or "").strip()
    if provider == BASELINE_PROVIDER:
        return "baseline", f"{BASELINE_PROVIDER}:{model_name}" if model_name else BASELINE_VARIANT
    if provider == CANDIDATE_PROVIDER and model_name == workflow:
        return "candidate", _candidate_variant(workflow)
    return None, None


def _plan_index(plan: dict[str, Any]) -> dict[tuple[int, str, str], dict[str, Any]]:
    out: dict[tuple[int, str, str], dict[str, Any]] = {}
    for unit in plan.get("units") or []:
        if not isinstance(unit, dict):
            continue
        key = (
            int(unit.get("case_id") or 0),
            str(unit.get("before_image_path") or "").strip(),
            str(unit.get("after_image_path") or "").strip(),
        )
        if key[0] and key[1] and key[2]:
            out[key] = unit
    return out


def extract_existing_records(
    *,
    plan: dict[str, Any],
    db_path: Path,
    roles: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    units_by_key = _plan_index(plan)
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    candidate_count = 0
    rejected_count = 0

    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, case_id, status, model_plan_json, input_refs_json,
                   output_refs_json, audit_json, error_message, created_at, updated_at
            FROM simulation_jobs
            ORDER BY id
            """
        ).fetchall()

    for row in rows:
        model_plan = _load_json(row["model_plan_json"], {})
        input_refs = _load_json(row["input_refs_json"], [])
        output_refs = _load_json(row["output_refs_json"], [])
        audit = _load_json(row["audit_json"], {})
        before, after = _case_relative_refs(input_refs if isinstance(input_refs, list) else [])
        unit = units_by_key.get((int(row["case_id"] or 0), before or "", after or ""))
        if not unit:
            continue
        role, variant = _role_and_variant(model_plan if isinstance(model_plan, dict) else {}, str(unit.get("workflow") or ""))
        if not role or role not in roles:
            continue
        candidate_count += 1
        usable_refs = _usable_output_refs(output_refs if isinstance(output_refs, list) else [])
        status = str(row["status"] or "")
        if status not in {"done", "done_with_issues"} or row["error_message"] or not usable_refs:
            rejected_count += 1
            continue
        record = {
            "ab_unit_id": unit.get("ab_unit_id"),
            "case_id": unit.get("case_id"),
            "customer_raw": unit.get("customer_raw"),
            "view": unit.get("view"),
            "workflow": unit.get("workflow"),
            "variant": variant,
            "variant_role": role,
            "provider": model_plan.get("provider") if isinstance(model_plan, dict) else None,
            "model_name": model_plan.get("model_name") if isinstance(model_plan, dict) else None,
            "simulation_job_id": row["id"],
            "status": status,
            "error_message": row["error_message"],
            "workflow_name": audit.get("workflow_name") if isinstance(audit, dict) else None,
            "qa_scores": audit.get("qa_scores") if isinstance(audit, dict) else None,
            "difference_analysis": audit.get("difference_analysis") if isinstance(audit, dict) else None,
            "fallback_used": bool(audit.get("fallback_used")) if isinstance(audit, dict) else False,
            "ok": True,
            "dry_run": False,
            "output_refs": output_refs,
            "elapsed_seconds": None,
            "finished_at": row["updated_at"] or row["created_at"] or _now(),
            "record_source": "existing_simulation_jobs",
        }
        selected[(str(unit.get("ab_unit_id") or ""), role)] = record

    records = [selected[key] for key in sorted(selected)]
    summary = {
        "generated_at": _now(),
        "scope": "comfyui_ab_existing_records_v1",
        "plan_unit_count": len(units_by_key),
        "role_filter": sorted(roles),
        "candidate_existing_job_count": candidate_count,
        "rejected_existing_job_count": rejected_count,
        "selected_record_count": len(records),
        "selected_unit_count": len({record["ab_unit_id"] for record in records}),
        "selected_by_role": {
            role: sum(1 for record in records if record.get("variant_role") == role)
            for role in sorted(roles)
        },
    }
    return records, summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-json", type=Path, required=True)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--role", action="append", choices=["baseline", "candidate"], default=["baseline"])
    parser.add_argument("--records-output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    plan = json.loads(args.plan_json.read_text(encoding="utf-8"))
    roles = {str(role).strip().lower() for role in args.role if str(role).strip()}
    records, summary = extract_existing_records(plan=plan, db_path=args.db_path, roles=roles)
    _write_jsonl(args.records_output, records)
    _write_json(args.summary_output, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
