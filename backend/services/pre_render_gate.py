"""Pre-render source gate for formal render enqueue."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .. import db, render_queue, source_images
from .. import source_selection
from . import review_ticket_service as tickets

STAGE = "pre_render_gate"
REQUIRED_SLOTS = ("front", "oblique", "side")
SLOT_LABELS = {"front": "正面", "oblique": "45°", "side": "侧面"}


# ---------------------------------------------------------------------------
# P0.2-B accepted_warnings 精确匹配（与 source_selection warning 维度对齐）
# ---------------------------------------------------------------------------


def _ticket_matching_dims(ticket: dict[str, Any]) -> dict[str, Any]:
    """从 ticket 抽出匹配维度。warning-path dims 在 evidence.warning，
    component-path dims 在 evidence.component；优先 component。"""
    evidence = ticket.get("evidence") if isinstance(ticket.get("evidence"), dict) else {}
    component = evidence.get("component") if isinstance(evidence.get("component"), dict) else {}
    warning = evidence.get("warning") if isinstance(evidence.get("warning"), dict) else {}
    dim_source = component or warning or {}
    return {
        "code": str(ticket.get("reason_code") or ""),
        "slot": ticket.get("slot") or dim_source.get("slot") or evidence.get("slot"),
        "selected_files": dim_source.get("selected_files") or [],
        "message_contains": dim_source.get("message_contains") or "",
        "message": str(ticket.get("message") or ""),
    }


def _basename(token: str) -> str:
    """Canonicalize a file token to its basename for matching.

    P0.5 (review interface C-1): source_selection.crop_component produces selected_files
    from raw `image_path/path/filename`（可能是 basename like "术后正面.jpg"），而
    accept_source_group_warning 写入端从 manifest 抽 `group_relative_path`（含前缀如
    "front/术后正面.jpg"）。直接 set intersect 永远空集 → 过滤失效。两侧都跑 basename
    归一化，确保 P0.2-A/B/C 链路真生效。
    """
    text = str(token or "").strip()
    return Path(text).name if text else text


def _accepted_warning_matches(ticket_dims: dict[str, Any], accepted: dict[str, Any]) -> bool:
    """slot+code 必匹配 + (selected_files 交集非空 OR message_contains 子串)；
    accepted 仅给 code+slot 时退化为广义接受。selected_files 双侧 basename 归一化。"""
    if str(accepted.get("code") or "") != str(ticket_dims.get("code") or ""):
        return False
    if str(accepted.get("slot") or "") != str(ticket_dims.get("slot") or ""):
        return False
    accepted_files = [_basename(t) for t in (accepted.get("selected_files") or []) if t]
    ticket_files = [_basename(t) for t in (ticket_dims.get("selected_files") or []) if t]
    mc = str(accepted.get("message_contains") or "")
    if not accepted_files and not mc:
        return True  # broad accept by code+slot
    files_intersect = bool(set(accepted_files) & set(ticket_files)) if accepted_files and ticket_files else False
    msg_match = bool(mc) and mc in (ticket_dims.get("message") or "")
    return files_intersect or msg_match


def _apply_accepted_warnings(
    tickets_in: list[dict[str, Any]],
    accepted_warnings: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """对 ticket 列表应用 accepted_warnings 过滤：被精确匹配的 ticket 丢弃。"""
    if not accepted_warnings:
        return tickets_in
    accepted = [a for a in accepted_warnings if isinstance(a, dict)]
    if not accepted:
        return tickets_in
    out: list[dict[str, Any]] = []
    for ticket in tickets_in:
        dims = _ticket_matching_dims(ticket)
        if any(_accepted_warning_matches(dims, a) for a in accepted):
            continue
        out.append(ticket)
    return out


def _read_accepted_warnings_from_meta(meta_raw: Any) -> list[dict[str, Any]]:
    """从 cases.meta_json 抽 source_group_selection.accepted_warnings；缺失返 []。"""
    if isinstance(meta_raw, str):
        try:
            meta = json.loads(meta_raw or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
    elif isinstance(meta_raw, dict):
        meta = meta_raw
    else:
        meta = {}
    controls = source_selection.selection_controls_from_meta(meta)
    accepted = controls.get("accepted_warnings") if isinstance(controls, dict) else None
    if not isinstance(accepted, list):
        return []
    return [item for item in accepted if isinstance(item, dict)]


def _crop_override_downstream_verified(conn: sqlite3.Connection, case_id: int, trace: dict[str, Any]) -> bool:
    """Return True only when the latest rendered board for this case cleared D6."""
    if not isinstance(trace, dict):
        return False
    try:
        row = conn.execute(
            """
            SELECT b.verdict, b.review_status
            FROM board_delivery_qa b
            JOIN render_jobs j ON j.id = b.job_id
            WHERE j.case_id = ?
            ORDER BY b.assessed_at DESC
            LIMIT 1
            """,
            (case_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    if row is None:
        return False
    return str(row["verdict"] or "") == "clean" or str(row["review_status"] or "") == "cleared"


def _case_rows(conn: sqlite3.Connection, case_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    row = conn.execute(
        """
        SELECT id, abs_path, meta_json, skill_image_metadata_json, tags_json, manual_blocking_issues_json,
               template_tier, manual_template_tier
        FROM cases
        WHERE id = ? AND trashed_at IS NULL
        """,
        (case_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"case {case_id} not found")
    primary = {
        "id": int(row["id"]),
        "abs_path": row["abs_path"],
        "meta_json": row["meta_json"],
        "skill_image_metadata_json": row["skill_image_metadata_json"],
        "role": "primary",
        "tags_json": row["tags_json"],
        "manual_blocking_issues_json": row["manual_blocking_issues_json"],
        "template_tier": row["template_tier"],
        "manual_template_tier": row["manual_template_tier"],
    }
    binding_ids = render_queue._source_binding_case_ids(row["meta_json"])
    if not binding_ids:
        return primary, []
    placeholders = ",".join("?" * len(binding_ids))
    fetched = conn.execute(
        f"""
        SELECT id, abs_path, meta_json, skill_image_metadata_json
        FROM cases
        WHERE trashed_at IS NULL AND id IN ({placeholders})
        """,
        binding_ids,
    ).fetchall()
    bound = [
        {
            "id": int(item["id"]),
            "abs_path": item["abs_path"],
            "meta_json": item["meta_json"],
            "skill_image_metadata_json": item["skill_image_metadata_json"],
            "role": "bound",
        }
        for item in fetched
    ]
    return primary, bound


def _profile(primary: dict[str, Any], bound: list[dict[str, Any]]) -> dict[str, Any]:
    if bound:
        profile = dict(render_queue._merged_bound_profile([primary, *bound]))
        profile["bound_case_ids"] = [int(item["id"]) for item in bound]
    else:
        profile = dict(render_queue._case_source_profile(primary.get("meta_json"), primary.get("abs_path")))
    if source_images.case_marked_not_source(
        render_queue._json_list(str(primary.get("tags_json") or "")),
        render_queue._json_list(str(primary.get("manual_blocking_issues_json") or "")),
    ):
        profile = {**profile, "source_kind": "manual_not_case_source_directory", "manual_not_source": True}
    return profile


def _ticket(
    *,
    ticket_type: str,
    reason_code: str,
    message: str,
    evidence: dict[str, Any],
    blocks_render: bool = True,
    blocks_publish: bool = True,
    slot: str | None = None,
    source_filename: str | None = None,
) -> dict[str, Any]:
    return {
        "ticket_type": ticket_type,
        "stage": STAGE,
        "status": "open",
        "blocks_render": blocks_render,
        "blocks_publish": blocks_publish,
        "reason_code": reason_code,
        "slot": slot,
        "source_filename": source_filename,
        "message": message,
        "evidence": evidence,
    }


def _template_from_tier(value: Any) -> str | None:
    text = str(value or "").strip()
    return {
        "single": "single-compare",
        "single-compare": "single-compare",
        "bi": "bi-compare",
        "bi-compare": "bi-compare",
        "tri": "tri-compare",
        "tri-compare": "tri-compare",
    }.get(text)


def _effective_template_and_required_slots(
    *,
    requested_template: str,
    primary: dict[str, Any],
    selection_plan: dict[str, Any],
) -> tuple[str, list[str]]:
    requested = _template_from_tier(requested_template) or "tri-compare"
    manual_template = _template_from_tier(primary.get("manual_template_tier"))
    effective = manual_template if requested == "tri-compare" and manual_template else requested
    renderable = [
        str(value)
        for value in (selection_plan.get("renderable_slots") or [])
        if str(value) in REQUIRED_SLOTS
    ] if isinstance(selection_plan, dict) else []
    if effective == "single-compare":
        return effective, ["front"]
    if effective == "bi-compare":
        other = next((view for view in ("oblique", "side") if view in renderable), "oblique")
        return effective, ["front", other]
    return "tri-compare", list(REQUIRED_SLOTS)


def _slot_tickets(
    selection_plan: dict[str, Any],
    source_profile: dict[str, Any],
    *,
    required_slots: list[str],
) -> list[dict[str, Any]]:
    source_kind = str(source_profile.get("source_kind") or "")
    if source_kind in {
        "generated_output_collection",
        "manual_not_case_source_directory",
        "empty",
        "missing_source_files",
    }:
        return []
    required = [slot for slot in required_slots if slot in SLOT_LABELS]
    raw_missing = selection_plan.get("missing_slots") if isinstance(selection_plan, dict) else []
    if not isinstance(raw_missing, list):
        raw_missing = []
    missing = [
        item for item in raw_missing
        if isinstance(item, dict) and str(item.get("view") or "") in required
    ]
    if not missing:
        slots = selection_plan.get("slots") if isinstance(selection_plan, dict) else {}
        slots = slots if isinstance(slots, dict) else {}
        for view in required:
            slot = slots.get(view) if isinstance(slots.get(view), dict) else {}
            role_missing = [
                *([] if isinstance(slot.get("before"), dict) else ["before"]),
                *([] if isinstance(slot.get("after"), dict) else ["after"]),
            ]
            if role_missing:
                missing.append({"view": view, "missing": role_missing})
    if not missing:
        renderable = selection_plan.get("renderable_slots") if isinstance(selection_plan, dict) else []
        renderable = renderable if isinstance(renderable, list) else []
        if source_kind in {"empty", "insufficient_source_photos", "missing_before_after_pair"}:
            if not any(str(view) in renderable for view in required):
                missing = [{"view": "front", "missing": ["before", "after"]}]
    if not missing:
        return []
    slot = str((missing[0] or {}).get("view") or "") if isinstance(missing[0], dict) else None
    slot = slot if slot in SLOT_LABELS else None
    readable = []
    for item in missing:
        if not isinstance(item, dict):
            continue
        view = str(item.get("view") or "")
        roles = ["术前" if role == "before" else "术后" for role in item.get("missing") or []]
        readable.append(f"{SLOT_LABELS.get(view, view)}缺{'/'.join(roles)}")
    message = "正式出图已阻断：源图槽位未配齐（" + "；".join(readable or ["缺少术前/术后配对"]) + "）。"
    return [
        _ticket(
            ticket_type="slot_fill",
            reason_code="missing_render_slots",
            message=message,
            slot=slot,
            evidence={
                "missing_slots": missing,
                "required_slots": list(REQUIRED_SLOTS),
                "effective_required_slots": required,
                "renderable_slots": selection_plan.get("renderable_slots") or [],
                "source_profile": source_profile,
                "recommended_action": "slot_fill",
            },
        )
    ]


def _component_ticket(
    *,
    view: str,
    component_name: str,
    component: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any] | None:
    status = str(component.get("status") or "")
    if status == "ok":
        return None
    code = str(component.get("code") or "")
    if component_name == "identity":
        reason = code or "identity_not_verified"
        return _ticket(
            ticket_type="identity_review",
            reason_code=reason,
            message=str(component.get("message") or "同一人证据未通过，需人工复核"),
            slot=view,
            blocks_render=True,
            blocks_publish=True,
            evidence={
                "slot": view,
                "component": component,
                "before": before,
                "after": after,
                "recommended_action": "identity_review",
            },
        )
    if status == "block":
        return _ticket(
            ticket_type="source_quality_review",
            reason_code=code or f"{component_name}_block",
            message=str(component.get("message") or "源图质量阻断正式出图"),
            slot=view,
            blocks_render=True,
            blocks_publish=True,
            evidence={
                "slot": view,
                "component": component,
                "before": before,
                "after": after,
                "recommended_action": "source_quality_review",
            },
        )
    return None


def _pair_tickets(selection_plan: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    slots = selection_plan.get("slots") if isinstance(selection_plan, dict) else {}
    if not isinstance(slots, dict):
        return out
    for view, slot in slots.items():
        if view not in REQUIRED_SLOTS or not isinstance(slot, dict):
            continue
        before = slot.get("before") if isinstance(slot.get("before"), dict) else None
        after = slot.get("after") if isinstance(slot.get("after"), dict) else None
        if not before or not after:
            continue
        pair_quality = slot.get("pair_quality") if isinstance(slot.get("pair_quality"), dict) else {}
        primary = ((pair_quality.get("metrics") or {}).get("primary_judge") or {}) if isinstance(pair_quality, dict) else {}
        if isinstance(primary, dict):
            for component_name in ("identity", "exposure", "crop"):
                component = primary.get(component_name)
                if isinstance(component, dict):
                    item = _component_ticket(
                        view=str(view),
                        component_name=component_name,
                        component=component,
                        before=before,
                        after=after,
                    )
                    if item:
                        out.append(item)
        warnings = pair_quality.get("warnings") if isinstance(pair_quality, dict) else []
        for warning in warnings or []:
            if not isinstance(warning, dict):
                continue
            code = str(warning.get("code") or "")
            severity = str(warning.get("severity") or "")
            # P0.2-B: 移除 crop_touches_frame 硬排除，让 warning-path 也能产 ticket；
            # 下方 dedup 会处理与 _component_ticket 的重复，accepted_warnings 会做精确豁免。
            if severity == "block" and code != "identity_embedding_mismatch":
                out.append(
                    _ticket(
                        ticket_type="source_quality_review",
                        reason_code=code or "source_quality_block",
                        message=str(warning.get("message") or "源图质量阻断正式出图"),
                        slot=str(view),
                        evidence={"slot": view, "warning": warning, "before": before, "after": after},
                    )
                )
        for role, candidate in (("before", before), ("after", after)):
            sharpness = candidate.get("sharpness_score")
            try:
                sharpness_value = float(sharpness)
            except (TypeError, ValueError):
                sharpness_value = None
            if sharpness_value is not None and sharpness_value <= 8:
                out.append(
                    _ticket(
                        ticket_type="source_quality_review",
                        reason_code="blur_or_low_sharpness",
                        message="源图清晰度过低，阻断正式出图",
                        slot=str(view),
                        source_filename=str(candidate.get("filename") or ""),
                        evidence={"slot": view, "role": role, "candidate": candidate, "sharpness_score": sharpness_value},
                    )
                )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str | None, str | None]] = set()
    for item in out:
        key = (
            str(item.get("ticket_type") or ""),
            str(item.get("reason_code") or ""),
            item.get("slot"),
            item.get("source_filename"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _source_profile_tickets(source_profile: dict[str, Any]) -> list[dict[str, Any]]:
    source_kind = str(source_profile.get("source_kind") or "")
    if int(source_profile.get("missing_source_count") or 0) > 0 or source_kind == "missing_source_files":
        return [
            _ticket(
                ticket_type="source_quality_review",
                reason_code="missing_source_files",
                message="正式出图已阻断：源组里有历史图片记录在当前磁盘不可读。",
                evidence={"source_profile": source_profile, "recommended_action": "manual_restore_or_rescan"},
            )
        ]
    if source_kind in {"manual_not_case_source_directory", "generated_output_collection", "empty"}:
        return [
            _ticket(
                ticket_type="source_quality_review",
                reason_code=source_kind or "invalid_source_directory",
                message="正式出图已阻断：当前目录不是可用于正式出图的真实源照片集合。",
                evidence={"source_profile": source_profile, "recommended_action": "source_quality_review"},
            )
        ]
    return []


def evaluate_pre_render_gate(
    case_id: int,
    *,
    template: str = "tri-compare",
    semantic_judge: str = "auto",
    persist_tickets: bool = False,
    accepted_warnings: list[dict[str, Any]] | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """P0.2-B: accepted_warnings 可显式注入；为 None 时自动从 cases.meta_json 读取
    source_group_selection.accepted_warnings（兼容已有 operator 接受流程）。"""
    owns_conn = conn is None
    active = conn or db.get_conn()
    try:
        primary, bound = _case_rows(active, case_id)
        source_profile = _profile(primary, bound)
        selection_context = render_queue._build_render_selection_context(active, [primary, *bound], {})
        selection_plan = selection_context["plan"]
        effective_template, required_slots = _effective_template_and_required_slots(
            requested_template=template,
            primary=primary,
            selection_plan=selection_plan,
        )
        planned_tickets = [
            *_source_profile_tickets(source_profile),
            *_slot_tickets(selection_plan, source_profile, required_slots=required_slots),
            *_pair_tickets(selection_plan),
        ]

        # Filter tickets that have already been resolved by manual decisions
        controls = selection_plan.get("selection_controls") or {}
        decisions = controls.get("ticket_decisions") or []
        resolved_keys = set()
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            if decision.get("decision") not in {
                "identity_review",
                "source_quality_review",
                "manual_quality_review",
                "slot_fill",
            }:
                continue
            reason_code = str(decision.get("reason_code"))
            evidence = decision.get("evidence") if isinstance(decision.get("evidence"), dict) else {}
            slot = decision.get("slot") or evidence.get("slot")
            if reason_code == "crop_touches_frame" and not _crop_override_downstream_verified(
                active,
                case_id,
                decision,
            ):
                continue
            resolved_keys.add((reason_code, slot))
        planned_tickets = [
            t for t in planned_tickets
            if (str(t.get("reason_code")), t.get("slot")) not in resolved_keys
        ]

        # P0.2-B: 应用 operator 已接受的 warnings（slot+code+selected_files/message_contains 精确匹配）。
        # 显式入参优先；否则从 cases.meta_json 自动读取以保持 operator UI 既有流程兼容。
        if accepted_warnings is None:
            accepted_warnings = _read_accepted_warnings_from_meta(primary.get("meta_json"))
        if accepted_warnings:
            planned_tickets = _apply_accepted_warnings(planned_tickets, accepted_warnings)

        persisted: list[dict[str, Any]] = []
        if persist_tickets:
            for item in planned_tickets:
                persisted.append(
                    tickets.upsert_open_ticket(
                        active,
                        case_id=case_id,
                        render_job_id=None,
                        ticket_type=str(item["ticket_type"]),
                        stage=str(item["stage"]),
                        reason_code=str(item["reason_code"]),
                        message=str(item["message"]),
                        evidence=item.get("evidence") if isinstance(item.get("evidence"), dict) else {},
                        blocks_render=bool(item.get("blocks_render")),
                        blocks_publish=bool(item.get("blocks_publish")),
                        slot=item.get("slot"),
                        source_filename=item.get("source_filename"),
                    )
                )
        result_tickets = persisted if persist_tickets else planned_tickets
        blocks_render = [ticket for ticket in result_tickets if bool(ticket.get("blocks_render"))]
        gate = {
            "case_id": case_id,
            "passed": not blocks_render,
            "template": template,
            "effective_template": effective_template,
            "required_slots": required_slots,
            "semantic_judge": semantic_judge,
            "policy": "pre_render_source_gate_v1",
            "stage": STAGE,
            "ticket_count": len(result_tickets),
            "blocks_render_count": len(blocks_render),
            "source_profile": source_profile,
            "selection_plan": selection_plan,
        }
        return {"gate": gate, "tickets": result_tickets}
    finally:
        if owns_conn:
            active.close()
