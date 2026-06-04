"""T76 pre-render ticket cleanup digest.

This script materializes real pre-render gate blockers as review tickets and
turns slot/crop blockers into an operator-facing repair queue. It never
resolves hard blockers or fabricates source choices.
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

from backend import db  # noqa: E402
from backend.services.pre_render_gate import evaluate_pre_render_gate  # noqa: E402

DEFAULT_DB_PATH = ROOT / "case-workbench.db"
DEFAULT_JSON_OUTPUT = ROOT / "tasks" / "t76_pre_render_ticket_cleanup_report.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "tasks" / "t76_pre_render_ticket_cleanup_report.md"
UNVERIFIED = "未验证/无法获取"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _active_case_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        """
        SELECT id
        FROM cases
        WHERE trashed_at IS NULL
        ORDER BY id
        """
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _open_ticket_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM review_tickets WHERE status = 'open'").fetchone()[0])


def _ticket_group_counts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ticket_type, reason_code, COUNT(*) AS count
        FROM review_tickets
        WHERE status = 'open'
        GROUP BY ticket_type, reason_code
        ORDER BY count DESC, ticket_type, reason_code
        """
    ).fetchall()
    return [
        {"ticket_type": row["ticket_type"], "reason_code": row["reason_code"], "count": int(row["count"])}
        for row in rows
    ]


def _source_kind_action(source_kind: str) -> str:
    if source_kind in {"generated_output_collection", "manual_not_case_source_directory", "empty"}:
        return "bind_real_source_directory_or_mark_non_source"
    if source_kind == "missing_source_files":
        return "restore_or_rescan_missing_sources"
    return "fill_missing_slot_or_lock_alternative_pair"


def _slot_action(case_id: int, ticket: dict[str, Any]) -> dict[str, Any]:
    evidence = ticket.get("evidence") if isinstance(ticket.get("evidence"), dict) else {}
    profile = evidence.get("source_profile") if isinstance(evidence.get("source_profile"), dict) else {}
    source_kind = str(profile.get("source_kind") or "")
    return {
        "case_id": case_id,
        "ticket_id": ticket.get("id"),
        "ticket_type": ticket.get("ticket_type"),
        "reason_code": ticket.get("reason_code"),
        "slot": ticket.get("slot"),
        "missing_slots": evidence.get("missing_slots") if isinstance(evidence.get("missing_slots"), list) else [],
        "renderable_slots": evidence.get("renderable_slots") if isinstance(evidence.get("renderable_slots"), list) else [],
        "source_kind": source_kind or None,
        "source_profile_summary": {
            "source_count": profile.get("source_count"),
            "existing_source_count": profile.get("existing_source_count"),
            "missing_source_count": profile.get("missing_source_count"),
            "source_kind": source_kind or None,
        },
        "recommended_action": _source_kind_action(source_kind),
        "blocks_render": bool(ticket.get("blocks_render")),
        "message": ticket.get("message"),
    }


