"""T77 review-ticket cleanup plan.

The goal is to consume stale ticket noise after pre-render gate policy changes,
without pretending that source selection, slot fill, crop repair, or identity
review has been completed. By default this script is report-only. ``--apply-safe``
only resolves stale slot tickets that are now superseded by an invalid-source
ticket for the same case.
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
DEFAULT_JSON_OUTPUT = ROOT / "tasks" / "t77_review_ticket_cleanup_plan.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "tasks" / "t77_review_ticket_cleanup_plan.md"
UNVERIFIED = "未验证/无法获取"
INVALID_SOURCE_KINDS = {
    "generated_output_collection",
    "manual_not_case_source_directory",
    "empty",
    "missing_source_files",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dict(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


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


def _open_tickets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM review_tickets
        WHERE status = 'open'
        ORDER BY case_id, id
        """
    ).fetchall()
    return [_ticket_row(row) for row in rows]


def _ticket_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "case_id": int(row["case_id"]),
        "render_job_id": row["render_job_id"],
        "ticket_type": str(row["ticket_type"] or ""),
        "stage": str(row["stage"] or ""),
        "status": str(row["status"] or ""),
        "blocks_render": bool(row["blocks_render"]),
        "blocks_publish": bool(row["blocks_publish"]),
        "reason_code": str(row["reason_code"] or ""),
        "slot": row["slot"],
        "source_filename": row["source_filename"],
        "message": row["message"],
        "evidence": _json_dict(row["evidence_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _ticket_key(ticket: dict[str, Any]) -> tuple[int, str, str, str, str, str]:
    return (
        int(ticket.get("case_id") or 0),
        str(ticket.get("ticket_type") or ""),
        str(ticket.get("stage") or ""),
        str(ticket.get("reason_code") or ""),
        str(ticket.get("slot") or ""),
        str(ticket.get("source_filename") or ""),
    )


def _planned_state(conn: sqlite3.Connection) -> dict[str, Any]:
    planned_keys: set[tuple[int, str, str, str, str, str]] = set()
    case_source_kinds: dict[int, str] = {}
    case_planned_reasons: dict[int, list[str]] = {}
    errors: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    for case_id in _active_case_ids(conn):
        try:
            result = evaluate_pre_render_gate(case_id, persist_tickets=False, conn=conn)
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
        gate = result.get("gate") if isinstance(result.get("gate"), dict) else {}
        profile = gate.get("source_profile") if isinstance(gate.get("source_profile"), dict) else {}
        case_source_kinds[case_id] = str(profile.get("source_kind") or "")
        reasons: list[str] = []
        for ticket in [item for item in (result.get("tickets") or []) if isinstance(item, dict)]:
            normalized = {
                "case_id": case_id,
                "ticket_type": ticket.get("ticket_type"),
                "stage": ticket.get("stage"),
                "reason_code": ticket.get("reason_code"),
                "slot": ticket.get("slot"),
                "source_filename": ticket.get("source_filename"),
            }
            planned_keys.add(_ticket_key(normalized))
            reason = str(ticket.get("reason_code") or "")
            if reason:
                reason_counts[reason] += 1
                reasons.append(reason)
        case_planned_reasons[case_id] = reasons
    return {
        "planned_keys": planned_keys,
        "case_source_kinds": case_source_kinds,
        "case_planned_reasons": case_planned_reasons,
        "errors": errors,
        "reason_counts": dict(sorted(reason_counts.items())),
    }


def _action_for(ticket: dict[str, Any], source_kind: str) -> str:
    reason = str(ticket.get("reason_code") or "")
    ticket_type = str(ticket.get("ticket_type") or "")
    if source_kind in {"generated_output_collection", "manual_not_case_source_directory", "empty"}:
        return "bind_real_source_directory_or_mark_non_source"
    if source_kind == "missing_source_files":
        return "restore_or_rescan_missing_sources"
    if ticket_type == "slot_fill" or reason == "missing_render_slots":
        return "fill_missing_slot_or_lock_alternative_pair"
    if reason == "crop_touches_frame":
        return "reselect_or_replace_crop_touching_source"
    if reason == "blur_or_low_sharpness":
        return "replace_low_sharpness_source"
    if ticket_type == "identity_review" or reason.startswith("identity_"):
        return "manual_identity_review"
    return "manual_quality_review"


def _manual_action(ticket: dict[str, Any], source_kind: str, duplicates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "case_id": ticket["case_id"],
        "ticket_ids": [item["id"] for item in duplicates],
        "ticket_type": ticket["ticket_type"],
        "reason_code": ticket["reason_code"],
        "slot": ticket.get("slot"),
        "source_filename": ticket.get("source_filename"),
        "source_kind": source_kind or None,
        "recommended_action": _action_for(ticket, source_kind),
        "duplicate_open_ticket_count": len(duplicates),
        "blocks_render": bool(ticket.get("blocks_render")),
        "message": ticket.get("message"),
    }


def _source_kind_from_evidence(ticket: dict[str, Any]) -> str:
    evidence = ticket.get("evidence") if isinstance(ticket.get("evidence"), dict) else {}
    profile = evidence.get("source_profile") if isinstance(evidence.get("source_profile"), dict) else {}
    return str(profile.get("source_kind") or "")


def _safe_resolution(ticket: dict[str, Any], current_source_kind: str, planned_reasons: list[str]) -> dict[str, Any] | None:
    if ticket.get("ticket_type") != "slot_fill" or ticket.get("reason_code") != "missing_render_slots":
        return None
    evidence_source_kind = _source_kind_from_evidence(ticket)
    source_kind = current_source_kind or evidence_source_kind
    if source_kind not in INVALID_SOURCE_KINDS:
        return None
    invalid_reason_present = source_kind in planned_reasons or any(
        reason in planned_reasons for reason in INVALID_SOURCE_KINDS
    )
    if not invalid_reason_present and source_kind not in {"generated_output_collection", "manual_not_case_source_directory"}:
        return None
    return {
        "ticket_id": ticket["id"],
        "case_id": ticket["case_id"],
        "ticket_type": ticket["ticket_type"],
        "reason_code": ticket["reason_code"],
        "slot": ticket.get("slot"),
        "source_kind": source_kind,
        "resolution": "superseded_by_invalid_source_profile",
        "note": "slot ticket was generated from an invalid/non-source directory; source_quality ticket remains the blocker",
    }


def _resolve_superseded(conn: sqlite3.Connection, action: dict[str, Any], *, reviewer: str) -> bool:
    now = _now()
    decision = {
        "decision": action["resolution"],
        "reviewer": reviewer,
        "note": action["note"],
        "source_kind": action.get("source_kind"),
        "decided_at": now,
    }
    cur = conn.execute(
        """
        UPDATE review_tickets
        SET status = 'resolved',
            decision_json = ?,
            updated_at = ?,
            resolved_at = ?
        WHERE id = ? AND status = 'open'
        """,
        (json.dumps(decision, ensure_ascii=False, sort_keys=True), now, now, int(action["ticket_id"])),
    )
    return cur.rowcount > 0


def _group_counts(tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter(
        (str(item.get("ticket_type") or ""), str(item.get("reason_code") or "")) for item in tickets
    )
    return [
        {"ticket_type": ticket_type, "reason_code": reason_code, "count": count}
        for (ticket_type, reason_code), count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def build_cleanup_plan(*, apply_safe: bool = False, reviewer: str = "codex_t77_cleanup") -> dict[str, Any]:
    db.init_schema()
    with db.connect() as conn:
        open_before = _open_tickets(conn)
        planned = _planned_state(conn)
        planned_keys = planned["planned_keys"]
        case_source_kinds = planned["case_source_kinds"]
        case_planned_reasons = planned["case_planned_reasons"]
        safe_resolutions: list[dict[str, Any]] = []
        manual_groups: dict[tuple[int, str, str, str, str], list[dict[str, Any]]] = {}
        stale_manual: list[dict[str, Any]] = []

        for ticket in open_before:
            current_source_kind = str(case_source_kinds.get(int(ticket["case_id"]), ""))
            if _ticket_key(ticket) not in planned_keys:
                safe = _safe_resolution(ticket, current_source_kind, case_planned_reasons.get(int(ticket["case_id"]), []))
                if safe:
                    safe_resolutions.append(safe)
                    continue
                stale_manual.append(
                    {
                        "ticket_id": ticket["id"],
                        "case_id": ticket["case_id"],
                        "ticket_type": ticket["ticket_type"],
                        "reason_code": ticket["reason_code"],
                        "slot": ticket.get("slot"),
                        "source_filename": ticket.get("source_filename"),
                        "recommended_action": "manual_review_stale_ticket",
                        "readiness": UNVERIFIED,
                    }
                )
                continue
            action_key = (
                int(ticket["case_id"]),
                str(ticket["ticket_type"]),
                str(ticket["reason_code"]),
                str(ticket.get("slot") or ""),
                str(ticket.get("source_filename") or ""),
            )
            manual_groups.setdefault(action_key, []).append(ticket)

        applied: list[int] = []
        if apply_safe:
            for action in safe_resolutions:
                if _resolve_superseded(conn, action, reviewer=reviewer):
                    applied.append(int(action["ticket_id"]))
            conn.commit()

        open_after = _open_tickets(conn)

    manual_actions = [
        _manual_action(items[0], str(case_source_kinds.get(int(items[0]["case_id"]), "")), items)
        for _, items in sorted(manual_groups.items(), key=lambda item: item[0])
    ]
    report = {
        "generated_at": _now(),
        "policy": "t77_review_ticket_cleanup_plan_v1",
        "used_mock_data": False,
        "apply_safe": apply_safe,
        "reviewer": reviewer if apply_safe else None,
        "summary": {
            "open_ticket_count": len(open_before),
            "open_ticket_count_after": len(open_after),
            "open_ticket_delta": len(open_after) - len(open_before),
            "safe_resolve_count": len(safe_resolutions),
            "applied_safe_resolution_count": len(applied),
            "manual_action_count": len(manual_actions),
            "stale_manual_review_count": len(stale_manual),
            "planned_reason_counts": planned["reason_counts"],
            "open_ticket_groups_before": _group_counts(open_before),
            "open_ticket_groups_after": _group_counts(open_after),
            "evaluation_error_count": len(planned["errors"]),
        },
        "safe_resolutions": safe_resolutions,
        "applied_ticket_ids": applied,
        "manual_actions": manual_actions,
        "stale_manual_review": stale_manual,
        "errors": planned["errors"],
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# T77 Review Ticket Cleanup Plan",
        "",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- policy: `{report.get('policy')}`",
        f"- used_mock_data: `{report.get('used_mock_data')}`",
        f"- apply_safe: `{report.get('apply_safe')}`",
        f"- open_ticket_count: `{summary.get('open_ticket_count')}`",
        f"- open_ticket_count_after: `{summary.get('open_ticket_count_after')}`",
        f"- safe_resolve_count: `{summary.get('safe_resolve_count')}`",
        f"- applied_safe_resolution_count: `{summary.get('applied_safe_resolution_count')}`",
        f"- manual_action_count: `{summary.get('manual_action_count')}`",
        "",
        "## Safe Resolutions",
        "",
    ]
    safe = report.get("safe_resolutions") if isinstance(report.get("safe_resolutions"), list) else []
    if safe:
        for item in safe[:80]:
            lines.append(
                "- "
                f"ticket `{item.get('ticket_id')}` case `{item.get('case_id')}` "
                f"reason `{item.get('reason_code')}` -> `{item.get('resolution')}` "
                f"source_kind `{item.get('source_kind')}`"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Manual Actions", ""])
    actions = report.get("manual_actions") if isinstance(report.get("manual_actions"), list) else []
    if actions:
        for item in actions[:140]:
            lines.append(
                "- "
                f"case `{item.get('case_id')}` tickets `{json.dumps(item.get('ticket_ids') or [], ensure_ascii=False)}` "
                f"type `{item.get('ticket_type')}` reason `{item.get('reason_code')}` "
                f"slot `{item.get('slot') or ''}` action `{item.get('recommended_action')}`"
            )
    else:
        lines.append("- clear")

    stale = report.get("stale_manual_review") if isinstance(report.get("stale_manual_review"), list) else []
    lines.extend(["", "## Stale Manual Review", ""])
    if stale:
        for item in stale[:80]:
            lines.append(
                "- "
                f"ticket `{item.get('ticket_id')}` case `{item.get('case_id')}` "
                f"reason `{item.get('reason_code')}` readiness `{item.get('readiness')}`"
            )
    else:
        lines.append("- none")

    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    lines.extend(["", "## Errors", ""])
    if errors:
        for item in errors[:40]:
            lines.append(f"- case `{item.get('case_id')}`: `{item.get('error_type')}` {item.get('error')}")
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def write_report(report: dict[str, Any], json_output: Path, markdown_output: Path) -> None:
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    markdown_output.write_text(render_markdown(report), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--apply-safe", action="store_true")
    parser.add_argument("--reviewer", default="codex_t77_cleanup")
    parser.add_argument("--json-output", default=str(DEFAULT_JSON_OUTPUT))
    parser.add_argument("--markdown-output", default=str(DEFAULT_MARKDOWN_OUTPUT))
    args = parser.parse_args(argv)

    db.DB_PATH = Path(args.db_path).expanduser().resolve()
    report = build_cleanup_plan(apply_safe=bool(args.apply_safe), reviewer=str(args.reviewer))
    write_report(report, Path(args.json_output), Path(args.markdown_output))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if report["summary"].get("evaluation_error_count") else 0


if __name__ == "__main__":
    raise SystemExit(main())
