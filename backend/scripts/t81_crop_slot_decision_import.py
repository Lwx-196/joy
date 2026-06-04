"""Import T80 crop/slot review decisions.

This script is fail-closed by design. Blank drafts or half-filled decisions are
reported as pending/invalid and never mutate tickets. Only rows with a real
reviewer and a valid action are importable, and DB writes require --apply.
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
from backend.services import review_ticket_service  # noqa: E402

DEFAULT_DRAFT_PATH = ROOT / "tasks" / "t80_crop_slot_review_packet" / "review_decisions_draft.json"
DEFAULT_REPORT_JSON = ROOT / "tasks" / "t81_crop_slot_decision_import_status.json"
DEFAULT_REPORT_MD = ROOT / "tasks" / "t81_crop_slot_decision_import_status.md"
UNVERIFIED = "未验证/无法获取"

CROP_ACTIONS = {
    "accept_current_pair": "source_quality_review",
    "needs_reselect_pair": "reselect_pair",
    "needs_replace_source": "source_quality_review",
    "defer_no_safe_alternative": "source_quality_review",
}
SLOT_ACTIONS = {
    "manual_phase_view_override": "slot_fill",
    "restore_or_add_source_photos": "slot_fill",
    "bind_or_rescan_real_source": "slot_fill",
    "template_policy_review": "slot_fill",
    "defer": "slot_fill",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _ints(values: Any) -> list[int]:
    out: list[int] = []
    if not isinstance(values, list):
        return out
    for value in values:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(out))


def _strings(values: Any) -> list[str]:
    raw_values = values if isinstance(values, list) else [values] if values else []
    out: list[str] = []
    for value in raw_values:
        cleaned = _clean(value)
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_item(
    *,
    kind: str,
    item: dict[str, Any],
    action_map: dict[str, str],
) -> dict[str, Any]:
    reviewer = _clean(item.get("reviewer"))
    action = _clean(item.get("action"))
    note = _clean(item.get("note")) or None
    ticket_ids = _ints(item.get("ticket_ids"))
    status = "importable"
    reason = None
    decision = action_map.get(action)
    if not reviewer and not action:
        status = "pending_blank"
        reason = "reviewer/action blank"
    elif not reviewer:
        status = "invalid"
        reason = "missing reviewer"
    elif not action:
        status = "invalid"
        reason = "missing action"
    elif action not in action_map:
        status = "invalid"
        reason = "unsupported action"
    elif not ticket_ids:
        status = "invalid"
        reason = "missing ticket_ids"
    return {
        "kind": kind,
        "unit_id": _clean(item.get("unit_id")),
        "case_id": int(item.get("case_id") or 0),
        "ticket_ids": ticket_ids,
        "reviewer": reviewer or None,
        "action": action or None,
        "decision": decision,
        "note": note,
        "status": status,
        "reason": reason,
        "selected_before": item.get("selected_before"),
        "selected_after": item.get("selected_after"),
        "selected_assets": _strings(item.get("selected_assets")),
    }


def normalize_draft(draft: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in draft.get("crop_decisions") or []:
        if isinstance(item, dict):
            items.append(_normalize_item(kind="crop", item=item, action_map=CROP_ACTIONS))
    for item in draft.get("slot_decisions") or []:
        if isinstance(item, dict):
            items.append(_normalize_item(kind="slot", item=item, action_map=SLOT_ACTIONS))
    return items


def _apply_one(conn: sqlite3.Connection, item: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    payload = {
        "t81_unit_id": item["unit_id"],
        "t81_action": item["action"],
        "t81_kind": item["kind"],
        "selected_before": item.get("selected_before"),
        "selected_after": item.get("selected_after"),
        "selected_assets": item.get("selected_assets") or [],
    }
    for ticket_id in item["ticket_ids"]:
        try:
            updated = review_ticket_service.apply_ticket_decision(
                conn,
                ticket_id=int(ticket_id),
                decision=str(item["decision"]),
                reviewer=str(item["reviewer"]),
                note=item.get("note"),
                payload=payload,
            )
            results.append({"ticket_id": int(ticket_id), "status": "applied", "updated_status": updated["status"]})
        except Exception as exc:  # noqa: BLE001 - report per-ticket failure without hiding others.
            results.append(
                {
                    "ticket_id": int(ticket_id),
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {str(exc)[:400]}",
                }
            )
    return results


def build_import_report(
    *,
    draft_path: Path = DEFAULT_DRAFT_PATH,
    apply: bool = False,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    draft = _load_json(draft_path)
    items = normalize_draft(draft)
    status_counts = Counter(item["status"] for item in items)
    kind_counts = Counter(item["kind"] for item in items)
    action_counts = Counter(str(item.get("action") or "blank") for item in items)
    importable = [item for item in items if item["status"] == "importable"]
    invalid = [item for item in items if item["status"] == "invalid"]
    pending = [item for item in items if item["status"] == "pending_blank"]

    applied_results: list[dict[str, Any]] = []
    if apply and importable:
        if conn is None:
            with db.connect() as owned_conn:
                for item in importable:
                    item_result = {
                        "unit_id": item["unit_id"],
                        "kind": item["kind"],
                        "ticket_results": _apply_one(owned_conn, item),
                    }
                    applied_results.append(item_result)
        else:
            for item in importable:
                item_result = {
                    "unit_id": item["unit_id"],
                    "kind": item["kind"],
                    "ticket_results": _apply_one(conn, item),
                }
                applied_results.append(item_result)

    applied_ticket_count = sum(
        1
        for item in applied_results
        for ticket in item.get("ticket_results", [])
        if ticket.get("status") == "applied"
    )
    failed_ticket_count = sum(
        1
        for item in applied_results
        for ticket in item.get("ticket_results", [])
        if ticket.get("status") == "failed"
    )
    return {
        "generated_at": _now(),
        "draft_path": str(draft_path),
        "mode": "apply" if apply else "dry_run",
        "ready_to_import": bool(importable) and not invalid,
        "summary": {
            "decision_count": len(items),
            "crop_decision_count": kind_counts.get("crop", 0),
            "slot_decision_count": kind_counts.get("slot", 0),
            "importable_decision_count": len(importable),
            "pending_blank_decision_count": len(pending),
            "invalid_decision_count": len(invalid),
            "applied_ticket_count": applied_ticket_count,
            "failed_ticket_count": failed_ticket_count,
        },
        "status_counts": dict(status_counts),
        "action_counts": dict(action_counts),
        "importable_decisions": importable,
        "invalid_decisions": invalid,
        "pending_sample": pending[:20],
        "applied_results": applied_results,
        "readiness_reason": (
            "found importable real reviewer/action decisions"
            if importable
            else f"{UNVERIFIED}: review_decisions_draft.json has no filled reviewer/action decisions"
        ),
        "mutation_note": (
            "DB tickets were updated via review_ticket_service.apply_ticket_decision"
            if applied_ticket_count
            else "No DB mutation was made" if not apply else "Apply mode ran but no tickets were updated"
        ),
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    summary = report["summary"]
    lines = [
        "# T81 Crop/Slot Decision Import Status",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- mode: `{report['mode']}`",
        f"- ready_to_import: `{report['ready_to_import']}`",
        f"- decision_count: `{summary['decision_count']}`",
        f"- importable_decision_count: `{summary['importable_decision_count']}`",
        f"- pending_blank_decision_count: `{summary['pending_blank_decision_count']}`",
        f"- invalid_decision_count: `{summary['invalid_decision_count']}`",
        f"- applied_ticket_count: `{summary['applied_ticket_count']}`",
        f"- failed_ticket_count: `{summary['failed_ticket_count']}`",
        f"- readiness_reason: {report['readiness_reason']}",
        f"- mutation_note: {report['mutation_note']}",
        "",
        "## Action Counts",
        "",
    ]
    for action, count in sorted(report["action_counts"].items()):
        lines.append(f"- `{action}`: `{count}`")
    if report["invalid_decisions"]:
        lines.extend(["", "## Invalid Decisions", ""])
        for item in report["invalid_decisions"][:20]:
            lines.append(f"- `{item['unit_id']}` case `{item['case_id']}`: {item['reason']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--draft", type=Path, default=DEFAULT_DRAFT_PATH)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_REPORT_JSON)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_REPORT_MD)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    report = build_import_report(draft_path=args.draft, apply=args.apply)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, args.markdown_output)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
