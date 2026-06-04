"""T78 slot/crop blocker reduction plan.

This consumes real review tickets only. It can safely apply explicit template
downgrades when the source gate already has a renderable front pair. Crop
touching sources are never auto-locked here; they become manual reselection or
replacement actions unless a later reviewed workflow proves an alternative.
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

from backend import audit, db, render_queue, source_images, source_selection  # noqa: E402
from backend.services.pre_render_gate import _case_rows, evaluate_pre_render_gate  # noqa: E402

DEFAULT_DB_PATH = ROOT / "case-workbench.db"
DEFAULT_JSON_OUTPUT = ROOT / "tasks" / "t78_slot_crop_reduction_plan.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "tasks" / "t78_slot_crop_reduction_plan.md"
UNVERIFIED = "未验证/无法获取"


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


def _group_counts(tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter(
        (str(item.get("ticket_type") or ""), str(item.get("reason_code") or "")) for item in tickets
    )
    return [
        {"ticket_type": ticket_type, "reason_code": reason_code, "count": count}
        for (ticket_type, reason_code), count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _resolved_template_tickets(conn: sqlite3.Connection, reviewer: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, case_id, decision_json, resolved_at
        FROM review_tickets
        WHERE status = 'resolved'
          AND ticket_type = 'slot_fill'
          AND reason_code = 'missing_render_slots'
          AND decision_json LIKE ?
        ORDER BY id
        """,
        (f"%{reviewer}%",),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        decision = _json_dict(row["decision_json"])
        out.append(
            {
                "ticket_id": int(row["id"]),
                "case_id": int(row["case_id"]),
                "decision": decision.get("decision"),
                "target_template_tier": decision.get("target_template_tier"),
                "target_template": decision.get("target_template"),
                "resolved_at": row["resolved_at"],
            }
        )
    return out


def _tier_from_renderable(renderable_slots: list[str]) -> str | None:
    renderable = [str(item) for item in renderable_slots if str(item) in {"front", "oblique", "side"}]
    if "front" not in renderable:
        return None
    if len(renderable) >= 3:
        return "tri"
    if len(renderable) >= 2:
        return "bi"
    return "single"


def _template_for_tier(tier: str | None) -> str | None:
    return {"single": "single-compare", "bi": "bi-compare", "tri": "tri-compare"}.get(str(tier or ""))


