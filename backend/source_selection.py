"""Shared source image ranking for source-group preflight and formal render.

The score here is intentionally business-facing rather than model-internal:
manual review, manual phase/view, confidence, source role, and face-detection
risk all affect whether an image should be selected for formal output.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


POSE_THRESHOLDS = {
    "front": {"yaw": 4.5, "pitch": 7.0, "roll": 4.0, "weighted": 11.0},
    "oblique": {"yaw": 8.0, "pitch": 8.0, "roll": 5.0, "weighted": 14.0},
    "side": {"yaw": 10.0, "pitch": 8.0, "roll": 5.0, "weighted": 16.0},
}
LOW_COMPARISON_VALUE_SLOTS = {"oblique", "side"}
LOW_COMPARISON_VALUE_SCORE = 55
SOURCE_GROUP_SELECTION_META_KEY = "source_group_selection"
SLOTS = {"front", "oblique", "side"}


def float_value(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _float_or_zero(value: Any) -> float:
    parsed = float_value(value)
    return parsed if parsed is not None else 0.0


def pose_delta(view: str, before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any] | None:
    if not before or not after:
        return None
    before_pose = before.get("pose") if isinstance(before.get("pose"), dict) else {}
    after_pose = after.get("pose") if isinstance(after.get("pose"), dict) else {}
    if not before_pose and not after_pose:
        return None
    yaw = abs(_float_or_zero(before_pose.get("yaw")) - _float_or_zero(after_pose.get("yaw")))
    pitch = abs(_float_or_zero(before_pose.get("pitch")) - _float_or_zero(after_pose.get("pitch")))
    roll = abs(_float_or_zero(before_pose.get("roll")) - _float_or_zero(after_pose.get("roll")))
    raw = {
        "yaw": round(yaw, 2),
        "pitch": round(pitch, 2),
        "roll": round(roll, 2),
        "weighted": round(yaw + pitch + roll * 0.5, 2),
    }
    if view not in {"oblique", "side"}:
        return raw
    before_direction = before.get("direction")
    after_direction = after.get("direction")
    same_direction = (
        before_direction
        and after_direction
        and before_direction == after_direction
        and before_direction not in {"center", "unknown", "unspecified"}
    )
    manual_same_slot = (
        before.get("view_source") == "manual"
        and after.get("view_source") == "manual"
        and before.get("view") == after.get("view") == view
    )
    if not same_direction and not manual_same_slot:
        return raw
    yaw_abs = abs(abs(_float_or_zero(before_pose.get("yaw"))) - abs(_float_or_zero(after_pose.get("yaw"))))
    normalized = {
        "yaw": round(yaw_abs, 2),
        "pitch": raw["pitch"],
        "roll": raw["roll"],
        "weighted": round(yaw_abs + raw["pitch"] + raw["roll"] * 0.5, 2),
        "normalization": "profile_abs_yaw_same_direction",
        "raw": raw,
    }
    return normalized if normalized["weighted"] < raw["weighted"] else raw


def pose_delta_within_threshold(view: str, delta: dict[str, Any] | None) -> bool:
    if not isinstance(delta, dict):
        return False
    thresholds = POSE_THRESHOLDS.get(view, {"weighted": 12.0})
    return all(_float_or_zero(delta.get(key)) <= float(limit) for key, limit in thresholds.items())


def render_slot_drop_reason(view: str, quality: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return why a non-front slot should be removed from formal output.

    A source-group lock means "prefer this pair if it is usable"; it should not
    force a panel whose before/after photos no longer have comparison value.
    """
    if view not in LOW_COMPARISON_VALUE_SLOTS or not isinstance(quality, dict):
        return None
    warnings = [item for item in (quality.get("warnings") or []) if isinstance(item, dict)]
    warning_codes = {str(item.get("code") or "") for item in warnings}
    score = int(quality.get("score") or 0)
    should_drop = (
        "pose_delta_large" in warning_codes
        or str(quality.get("severity") or "") == "block"
        or str(quality.get("label") or "") == "risky"
        or score < LOW_COMPARISON_VALUE_SCORE
    )
    if not should_drop:
        return None
    label = {"oblique": "45°", "side": "侧面"}.get(view, view)
    trigger_codes = sorted(code for code in warning_codes if code)
    return {
        "code": "low_comparison_value",
        "slot": view,
        "message": f"{label}术前术后不具备稳定对比价值，已从正式出图降级移除",
        "score": score,
        "trigger_codes": trigger_codes,
    }


def _with_render_slot_drop(view: str, quality: dict[str, Any] | None, reason: dict[str, Any]) -> dict[str, Any]:
    base = dict(quality or {})
    warnings = [item for item in (base.get("warnings") or []) if isinstance(item, dict)]
    warnings.append(
        {
            "code": "render_slot_dropped",
            "severity": "info",
            "message": reason.get("message") or "该槽位已从正式出图降级移除",
        }
    )
    base["render_slot_status"] = "dropped"
    base["drop_reason"] = reason
    base["warnings"] = warnings[:8]
    return base


def _source_key(case_id: Any, filename: Any) -> str | None:
    try:
        cid = int(case_id)
    except (TypeError, ValueError):
        return None
    name = str(filename or "").strip()
    if not name:
        return None
    return f"{cid}:{name}"