def _crop_files(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    component = evidence.get("component") if isinstance(evidence.get("component"), dict) else {}
    roles = {str(role) for role in (component.get("roles") or []) if str(role)}
    out: list[dict[str, Any]] = []
    for role in ("before", "after"):
        candidate = evidence.get(role) if isinstance(evidence.get(role), dict) else {}
        if roles and role not in roles:
            continue
        filename = str(candidate.get("filename") or candidate.get("render_filename") or "").strip()
        if not filename:
            continue
        out.append(
            {
                "role": role,
                "case_id": candidate.get("case_id"),
                "filename": filename,
                "crop_touches_frame": bool(
                    candidate.get("crop_touches_frame") or candidate.get("face_crop_touches_frame")
                ),
                "crop_margin": candidate.get("crop_margin"),
                "face_crop_margin": candidate.get("face_crop_margin"),
            }
        )
    return out


def _crop_action(case_id: int, ticket: dict[str, Any]) -> dict[str, Any]:
    evidence = ticket.get("evidence") if isinstance(ticket.get("evidence"), dict) else {}
    component = evidence.get("component") if isinstance(evidence.get("component"), dict) else {}
    return {
        "case_id": case_id,
        "ticket_id": ticket.get("id"),
        "ticket_type": ticket.get("ticket_type"),
        "reason_code": ticket.get("reason_code"),
        "slot": ticket.get("slot"),
        "roles": component.get("roles") if isinstance(component.get("roles"), list) else [],
        "files": _crop_files(evidence),
        "component": component,
        "recommended_action": "reselect_or_replace_crop_touching_source",
        "blocks_render": bool(ticket.get("blocks_render")),
        "message": ticket.get("message"),
    }


def _case_result(case_id: int, result: dict[str, Any]) -> dict[str, Any]:
    gate = result.get("gate") if isinstance(result.get("gate"), dict) else {}
    tickets = [ticket for ticket in (result.get("tickets") or []) if isinstance(ticket, dict)]
    return {
        "case_id": case_id,
        "passed": bool(gate.get("passed")),
        "ticket_count": len(tickets),
        "blocks_render_count": int(gate.get("blocks_render_count") or 0),
        "reason_codes": [str(ticket.get("reason_code") or "") for ticket in tickets if ticket.get("reason_code")],
        "ticket_types": [str(ticket.get("ticket_type") or "") for ticket in tickets if ticket.get("ticket_type")],
    }


def build_cleanup_report(*, persist_tickets: bool = False) -> dict[str, Any]:
    """Run the real pre-render gate for all active cases and build cleanup queues."""
    db.init_schema()
    slot_actions: list[dict[str, Any]] = []
    crop_actions: list[dict[str, Any]] = []
    case_results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    ticket_type_counts: Counter[str] = Counter()

    with db.connect() as conn:
        before_open = _open_ticket_count(conn)
        case_ids = _active_case_ids(conn)
        for case_id in case_ids:
            try:
                result = evaluate_pre_render_gate(case_id, persist_tickets=persist_tickets, conn=conn)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {
                        "case_id": case_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                        "readiness": UNVERIFIED,
                    }
                )
                continue
            tickets = [ticket for ticket in (result.get("tickets") or []) if isinstance(ticket, dict)]
            for ticket in tickets:
                reason = str(ticket.get("reason_code") or "")
                ticket_type = str(ticket.get("ticket_type") or "")
                if reason:
                    reason_counts[reason] += 1
                if ticket_type:
                    ticket_type_counts[ticket_type] += 1
                if ticket_type == "slot_fill" and reason == "missing_render_slots":
                    slot_actions.append(_slot_action(case_id, ticket))
                if ticket_type == "source_quality_review" and reason == "crop_touches_frame":
                    crop_actions.append(_crop_action(case_id, ticket))
            case_results.append(_case_result(case_id, result))
        after_open = _open_ticket_count(conn)
        open_ticket_groups = _ticket_group_counts(conn)

    blocked_case_count = sum(1 for item in case_results if not item["passed"])
    passed_case_count = sum(1 for item in case_results if item["passed"])
    status = "tickets_persisted" if persist_tickets else "dry_run"
    if errors:
        status = "completed_with_unverified_errors"
    return {
        "generated_at": _now(),
        "run_status": status,
        "policy": "pre_render_ticket_cleanup_v1",
        "used_mock_data": False,
        "persist_tickets": persist_tickets,
        "summary": {
            "evaluated_case_count": len(case_results),
            "passed_case_count": passed_case_count,
            "blocked_case_count": blocked_case_count,
            "evaluation_error_count": len(errors),
            "slot_action_count": len(slot_actions),
            "crop_action_count": len(crop_actions),
            "reason_counts": dict(sorted(reason_counts.items())),
            "ticket_type_counts": dict(sorted(ticket_type_counts.items())),
            "open_ticket_count_before": before_open,
            "open_ticket_count_after": after_open,
            "open_ticket_delta": after_open - before_open,
            "open_ticket_groups": open_ticket_groups,
        },
        "slot_actions": sorted(slot_actions, key=lambda item: (int(item["case_id"]), str(item.get("slot") or ""))),
        "crop_actions": sorted(crop_actions, key=lambda item: (int(item["case_id"]), str(item.get("slot") or ""))),
        "case_results": sorted(case_results, key=lambda item: int(item["case_id"])),
        "errors": errors,
    }


