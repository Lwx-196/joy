"""Import T65 formal render review decisions into render_quality.

Only `accept_template_downgrade` can set `can_publish=1`, and only when the
current render_quality row has no hard blockers. All other human decisions are
imported as review evidence with `can_publish=0`.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/Users/a1234/Desktop/案例生成器/case-workbench")
DEFAULT_DB = ROOT / "case-workbench.db"
DEFAULT_VALIDATION = ROOT / "tasks" / "t65_formal_review_decision_validation.json"
DEFAULT_OUTPUT = ROOT / "tasks" / "t65_formal_review_import_report.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "tasks" / "t65_formal_review_import_report.md"


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


def _json_dict(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _metrics_keep_unpublishable(metrics: dict[str, Any]) -> bool:
    for action in metrics.get("action_suggestions") or []:
        if not isinstance(action, dict):
            continue
        gate = action.get("publish_gate")
        if isinstance(gate, dict) and gate.get("can_publish_after_acceptance") is False:
            return True
    for alert in metrics.get("composition_alerts") or []:
        if isinstance(alert, dict) and str(alert.get("code") or "") == "front_source_crop_touches_frame":
            return True
    return False


def _hard_blockers(row: sqlite3.Row, metrics: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if int(row["blocking_count"] or 0) > 0:
        blockers.append("render_quality.blocking_count")
    manifest_status = str(row["manifest_status"] or "")
    if manifest_status in {"missing", "error", "failed"}:
        blockers.append(f"render_quality.manifest_status:{manifest_status}")
    quality_status = str(row["quality_status"] or "")
    if quality_status in {"blocked", "failed"}:
        blockers.append(f"render_quality.quality_status:{quality_status}")
    if _metrics_keep_unpublishable(metrics):
        blockers.append("render_quality.metrics_publish_gate")
    return list(dict.fromkeys(blockers))


def _review_verdict(decision: str, can_publish: bool) -> str:
    if decision == "reject":
        return "rejected"
    if can_publish:
        return "approved"
    return "needs_recheck"


def _note(decision: dict[str, Any], hard_blockers: list[str]) -> str:
    original = str(decision.get("review_note") or "").strip()
    prefix = f"T65 human review: {decision.get('decision')}"
    if hard_blockers:
        prefix += f"; publish blocked by {', '.join(hard_blockers)}"
    return f"{prefix}\n{original}".strip()


def import_review_decisions(validation: dict[str, Any], *, db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    now = _now()
    accepted = [item for item in _as_list(validation.get("accepted_decisions")) if isinstance(item, dict)]
    imported: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for decision in accepted:
            job_id = int(decision.get("job_id") or 0)
            row = conn.execute("SELECT * FROM render_quality WHERE render_job_id = ?", (job_id,)).fetchone()
            if not row:
                missing.append({"job_id": job_id, "case_id": decision.get("case_id"), "reason": "render_quality row not found"})
                continue
            metrics = _json_dict(row["metrics_json"])
            hard_blockers = _hard_blockers(row, metrics)
            decision_value = str(decision.get("decision") or "")
            can_publish = decision_value == "accept_template_downgrade" and not hard_blockers
            event = {
                "scope": "t65_formal_render_review_import_v1",
                "imported_at": now,
                "case_id": decision.get("case_id"),
                "job_id": job_id,
                "reviewer": decision.get("reviewer"),
                "decision": decision_value,
                "review_note": decision.get("review_note"),
                "can_publish": can_publish,
                "hard_blockers": hard_blockers,
            }
            history = metrics.get("t65_human_review_history")
            if not isinstance(history, list):
                history = []
            metrics["t65_human_review"] = event
            metrics["t65_human_review_history"] = [*history[-9:], event]
            conn.execute(
                """
                UPDATE render_quality
                SET review_verdict = ?,
                    reviewer = ?,
                    review_note = ?,
                    can_publish = ?,
                    metrics_json = ?,
                    reviewed_at = ?,
                    updated_at = ?
                WHERE render_job_id = ?
                """,
                (
                    _review_verdict(decision_value, can_publish),
                    str(decision.get("reviewer") or ""),
                    _note(decision, hard_blockers),
                    1 if can_publish else 0,
                    json.dumps(metrics, ensure_ascii=False),
                    now,
                    now,
                    job_id,
                ),
            )
            imported.append(
                {
                    "case_id": decision.get("case_id"),
                    "job_id": job_id,
                    "decision": decision_value,
                    "reviewer": decision.get("reviewer"),
                    "can_publish": can_publish,
                    "review_verdict": _review_verdict(decision_value, can_publish),
                    "hard_blockers": hard_blockers,
                }
            )
        conn.commit()
    return {
        "generated_at": now,
        "scope": "t65_formal_render_review_import_report_v1",
        "run_status": "completed_real_review_import",
        "decision": "已导入 T65 真实人工复核决策；只有 accept_template_downgrade 且无 hard blocker 的 job 被置为 can_publish=true。",
        "used_mock_data": False,
        "summary": {
            "accepted_decision_count": len(accepted),
            "imported_count": len(imported),
            "missing_quality_row_count": len(missing),
            "publishable_import_count": sum(1 for item in imported if item.get("can_publish")),
            "needs_recheck_import_count": sum(1 for item in imported if item.get("review_verdict") == "needs_recheck"),
            "rejected_import_count": sum(1 for item in imported if item.get("review_verdict") == "rejected"),
        },
        "imported_items": imported,
        "missing_items": missing,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# T65 Formal Render Review Import Report",
        "",
        f"- run_status: `{report.get('run_status')}`",
        f"- decision: {report.get('decision')}",
    ]
    for key, value in (report.get("summary") or {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.append("")
    for item in report.get("imported_items") or []:
        lines.extend(
            [
                f"## Case {item.get('case_id')} / Job {item.get('job_id')}",
                "",
                f"- decision: `{item.get('decision')}`",
                f"- review_verdict: `{item.get('review_verdict')}`",
                f"- can_publish: `{item.get('can_publish')}`",
                f"- hard_blockers: `{', '.join(item.get('hard_blockers') or [])}`",
                "",
            ]
        )
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import T65 formal render human review decisions.")
    parser.add_argument("--validation-json", type=Path, default=DEFAULT_VALIDATION)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    report = import_review_decisions(_load_json(args.validation_json), db_path=args.db_path)
    _write_json(args.output, report)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