def _normalize_lock_image(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        case_id = int(raw.get("case_id"))
    except (TypeError, ValueError):
        return None
    filename = str(raw.get("filename") or "").strip()
    if case_id <= 0 or not filename:
        return None
    return {"case_id": case_id, "filename": filename}


def selection_controls_from_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize source-group operator controls stored in cases.meta_json.

    The controls are intentionally schema-light: locks and accepted warnings are
    audit hints for source selection/render QA. They never move, delete, or copy
    real source photos.
    """
    raw = (meta or {}).get(SOURCE_GROUP_SELECTION_META_KEY)
    if not isinstance(raw, dict):
        raw = {}
    locked_slots: dict[str, dict[str, Any]] = {}
    raw_locks = raw.get("locked_slots") if isinstance(raw.get("locked_slots"), dict) else {}
    for view, item in raw_locks.items():
        slot = str(view or "").strip()
        if slot not in SLOTS or not isinstance(item, dict):
            continue
        before = _normalize_lock_image(item.get("before"))
        after = _normalize_lock_image(item.get("after"))
        if not before or not after:
            continue
        locked_slots[slot] = {
            "before": before,
            "after": after,
            "reviewer": str(item.get("reviewer") or "operator"),
            "reason": str(item.get("reason") or "").strip() or None,
            "updated_at": str(item.get("updated_at") or ""),
        }
    accepted_warnings: list[dict[str, Any]] = []
    raw_acceptances = raw.get("accepted_warnings") if isinstance(raw.get("accepted_warnings"), list) else []
    for item in raw_acceptances:
        if not isinstance(item, dict):
            continue
        slot = str(item.get("slot") or "").strip()
        code = str(item.get("code") or "").strip()
        contains = str(item.get("message_contains") or "").strip()
        if slot not in SLOTS or not code:
            continue
        normalized = {
            "job_id": item.get("job_id"),
            "slot": slot,
            "code": code,
            "message_contains": contains,
            "reviewer": str(item.get("reviewer") or "operator"),
            "note": str(item.get("note") or "").strip() or None,
            "accepted_at": str(item.get("accepted_at") or item.get("updated_at") or ""),
        }
        selected_files = []
        for value in item.get("selected_files") or []:
            text = str(value or "").strip()
            if text:
                selected_files.append(text)
        if selected_files:
            normalized["selected_files"] = list(dict.fromkeys(selected_files))
        selected_pair = item.get("selected_pair") if isinstance(item.get("selected_pair"), dict) else {}
        pair = {
            role: str(selected_pair.get(role) or "").strip()
            for role in ("before", "after")
            if str(selected_pair.get(role) or "").strip()
        }
        if pair:
            normalized["selected_pair"] = pair
        accepted_warnings.append(normalized)
    return {"locked_slots": locked_slots, "accepted_warnings": accepted_warnings}


def candidate_matches_lock(candidate: dict[str, Any], spec: dict[str, Any] | None) -> bool:
    if not isinstance(spec, dict):
        return False
    try:
        case_id = int(spec.get("case_id"))
    except (TypeError, ValueError):
        return False
    filename = str(spec.get("filename") or "").strip()
    if not filename:
        return False
    if int(candidate.get("case_id") or 0) != case_id:
        return False
    names = {
        str(candidate.get("filename") or ""),
        str(candidate.get("render_filename") or ""),
        Path(str(candidate.get("filename") or "")).name,
        Path(str(candidate.get("render_filename") or "")).name,
    }
    return filename in names or Path(filename).name in names


def _apply_lock_marker(candidate: dict[str, Any], view: str, lock: dict[str, Any], role: str) -> None:
    candidate["source_group_lock"] = {
        "locked": True,
        "slot": view,
        "role": role,
        "reviewer": lock.get("reviewer"),
        "reason": lock.get("reason"),
        "updated_at": lock.get("updated_at"),
    }
    reasons = list(candidate.get("selection_reasons") or [])
    if "人工锁定正式出图配对" not in reasons:
        candidate["selection_reasons"] = ["人工锁定正式出图配对", *reasons][:5]


def _locked_pair_quality(
    view: str,
    before: dict[str, Any],
    after: dict[str, Any],
    lock: dict[str, Any],
) -> dict[str, Any]:
    quality = slot_pair_quality(view, before, after) or {
        "score": min(int(before.get("selection_score") or 0), int(after.get("selection_score") or 0)),
        "label": "review",
        "severity": "review",
        "reasons": [],
        "warnings": [],
        "metrics": {},
    }
    quality = dict(quality)
    reasons = list(quality.get("reasons") or [])
    if "人工锁定正式出图配对" not in reasons:
        reasons = ["人工锁定正式出图配对", *reasons]
    warnings = list(quality.get("warnings") or [])
    warnings.append(
        {
            "code": "source_group_slot_locked",
            "severity": "info",
            "message": "该槽位由人工锁定配对，renderer 将优先使用这组候选",
        }
    )
    metrics = dict(quality.get("metrics") or {})
    metrics["source_group_lock"] = {
        "locked": True,
        "slot": view,
        "reviewer": lock.get("reviewer"),
        "reason": lock.get("reason"),
        "updated_at": lock.get("updated_at"),
    }
    quality["reasons"] = reasons[:4]
    quality["warnings"] = warnings[:6]
    quality["metrics"] = metrics
    return quality


def _name_keys(name: Any) -> list[str]:
    value = str(name or "").strip()
    if not value:
        return []
    keys = [f"name:{value}"]
    base = Path(value).name
    if base and base != value:
        keys.append(f"name:{base}")
    return keys


def _candidate_identity_keys(candidate: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    source = _source_key(candidate.get("case_id"), candidate.get("filename"))
    if source:
        keys.add(source)
    for name in (candidate.get("render_filename"), candidate.get("filename")):
        keys.update(_name_keys(name))
    return keys


def _item_names(item: dict[str, Any] | None) -> list[str]:
    if not isinstance(item, dict):
        return []
    names: list[str] = []
    for key in ("name", "render_filename", "filename"):
        value = str(item.get(key) or "").strip()
        if value:
            names.append(value)
    return list(dict.fromkeys(names))


def _render_name_to_source_map(payload: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    provenance = payload.get("render_selection_source_provenance")
    if not isinstance(provenance, list):
        provenance = []
    for item in provenance:
        if not isinstance(item, dict):
            continue
        source = _source_key(item.get("case_id"), item.get("filename"))
        if not source:
            continue
        for name in (item.get("render_filename"), item.get("filename")):
            for key in _name_keys(name):
                mapping[key] = source
    return mapping


def _feedback_key_for_name(render_to_source: dict[str, str], name: Any) -> str | None:
    for key in _name_keys(name):
        if key in render_to_source:
            return render_to_source[key]
    keys = _name_keys(name)
    return keys[0] if keys else None


def _ensure_candidate_feedback(feedback: dict[str, Any], key: str) -> dict[str, Any]:
    penalties = feedback.setdefault("candidate_penalties", {})
    item = penalties.setdefault(
        key,
        {
            "penalty": 0,
            "codes": [],
            "reasons": [],
            "source_job_id": feedback.get("source_job_id"),
        },
    )
    return item


def _add_candidate_penalty(
    feedback: dict[str, Any],
    key: str | None,
    *,
    penalty: int,
    code: str,
    reason: str,
) -> None:
    if not key or code == "cross_case_pair":
        return
    item = _ensure_candidate_feedback(feedback, key)
    item["penalty"] = min(80, int(item.get("penalty") or 0) + int(penalty))
    if code not in item["codes"]:
        item["codes"].append(code)
    if reason not in item["reasons"]:
        item["reasons"].append(reason)


def _add_pair_penalty(
    feedback: dict[str, Any],
    *,
    view: Any,
    before_name: Any,
    after_name: Any,
    before_key: str | None,
    after_key: str | None,
    penalty: int,
    code: str,
    reason: str,
) -> None:
    if code == "cross_case_pair":
        return
    slot = str(view or "").strip()
    if slot not in {"front", "oblique", "side"}:
        return
    before_render = str(before_name or "").strip()
    after_render = str(after_name or "").strip()
    if not before_render or not after_render:
        return
    penalties = feedback.setdefault("pair_penalties", [])
    for item in penalties:
        if (
            isinstance(item, dict)
            and item.get("view") == slot
            and item.get("before_render_filename") == before_render
            and item.get("after_render_filename") == after_render
        ):
            item["penalty"] = min(80, int(item.get("penalty") or 0) + int(penalty))
            if code not in item["codes"]:
                item["codes"].append(code)
            if reason not in item["reasons"]:
                item["reasons"].append(reason)
            return
    penalties.append(
        {
            "view": slot,
            "before_render_filename": before_render,
            "after_render_filename": after_render,
            "before_source_key": before_key,
            "after_source_key": after_key,
            "penalty": min(80, int(penalty)),
            "codes": [code],
            "reasons": [reason],
            "source_job_id": feedback.get("source_job_id"),
        }
    )


def _slot_from_text(text: str) -> str | None:
    if "正面" in text or "front" in text.lower():
        return "front"
    if "45" in text or "45°" in text or "oblique" in text.lower():
        return "oblique"
    if "侧面" in text or "侧脸" in text or "side" in text.lower():
        return "side"
    return None


def _applied_pair_by_slot(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    audit = payload.get("render_selection_audit")
    slots = audit.get("applied_slots") if isinstance(audit, dict) else None
    if not isinstance(slots, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in slots:
        if not isinstance(item, dict):
            continue
        slot = str(item.get("slot") or "").strip()
        if slot in {"front", "oblique", "side"}:
            out[slot] = item
    return out


def render_feedback_from_payload(source_job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Extract selected-image quality feedback from a completed render payload.

    The feedback is intentionally conservative: review-only `cross_case_pair`
    warnings are ignored, while selected-image face/pose/composition failures
    become ranking penalties for the next render. Candidate noise remains audit
    data and does not affect source selection.
    """
    if not isinstance(payload, dict):
        payload = {}
    feedback: dict[str, Any] = {
        "source_job_id": int(source_job_id),
        "candidate_penalties": {},
        "pair_penalties": [],
        "ignored_codes": ["cross_case_pair"],
    }
    render_to_source = _render_name_to_source_map(payload)
    applied_by_slot = _applied_pair_by_slot(payload)

    layers = payload.get("warning_layers")
    selected_actionable = layers.get("selected_actionable") if isinstance(layers, dict) else None
    if not isinstance(selected_actionable, list):
        selected_actionable = []
    for raw_text in selected_actionable:
        text = str(raw_text or "")
        if not text.strip():
            continue
        matched_name = False
        for name_key, source_key in render_to_source.items():
            render_name = name_key.removeprefix("name:")
            if render_name and render_name in text:
                matched_name = True
                if "面部检测失败" in text or "正脸检测失败" in text:
                    _add_candidate_penalty(
                        feedback,
                        source_key,
                        penalty=42,
                        code="selected_face_detection_failure",
                        reason="上一轮正式出图入选图面部检测失败",
                    )
                if "清晰度差" in text:
                    _add_candidate_penalty(
                        feedback,
                        source_key,
                        penalty=18,
                        code="selected_sharpness_mismatch",
                        reason="上一轮正式出图入选图清晰度差异过大",
                    )
        slot = _slot_from_text(text)
        if matched_name or slot not in applied_by_slot:
            continue
        pair = applied_by_slot[slot]
        before = pair.get("before")
        after = pair.get("after")
        before_key = _feedback_key_for_name(render_to_source, before)
        after_key = _feedback_key_for_name(render_to_source, after)
        if "姿态差" in text:
            _add_pair_penalty(
                feedback,
                view=slot,
                before_name=before,
                after_name=after,
                before_key=before_key,
                after_key=after_key,
                penalty=24,
                code="selected_pose_delta_large",
                reason="上一轮正式出图该配对姿态差需复核",
            )
        elif "方向不一致" in text:
            _add_pair_penalty(
                feedback,
                view=slot,
                before_name=before,
                after_name=after,
                before_key=before_key,
                after_key=after_key,
                penalty=20,
                code="selected_direction_mismatch",
                reason="上一轮正式出图该配对方向不一致",
            )
        elif "清晰度差" in text:
            _add_pair_penalty(
                feedback,
                view=slot,
                before_name=before,
                after_name=after,
                before_key=before_key,
                after_key=after_key,
                penalty=18,
                code="selected_pair_sharpness_mismatch",
                reason="上一轮正式出图该配对清晰度差异过大",
            )

    for row in payload.get("selection_quality") or []:
        if not isinstance(row, dict):
            continue
        slot = str(row.get("slot") or "").strip()
        before_names = _item_names(row.get("before") if isinstance(row.get("before"), dict) else None)
        after_names = _item_names(row.get("after") if isinstance(row.get("after"), dict) else None)
        actions = [str(item) for item in (row.get("actions") or []) if str(item)]
        for role, names in (("before", before_names), ("after", after_names)):
            item = row.get(role)
            item = item if isinstance(item, dict) else {}
            has_profile_fallback = isinstance(item.get("profile_fallback"), dict)
            sharpness = float_value(item.get("sharpness_score"))
            role_actions = [action for action in actions if action.startswith(f"{role}:")]
            if has_profile_fallback or any("侧脸兜底" in action for action in role_actions):
                for name in names:
                    _add_candidate_penalty(
                        feedback,
                        _feedback_key_for_name(render_to_source, name),
                        penalty=36,
                        code="selected_profile_alignment_fallback",
                        reason="上一轮正式出图入选图触发侧面轮廓兜底",
                    )
            if sharpness is not None and sharpness <= 0:
                for name in names:
                    _add_candidate_penalty(
                        feedback,
                        _feedback_key_for_name(render_to_source, name),
                        penalty=14,
                        code="selected_zero_sharpness",
                        reason="上一轮正式出图入选图清晰度评分为 0",
                    )
        if any("姿态差" in action for action in actions) and before_names and after_names:
            _add_pair_penalty(
                feedback,
                view=slot,
                before_name=before_names[0],
                after_name=after_names[0],
                before_key=_feedback_key_for_name(render_to_source, before_names[0]),
                after_key=_feedback_key_for_name(render_to_source, after_names[0]),
                penalty=24,
                code="selected_pose_delta_large",
                reason="上一轮正式出图该配对姿态差需复核",
            )

    for alert in payload.get("composition_alerts") or []:
        if not isinstance(alert, dict):
            continue
        slot = str(alert.get("slot") or "").strip()
        if slot not in applied_by_slot:
            continue
        code = str(alert.get("code") or "composition_review")
        if code == "cross_case_pair":
            continue
        pair = applied_by_slot[slot]
        before = pair.get("before")
        after = pair.get("after")
        _add_pair_penalty(
            feedback,
            view=slot,
            before_name=before,
            after_name=after,
            before_key=_feedback_key_for_name(render_to_source, before),
            after_key=_feedback_key_for_name(render_to_source, after),
            penalty=18,
            code=code or "composition_review",
            reason=str(alert.get("message") or "上一轮正式出图构图需复核"),
        )

    if not feedback["candidate_penalties"]:
        feedback.pop("candidate_penalties")
    if not feedback["pair_penalties"]:
        feedback.pop("pair_penalties")
    return feedback


def merge_render_feedbacks(feedbacks: list[dict[str, Any]]) -> dict[str, Any] | None:
    usable = [item for item in feedbacks if isinstance(item, dict) and (item.get("candidate_penalties") or item.get("pair_penalties"))]
    if not usable:
        return None
    source_job_ids: list[int] = []
    merged: dict[str, Any] = {
        "source_job_id": usable[0].get("source_job_id"),
        "source_job_ids": source_job_ids,
        "candidate_penalties": {},
        "pair_penalties": [],
        "ignored_codes": ["cross_case_pair"],
    }
    for feedback in usable:
        try:
            jid = int(feedback.get("source_job_id"))
        except (TypeError, ValueError):
            jid = 0
        if jid and jid not in source_job_ids:
            source_job_ids.append(jid)
        for key, penalty in (feedback.get("candidate_penalties") or {}).items():
            if not isinstance(penalty, dict):
                continue
            target = _ensure_candidate_feedback(merged, str(key))
            target["penalty"] = min(80, int(target.get("penalty") or 0) + int(penalty.get("penalty") or 0))
            for code in penalty.get("codes") or []:
                value = str(code)
                if value and value not in target["codes"]:
                    target["codes"].append(value)
            for reason in penalty.get("reasons") or []:
                value = str(reason)
                if value and value not in target["reasons"]:
                    target["reasons"].append(value)
        for penalty in feedback.get("pair_penalties") or []:
            if not isinstance(penalty, dict):
                continue
            codes = [str(code) for code in (penalty.get("codes") or []) if str(code)]
            reasons = [str(reason) for reason in (penalty.get("reasons") or []) if str(reason)]
            _add_pair_penalty(
                merged,
                view=penalty.get("view"),
                before_name=penalty.get("before_render_filename"),
                after_name=penalty.get("after_render_filename"),
                before_key=penalty.get("before_source_key"),
                after_key=penalty.get("after_source_key"),
                penalty=int(penalty.get("penalty") or 0),
                code=codes[0] if codes else "render_feedback_pair_penalty",
                reason=reasons[0] if reasons else "上一轮正式出图该配对需复核",
            )
            merged_pair = merged["pair_penalties"][-1]
            for code in codes[1:]:
                if code not in merged_pair["codes"]:
                    merged_pair["codes"].append(code)
            for reason in reasons[1:]:
                if reason not in merged_pair["reasons"]:
                    merged_pair["reasons"].append(reason)
    if not merged["candidate_penalties"]:
        merged.pop("candidate_penalties")
    if not merged["pair_penalties"]:
        merged.pop("pair_penalties")
    return merged


def render_feedback_from_history(
    conn: sqlite3.Connection,
    case_id: int,
    *,
    limit: int = 3,
) -> dict[str, Any] | None:
    rows = conn.execute(
        """
        SELECT id, meta_json, manifest_path
        FROM render_jobs
        WHERE case_id = ?
          AND status IN ('done', 'done_with_issues')
        ORDER BY COALESCE(finished_at, enqueued_at) DESC, id DESC
        LIMIT ?
        """,
        (case_id, max(1, int(limit))),
    ).fetchall()
    feedbacks: list[dict[str, Any]] = []
    for row in rows:
        payload: dict[str, Any] = {}
        raw_meta = row["meta_json"] if isinstance(row, sqlite3.Row) else row[1]
        if raw_meta:
            try:
                parsed = json.loads(raw_meta)
                if isinstance(parsed, dict):
                    payload.update(parsed)
            except (TypeError, ValueError):
                pass
        manifest_path = row["manifest_path"] if isinstance(row, sqlite3.Row) else row[2]
        path = Path(str(manifest_path or ""))
        if path.is_file():
            try:
                parsed_manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                parsed_manifest = {}
            if isinstance(parsed_manifest, dict):
                for key in (
                    "render_selection_source_provenance",
                    "render_selection_audit",
                    "selection_quality",
                    "warning_layers",
                    "composition_alerts",
                ):
                    payload.setdefault(key, parsed_manifest.get(key))
        feedback = render_feedback_from_payload(int(row["id"] if isinstance(row, sqlite3.Row) else row[0]), payload)
        if feedback.get("candidate_penalties") or feedback.get("pair_penalties"):
            feedbacks.append(feedback)
    return merge_render_feedbacks(feedbacks)



def _feedback_candidate_matches(
    candidate: dict[str, Any],
    *,
    source_key: str | None,
    render_name: str | None,
) -> bool:
    keys = _candidate_identity_keys(candidate)
    if source_key and source_key in keys:
        return True
    return any(key in keys for key in _name_keys(render_name))


def apply_render_feedback(candidate: dict[str, Any], feedback: dict[str, Any] | None) -> None:
    if not isinstance(feedback, dict):
        return
    keys = _candidate_identity_keys(candidate)
    candidate_penalties = feedback.get("candidate_penalties")
    if isinstance(candidate_penalties, dict):
        matched = [item for key, item in candidate_penalties.items() if key in keys and isinstance(item, dict)]
    else:
        matched = []
    pair_penalties = []
    for item in feedback.get("pair_penalties") or []:
        if not isinstance(item, dict):
            continue
        if _feedback_candidate_matches(
            candidate,
            source_key=str(item.get("before_source_key") or "") or None,
            render_name=str(item.get("before_render_filename") or "") or None,
        ) or _feedback_candidate_matches(
            candidate,
            source_key=str(item.get("after_source_key") or "") or None,
            render_name=str(item.get("after_render_filename") or "") or None,
        ):
            pair_penalties.append(item)
    if pair_penalties:
        candidate["render_feedback_pair_penalties"] = pair_penalties
    if not matched:
        return
    penalty = min(80, sum(int(item.get("penalty") or 0) for item in matched))
    if penalty <= 0:
        return
    codes: list[str] = []
    reasons: list[str] = []
    for item in matched:
        for code in item.get("codes") or []:
            value = str(code)
            if value and value not in codes:
                codes.append(value)
        for reason in item.get("reasons") or []:
            value = str(reason)
            if value and value not in reasons:
                reasons.append(value)
    candidate["selection_score"] = max(0, int(candidate.get("selection_score") or 0) - penalty)
    candidate["render_feedback"] = {
        "source_job_id": feedback.get("source_job_id"),
        "source_job_ids": feedback.get("source_job_ids") or [feedback.get("source_job_id")],
        "penalty": penalty,
        "codes": codes,
        "reasons": reasons,
    }
    selection_reasons = [str(item) for item in (candidate.get("selection_reasons") or []) if item]
    selection_reasons.append("上一轮正式出图诊断反馈已降权")
    candidate["selection_reasons"] = selection_reasons[:6]
    warnings = [item for item in (candidate.get("quality_warnings") or []) if isinstance(item, dict)]
    warnings.append(
        {
            "code": "render_feedback_penalty",
            "severity": "review",
            "message": "上一轮正式出图诊断提示该候选需换片或复核",
        }
    )
    candidate["quality_warnings"] = warnings[:6]
    if str(candidate.get("risk_level") or "ok") == "ok":
        candidate["risk_level"] = "review"


def candidate_quality(image: dict[str, Any], source_role: str) -> dict[str, Any]:
    score = 50
    reasons: list[str] = []
    warnings: list[dict[str, str]] = []
    verdict = str(image.get("review_verdict") or "")
    view = str(image.get("view") or "")
    phase_source = str(image.get("phase_source") or "")
    view_source = str(image.get("view_source") or "")
    rejection_reason = str(image.get("rejection_reason") or "")
    issues = [str(issue) for issue in (image.get("issues") or []) if issue]
    if verdict == "usable":
        score += 28
        reasons.append("人工复核可用")
    elif verdict == "deferred":
        score -= 12
        reasons.append("人工标记低优先")
        warnings.append({"code": "deferred", "severity": "review", "message": "低优先候选，仅在缺少更好照片时使用"})
    elif verdict == "needs_repick":
        score -= 40
        warnings.append({"code": "needs_repick", "severity": "block", "message": "人工标记需换片"})
    if image.get("manual"):
        score += 18
        reasons.append("人工整理过阶段/角度")
    elif phase_source in {"filename", "directory"} and view_source in {"pose", "filename", "directory"}:
        score += 7
        reasons.append("自动识别阶段和角度完整")
    if source_role == "primary":
        score += 5
        reasons.append("来自主目录")
    else:
        score += 2
        reasons.append("来自绑定目录")
    confidence = float_value(image.get("angle_confidence"))
    if confidence is not None:
        if confidence >= 0.9:
            score += 14
            reasons.append("角度置信度高")
        elif confidence >= 0.7:
            score += 8
            reasons.append("角度置信度中等")
        elif confidence >= 0.55:
            score += 2
            warnings.append({"code": "angle_confidence_low", "severity": "review", "message": "角度置信度偏低，建议复核姿态"})
        else:
            score -= 14
            warnings.append({"code": "angle_confidence_low", "severity": "review", "message": "角度置信度低，可能影响术前术后对齐"})
    if rejection_reason == "face_detection_failure" or any("面部检测失败" in issue for issue in issues):
        if view == "front":
            score -= 35
            warnings.append({"code": "front_face_detection_failure", "severity": "block", "message": "正面图面部检测失败，正式出图风险高"})
        elif view in {"oblique", "side"}:
            score -= 6
            warnings.append({"code": "profile_face_detection_review", "severity": "review", "message": "侧面/45°正脸检测失败可接受，但需复核轮廓方向"})
        else:
            score -= 18
            warnings.append({"code": "face_detection_failure", "severity": "review", "message": "面部检测失败，需确认是否适合出图"})
    elif any("正脸检测失败，已使用侧脸检测兜底" in issue for issue in issues):
        warnings.append({"code": "profile_fallback", "severity": "info", "message": "侧脸检测已兜底，不作为阻断"})
        reasons.append("侧脸检测兜底可用")
    if not reasons:
        reasons.append("基础候选")
    if view in {"oblique", "side"} and (
        not image.get("direction") or str(image.get("direction")) in {"unknown", "unspecified", "center"}
    ):
        fn = str(image.get("filename") or image.get("render_filename") or "")
        inferred = None
        if any(token in fn for token in ("右45", "右侧", "右脸")):
            inferred = "right"
        elif any(token in fn for token in ("左45", "左侧", "左脸")):
            inferred = "left"
        if inferred:
            image["direction"] = inferred
            image["direction_source"] = "filename_fallback"
    severity_rank = {"block": 2, "review": 1, "info": 0}
    max_severity = max((severity_rank.get(str(item.get("severity")), 0) for item in warnings), default=0)
    risk_level = "block" if max_severity >= 2 else "review" if max_severity == 1 else "ok"
    return {
        "selection_score": max(0, min(100, round(score))),
        "selection_reasons": reasons[:5],
        "quality_warnings": warnings[:5],
        "risk_level": risk_level,
    }


def candidate_rank(candidate: dict[str, Any]) -> tuple[int, int, int, str]:
    risk_rank = {"ok": 0, "review": 1, "block": 2}.get(str(candidate.get("risk_level") or "ok"), 1)
    source_rank = 0 if candidate.get("source_role") == "primary" else 1
    return (
        risk_rank,
        -int(candidate.get("selection_score") or 0),
        source_rank,
        str(candidate.get("filename") or candidate.get("render_filename") or Path(str(candidate.get("path") or "")).name),
    )


def _matched_pair_feedback(view: str, before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for source in (
        *(before.get("render_feedback_pair_penalties") or []),
        *(after.get("render_feedback_pair_penalties") or []),
    ):
        if not isinstance(source, dict):
            continue
        if str(source.get("view") or "") != view:
            continue
        before_matches = _feedback_candidate_matches(
            before,
            source_key=str(source.get("before_source_key") or "") or None,
            render_name=str(source.get("before_render_filename") or "") or None,
        )
        after_matches = _feedback_candidate_matches(
            after,
            source_key=str(source.get("after_source_key") or "") or None,
            render_name=str(source.get("after_render_filename") or "") or None,
        )
        if not before_matches or not after_matches:
            continue
        key = (
            str(source.get("view") or ""),
            str(source.get("before_render_filename") or ""),
            str(source.get("after_render_filename") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        records.append(source)
    return records


def slot_pair_quality(view: str, before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any] | None:
    if not before or not after:
        return None
    before_score = int(before.get("selection_score") or 0)
    after_score = int(after.get("selection_score") or 0)
    score = min(before_score, after_score)
    reasons: list[str] = []
    warnings: list[dict[str, str]] = []
    metrics: dict[str, Any] = {
        "before_score": before_score,
        "after_score": after_score,
        "same_source_case": before.get("case_id") == after.get("case_id"),
    }
    if before.get("case_id") == after.get("case_id"):
        score += 4
        reasons.append("术前术后来自同一 case")
    else:
        score -= 5
        warnings.append({"code": "cross_case_pair", "severity": "review", "message": "术前术后来自不同 case，需确认同次治疗"})
    if before.get("manual") and after.get("manual"):
        score += 18
        reasons.append("术前术后均为人工精选 pair")
        metrics["manual_pair"] = True
    before_conf = float_value(before.get("angle_confidence"))
    after_conf = float_value(after.get("angle_confidence"))
    if before_conf is not None and after_conf is not None:
        delta = round(abs(before_conf - after_conf), 3)
        metrics["angle_confidence_delta"] = delta
        if delta >= 0.25:
            score -= 10
            warnings.append({"code": "angle_confidence_delta", "severity": "review", "message": "术前术后角度置信度差异较大"})
        elif delta <= 0.08:
            score += 4
            reasons.append("术前术后角度置信度接近")
    pose = pose_delta(view, before, after)
    if pose:
        metrics["pose_delta"] = pose
        weighted = _float_or_zero(pose.get("weighted"))
        is_manual_pair = bool(before.get("manual") and after.get("manual"))
        if pose_delta_within_threshold(view, pose):
            score += 12
            reasons.append("术前术后姿态接近")
        elif weighted <= 14 and view == "front":
            if is_manual_pair:
                reasons.append("人工精选 pair 姿态略有差异（已忽略复核）")
            else:
                score -= 8
                warnings.append({"code": "pose_delta_review", "severity": "review", "message": "正面术前术后姿态略有差异，建议复核"})
        else:
            score -= 18
            warnings.append({"code": "pose_delta_large", "severity": "review", "message": "术前术后姿态差较大，建议换片或人工调整"})
    if view in {"oblique", "side"}:
        before_direction = before.get("direction")
        after_direction = after.get("direction")
        if before_direction and after_direction and before_direction == after_direction and before_direction not in {"center", "unknown", "unspecified"}:
            score += 4
            reasons.append("侧向方向一致")
        elif before_direction and after_direction and before_direction != after_direction:
            score -= 16
            warnings.append({"code": "direction_mismatch", "severity": "review", "message": "侧向术前术后方向不一致"})
    combined_warnings = [
        *(before.get("quality_warnings") or []),
        *(after.get("quality_warnings") or []),
    ]
    if any(str(item.get("severity")) == "block" for item in combined_warnings if isinstance(item, dict)):
        score -= 18
        warnings.append({"code": "candidate_block_risk", "severity": "block", "message": "首选候选含阻断级风险"})
    elif any(str(item.get("severity")) == "review" for item in combined_warnings if isinstance(item, dict)):
        score -= 4
        warnings.append({"code": "candidate_review_risk", "severity": "review", "message": "首选候选含需复核风险"})
    if view in {"oblique", "side"} and any(
        str(item.get("code")) in {"profile_face_detection_review", "profile_fallback"}
        for item in combined_warnings
        if isinstance(item, dict)
    ):
        warnings.append({"code": "profile_expected_review", "severity": "info", "message": "侧面/45°面检提示已降噪为轮廓复核"})
    pair_feedback = _matched_pair_feedback(view, before, after)
    if pair_feedback:
        total_penalty = min(80, sum(int(item.get("penalty") or 0) for item in pair_feedback))
        score -= total_penalty
        feedback_reasons: list[str] = []
        feedback_codes: list[str] = []
        for item in pair_feedback:
            for code in item.get("codes") or []:
                value = str(code)
                if value and value not in feedback_codes:
                    feedback_codes.append(value)
            for reason in item.get("reasons") or []:
                value = str(reason)
                if value and value not in feedback_reasons:
                    feedback_reasons.append(value)
        metrics["render_feedback_penalty"] = total_penalty
        metrics["render_feedback_source_job_id"] = pair_feedback[0].get("source_job_id")
        metrics["render_feedback_codes"] = feedback_codes
        warnings.append(
            {
                "code": "render_feedback_pair_penalty",
                "severity": "review",
                "message": "上一轮正式出图诊断提示该配对需重选或复核",
            }
        )
        reasons.extend(feedback_reasons[:2])
    score = max(0, min(100, round(score)))
    severity_rank = {"block": 2, "review": 1, "info": 0}
    max_severity = max((severity_rank.get(str(item.get("severity")), 0) for item in warnings), default=0)
    if max_severity >= 2 or score < 55:
        label = "risky"
        severity = "block" if max_severity >= 2 else "review"
    elif max_severity == 1 or score < 75:
        label = "review"
        severity = "review"
    else:
        label = "strong"
        severity = "ok"
    if not reasons:
        reasons.append("首选候选已配齐")
    return {
        "score": score,
        "label": label,
        "severity": severity,
        "reasons": reasons[:4],
        "warnings": warnings[:5],
        "metrics": metrics,
    }


def select_best_pair(
    view: str,
    before_candidates: list[dict[str, Any]],
    after_candidates: list[dict[str, Any]],
    *,
    limit: int = 8,
    lock: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    if not before_candidates or not after_candidates:
        return (
            before_candidates[0] if before_candidates else None,
            after_candidates[0] if after_candidates else None,
            None,
        )
    if isinstance(lock, dict):
        locked_before = next(
            (item for item in before_candidates if candidate_matches_lock(item, lock.get("before"))),
            None,
        )
        locked_after = next(
            (item for item in after_candidates if candidate_matches_lock(item, lock.get("after"))),
            None,
        )
        if locked_before and locked_after:
            _apply_lock_marker(locked_before, view, lock, "before")
            _apply_lock_marker(locked_after, view, lock, "after")
            quality = _locked_pair_quality(view, locked_before, locked_after, lock)
            drop_reason = render_slot_drop_reason(view, quality)
            if drop_reason:
                return None, None, _with_render_slot_drop(view, quality, drop_reason)
            return locked_before, locked_after, quality
    ranked_before = sorted(before_candidates, key=candidate_rank)[:limit]
    ranked_after = sorted(after_candidates, key=candidate_rank)[:limit]
    pair_rows: list[tuple[tuple[int, int, tuple[int, int, int, str], tuple[int, int, int, str]], dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    severity_rank = {"ok": 0, "review": 1, "block": 2}
    for before in ranked_before:
        for after in ranked_after:
            quality = slot_pair_quality(view, before, after)
            if not quality:
                continue
            rank = (
                severity_rank.get(str(quality.get("severity") or "review"), 1),
                -int(quality.get("score") or 0),
                candidate_rank(before),
                candidate_rank(after),
            )
            pair_rows.append((rank, before, after, quality))
    if not pair_rows:
        before = before_candidates[0]
        after = after_candidates[0]
        quality = slot_pair_quality(view, before, after)
        drop_reason = render_slot_drop_reason(view, quality)
        if drop_reason:
            return None, None, _with_render_slot_drop(view, quality, drop_reason)
        return before, after, quality
    pair_rows.sort(key=lambda item: item[0])
    best_dropped: tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]] | None = None
    for _, before, after, quality in pair_rows:
        drop_reason = render_slot_drop_reason(view, quality)
        if drop_reason:
            if best_dropped is None:
                best_dropped = (before, after, quality, drop_reason)
            continue
        return before, after, quality
    if best_dropped is not None:
        _before, _after, quality, drop_reason = best_dropped
        return None, None, _with_render_slot_drop(view, quality, drop_reason)
    _, before, after, quality = pair_rows[0]
    return before, after, quality