def write_report(report: dict[str, Any], json_output: Path, markdown_output: Path) -> None:
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    markdown_output.write_text(render_markdown(report), encoding="utf-8")


def render_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# T76 Pre-render Ticket / Slot / Crop Cleanup Report",
        "",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- run_status: `{report.get('run_status')}`",
        f"- persist_tickets: `{report.get('persist_tickets')}`",
        f"- used_mock_data: `{report.get('used_mock_data')}`",
        f"- evaluated_case_count: `{summary.get('evaluated_case_count')}`",
        f"- passed_case_count: `{summary.get('passed_case_count')}`",
        f"- blocked_case_count: `{summary.get('blocked_case_count')}`",
        f"- slot_action_count: `{summary.get('slot_action_count')}`",
        f"- crop_action_count: `{summary.get('crop_action_count')}`",
        f"- open_ticket_count_before: `{summary.get('open_ticket_count_before')}`",
        f"- open_ticket_count_after: `{summary.get('open_ticket_count_after')}`",
        "",
        "## Reason Counts",
        "",
    ]
    reason_counts = summary.get("reason_counts") if isinstance(summary.get("reason_counts"), dict) else {}
    if reason_counts:
        for reason, count in sorted(reason_counts.items(), key=lambda item: (-int(item[1]), item[0])):
            lines.append(f"- `{reason}`: {count}")
    else:
        lines.append(f"- {UNVERIFIED}: no gate reasons were produced")

    lines.extend(["", "## Slot Actions", ""])
    slot_actions = report.get("slot_actions") if isinstance(report.get("slot_actions"), list) else []
    if slot_actions:
        for item in slot_actions[:80]:
            lines.append(
                "- "
                f"case `{item.get('case_id')}` slot `{item.get('slot') or ''}` "
                f"missing `{json.dumps(item.get('missing_slots') or [], ensure_ascii=False)}` "
                f"action `{item.get('recommended_action')}`"
            )
    else:
        lines.append("- clear")

    lines.extend(["", "## Crop Actions", ""])
    crop_actions = report.get("crop_actions") if isinstance(report.get("crop_actions"), list) else []
    if crop_actions:
        for item in crop_actions[:80]:
            file_text = json.dumps(item.get("files") or [], ensure_ascii=False)
            lines.append(
                "- "
                f"case `{item.get('case_id')}` slot `{item.get('slot') or ''}` "
                f"roles `{json.dumps(item.get('roles') or [], ensure_ascii=False)}` "
                f"files `{file_text}` "
                f"action `{item.get('recommended_action')}`"
            )
    else:
        lines.append("- clear")

    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    lines.extend(["", "## Errors", ""])
    if errors:
        for item in errors[:40]:
            lines.append(f"- case `{item.get('case_id')}`: `{item.get('error_type')}` {item.get('error')}")
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--persist-tickets", action="store_true")
    parser.add_argument("--json-output", default=str(DEFAULT_JSON_OUTPUT))
    parser.add_argument("--markdown-output", default=str(DEFAULT_MARKDOWN_OUTPUT))
    args = parser.parse_args(argv)

    db.DB_PATH = Path(args.db_path).expanduser().resolve()
    report = build_cleanup_report(persist_tickets=bool(args.persist_tickets))
    write_report(report, Path(args.json_output), Path(args.markdown_output))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if report.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