def _slot_ticket_actions(
    *,
    open_slot_tickets: list[dict[str, Any]],
    gate_results: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for ticket in open_slot_tickets:
        case_id = int(ticket["case_id"])
        result = gate_results.get(case_id) or {}
        gate = result.get("gate") if isinstance(result.get("gate"), dict) else {}
        plan = gate.get("selection_plan") if isinstance(gate.get("selection_plan"), dict) else {}
        profile = gate.get("source_profile") if isinstance(gate.get("source_profile"), dict) else {}
        source_kind = str(profile.get("source_kind") or "")
        if source_kind in {"generated_output_collection", "manual_not_case_source_directory", "empty", "missing_source_files"}:
            continue
        renderable_slots = [str(item) for item in (plan.get("renderable_slots") or []) if str(item)]
        target_tier = _tier_from_renderable(renderable_slots)
        target_template = _template_for_tier(target_tier)
        if not target_tier or not target_template:
            continue
        current_effective = str(gate.get("effective_template") or "")
        resolution = (
            "stale_slot_ticket_renderable_tri"
            if target_tier == "tri"
            else f"template_downgrade_to_{target_tier}"
        )
        actions.append(
            {
                "case_id": case_id,
                "ticket_id": int(ticket["id"]),
                "ticket_type": ticket["ticket_type"],
                "reason_code": ticket["reason_code"],
                "renderable_slots": renderable_slots,
                "target_template_tier": target_tier,
                "target_template": target_template,
                "current_effective_template": current_effective,
                "resolution": resolution,
                "safe_to_apply": True,
                "blocks_render": bool(ticket.get("blocks_render")),
                "message": ticket.get("message"),
            }
        )
    return actions


def _candidate_rows(conn: sqlite3.Connection, case_id: int) -> list[dict[str, Any]]:
    primary, bound = _case_rows(conn, case_id)
    rows = [primary, *bound]
    primary_metadata = render_queue._skill_metadata_by_file(str(primary.get("skill_image_metadata_json") or ""))
    candidates: list[dict[str, Any]] = []
    for index, source_row in enumerate(rows):
        row_case_id = int(source_row["id"])
        role = str(source_row.get("role") or ("primary" if index == 0 else "bound"))
        case_dir = str(source_row.get("abs_path") or "")
        case_title = Path(case_dir).name or f"case {row_case_id}"
        meta_json = str(source_row.get("meta_json") or "")
        meta = render_queue._parse_case_meta(meta_json)
        raw_files = [str(item) for item in (meta.get("image_files") or []) if item]
        existing_files = [
            str(item)
            for item in source_images.existing_source_image_files(case_dir, raw_files)["existing"]
            if source_images.is_source_image_file(str(item))
        ]
        metadata_by_file = render_queue._skill_metadata_by_file(str(source_row.get("skill_image_metadata_json") or ""))
        review_states = render_queue._image_review_states(meta_json)
        manual_overrides = render_queue._fetch_case_image_overrides(conn, row_case_id)
        for filename in existing_files:
            state = render_queue._state_for_filename(review_states, filename) or {}
            if bool(state.get("render_excluded") or state.get("verdict") == "excluded"):
                continue
            metadata = metadata_by_file.get(filename) or metadata_by_file.get(Path(filename).name)
            fallback = primary_metadata.get(filename) or primary_metadata.get(Path(filename).name)
            metadata = render_queue._selection_metadata_with_fallback(metadata, fallback)
            manual_override = manual_overrides.get(filename) or manual_overrides.get(Path(filename).name)
            phase, phase_source, view, view_source, manual = render_queue._selection_phase_view(
                filename,
                case_title,
                manual_override,
                metadata,
            )
            if phase not in {"before", "after"} or view not in {"front", "oblique", "side"}:
                continue
            candidate: dict[str, Any] = {
                "case_id": row_case_id,
                "source_role": role,
                "filename": filename,
                "render_filename": filename,
                "phase": phase,
                "phase_source": phase_source,
                "view": view,
                "view_source": view_source,
                "manual": manual,
                "review_verdict": state.get("verdict"),
                "angle_confidence": (metadata or {}).get("angle_confidence") if isinstance(metadata, dict) else None,
                "rejection_reason": (metadata or {}).get("rejection_reason") if isinstance(metadata, dict) else None,
                "issues": [str(issue) for issue in ((metadata or {}).get("issues") or [])] if isinstance(metadata, dict) else [],
                "pose": (metadata or {}).get("pose") if isinstance(metadata, dict) else None,
                "direction": (metadata or {}).get("direction") if isinstance(metadata, dict) else None,
                "sharpness_score": (metadata or {}).get("sharpness_score") if isinstance(metadata, dict) else None,
                "brightness": (metadata or {}).get("brightness") if isinstance(metadata, dict) else None,
                "mean_luma": (metadata or {}).get("mean_luma") if isinstance(metadata, dict) else None,
                "luma": (metadata or {}).get("luma") if isinstance(metadata, dict) else None,
                "exposure_score": (metadata or {}).get("exposure_score") if isinstance(metadata, dict) else None,
                "exposure": (metadata or {}).get("exposure") if isinstance(metadata, dict) else None,
                "crop_touches_frame": (metadata or {}).get("crop_touches_frame") if isinstance(metadata, dict) else None,
                "face_crop_touches_frame": (metadata or {}).get("face_crop_touches_frame") if isinstance(metadata, dict) else None,
                "crop_margin": (metadata or {}).get("crop_margin") if isinstance(metadata, dict) else None,
                "face_crop_margin": (metadata or {}).get("face_crop_margin") if isinstance(metadata, dict) else None,
                "identity_similarity": (metadata or {}).get("identity_similarity") if isinstance(metadata, dict) else None,
                "same_person_similarity": (metadata or {}).get("same_person_similarity") if isinstance(metadata, dict) else None,
                "arcface_similarity": (metadata or {}).get("arcface_similarity") if isinstance(metadata, dict) else None,
                "identity_embedding": (metadata or {}).get("identity_embedding") if isinstance(metadata, dict) else None,
                "face_count": (metadata or {}).get("face_count") if isinstance(metadata, dict) else None,
                "identity_provider": (metadata or {}).get("identity_provider") if isinstance(metadata, dict) else None,
            }
            candidate.update(source_selection.candidate_quality(candidate, role))
            candidates.append(candidate)
    return candidates


def _sharpness_ok(candidate: dict[str, Any]) -> bool:
    value = source_selection.float_value(candidate.get("sharpness_score"))
    return value is None or value > 8


def _crop_ok(candidate: dict[str, Any]) -> bool:
    if candidate.get("crop_touches_frame") or candidate.get("face_crop_touches_frame"):
        return False
    margin = source_selection.float_value(candidate.get("crop_margin"))
    face_margin = source_selection.float_value(candidate.get("face_crop_margin"))
    return not ((margin is not None and margin < 0.025) or (face_margin is not None and face_margin < 0.025))


def _pair_ok(pair_quality: dict[str, Any] | None) -> bool:
    if not isinstance(pair_quality, dict):
        return False
    primary = ((pair_quality.get("metrics") or {}).get("primary_judge") or {}) if isinstance(pair_quality, dict) else {}
    if isinstance(primary, dict):
        for component in primary.values():
            if isinstance(component, dict) and str(component.get("status") or "") == "block":
                return False
    for warning in pair_quality.get("warnings") or []:
        if isinstance(warning, dict) and str(warning.get("severity") or "") == "block":
            return False
    return True


def _safe_front_alternative_pairs(conn: sqlite3.Connection, case_id: int) -> list[dict[str, Any]]:
    candidates = _candidate_rows(conn, case_id)
    before_items = [
        item for item in candidates
        if item.get("phase") == "before" and item.get("view") == "front" and _crop_ok(item) and _sharpness_ok(item)
    ]
    after_items = [
        item for item in candidates
        if item.get("phase") == "after" and item.get("view") == "front" and _crop_ok(item) and _sharpness_ok(item)
    ]
    pairs: list[dict[str, Any]] = []
    for before in before_items:
        for after in after_items:
            pair_quality = source_selection.slot_pair_quality("front", before, after)
            if not _pair_ok(pair_quality):
                continue
            pairs.append(
                {
                    "before": {"case_id": before.get("case_id"), "filename": before.get("filename")},
                    "after": {"case_id": after.get("case_id"), "filename": after.get("filename")},
                    "pair_score": (pair_quality or {}).get("pair_score"),
                    "risk_level": (pair_quality or {}).get("risk_level"),
                }
            )
    pairs.sort(key=lambda item: (-(int(item.get("pair_score") or 0)), str(item["before"].get("filename") or "")))
    return pairs


def _crop_reselect_actions(
    *,
    conn: sqlite3.Connection,
    open_crop_tickets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen_cases: set[int] = set()
    for ticket in open_crop_tickets:
        case_id = int(ticket["case_id"])
        if case_id in seen_cases:
            continue
        seen_cases.add(case_id)
        try:
            safe_pairs = _safe_front_alternative_pairs(conn, case_id)
            readiness = "verified"
            error = None
        except Exception as exc:  # noqa: BLE001
            safe_pairs = []
            readiness = UNVERIFIED
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
        actions.append(
            {
                "case_id": case_id,
                "ticket_ids": [
                    int(item["id"]) for item in open_crop_tickets
                    if int(item["case_id"]) == case_id
                ],
                "reason_code": "crop_touches_frame",
                "safe_alternative_pair_count": len(safe_pairs),
                "safe_alternative_pairs": safe_pairs[:5],
                "recommended_action": (
                    "manual_review_safe_alternative_pair" if safe_pairs else "manual_reselect_or_replace_front_source"
                ),
                "auto_lock_applied": False,
                "readiness": readiness,
                "error": error,
            }
        )
    return sorted(actions, key=lambda item: int(item["case_id"]))


def _append_template_trace(meta: dict[str, Any], action: dict[str, Any], *, reviewer: str, now: str) -> dict[str, Any]:
    updated = dict(meta)
    controls = updated.get(source_selection.SOURCE_GROUP_SELECTION_META_KEY)
    if not isinstance(controls, dict):
        controls = {}
    traces = controls.get("template_downgrade_trace")
    if not isinstance(traces, list):
        traces = []
    trace = {
        "task": "T78",
        "reviewer": reviewer,
        "decided_at": now,
        "resolution": action.get("resolution"),
        "target_template_tier": action.get("target_template_tier"),
        "target_template": action.get("target_template"),
        "renderable_slots": action.get("renderable_slots") or [],
        "ticket_id": action.get("ticket_id"),
        "reason": "reduce missing_render_slots blocker only; crop blockers remain fail-closed",
    }
    controls["template_downgrade_trace"] = [*traces[-19:], trace]
    updated[source_selection.SOURCE_GROUP_SELECTION_META_KEY] = controls
    return updated


def _apply_template_action(conn: sqlite3.Connection, action: dict[str, Any], *, reviewer: str) -> bool:
    case_id = int(action["case_id"])
    target_tier = str(action.get("target_template_tier") or "")
    if target_tier not in {"single", "bi", "tri"}:
        return False
    now = _now()
    befores = audit.snapshot_before(conn, [case_id])
    row = conn.execute("SELECT meta_json, manual_template_tier FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not row:
        return False
    meta = _json_dict(row["meta_json"])
    updated_meta = _append_template_trace(meta, action, reviewer=reviewer, now=now)
    conn.execute(
        """
        UPDATE cases
        SET manual_template_tier = ?,
            meta_json = ?,
            indexed_at = ?
        WHERE id = ?
        """,
        (target_tier, json.dumps(updated_meta, ensure_ascii=False, sort_keys=True), now, case_id),
    )
    decision = {
        "decision": action.get("resolution"),
        "reviewer": reviewer,
        "decided_at": now,
        "target_template_tier": target_tier,
        "target_template": action.get("target_template"),
        "renderable_slots": action.get("renderable_slots") or [],
        "note": "T78 safe template downgrade; remaining crop/source blockers are unchanged",
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
    audit.record_after(conn, [case_id], befores, op="t78_template_downgrade", source_route="backend/scripts/t78_slot_crop_reduction.py", actor=reviewer)
    return cur.rowcount > 0


def build_reduction_plan(*, apply_safe: bool = False, reviewer: str = "codex_t78_slot_crop") -> dict[str, Any]:
    db.init_schema()
    errors: list[dict[str, Any]] = []
    gate_results: dict[int, dict[str, Any]] = {}
    with db.connect() as conn:
        open_before = _open_tickets(conn)
        for case_id in _active_case_ids(conn):
            try:
                gate_results[case_id] = evaluate_pre_render_gate(case_id, persist_tickets=False, conn=conn)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    {
                        "case_id": case_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                        "readiness": UNVERIFIED,
                    }
                )
        open_slot_tickets = [
            item for item in open_before
            if item.get("ticket_type") == "slot_fill" and item.get("reason_code") == "missing_render_slots"
        ]
        open_crop_tickets = [
            item for item in open_before
            if item.get("ticket_type") == "source_quality_review" and item.get("reason_code") == "crop_touches_frame"
        ]
        template_actions = _slot_ticket_actions(open_slot_tickets=open_slot_tickets, gate_results=gate_results)
        crop_actions = _crop_reselect_actions(conn=conn, open_crop_tickets=open_crop_tickets)
        applied_ticket_ids: list[int] = []
        if apply_safe:
            for action in template_actions:
                if _apply_template_action(conn, action, reviewer=reviewer):
                    applied_ticket_ids.append(int(action["ticket_id"]))
            conn.commit()
        open_after = _open_tickets(conn)
        resolved_template_tickets = _resolved_template_tickets(conn, reviewer)

    blocked_case_count = sum(
        1 for result in gate_results.values()
        if not bool((result.get("gate") if isinstance(result.get("gate"), dict) else {}).get("passed"))
    )
    passed_case_count = len(gate_results) - blocked_case_count
    crop_safe_pair_count = sum(int(item.get("safe_alternative_pair_count") or 0) for item in crop_actions)
    report = {
        "generated_at": _now(),
        "policy": "t78_slot_crop_reduction_v1",
        "used_mock_data": False,
        "apply_safe": apply_safe,
        "reviewer": reviewer if apply_safe else None,
        "summary": {
            "evaluated_case_count": len(gate_results),
            "passed_case_count": passed_case_count,
            "blocked_case_count": blocked_case_count,
            "evaluation_error_count": len(errors),
            "open_ticket_count": len(open_before),
            "open_ticket_count_after": len(open_after),
            "open_ticket_delta": len(open_after) - len(open_before),
            "open_ticket_groups_before": _group_counts(open_before),
            "open_ticket_groups_after": _group_counts(open_after),
            "open_slot_ticket_count": len(open_slot_tickets),
            "open_crop_ticket_count": len(open_crop_tickets),
            "template_downgrade_candidate_count": len(template_actions),
            "template_downgrade_applied_count": len(applied_ticket_ids),
            "template_downgrade_resolved_total_count": len(resolved_template_tickets),
            "crop_reselect_action_count": len(crop_actions),
            "crop_safe_lock_count": 0,
            "crop_safe_alternative_pair_count": crop_safe_pair_count,
        },
        "template_downgrade_actions": sorted(template_actions, key=lambda item: (int(item["case_id"]), int(item["ticket_id"]))),
        "applied_ticket_ids": applied_ticket_ids,
        "resolved_template_tickets": resolved_template_tickets,
        "crop_reselect_actions": crop_actions,
        "errors": errors,
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# T78 Slot / Crop Blocker Reduction Plan",
        "",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- policy: `{report.get('policy')}`",
        f"- used_mock_data: `{report.get('used_mock_data')}`",
        f"- apply_safe: `{report.get('apply_safe')}`",
        f"- evaluated_case_count: `{summary.get('evaluated_case_count')}`",
        f"- passed_case_count: `{summary.get('passed_case_count')}`",
        f"- blocked_case_count: `{summary.get('blocked_case_count')}`",
        f"- open_ticket_count: `{summary.get('open_ticket_count')}`",
        f"- open_ticket_count_after: `{summary.get('open_ticket_count_after')}`",
        f"- template_downgrade_candidate_count: `{summary.get('template_downgrade_candidate_count')}`",
        f"- template_downgrade_applied_count: `{summary.get('template_downgrade_applied_count')}`",
        f"- template_downgrade_resolved_total_count: `{summary.get('template_downgrade_resolved_total_count')}`",
        f"- open_crop_ticket_count: `{summary.get('open_crop_ticket_count')}`",
        f"- crop_safe_lock_count: `{summary.get('crop_safe_lock_count')}`",
        "",
        "## Template Downgrade Actions",
        "",
    ]
    for item in (report.get("template_downgrade_actions") or [])[:140]:
        lines.append(
            "- "
            f"case `{item.get('case_id')}` ticket `{item.get('ticket_id')}` "
            f"renderable `{json.dumps(item.get('renderable_slots') or [], ensure_ascii=False)}` "
            f"-> `{item.get('target_template_tier')}` / `{item.get('target_template')}` "
            f"resolution `{item.get('resolution')}`"
        )
    if not report.get("template_downgrade_actions"):
        lines.append("- none")
    lines.extend(["", "## Crop Reselect / Replace Actions", ""])
    for item in (report.get("crop_reselect_actions") or [])[:140]:
        lines.append(
            "- "
            f"case `{item.get('case_id')}` tickets `{json.dumps(item.get('ticket_ids') or [], ensure_ascii=False)}` "
            f"safe_alternative_pair_count `{item.get('safe_alternative_pair_count')}` "
            f"action `{item.get('recommended_action')}` readiness `{item.get('readiness')}`"
        )
    if not report.get("crop_reselect_actions"):
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
    parser.add_argument("--reviewer", default="codex_t78_slot_crop")
    parser.add_argument("--json-output", default=str(DEFAULT_JSON_OUTPUT))
    parser.add_argument("--markdown-output", default=str(DEFAULT_MARKDOWN_OUTPUT))
    args = parser.parse_args(argv)

    db.DB_PATH = Path(args.db_path).expanduser().resolve()
    report = build_reduction_plan(apply_safe=bool(args.apply_safe), reviewer=str(args.reviewer))
    write_report(report, Path(args.json_output), Path(args.markdown_output))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if report["summary"].get("evaluation_error_count") else 0


if __name__ == "__main__":
    raise SystemExit(main())
