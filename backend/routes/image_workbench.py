"""Cross-case source image classification workbench.

This route turns the deterministic case-group diagnosis into an operator queue:
each row is a real source image, enriched with manual overrides, review state,
render usage and render-preflight risk. No mock rows are generated here.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import audit, db, scanner, source_images

router = APIRouter(tags=["image-workbench"])

IMAGE_REVIEW_META_KEY = "image_review_states"
TRASH_DIR_NAME = ".case-workbench-trash"
LOW_CONFIDENCE_BELOW = 0.65
SAFE_CONFIRM_CONFIDENCE = 0.85
ALLOWED_PHASES = {"before", "after"}
ALLOWED_VIEWS = {"front", "oblique", "side"}
ALLOWED_BODY_PARTS = {"face", "body", "unknown"}
ALLOWED_VERDICTS = {"usable", "deferred", "needs_repick", "excluded", "reopen"}
ANGLE_GROUP_MAX_ITEMS = 180
ANGLE_GROUP_DISTANCE_THRESHOLD = 0.12
LOCAL_ANGLE_HIGH_CONFIDENCE = 0.72
LOCAL_ANGLE_REVIEW_CONFIDENCE = 0.55
_LOCAL_ANGLE_FEATURE_CACHE: dict[tuple[str, int, int], dict[str, Any] | None] = {}

VERDICT_LABEL = {
    "usable": "已确认可用",
    "deferred": "低优先",
    "needs_repick": "需换片",
    "excluded": "不用于出图",
}

TREATMENT_TOKENS = (
    "下颌线",
    "口角",
    "泪沟",
    "眼下",
    "苹果肌",
    "太阳穴",
    "法令纹",
    "鼻根",
    "额头",
    "颈纹",
    "直角肩",
    "肩颈",
)

SOURCE_BLOCKER_LABEL = {
    "missing_source_files": "源文件缺失",
    "no_real_source_photos": "非案例源目录",
    "insufficient_source_photos": "真实源图不足",
    "missing_before_after_pair": "缺术前/术后配对",
}

SOURCE_BLOCKER_ACTION = {
    "missing_source_files": "恢复源文件或清除失效绑定后再出图",
    "no_real_source_photos": "标记为素材归档，或选择真实术前/术后源目录",
    "insufficient_source_photos": "补充至少一张术前和一张术后源图",
    "missing_before_after_pair": "补齐阶段分类或绑定互补源目录",
}

SOURCE_FIX_REASONS = {"missing_source_files", "no_real_source_photos", "insufficient_source_photos"}


def _phase_from_filename(filename: str) -> str | None:
    lower = filename.lower()
    if re.search(r"术前|before|pre", lower):
        return "before"
    if re.search(r"术后|after|post", lower):
        return "after"
    return None


def _view_from_filename(filename: str) -> str | None:
    if re.search(r"3/4|34|45|45°|微侧|斜侧|半侧|oblique", filename, re.I):
        return "oblique"
    if re.search(r"侧面|侧脸|side|profile", filename, re.I):
        return "side"
    if re.search(r"正面|正脸|front", filename, re.I):
        return "front"
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_load(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return fallback
    return data


def _source_binding_case_ids(raw_meta: str | None) -> list[int]:
    meta = _json_load(raw_meta, {})
    if not isinstance(meta, dict):
        return []
    bindings = meta.get(source_images.SOURCE_BINDINGS_META_KEY)
    if isinstance(bindings, dict):
        raw_ids = bindings.get("case_ids") or []
    elif isinstance(bindings, list):
        raw_ids = bindings
    else:
        raw_ids = []

    out: list[int] = []
    for item in raw_ids:
        try:
            case_id = int(item)
        except (TypeError, ValueError):
            continue
        if case_id > 0 and case_id not in out:
            out.append(case_id)
    return out


def _source_group_case_ids(conn: sqlite3.Connection, case_id: int) -> list[int]:
    row = conn.execute(
        "SELECT id, meta_json FROM cases WHERE id = ? AND trashed_at IS NULL",
        (case_id,),
    ).fetchone()
    if not row:
        return [case_id]
    ids = [int(row["id"])]
    for bound_id in _source_binding_case_ids(row["meta_json"]):
        if bound_id not in ids:
            ids.append(bound_id)
    return ids


def _review_states(raw_meta: str | None) -> dict[str, dict[str, Any]]:
    meta = _json_load(raw_meta, {})
    if not isinstance(meta, dict):
        return {}
    states = meta.get(IMAGE_REVIEW_META_KEY)
    if not isinstance(states, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in states.items():
        if isinstance(value, dict):
            out[str(key)] = dict(value)
    return out


def _case_meta(raw_meta: str | None) -> dict[str, Any]:
    data = _json_load(raw_meta, {})
    return data if isinstance(data, dict) else {}


def _image_files(raw_meta: str | None) -> list[str]:
    meta = _case_meta(raw_meta)
    return source_images.filter_source_image_files([str(item) for item in (meta.get("image_files") or []) if item])


def _manual_overrides(conn: sqlite3.Connection, case_ids: list[int]) -> dict[tuple[int, str], dict[str, Any]]:
    if not case_ids:
        return {}
    placeholders = ",".join("?" * len(case_ids))
    rows = conn.execute(
        f"""
        SELECT case_id, filename, manual_phase, manual_view, manual_transform_json
        FROM case_image_overrides
        WHERE case_id IN ({placeholders})
        """,
        case_ids,
    ).fetchall()
    out: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        transform = _json_load(row["manual_transform_json"], None)
        out[(int(row["case_id"]), str(row["filename"]))] = {
            "manual_phase": row["manual_phase"],
            "manual_view": row["manual_view"],
            "manual_transform": transform if isinstance(transform, dict) else None,
        }
    return out


def _latest_manifest_rows(conn: sqlite3.Connection, case_ids: list[int]) -> list[sqlite3.Row]:
    if not case_ids:
        return []
    placeholders = ",".join("?" * len(case_ids))
    return conn.execute(
        f"""
        SELECT j.case_id, j.id, j.manifest_path
        FROM render_jobs j
        JOIN (
          SELECT case_id, MAX(id) AS max_id
          FROM render_jobs
          WHERE case_id IN ({placeholders})
            AND status IN ('done', 'done_with_issues')
            AND manifest_path IS NOT NULL
          GROUP BY case_id
        ) latest ON latest.max_id = j.id
        """,
        case_ids,
    ).fetchall()


def _used_files_from_manifest(manifest_path: str | None) -> set[str]:
    if not manifest_path:
        return set()
    path = Path(manifest_path)
    if not path.is_file():
        return set()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return set()
    if not isinstance(manifest, dict):
        return set()
    used: set[str] = set()
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        selected = group.get("selected_slots")
        if not isinstance(selected, dict):
            continue
        for slot in selected.values():
            if not isinstance(slot, dict):
                continue
            for role in ("before", "after"):
                item = slot.get(role)
                if not isinstance(item, dict):
                    continue
                for key in ("name", "relative_path", "group_relative_path"):
                    value = item.get(key)
                    if value:
                        used.add(str(value))
                        used.add(Path(str(value)).name)
    return used


def _used_files_by_case(conn: sqlite3.Connection, case_ids: list[int]) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    for row in _latest_manifest_rows(conn, case_ids):
        out[int(row["case_id"])] = _used_files_from_manifest(row["manifest_path"])
    return out


def _strip_case_prefix(root_path: str, case_abs_path: str, image_path: str) -> str:
    image = Path(image_path)
    if image.is_absolute():
        try:
            return str(image.resolve().relative_to(Path(case_abs_path).resolve()))
        except ValueError:
            return image.name
    root = Path(root_path)
    case_dir = Path(case_abs_path)
    try:
        return str((root / image_path).resolve().relative_to(case_dir.resolve()))
    except (OSError, ValueError):
        pass
    try:
        prefix = case_dir.resolve().relative_to(root.resolve())
        parts = image.parts
        prefix_parts = prefix.parts
        if prefix_parts and parts[: len(prefix_parts)] == prefix_parts:
            return str(Path(*parts[len(prefix_parts):]))
    except (OSError, ValueError):
        pass
    return image.name


def _review_for_filename(states: dict[str, dict[str, Any]], filename: str) -> dict[str, Any] | None:
    return states.get(filename) or states.get(Path(filename).name)


def _manual_for_filename(
    overrides: dict[tuple[int, str], dict[str, Any]],
    case_id: int,
    filename: str,
) -> dict[str, Any] | None:
    return overrides.get((case_id, filename)) or overrides.get((case_id, Path(filename).name))


def _phase(obs_phase: str, manual: dict[str, Any] | None) -> tuple[str, str]:
    value = (manual or {}).get("manual_phase") or obs_phase
    return str(value or "unknown"), "manual" if (manual or {}).get("manual_phase") else "auto"


def _view(obs_view: str, manual: dict[str, Any] | None) -> tuple[str, str]:
    value = (manual or {}).get("manual_view") or obs_view
    return str(value or "unknown"), "manual" if (manual or {}).get("manual_view") else "auto"


def _treatment_from_text(text: str) -> str | None:
    hits = [token for token in TREATMENT_TOKENS if token in text]
    if not hits:
        return None
    return "、".join(dict.fromkeys(hits))


def _queue_state(
    *,
    phase: str,
    view: str,
    confidence: float,
    manual: bool,
    review_state: dict[str, Any] | None,
    used_in_render: bool,
) -> tuple[str, list[str]]:
    verdict = str((review_state or {}).get("verdict") or "")
    render_excluded = bool((review_state or {}).get("render_excluded") or verdict == "excluded")
    missing = phase not in ALLOWED_PHASES or view not in ALLOWED_VIEWS
    low_conf = confidence < LOW_CONFIDENCE_BELOW
    reasons: list[str] = []
    if missing:
        reasons.append("待补充阶段或角度")
    if low_conf:
        reasons.append("低置信度")
    if verdict == "needs_repick":
        reasons.append("已标记需换片")
    if render_excluded:
        return "render_excluded", reasons or ["不用于正式出图"]
    if verdict == "needs_repick":
        return "needs_repick", reasons
    if missing:
        return "needs_manual", reasons
    if bool((review_state or {}).get("copied_requires_review")) and verdict not in {"usable", "deferred", "needs_repick", "excluded"}:
        return "copied_review", reasons or ["跨案例补图待确认"]
    if verdict in {"usable", "deferred"}:
        return verdict, reasons
    if low_conf:
        return "low_confidence", reasons
    if used_in_render:
        return "used_in_render", reasons
    if manual:
        return "manual", reasons
    return "identified", reasons


def _case_source_context(row: sqlite3.Row) -> dict[str, Any]:
    meta = _case_meta(row["meta_json"])
    raw_files = [str(item) for item in (meta.get("image_files") or []) if item]
    profile = source_images.classify_existing_case_source_profile(str(row["abs_path"] or ""), raw_files)
    source_kind = str(profile.get("source_kind") or "")
    source_count = int(profile.get("source_count") or 0)
    before_count = int(profile.get("before_count") or 0)
    after_count = int(profile.get("after_count") or 0)
    reason: str | None = None
    if int(profile.get("missing_source_count") or 0) > 0 or source_kind == "missing_source_files":
        reason = "missing_source_files"
    elif source_kind in {"generated_output_collection", "empty"}:
        reason = "no_real_source_photos"
    elif source_kind == "insufficient_source_photos":
        reason = "insufficient_source_photos"
    elif source_kind == "missing_before_after_pair":
        reason = "missing_before_after_pair"
    source_phase_hint: str | None = None
    if source_count > 0 and before_count == source_count and after_count == 0:
        source_phase_hint = "before"
    elif source_count > 0 and after_count == source_count and before_count == 0:
        source_phase_hint = "after"
    return {
        "source_kind": source_kind,
        "reason": reason,
        "reason_label": SOURCE_BLOCKER_LABEL.get(reason or "", None),
        "recommended_action": SOURCE_BLOCKER_ACTION.get(reason or "", None),
        "source_count": source_count,
        "before_count": before_count,
        "after_count": after_count,
        "source_phase_hint": source_phase_hint,
        "source_phase_hint_label": {"before": "术前", "after": "术后"}.get(source_phase_hint or ""),
        "missing_source_count": int(profile.get("missing_source_count") or 0),
    }


def _source_processing_mode(reason: str | None) -> tuple[str, str]:
    if reason in SOURCE_FIX_REASONS:
        return "source_fix", "源目录修复"
    if reason == "missing_before_after_pair":
        return "classify_or_bind", "分类或绑定互补目录"
    return "classification", "分类整理"


def _classification_suggestion(
    item: dict[str, Any],
    source_context: dict[str, Any] | None = None,
    *,
    include_local_angle: bool = False,
) -> dict[str, Any]:
    phase_known = item["phase"] in ALLOWED_PHASES
    view_known = item["view"] in ALLOWED_VIEWS
    verdict = str((item.get("review_state") or {}).get("verdict") or "")
    source_phase_hint = str((source_context or {}).get("source_phase_hint") or "")
    actions: list[dict[str, Any]] = []
    task_groups: list[str] = []
    if not phase_known:
        task_groups.append("missing_phase")
        actions.append(
            {
                "code": "confirm_source_phase" if source_phase_hint in ALLOWED_PHASES else "set_phase",
                "label": f"确认目录阶段：{SOURCE_BLOCKER_LABEL.get(source_phase_hint, '') or {'before': '术前', 'after': '术后'}.get(source_phase_hint, '')}"
                if source_phase_hint in ALLOWED_PHASES
                else "补阶段",
                "primary": True,
            }
        )
    if not view_known:
        task_groups.append("missing_view")
        actions.append({"code": "set_view", "label": "判角度", "primary": not actions})
    if item.get("low_confidence"):
        task_groups.append("low_confidence")
        actions.append({"code": "confirm_angle", "label": "复核低置信角度", "primary": not actions})
    if item.get("render_excluded"):
        task_groups.append("render_excluded")
        actions.append({"code": "reopen_or_keep_excluded", "label": "保持排除或重开", "primary": False})
    elif verdict == "needs_repick":
        task_groups.append("needs_repick")
        actions.append({"code": "replace_image", "label": "换片", "primary": not actions})
    elif verdict not in {"usable", "deferred"} and not item.get("used_in_render"):
        task_groups.append("missing_usability")
        actions.append({"code": "confirm_usability", "label": "确认是否适合出图", "primary": not actions})
    if item.get("used_in_render"):
        task_groups.append("used_in_render")
    if source_context and source_context.get("reason"):
        task_groups.append("blocked_case")
        actions.append(
            {
                "code": "open_source_blocker",
                "label": source_context.get("recommended_action") or "处理案例源目录阻断",
                "primary": False,
            }
        )
    if not actions:
        actions.append({"code": "no_action", "label": "已可进入预检候选", "primary": False})
    if item["queue_state"] in {"needs_manual", "needs_repick", "copied_review"} or (source_context or {}).get("reason"):
        blocker_level = "block"
    elif item["queue_state"] == "low_confidence":
        blocker_level = "review"
    else:
        blocker_level = "ok"
    suggested_phase = item["phase"] if phase_known else (source_phase_hint if source_phase_hint in ALLOWED_PHASES else None)
    suggested_view = item["view"] if view_known else None
    suggested_body = item["body_part"] if item["body_part"] in ALLOWED_BODY_PARTS else None
    confidence = float(item.get("confidence") or 0)
    confidence_band = _confidence_band(confidence)
    render_gate = _render_gate_summary(item, source_context, blocker_level)
    return {
        "suggested_labels": {
            "phase": suggested_phase,
            "view": suggested_view,
            "body_part": suggested_body,
            "treatment_area": item.get("treatment_area"),
        },
        "label_confidence": item.get("confidence"),
        "confidence_band": confidence_band,
        "task_groups": list(dict.fromkeys(task_groups or ["ready"])),
        "blocker_level": blocker_level,
        "render_gate": render_gate,
        "recommended_actions": actions,
        "classification_layers": {
            "deterministic": {
                "phase": item["phase"] if item["phase_source"] in {"filename", "directory"} else None,
                "view": item["view"] if item["view_source"] in {"filename", "directory"} else None,
                "source": f"{item['phase_source']}/{item['view_source']}",
                "source_phase_hint": source_phase_hint if source_phase_hint in ALLOWED_PHASES else None,
                "source_phase_hint_label": (source_context or {}).get("source_phase_hint_label"),
            },
            "local_visual": _local_visual_layer(item, confidence_band, include_local_angle=include_local_angle),
            "visual": {
                "phase": item["phase"] if item.get("source") in {"skill_v3", "vision", "case_meta"} and item["phase_source"] != "manual" else None,
                "view": item["view"] if item.get("source") in {"skill_v3", "vision", "case_meta"} and item["view_source"] != "manual" else None,
                "confidence": item.get("confidence"),
                "source": item.get("source"),
            },
            "manual": {
                "phase": item["phase"] if item["phase_source"] == "manual" else None,
                "view": item["view"] if item["view_source"] == "manual" else None,
                "verdict": verdict or None,
                "reviewer": (item.get("review_state") or {}).get("reviewer") if isinstance(item.get("review_state"), dict) else None,
            },
        },
    }


def _confidence_band(confidence: float) -> str:
    if confidence >= SAFE_CONFIRM_CONFIDENCE:
        return "high"
    if confidence >= 0.55:
        return "review"
    return "low"


def _local_visual_layer(
    item: dict[str, Any],
    confidence_band: str,
    *,
    include_local_angle: bool = False,
) -> dict[str, Any]:
    quality = item.get("quality") if isinstance(item.get("quality"), dict) else {}
    phase = item.get("phase") if item.get("phase") in ALLOWED_PHASES else None
    view = item.get("view") if item.get("view") in ALLOWED_VIEWS else None
    manual = bool(item.get("manual"))
    if manual:
        decision = "manual_confirmed"
    elif phase and view and confidence_band == "high":
        decision = "auto_candidate"
    else:
        decision = "needs_review"
    view_suggestion = None
    if include_local_angle and (view is None or confidence_band != "high"):
        feature = _angle_group_feature(item)
        if feature and isinstance(feature.get("local_angle"), dict):
            view_suggestion = feature["local_angle"]
    return {
        "phase": None if manual else phase,
        "view": None if manual else view,
        "confidence": item.get("confidence"),
        "confidence_band": confidence_band,
        "decision": decision,
        "view_suggestion": view_suggestion,
        "signals": {
            "source": item.get("source"),
            "phase_source": item.get("phase_source"),
            "view_source": item.get("view_source"),
            "pose": quality.get("pose"),
            "sharpness_score": quality.get("sharpness_score"),
            "rejection_reason": quality.get("rejection_reason"),
        },
    }


def _render_gate_summary(
    item: dict[str, Any],
    source_context: dict[str, Any] | None,
    blocker_level: str,
) -> dict[str, Any]:
    task_groups = set(item.get("task_groups") or [])
    if blocker_level == "block" or source_context and source_context.get("reason"):
        blocks_render = True
        reason = (source_context or {}).get("reason") or "classification_open"
    elif item.get("low_confidence") or "low_confidence" in task_groups:
        blocks_render = True
        reason = "low_confidence_requires_review"
    elif item.get("queue_state") in {"needs_manual", "needs_repick", "copied_review"}:
        blocks_render = True
        reason = str(item.get("queue_state"))
    else:
        blocks_render = False
        reason = "ready"
    return {
        "blocks_render": blocks_render,
        "reason": reason,
        "level": "block" if blocks_render else "ok",
        "message": "未闭环或低置信图片不会进入正式出图" if blocks_render else "可进入 source-group 候选",
    }


def _enrich_classification_item(
    item: dict[str, Any],
    source_context: dict[str, Any] | None = None,
    *,
    include_local_angle: bool = False,
) -> dict[str, Any]:
    suggestion = _classification_suggestion(item, source_context, include_local_angle=include_local_angle)
    safe_confirm = _safe_confirm_status(item, source_context, SAFE_CONFIRM_CONFIDENCE)
    return {
        **item,
        "classification_suggestion": suggestion,
        "task_groups": suggestion["task_groups"],
        "blocker_level": suggestion["blocker_level"],
        "recommended_actions": suggestion["recommended_actions"],
        "case_preflight": source_context or {},
        "safe_confirm": safe_confirm,
    }


def _attach_local_angle_suggestions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in items:
        suggestion = item.get("classification_suggestion")
        if not isinstance(suggestion, dict):
            continue
        layers = suggestion.get("classification_layers")
        if not isinstance(layers, dict):
            continue
        local_visual = layers.get("local_visual")
        if not isinstance(local_visual, dict) or local_visual.get("view_suggestion"):
            continue
        if str(item.get("view") or "") in ALLOWED_VIEWS and not item.get("low_confidence"):
            continue
        feature = _angle_group_feature(item)
        if feature and isinstance(feature.get("local_angle"), dict):
            local_visual["view_suggestion"] = feature["local_angle"]
    return items


def _safe_confirm_status(
    item: dict[str, Any],
    source_context: dict[str, Any] | None,
    min_confidence: float,
) -> dict[str, Any]:
    phase = str(item.get("phase") or "")
    view = str(item.get("view") or "")
    confidence = float(item.get("confidence") or 0)
    if (source_context or {}).get("reason"):
        return {"eligible": False, "reason": "case_source_blocked", "threshold": min_confidence}
    if item.get("manual"):
        return {"eligible": False, "reason": "already_manual", "threshold": min_confidence}
    if item.get("render_excluded"):
        return {"eligible": False, "reason": "render_excluded", "threshold": min_confidence}
    if phase not in ALLOWED_PHASES:
        return {"eligible": False, "reason": "missing_phase", "threshold": min_confidence}
    if view not in ALLOWED_VIEWS:
        return {"eligible": False, "reason": "missing_view", "threshold": min_confidence}
    if confidence < min_confidence:
        return {"eligible": False, "reason": "below_confidence_threshold", "threshold": min_confidence}
    if item.get("queue_state") in {"needs_manual", "low_confidence", "needs_repick", "copied_review"}:
        return {"eligible": False, "reason": f"queue_state_{item.get('queue_state')}", "threshold": min_confidence}
    return {
        "eligible": True,
        "reason": "high_confidence_suggestion",
        "threshold": min_confidence,
        "phase": phase,
        "view": view,
        "would_mark_usable": True,
    }


def _preview_url(case_id: int, filename: str) -> str:
    return f"/api/cases/{case_id}/files?name={quote(filename)}"


def _classification_item(
    row: sqlite3.Row,
    *,
    overrides: dict[tuple[int, str], dict[str, Any]],
    used_by_case: dict[int, set[str]],
) -> dict[str, Any]:
    case_id = int(row["case_id"])
    filename = _strip_case_prefix(row["root_path"], row["case_abs_path"], row["image_path"])
    manual = _manual_for_filename(overrides, case_id, filename)
    states = _review_states(row["case_meta_json"])
    review_state = _review_for_filename(states, filename)
    phase, phase_source = _phase(str(row["phase"] or "unknown"), manual)
    view, view_source = _view(str(row["view"] or "unknown"), manual)
    confidence = float(row["confidence"] or 0)
    used_names = used_by_case.get(case_id, set())
    used_in_render = filename in used_names or Path(filename).name in used_names
    manual_applied = bool(manual and (manual.get("manual_phase") or manual.get("manual_view")))
    state, reasons = _queue_state(
        phase=phase,
        view=view,
        confidence=confidence,
        manual=manual_applied,
        review_state=review_state,
        used_in_render=used_in_render,
    )
    review_body_part = str((review_state or {}).get("body_part") or "").strip()
    body_part = review_body_part or str(row["body_part"] or "unknown")
    treatment_area = str((review_state or {}).get("treatment_area") or "").strip()
    if not treatment_area:
        treatment_area = _treatment_from_text(f"{row['title']} {row['root_path']} {filename}") or ""
    return {
        "case_id": case_id,
        "group_id": row["group_id"],
        "observation_id": row["id"],
        "filename": filename,
        "image_path": row["image_path"],
        "preview_url": _preview_url(case_id, filename),
        "case_url": f"/cases/{case_id}",
        "case_title": row["title"],
        "case_abs_path": row["case_abs_path"],
        "customer_raw": row["customer_raw"],
        "customer_id": row["customer_id"] if "customer_id" in row.keys() else None,
        "phase": phase,
        "phase_source": phase_source,
        "view": view,
        "view_source": view_source,
        "body_part": body_part,
        "treatment_area": treatment_area or None,
        "confidence": confidence,
        "source": row["source"],
        "reasons": list(dict.fromkeys([*(_json_load(row["reasons_json"], []) or []), *reasons])),
        "queue_state": state,
        "manual": manual_applied,
        "used_in_render": used_in_render,
        "low_confidence": confidence < LOW_CONFIDENCE_BELOW,
        "needs_manual": phase not in ALLOWED_PHASES or view not in ALLOWED_VIEWS,
        "render_excluded": bool((review_state or {}).get("render_excluded")),
        "review_state": review_state,
        "quality": _json_load(row["quality_json"], {}),
        "updated_at": row["updated_at"],
    }


def _skill_by_file(raw: str | None) -> dict[str, dict[str, Any]]:
    items = _json_load(raw, [])
    if not isinstance(items, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in (item.get("filename"), item.get("relative_path")):
            if key:
                out[str(key)] = item
                out[Path(str(key)).name] = item
    return out


def _case_phase_view_from_skill(
    filename: str,
    manual: dict[str, Any] | None,
    skill: dict[str, Any] | None,
) -> tuple[str, str, str, str, float, str]:
    manual_phase = (manual or {}).get("manual_phase")
    manual_view = (manual or {}).get("manual_view")
    phase = manual_phase if manual_phase in ALLOWED_PHASES else None
    view = manual_view if manual_view in ALLOWED_VIEWS else None
    phase_source = "manual" if phase else "auto"
    view_source = "manual" if view else "auto"
    confidence = 0.35
    source = "case_meta"
    if not phase and isinstance(skill, dict) and skill.get("phase") in ALLOWED_PHASES:
        phase = str(skill["phase"])
        phase_source = str(skill.get("phase_source") or "skill")
    if not view and isinstance(skill, dict):
        view_bucket = skill.get("view_bucket") or skill.get("angle")
        if view_bucket in ALLOWED_VIEWS:
            view = str(view_bucket)
            view_source = str(skill.get("angle_source") or "skill")
    if isinstance(skill, dict):
        source = "skill_v3"
        try:
            confidence = float(skill.get("angle_confidence")) if skill.get("angle_confidence") is not None else 0.9
        except (TypeError, ValueError):
            confidence = 0.9
    if not phase:
        phase = _phase_from_filename(filename) or "unknown"
        if phase != "unknown":
            phase_source = "filename"
    if not view:
        view = _view_from_filename(filename) or "unknown"
        if view != "unknown":
            view_source = "filename"
    if manual_phase or manual_view:
        confidence = 1.0 if phase in ALLOWED_PHASES and view in ALLOWED_VIEWS else max(confidence, 0.6)
    return phase, phase_source, view, view_source, confidence, source


def _case_image_item(
    row: sqlite3.Row,
    filename: str,
    *,
    overrides: dict[tuple[int, str], dict[str, Any]],
    used_by_case: dict[int, set[str]],
    observation: dict[str, Any] | None,
    skill_by_name: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    case_id = int(row["id"])
    manual = _manual_for_filename(overrides, case_id, filename)
    states = _review_states(row["meta_json"])
    review_state = _review_for_filename(states, filename)
    skill = skill_by_name.get(filename) or skill_by_name.get(Path(filename).name)
    phase, phase_source, view, view_source, confidence, source = _case_phase_view_from_skill(filename, manual, skill)
    if observation and observation.get("confidence") is not None and not manual and not skill:
        try:
            confidence = float(observation["confidence"])
        except (TypeError, ValueError):
            pass
    used_names = used_by_case.get(case_id, set())
    used_in_render = filename in used_names or Path(filename).name in used_names
    manual_applied = bool(manual and (manual.get("manual_phase") or manual.get("manual_view")))
    state, reasons = _queue_state(
        phase=phase,
        view=view,
        confidence=confidence,
        manual=manual_applied,
        review_state=review_state,
        used_in_render=used_in_render,
    )
    obs_reasons = (observation or {}).get("reasons") or []
    body_part = str((review_state or {}).get("body_part") or (observation or {}).get("body_part") or "")
    if not body_part:
        if row["category"] == "body":
            body_part = "body"
        elif row["category"] == "standard_face":
            body_part = "face"
        else:
            body_part = "unknown"
    treatment_area = str((review_state or {}).get("treatment_area") or "").strip()
    if not treatment_area:
        treatment_area = _treatment_from_text(f"{row['abs_path']} {filename}") or ""
    quality = dict(skill or {})
    if observation and isinstance(observation.get("quality"), dict):
        quality.update(observation["quality"])
    return {
        "case_id": case_id,
        "group_id": row["group_id"],
        "observation_id": (observation or {}).get("id"),
        "filename": filename,
        "image_path": filename,
        "preview_url": _preview_url(case_id, filename),
        "case_url": f"/cases/{case_id}",
        "case_title": row["title"] or Path(str(row["abs_path"])).name,
        "case_abs_path": row["abs_path"],
        "customer_raw": row["customer_raw"],
        "customer_id": row["customer_id"],
        "phase": phase,
        "phase_source": phase_source,
        "view": view,
        "view_source": view_source,
        "body_part": body_part,
        "treatment_area": treatment_area or None,
        "confidence": confidence,
        "source": source,
        "reasons": list(dict.fromkeys([*[str(x) for x in obs_reasons if x], *reasons])),
        "queue_state": state,
        "manual": manual_applied,
        "used_in_render": used_in_render,
        "low_confidence": confidence < LOW_CONFIDENCE_BELOW,
        "needs_manual": phase not in ALLOWED_PHASES or view not in ALLOWED_VIEWS,
        "render_excluded": bool((review_state or {}).get("render_excluded")),
        "review_state": review_state,
        "quality": quality,
        "updated_at": row["indexed_at"],
    }


def _filter_item(item: dict[str, Any], *, status: str, phase: str, view: str, body_part: str, q: str) -> bool:
    if status and status != "all":
        if status == "review_needed":
            if item["queue_state"] not in {"needs_manual", "low_confidence", "needs_repick", "copied_review"}:
                return False
        elif status in {"missing_phase", "missing_view", "missing_usability", "blocked_case"}:
            if status not in set(item.get("task_groups") or []):
                return False
        elif status == "used_in_render":
            if not item["used_in_render"]:
                return False
        elif status == "ready_for_render":
            if item["queue_state"] in {"needs_manual", "low_confidence", "needs_repick", "render_excluded"}:
                return False
        elif item["queue_state"] != status:
            return False
    if phase and phase != "all" and item["phase"] != phase:
        return False
    if view and view != "all" and item["view"] != view:
        return False
    if body_part and body_part != "all" and item["body_part"] != body_part:
        return False
    if q:
        needle = q.lower()
        hay = " ".join(str(item.get(k) or "") for k in ("filename", "case_title", "case_abs_path", "customer_raw", "treatment_area")).lower()
        if needle not in hay:
            return False
    return True


def _case_group_summaries(
    all_items: list[dict[str, Any]],
    filtered_items: list[dict[str, Any]],
    source_context_by_case: dict[int, dict[str, Any]],
    *,
    limit: int = 40,
) -> list[dict[str, Any]]:
    filtered_keys = {(int(item["case_id"]), str(item["filename"])) for item in filtered_items}
    buckets: dict[int, dict[str, Any]] = {}
    for item in all_items:
        case_id = int(item["case_id"])
        bucket = buckets.setdefault(
            case_id,
            {
                "case_id": case_id,
                "case_title": item.get("case_title"),
                "case_url": item.get("case_url") or f"/cases/{case_id}",
                "customer_raw": item.get("customer_raw"),
                "total_count": 0,
                "filtered_count": 0,
                "needs_manual_count": 0,
                "missing_phase_count": 0,
                "missing_view_count": 0,
                "missing_usability_count": 0,
                "low_confidence_count": 0,
                "used_in_render_count": 0,
                "render_excluded_count": 0,
                "safe_confirm_count": 0,
                "slots": {
                    "front": {"before": 0, "after": 0},
                    "oblique": {"before": 0, "after": 0},
                    "side": {"before": 0, "after": 0},
                },
            },
        )
        bucket["total_count"] += 1
        if (case_id, str(item["filename"])) in filtered_keys:
            bucket["filtered_count"] += 1
        groups = set(item.get("task_groups") or [])
        if item.get("needs_manual"):
            bucket["needs_manual_count"] += 1
        if "missing_phase" in groups:
            bucket["missing_phase_count"] += 1
        if "missing_view" in groups:
            bucket["missing_view_count"] += 1
        if "missing_usability" in groups:
            bucket["missing_usability_count"] += 1
        if item.get("low_confidence"):
            bucket["low_confidence_count"] += 1
        if item.get("used_in_render"):
            bucket["used_in_render_count"] += 1
        if item.get("render_excluded"):
            bucket["render_excluded_count"] += 1
        if (item.get("safe_confirm") or {}).get("eligible"):
            bucket["safe_confirm_count"] += 1
        phase = str(item.get("phase") or "")
        view = str(item.get("view") or "")
        if not item.get("render_excluded") and view in bucket["slots"] and phase in {"before", "after"}:
            bucket["slots"][view][phase] += 1

    out: list[dict[str, Any]] = []
    for case_id, bucket in buckets.items():
        source_context = source_context_by_case.get(case_id) or {}
        source_reason = str(source_context.get("reason") or "")
        processing_mode, processing_mode_label = _source_processing_mode(source_reason or None)
        missing_slots: list[dict[str, Any]] = []
        for view, counts in bucket["slots"].items():
            missing = [
                *([] if counts["before"] else ["before"]),
                *([] if counts["after"] else ["after"]),
            ]
            if missing:
                missing_slots.append(
                    {
                        "view": view,
                        "label": {"front": "正面", "oblique": "45°", "side": "侧面"}[view],
                        "missing": missing,
                    }
                )
        hard_blockers: list[dict[str, Any]] = []
        if source_context.get("reason"):
            hard_blockers.append(
                {
                    "code": source_context.get("reason"),
                    "label": source_context.get("reason_label"),
                    "recommended_action": source_context.get("recommended_action"),
                }
            )
        if bucket["needs_manual_count"]:
            hard_blockers.append(
                {
                    "code": "classification_open",
                    "label": "分类未闭环",
                    "recommended_action": "补齐阶段、角度和可用性",
                    "count": bucket["needs_manual_count"],
                }
            )
        if missing_slots:
            hard_blockers.append(
                {
                    "code": "missing_render_slots",
                    "label": "缺三联槽位",
                    "recommended_action": "补齐正面、45°、侧面术前术后配对",
                    "slots": missing_slots,
                }
            )
        score = 100
        score -= min(int(bucket["needs_manual_count"]) * 6, 42)
        score -= min(int(bucket["low_confidence_count"]) * 4, 24)
        score -= min(len(missing_slots) * 12, 42)
        if source_context.get("reason"):
            score = min(score, 40)
        readiness_score = max(0, min(100, int(score)))
        if source_context.get("reason"):
            next_action = source_context.get("recommended_action") or "处理源目录阻断"
        elif bucket["needs_manual_count"]:
            next_action = "进入分类队列补齐阶段/角度"
        elif missing_slots:
            next_action = "查找补图候选并补齐槽位"
        elif bucket["safe_confirm_count"]:
            next_action = "可批量确认高置信建议"
        else:
            next_action = "可进入正式出图预检"
        priority_score = (
            (100 if source_context.get("reason") else 0)
            + int(bucket["needs_manual_count"]) * 20
            + len(missing_slots) * 14
            + int(bucket["low_confidence_count"]) * 8
            + int(bucket["used_in_render_count"]) * 4
            + int(bucket["safe_confirm_count"]) * 2
        )
        classification_url = f"/images?case_id={case_id}&status=review_needed&focus=classification_blockers&return=%2Fcases%2F{case_id}"
        source_fix_url = f"/cases/{case_id}#source-group-preflight"
        out.append(
            {
                **bucket,
                "source_context": source_context,
                "processing_mode": processing_mode,
                "processing_mode_label": processing_mode_label,
                "missing_slots": missing_slots,
                "hard_blockers": hard_blockers,
                "preflight_status": "ready" if not hard_blockers else "blocked",
                "readiness_score": readiness_score,
                "next_action": next_action,
                "priority_score": priority_score,
                "queue_url": source_fix_url if processing_mode == "source_fix" else classification_url,
                "classification_url": classification_url,
                "source_fix_url": source_fix_url,
            }
        )
    out.sort(
        key=lambda item: (
            0 if int(item["filtered_count"]) > 0 else 1,
            -int(item["priority_score"]),
            int(item["readiness_score"]),
            -int(item["case_id"]),
        )
    )
    return out[:limit]


def _filename_bucket(filename: str) -> str:
    name = Path(filename).stem
    phase = _phase_from_filename(name)
    view = _view_from_filename(name)
    if phase or view:
        return "/".join(part for part in (phase, view) if part)
    normalized = re.sub(r"\d+", "#", name).strip(" _-")
    if normalized and normalized != name:
        return normalized[:28]
    parent = Path(filename).parent
    if str(parent) not in {"", "."}:
        return str(parent).split("/", 1)[0][:28]
    return "未命名序列"


def _count_by(items: list[dict[str, Any]], key: str, allowed: set[str] | None = None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "unknown")
        if allowed is not None and value not in allowed:
            value = "unknown"
        counts[value] = counts.get(value, 0) + 1
    return counts


def _dominant_value(counts: dict[str, int], total: int, *, allowed: set[str], min_ratio: float = 0.8) -> str | None:
    if total <= 0:
        return None
    best_value = None
    best_count = 0
    for value, count in counts.items():
        if value not in allowed:
            continue
        if count > best_count:
            best_value = value
            best_count = count
    if best_value and best_count / total >= min_ratio:
        return best_value
    return None


def _batch_group_summaries(items: list[dict[str, Any]], *, limit: int = 80) -> list[dict[str, Any]]:
    buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for item in items:
        source_reason = str((item.get("case_preflight") or {}).get("reason") or "")
        processing_mode, _label = _source_processing_mode(source_reason or None)
        task_groups = tuple(
            sorted(
                group
                for group in (item.get("task_groups") or [])
                if group in {"missing_phase", "missing_view", "low_confidence", "missing_usability", "blocked_case", "needs_repick", "copied_review"}
            )
        )
        key = (
            int(item["case_id"]),
            processing_mode,
            source_reason,
            task_groups,
            str(item.get("phase") or "unknown") if item.get("phase") in ALLOWED_PHASES else "unknown",
            str(item.get("view") or "unknown") if item.get("view") in ALLOWED_VIEWS else "unknown",
            str(item.get("body_part") or "unknown"),
            _filename_bucket(str(item.get("filename") or "")),
        )
        buckets.setdefault(key, []).append(item)

    out: list[dict[str, Any]] = []
    for index, (key, bucket_items) in enumerate(buckets.items(), start=1):
        case_id, processing_mode, source_reason, task_groups, phase_key, view_key, body_key, filename_bucket = key
        source_context = bucket_items[0].get("case_preflight") or {}
        processing_mode_label = _source_processing_mode(source_reason or None)[1]
        source_phase_hint = str(source_context.get("source_phase_hint") or "")
        source_phase_hint_label = source_context.get("source_phase_hint_label")
        phase_counts = _count_by(bucket_items, "phase", ALLOWED_PHASES)
        view_counts = _count_by(bucket_items, "view", ALLOWED_VIEWS)
        body_counts = _count_by(bucket_items, "body_part", ALLOWED_BODY_PARTS)
        total = len(bucket_items)
        missing_phase_count = sum(1 for item in bucket_items if item.get("phase") not in ALLOWED_PHASES)
        missing_view_count = sum(1 for item in bucket_items if item.get("view") not in ALLOWED_VIEWS)
        missing_usability_count = sum(1 for item in bucket_items if "missing_usability" in set(item.get("task_groups") or []))
        low_confidence_count = sum(1 for item in bucket_items if item.get("low_confidence"))
        safe_confirm_count = sum(1 for item in bucket_items if (item.get("safe_confirm") or {}).get("eligible"))
        used_in_render_count = sum(1 for item in bucket_items if item.get("used_in_render"))
        suggested_phase = _dominant_value(phase_counts, total, allowed=ALLOWED_PHASES)
        if not suggested_phase and missing_phase_count and source_phase_hint in ALLOWED_PHASES:
            suggested_phase = source_phase_hint
        suggested_view = _dominant_value(view_counts, total, allowed=ALLOWED_VIEWS)
        recommended_patch: dict[str, Any] = {}
        if missing_phase_count and suggested_phase:
            recommended_patch["manual_phase"] = suggested_phase
        if missing_view_count and suggested_view:
            recommended_patch["manual_view"] = suggested_view
        action_labels: list[str] = []
        for item in bucket_items:
            for action in item.get("recommended_actions") or []:
                label = str(action.get("label") or "")
                if label and label not in action_labels:
                    action_labels.append(label)
        if processing_mode == "source_fix":
            primary_action = source_context.get("recommended_action") or "先修复源目录，不进入普通分类"
        elif missing_phase_count and source_phase_hint in ALLOWED_PHASES:
            primary_action = f"确认目录阶段为{source_phase_hint_label or source_phase_hint}，角度继续人工判断"
        elif missing_phase_count or missing_view_count:
            primary_action = "人工批量补齐阶段/角度"
        elif safe_confirm_count:
            primary_action = "确认高置信分类建议"
        elif missing_usability_count:
            primary_action = "批量确认是否适合出图"
        else:
            primary_action = action_labels[0] if action_labels else "进入源组预检"
        avg_confidence = round(sum(float(item.get("confidence") or 0) for item in bucket_items) / total, 4)
        min_confidence = round(min(float(item.get("confidence") or 0) for item in bucket_items), 4)
        group_id = f"{case_id}:{processing_mode}:{source_reason or 'normal'}:{index}"
        out.append(
            {
                "id": group_id,
                "case_id": case_id,
                "case_title": bucket_items[0].get("case_title"),
                "case_url": bucket_items[0].get("case_url") or f"/cases/{case_id}",
                "processing_mode": processing_mode,
                "processing_mode_label": processing_mode_label,
                "source_reason": source_reason or None,
                "source_reason_label": source_context.get("reason_label"),
                "source_phase_hint": source_phase_hint if source_phase_hint in ALLOWED_PHASES else None,
                "source_phase_hint_label": source_phase_hint_label,
                "recommended_action": primary_action,
                "task_groups": list(task_groups),
                "filename_bucket": filename_bucket,
                "phase_key": phase_key,
                "view_key": view_key,
                "body_part": body_key,
                "item_count": total,
                "filenames": [str(item["filename"]) for item in bucket_items],
                "sample_images": [
                    {
                        "case_id": int(item["case_id"]),
                        "filename": str(item["filename"]),
                        "preview_url": item.get("preview_url"),
                    }
                    for item in bucket_items[:6]
                ],
                "missing_phase_count": missing_phase_count,
                "missing_view_count": missing_view_count,
                "missing_usability_count": missing_usability_count,
                "low_confidence_count": low_confidence_count,
                "safe_confirm_count": safe_confirm_count,
                "used_in_render_count": used_in_render_count,
                "phase_counts": phase_counts,
                "view_counts": view_counts,
                "body_part_counts": body_counts,
                "confidence_avg": avg_confidence,
                "confidence_min": min_confidence,
                "suggested_phase": suggested_phase,
                "suggested_view": suggested_view,
                "recommended_patch": recommended_patch or None,
                "can_bulk_apply_suggestion": bool(recommended_patch) and min_confidence >= SAFE_CONFIRM_CONFIDENCE,
                "classification_url": f"/images?case_id={case_id}&status=review_needed&focus=classification_blockers&return=%2Fcases%2F{case_id}",
                "source_fix_url": f"/cases/{case_id}#source-group-preflight",
            }
        )
    out.sort(
        key=lambda group: (
            0 if group["processing_mode"] != "source_fix" else 1,
            -int(group["item_count"]),
            -int(group["missing_phase_count"] + group["missing_view_count"] + group["low_confidence_count"]),
            int(group["case_id"]),
            str(group["filename_bucket"]),
        )
    )
    return out[:limit]


def _filename_sequence(filename: str) -> int:
    matches = re.findall(r"\d+", Path(filename).stem)
    return int(matches[-1]) if matches else 0


def _orientation_label(orientation: str) -> str:
    return {"portrait": "竖图", "landscape": "横图", "square": "方图"}.get(orientation, orientation)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _feature_cache_key(image_path: Path) -> tuple[str, int, int] | None:
    try:
        stat = image_path.stat()
    except OSError:
        return None
    return (str(image_path), int(stat.st_mtime_ns), int(stat.st_size))


def _mean_rgb(pixels: list[tuple[int, int, int]]) -> tuple[float, float, float]:
    if not pixels:
        return (0.0, 0.0, 0.0)
    return (
        sum(pixel[0] for pixel in pixels) / len(pixels),
        sum(pixel[1] for pixel in pixels) / len(pixels),
        sum(pixel[2] for pixel in pixels) / len(pixels),
    )


def _skin_like(pixel: tuple[int, int, int]) -> bool:
    r, g, b = pixel
    return (
        r > 90
        and g > 45
        and b > 28
        and r > g * 1.08
        and g >= b * 0.82
        and max(pixel) - min(pixel) > 22
    )


def _bounded(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _confidence_band_from_local_angle(confidence: float) -> str:
    if confidence >= LOCAL_ANGLE_HIGH_CONFIDENCE:
        return "high"
    if confidence >= LOCAL_ANGLE_REVIEW_CONFIDENCE:
        return "review"
    return "low"


def _local_angle_suggestion_from_signals(signals: dict[str, Any]) -> dict[str, Any]:
    symmetry = float(signals.get("symmetry_delta") or 0)
    skin_side_bias = abs(float(signals.get("skin_side_bias") or 0))
    center_offset = abs(float(signals.get("subject_center_offset") or 0))
    left_right_bias = abs(float(signals.get("left_right_bias") or 0))
    bbox = signals.get("subject_bbox") if isinstance(signals.get("subject_bbox"), dict) else {}
    bbox_width = float(bbox.get("width_ratio") or 0)
    bbox_height = float(bbox.get("height_ratio") or 0)
    side_metric = (
        _bounded(symmetry / 0.24) * 0.42
        + _bounded(skin_side_bias / 0.34) * 0.28
        + _bounded(center_offset / 0.24) * 0.16
        + _bounded(left_right_bias / 0.16) * 0.10
        + (0.04 if 0 < bbox_width < 0.36 and bbox_height > 0.44 else 0.0)
    )
    if side_metric <= 0.34:
        suggested_view = "front"
        margin = 0.34 - side_metric
    elif side_metric >= 0.42 or (skin_side_bias >= 0.42 and symmetry >= 0.05):
        suggested_view = "side"
        margin = max(side_metric - 0.42, skin_side_bias - 0.42)
    else:
        suggested_view = "oblique"
        margin = 0.08 - abs(side_metric - 0.38)
    confidence = _bounded(0.55 + max(margin, 0.0) * 0.95, 0.50, 0.92)
    if suggested_view == "oblique":
        confidence = _bounded(0.56 + max(margin, 0.0) * 0.55, 0.50, 0.78)
    reason_labels: list[str] = []
    if suggested_view == "front":
        reason_labels.extend(["主体居中", "左右轮廓较对称"])
    elif suggested_view == "side":
        reason_labels.extend(["左右轮廓不对称", "侧向肤色/边缘偏移明显"])
    else:
        reason_labels.extend(["轮廓有侧向偏移", "不对称程度介于正面和侧面之间"])
    if signals.get("exif_transposed"):
        reason_labels.append("已应用 EXIF 方向修正")
    return {
        "suggested_view": suggested_view,
        "suggested_view_label": {"front": "正面", "oblique": "45°", "side": "侧面"}[suggested_view],
        "confidence": round(confidence, 4),
        "confidence_band": _confidence_band_from_local_angle(confidence),
        "decision": "auto_candidate" if confidence >= LOCAL_ANGLE_HIGH_CONFIDENCE else "review",
        "reason_labels": reason_labels,
        "signals": {
            **signals,
            "side_metric": round(side_metric, 4),
        },
    }


def _compute_local_angle_feature(image_path: Path) -> dict[str, Any] | None:
    """Read a real source image and build local-only angle/composition signals.

    The feature is intentionally lightweight and local: no model call, no mock
    rows, and no classification decision. It only groups visually similar
    thumbnails so an operator can batch-confirm the angle.
    """
    try:
        from PIL import Image, ImageFilter, ImageOps
    except Exception:
        return None

    cache_key = _feature_cache_key(image_path)
    if cache_key is None:
        return None
    if cache_key in _LOCAL_ANGLE_FEATURE_CACHE:
        cached = _LOCAL_ANGLE_FEATURE_CACHE[cache_key]
        return dict(cached) if isinstance(cached, dict) else None
    try:
        with Image.open(image_path) as opened:
            transposed = ImageOps.exif_transpose(opened)
            exif_transposed = transposed.size != opened.size
            image = transposed.convert("RGB")
            width, height = image.size
            small = ImageOps.fit(image, (16, 16), method=Image.Resampling.BILINEAR)
            pixels = list(small.getdata())
            rgb_vector = tuple(float(channel) / 255.0 for pixel in small.getdata() for channel in pixel)
            gray_image = ImageOps.grayscale(small)
            gray_values = [float(value) / 255.0 for value in gray_image.getdata()]
            edge_values = [float(value) / 255.0 for value in gray_image.filter(ImageFilter.FIND_EDGES).getdata()]
    except Exception:
        _LOCAL_ANGLE_FEATURE_CACHE[cache_key] = None
        return None

    if width <= 0 or height <= 0:
        _LOCAL_ANGLE_FEATURE_CACHE[cache_key] = None
        return None
    grid = 16
    border_pixels = [
        pixels[row * grid + col]
        for row in range(grid)
        for col in range(grid)
        if row in {0, grid - 1} or col in {0, grid - 1}
    ]
    bg_r, bg_g, bg_b = _mean_rgb(border_pixels)
    active_positions: list[tuple[int, int]] = []
    skin_positions: list[tuple[int, int]] = []
    for row in range(grid):
        for col in range(grid):
            index = row * grid + col
            pixel = pixels[index]
            color_distance = math.sqrt(
                (pixel[0] - bg_r) ** 2 + (pixel[1] - bg_g) ** 2 + (pixel[2] - bg_b) ** 2
            ) / 441.7
            is_skin = _skin_like(pixel)
            if color_distance > 0.10 or edge_values[index] > 0.24 or is_skin:
                active_positions.append((row, col))
            if is_skin:
                skin_positions.append((row, col))
    if active_positions:
        min_row = min(row for row, _col in active_positions)
        max_row = max(row for row, _col in active_positions)
        min_col = min(col for _row, col in active_positions)
        max_col = max(col for _row, col in active_positions)
    else:
        min_row, max_row, min_col, max_col = 0, grid - 1, 0, grid - 1
    bbox_width = (max_col - min_col + 1) / grid
    bbox_height = (max_row - min_row + 1) / grid
    bbox_center_x = (min_col + max_col + 1) / (2 * grid)
    bbox_center_y = (min_row + max_row + 1) / (2 * grid)
    symmetry_delta = _mean(
        [
            abs(gray_values[row * grid + col] - gray_values[row * grid + (grid - 1 - col)])
            for row in range(grid)
            for col in range(grid // 2)
        ]
    )
    skin_left = sum(1 for _row, col in skin_positions if col < grid // 2)
    skin_right = sum(1 for _row, col in skin_positions if col >= grid // 2)
    skin_total = max(len(skin_positions), 1)
    skin_side_bias = (skin_left - skin_right) / skin_total
    skin_center_x = (
        sum((col + 0.5) / grid for _row, col in skin_positions) / len(skin_positions)
        if skin_positions
        else bbox_center_x
    )
    aspect = width / height
    orientation = "portrait" if height > width * 1.1 else "landscape" if width > height * 1.1 else "square"
    brightness = _mean(gray_values)
    contrast = math.sqrt(_mean([(value - brightness) ** 2 for value in gray_values]))
    left_values = [gray_values[row * 16 + col] for row in range(16) for col in range(5)]
    right_values = [gray_values[row * 16 + col] for row in range(16) for col in range(11, 16)]
    top_values = [gray_values[row * 16 + col] for row in range(5) for col in range(16)]
    bottom_values = [gray_values[row * 16 + col] for row in range(11, 16) for col in range(16)]
    signals = {
        "exif_transposed": exif_transposed,
        "subject_bbox": {
            "x": round(min_col / grid, 4),
            "y": round(min_row / grid, 4),
            "width_ratio": round(bbox_width, 4),
            "height_ratio": round(bbox_height, 4),
            "center_x": round(bbox_center_x, 4),
            "center_y": round(bbox_center_y, 4),
        },
        "subject_center_offset": round(bbox_center_x - 0.5, 4),
        "skin_center_offset": round(skin_center_x - 0.5, 4),
        "skin_side_bias": round(skin_side_bias, 4),
        "symmetry_delta": round(symmetry_delta, 4),
        "left_right_bias": round(_mean(left_values) - _mean(right_values), 4),
        "top_bottom_bias": round(_mean(top_values) - _mean(bottom_values), 4),
        "active_pixel_ratio": round(len(active_positions) / (grid * grid), 4),
        "skin_pixel_ratio": round(len(skin_positions) / (grid * grid), 4),
    }
    feature = {
        "width": int(width),
        "height": int(height),
        "aspect": float(aspect),
        "orientation": orientation,
        "brightness": float(brightness),
        "contrast": float(contrast),
        "edge_density": float(_mean(edge_values)),
        "left_right_bias": float(_mean(left_values) - _mean(right_values)),
        "top_bottom_bias": float(_mean(top_values) - _mean(bottom_values)),
        "vector": rgb_vector,
        "local_angle": _local_angle_suggestion_from_signals(signals),
    }
    _LOCAL_ANGLE_FEATURE_CACHE[cache_key] = dict(feature)
    return feature


def _angle_group_feature(item: dict[str, Any]) -> dict[str, Any] | None:
    filename = str(item.get("filename") or "")
    case_abs_path = str(item.get("case_abs_path") or "")
    if not filename or not case_abs_path:
        return None
    image_path = (Path(case_abs_path) / filename).resolve()
    try:
        image_path.relative_to(Path(case_abs_path).resolve())
    except ValueError:
        return None
    if not image_path.is_file():
        return None
    feature = _compute_local_angle_feature(image_path)
    if feature is None:
        return None
    return {
        **feature,
        "item": item,
        "filename": filename,
        "sequence": _filename_sequence(filename),
    }


def _angle_group_distance(a: dict[str, Any], b: dict[str, Any]) -> float:
    vector_size = len(a["vector"])
    if vector_size <= 0 or vector_size != len(b["vector"]):
        return 1.0
    a_view = str((a.get("local_angle") or {}).get("suggested_view") or "")
    b_view = str((b.get("local_angle") or {}).get("suggested_view") or "")
    view_penalty = 0.22 if a_view and b_view and a_view != b_view else 0.0
    vector_delta = math.sqrt(sum((float(left) - float(right)) ** 2 for left, right in zip(a["vector"], b["vector"])) / vector_size)
    return (
        vector_delta
        + abs(float(a["aspect"]) - float(b["aspect"])) * 0.3
        + abs(float(a["edge_density"]) - float(b["edge_density"])) * 0.6
        + abs(float(a["left_right_bias"]) - float(b["left_right_bias"])) * 0.15
        + view_penalty
    )


def _angle_group_update_centroid(group: dict[str, Any], feature: dict[str, Any]) -> None:
    count = len(group["features"])
    if count <= 1:
        group["centroid"] = feature
        return
    centroid = dict(group["centroid"])
    vector_size = len(feature["vector"])
    centroid["vector"] = tuple(
        sum(float(item["vector"][index]) for item in group["features"]) / count for index in range(vector_size)
    )
    for key in ("aspect", "edge_density", "left_right_bias", "brightness", "contrast", "top_bottom_bias"):
        centroid[key] = sum(float(item[key]) for item in group["features"]) / count
    group["centroid"] = centroid


def _sequence_range_label(features: list[dict[str, Any]]) -> str:
    seqs = [int(item["sequence"]) for item in features if int(item.get("sequence") or 0) > 0]
    if not seqs:
        return "无连续编号"
    if min(seqs) == max(seqs):
        return f"编号 {min(seqs)}"
    return f"编号 {min(seqs)}-{max(seqs)}"


def _angle_vote_summary(features: list[dict[str, Any]], similarity_score: int) -> dict[str, Any]:
    votes = {view: 0 for view in ("front", "oblique", "side")}
    confidence_by_view = {view: [] for view in ("front", "oblique", "side")}
    evidence: list[str] = []
    for feature in features:
        local = feature.get("local_angle") if isinstance(feature.get("local_angle"), dict) else {}
        view = str(local.get("suggested_view") or "")
        if view not in votes:
            continue
        votes[view] += 1
        confidence_by_view[view].append(float(local.get("confidence") or 0))
        for label in local.get("reason_labels") or []:
            label = str(label)
            if label and label not in evidence:
                evidence.append(label)
    suggested_view = None
    if any(votes.values()):
        suggested_view = sorted(votes.items(), key=lambda item: (-item[1], item[0]))[0][0]
    item_count = max(len(features), 1)
    dominant_count = votes.get(suggested_view or "", 0) if suggested_view else 0
    confidence_values = confidence_by_view.get(suggested_view or "", []) if suggested_view else []
    confidence_avg = _mean(confidence_values)
    agreement_ratio = dominant_count / item_count
    can_quick_confirm = bool(
        suggested_view
        and dominant_count == len(features)
        and confidence_avg >= LOCAL_ANGLE_REVIEW_CONFIDENCE
        and similarity_score >= 70
    )
    return {
        "local_angle_votes": votes,
        "suggested_view": suggested_view,
        "suggested_view_label": {"front": "正面", "oblique": "45°", "side": "侧面"}.get(suggested_view or ""),
        "suggested_view_confidence": round(confidence_avg, 4) if suggested_view else None,
        "suggested_view_agreement": round(agreement_ratio, 4),
        "can_quick_confirm_angle": can_quick_confirm,
        "recommended_patch": {"manual_view": suggested_view} if suggested_view else None,
        "angle_evidence_labels": evidence[:6],
    }


def _angle_sort_groups(items: list[dict[str, Any]], *, case_id: int | None = None, limit: int = 40) -> list[dict[str, Any]]:
    if case_id is None:
        return []
    candidates = [
        item
        for item in items
        if int(item.get("case_id") or 0) == int(case_id)
        and (
            str(item.get("phase") or "") in ALLOWED_PHASES
            or str((item.get("case_preflight") or {}).get("source_phase_hint") or "") in ALLOWED_PHASES
        )
        and str(item.get("view") or "") not in ALLOWED_VIEWS
        and not item.get("render_excluded")
    ]
    if not candidates or len(candidates) > ANGLE_GROUP_MAX_ITEMS:
        return []

    features = [feature for item in candidates if (feature := _angle_group_feature(item)) is not None]
    features.sort(key=lambda feature: (str(feature["orientation"]), int(feature["sequence"]), str(feature["filename"])))
    groups: list[dict[str, Any]] = []
    for feature in features:
        best_group: dict[str, Any] | None = None
        best_distance = 999.0
        for group in groups:
            if group["orientation"] != feature["orientation"]:
                continue
            distance = _angle_group_distance(feature, group["centroid"])
            if distance < best_distance:
                best_group = group
                best_distance = distance
        if best_group is not None and best_distance <= ANGLE_GROUP_DISTANCE_THRESHOLD:
            best_group["features"].append(feature)
            best_group["distances"].append(best_distance)
            _angle_group_update_centroid(best_group, feature)
        else:
            groups.append(
                {
                    "orientation": feature["orientation"],
                    "features": [feature],
                    "distances": [0.0],
                    "centroid": feature,
                }
            )

    out: list[dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        group_features = sorted(group["features"], key=lambda feature: (int(feature["sequence"]), str(feature["filename"])))
        item_count = len(group_features)
        filenames = [str(feature["filename"]) for feature in group_features]
        distance_avg = sum(float(value) for value in group["distances"]) / max(len(group["distances"]), 1)
        brightness_avg = sum(float(feature["brightness"]) for feature in group_features) / item_count
        edge_avg = sum(float(feature["edge_density"]) for feature in group_features) / item_count
        aspect_avg = sum(float(feature["aspect"]) for feature in group_features) / item_count
        similarity_score = max(0, min(100, int(round(100 - distance_avg * 420))))
        sequence_label = _sequence_range_label(group_features)
        angle_votes = _angle_vote_summary(group_features, similarity_score)
        phase_hints = {
            str((feature["item"].get("case_preflight") or {}).get("source_phase_hint") or "")
            for feature in group_features
            if str((feature["item"].get("case_preflight") or {}).get("source_phase_hint") or "") in ALLOWED_PHASES
        }
        missing_phase_count = sum(1 for feature in group_features if str(feature["item"].get("phase") or "") not in ALLOWED_PHASES)
        suggested_phase = next(iter(phase_hints)) if len(phase_hints) == 1 else None
        recommended_patch = dict(angle_votes.get("recommended_patch") or {})
        if suggested_phase and missing_phase_count:
            recommended_patch["manual_phase"] = suggested_phase
        suggested_label = angle_votes.get("suggested_view_label")
        suggested_confidence = angle_votes.get("suggested_view_confidence")
        confidence_text = f" · 建议{suggested_label} {int(round(float(suggested_confidence or 0) * 100))}%" if suggested_label else ""
        out.append(
            {
                "id": f"angle:{case_id}:{index}",
                "case_id": case_id,
                "case_title": group_features[0]["item"].get("case_title"),
                "orientation": group["orientation"],
                "orientation_label": _orientation_label(str(group["orientation"])),
                "item_count": item_count,
                "filenames": filenames,
                "sample_images": [
                    {
                        "case_id": int(feature["item"]["case_id"]),
                        "filename": str(feature["filename"]),
                        "preview_url": feature["item"].get("preview_url"),
                    }
                    for feature in group_features[:8]
                ],
                "images": [
                    {
                        "case_id": int(feature["item"]["case_id"]),
                        "filename": str(feature["filename"]),
                        "preview_url": feature["item"].get("preview_url"),
                    }
                    for feature in group_features
                ],
                "sequence_range": sequence_label,
                "composition_summary": f"{_orientation_label(str(group['orientation']))} / {sequence_label} / 相似度 {similarity_score}{confidence_text}",
                "reason_labels": [
                    "真实缩略图构图相似",
                    sequence_label,
                    f"边缘密度 {round(edge_avg, 2)}",
                    f"平均亮度 {round(brightness_avg, 2)}",
                    *angle_votes["angle_evidence_labels"][:3],
                ],
                "metrics": {
                    "similarity_score": similarity_score,
                    "distance_avg": round(distance_avg, 4),
                    "aspect_avg": round(aspect_avg, 4),
                    "brightness_avg": round(brightness_avg, 4),
                    "edge_density_avg": round(edge_avg, 4),
                    "local_angle_confidence_avg": suggested_confidence,
                    "local_angle_agreement": angle_votes.get("suggested_view_agreement"),
                },
                **angle_votes,
                "suggested_phase": suggested_phase,
                "suggested_phase_label": {"before": "术前", "after": "术后"}.get(suggested_phase or ""),
                "missing_phase_count": missing_phase_count,
                "recommended_patch": recommended_patch or None,
                "recommended_action": (
                    f"本地建议判为{suggested_label}，进入大图复核后可批量确认"
                    if suggested_label
                    else "人工查看样张后批量设为正面、45°或侧面"
                ),
            }
        )
    out.sort(
        key=lambda group: (
            -int(group["item_count"]),
            -int((group.get("metrics") or {}).get("similarity_score") or 0),
            str(group["sequence_range"]),
        )
    )
    return out[:limit]


_TASK_QUEUE_META = {
    "missing_phase": ("缺阶段", True, "批量确认术前/术后阶段"),
    "missing_view": ("缺角度", True, "按相似构图批量判角"),
    "low_confidence": ("低置信", True, "复核本地视觉低置信建议"),
    "missing_usability": ("缺可用性", True, "确认是否适合正式出图"),
    "blocked_case": ("被阻断 case", True, "回 case source-group 预检处理阻断"),
    "used_in_render": ("已用于正式出图", False, "复核已入选正式图的源片"),
    "angle_sort_groups": ("可批量判角分组", False, "打开分组大图后批量确认角度"),
}


def _task_queue_summary(items: list[dict[str, Any]], angle_sort_groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lanes: dict[str, dict[str, Any]] = {}
    for key, (label, blocks_render, action) in _TASK_QUEUE_META.items():
        if key == "angle_sort_groups":
            count = len(angle_sort_groups)
            item_count = sum(int(group.get("item_count") or 0) for group in angle_sort_groups)
        else:
            count = sum(1 for item in items if key in set(item.get("task_groups") or []))
            item_count = count
        lanes[key] = {
            "key": key,
            "label": label,
            "count": count,
            "item_count": item_count,
            "blocks_render": blocks_render,
            "recommended_action": action,
            "queue_url": f"/images?status={key}" if key != "angle_sort_groups" else "/images?status=review_needed&focus=angle_sort_groups",
        }
    return lanes


def _production_summary(
    *,
    items: list[dict[str, Any]],
    batch_groups: list[dict[str, Any]],
    angle_sort_groups: list[dict[str, Any]],
) -> dict[str, Any]:
    blocking = [
        item for item in items
        if (item.get("classification_suggestion") or {}).get("render_gate", {}).get("blocks_render")
    ]
    return {
        "review_needed_total": len(items),
        "blocking_image_count": len(blocking),
        "low_confidence_count": sum(1 for item in items if item.get("low_confidence")),
        "bulk_group_count": len(batch_groups),
        "angle_sort_group_count": len(angle_sort_groups),
        "ready_for_render_candidate_count": len(items) - len(blocking),
        "policy": {
            "name": "local_first_publishability_gate",
            "high_confidence_threshold": SAFE_CONFIRM_CONFIDENCE,
            "low_confidence_blocks_render": True,
        },
    }


@router.get("/api/image-workbench/queue")
def list_image_classification_queue(
    status: str = Query("review_needed"),
    phase: str = Query("all"),
    view: str = Query("all"),
    body_part: str = Query("all"),
    q: str = Query(""),
    case_id: int | None = Query(default=None),
    source_group_case_id: int | None = Query(default=None),
    limit: int = Query(120, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    where = ["c.trashed_at IS NULL"]
    params: list[Any] = []
    with db.connect() as conn:
        if source_group_case_id:
            source_case_ids = _source_group_case_ids(conn, source_group_case_id)
            placeholders = ",".join("?" * len(source_case_ids))
            where.append(f"c.id IN ({placeholders})")
            params.extend(source_case_ids)
        elif case_id:
            where.append("c.id = ?")
            params.append(case_id)

        case_rows = conn.execute(
            f"""
            SELECT
              c.*,
              g.id AS group_id,
              COALESCE(g.title, c.abs_path) AS title,
              COALESCE(g.root_path, c.abs_path) AS root_path
            FROM cases c
            LEFT JOIN case_groups g ON g.primary_case_id = c.id
            WHERE {' AND '.join(where)}
            ORDER BY c.indexed_at DESC, c.id DESC
            """,
            params,
        ).fetchall()
        obs_rows = conn.execute(
            """
            SELECT
              o.*,
              g.root_path,
              c.abs_path AS case_abs_path
            FROM image_observations o
            JOIN case_groups g ON g.id = o.group_id
            JOIN cases c ON c.id = o.case_id
            WHERE c.trashed_at IS NULL
            """
        ).fetchall()
        obs_by_case_file: dict[tuple[int, str], dict[str, Any]] = {}
        for obs in obs_rows:
            if obs["case_id"] is None:
                continue
            filename = _strip_case_prefix(obs["root_path"], obs["case_abs_path"], obs["image_path"])
            obs_by_case_file[(int(obs["case_id"]), filename)] = {
                "id": obs["id"],
                "body_part": obs["body_part"],
                "confidence": obs["confidence"],
                "source": obs["source"],
                "reasons": _json_load(obs["reasons_json"], []),
                "quality": _json_load(obs["quality_json"], {}),
            }
        case_ids = [int(row["id"]) for row in case_rows]
        overrides = _manual_overrides(conn, case_ids)
        used_by_case = _used_files_by_case(conn, case_ids)
        source_context_by_case = {int(row["id"]): _case_source_context(row) for row in case_rows}
    q_norm = q.strip()
    all_items: list[dict[str, Any]] = []
    for row in case_rows:
        skill_by_name = _skill_by_file(row["skill_image_metadata_json"])
        for filename in _image_files(row["meta_json"]):
            item = _case_image_item(
                row,
                filename,
                overrides=overrides,
                used_by_case=used_by_case,
                observation=obs_by_case_file.get((int(row["id"]), filename)),
                skill_by_name=skill_by_name,
            )
            all_items.append(
                _enrich_classification_item(item, source_context_by_case.get(int(row["id"])))
            )
    all_items.sort(
        key=lambda item: (
            0 if item["queue_state"] == "needs_manual" else 1 if item["queue_state"] == "low_confidence" else 2,
            item["confidence"],
            -int(item["case_id"]),
            item["filename"],
        )
    )
    items = [
        item
        for item in all_items
        if _filter_item(item, status=status, phase=phase, view=view, body_part=body_part, q=q_norm)
    ]
    counts: dict[str, int] = {}
    summary = {
        "needs_manual": 0,
        "low_confidence": 0,
        "manual": 0,
        "identified": 0,
        "used_in_render": 0,
        "render_excluded": 0,
        "needs_repick": 0,
        "copied_review": 0,
        "missing_phase": 0,
        "missing_view": 0,
        "missing_usability": 0,
        "blocked_case": 0,
    }
    for item in items:
        counts[item["queue_state"]] = counts.get(item["queue_state"], 0) + 1
        if item["queue_state"] in summary and item["queue_state"] not in {"manual", "used_in_render"}:
            summary[item["queue_state"]] += 1
        if item["used_in_render"]:
            summary["used_in_render"] += 1
        if item["manual"]:
            summary["manual"] += 1
        for group in item.get("task_groups") or []:
            if group in summary:
                summary[group] += 1
    paged = items[offset : offset + limit]
    if case_id or source_group_case_id or limit <= 40:
        paged = _attach_local_angle_suggestions(paged)
    case_groups = _case_group_summaries(all_items, items, source_context_by_case)
    batch_groups = _batch_group_summaries(items)
    if source_group_case_id:
        angle_sort_groups = []
        for angle_case_id in sorted({int(row["id"]) for row in case_rows}):
            angle_sort_groups.extend(_angle_sort_groups(items, case_id=angle_case_id))
        angle_sort_groups.sort(
            key=lambda group: (
                int(group["case_id"]) != int(source_group_case_id),
                -int(group["item_count"]),
                -int((group.get("metrics") or {}).get("similarity_score") or 0),
                str(group.get("sequence_range") or ""),
            )
        )
    else:
        if case_id:
            angle_sort_groups = _angle_sort_groups(items, case_id=case_id)
        else:
            candidate_counts: dict[int, int] = {}
            for item in paged:
                if (
                    str(item.get("phase") or "") not in ALLOWED_PHASES
                    and str((item.get("case_preflight") or {}).get("source_phase_hint") or "") not in ALLOWED_PHASES
                ):
                    continue
                if str(item.get("view") or "") in ALLOWED_VIEWS:
                    continue
                if item.get("render_excluded"):
                    continue
                cid = int(item.get("case_id") or 0)
                if cid > 0:
                    candidate_counts[cid] = candidate_counts.get(cid, 0) + 1
            angle_sort_groups = []
            for angle_case_id, _count in sorted(candidate_counts.items(), key=lambda pair: (-pair[1], -pair[0]))[:6]:
                angle_sort_groups.extend(_angle_sort_groups(paged, case_id=angle_case_id, limit=3))
            angle_sort_groups.sort(
                key=lambda group: (
                    -int(group["item_count"]),
                    -int((group.get("metrics") or {}).get("similarity_score") or 0),
                    int(group["case_id"]),
                    str(group.get("sequence_range") or ""),
                )
            )
            angle_sort_groups = angle_sort_groups[:40]
    task_queues = _task_queue_summary(items, angle_sort_groups)
    return {
        "items": paged,
        "total": len(items),
        "limit": limit,
        "offset": offset,
        "status": status,
        "counts": counts,
        "summary": summary,
        "task_queues": task_queues,
        "production_summary": _production_summary(
            items=items,
            batch_groups=batch_groups,
            angle_sort_groups=angle_sort_groups,
        ),
        "case_groups": case_groups,
        "batch_groups": batch_groups,
        "angle_sort_groups": angle_sort_groups,
    }


def _workbench_case_rows(conn: sqlite3.Connection, case_id: int | None = None) -> list[sqlite3.Row]:
    where = ["c.trashed_at IS NULL"]
    params: list[Any] = []
    if case_id is not None:
        where.append("c.id = ?")
        params.append(case_id)
    return conn.execute(
        f"""
        SELECT
          c.*,
          g.id AS group_id,
          COALESCE(g.title, c.abs_path) AS title,
          COALESCE(g.root_path, c.abs_path) AS root_path
        FROM cases c
        LEFT JOIN case_groups g ON g.primary_case_id = c.id
        WHERE {' AND '.join(where)}
        ORDER BY c.indexed_at DESC, c.id DESC
        """,
        params,
    ).fetchall()


def _observation_map(conn: sqlite3.Connection) -> dict[tuple[int, str], dict[str, Any]]:
    obs_rows = conn.execute(
        """
        SELECT
          o.*,
          g.root_path,
          c.abs_path AS case_abs_path
        FROM image_observations o
        JOIN case_groups g ON g.id = o.group_id
        JOIN cases c ON c.id = o.case_id
        WHERE c.trashed_at IS NULL
        """
    ).fetchall()
    obs_by_case_file: dict[tuple[int, str], dict[str, Any]] = {}
    for obs in obs_rows:
        if obs["case_id"] is None:
            continue
        filename = _strip_case_prefix(obs["root_path"], obs["case_abs_path"], obs["image_path"])
        obs_by_case_file[(int(obs["case_id"]), filename)] = {
            "id": obs["id"],
            "body_part": obs["body_part"],
            "confidence": obs["confidence"],
            "source": obs["source"],
            "reasons": _json_load(obs["reasons_json"], []),
            "quality": _json_load(obs["quality_json"], {}),
        }
    return obs_by_case_file


def _workbench_items_for_rows(conn: sqlite3.Connection, case_rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    case_ids = [int(row["id"]) for row in case_rows]
    overrides = _manual_overrides(conn, case_ids)
    used_by_case = _used_files_by_case(conn, case_ids)
    obs_by_case_file = _observation_map(conn)
    source_context_by_case = {int(row["id"]): _case_source_context(row) for row in case_rows}
    items: list[dict[str, Any]] = []
    for row in case_rows:
        skill_by_name = _skill_by_file(row["skill_image_metadata_json"])
        for filename in _image_files(row["meta_json"]):
            item = _case_image_item(
                row,
                filename,
                overrides=overrides,
                used_by_case=used_by_case,
                observation=obs_by_case_file.get((int(row["id"]), filename)),
                skill_by_name=skill_by_name,
            )
            items.append(
                _enrich_classification_item(item, source_context_by_case.get(int(row["id"])))
            )
    return items


def _dominant(values: list[str | None], fallback: str | None = None) -> str | None:
    counts: dict[str, int] = {}
    for value in values:
        value = str(value or "").strip()
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return fallback
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _case_default_body_part(row: sqlite3.Row) -> str:
    category = str(row["category"] or "")
    if category == "body":
        return "body"
    if category == "standard_face":
        return "face"
    return "unknown"


def _render_slot_gaps(row: sqlite3.Row, target_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    slots = {
        view: {"before": [], "after": []}
        for view in ("front", "oblique", "side")
    }
    default_body_part = _case_default_body_part(row)
    for item in target_items:
        if item.get("render_excluded"):
            continue
        view = str(item.get("view") or "")
        phase = str(item.get("phase") or "")
        if view not in slots or phase not in {"before", "after"}:
            continue
        slots[view][phase].append(item)
    gaps: list[dict[str, Any]] = []
    for view, roles in slots.items():
        context = [*roles["before"], *roles["after"]]
        body_part = _dominant([item.get("body_part") for item in context], default_body_part) or "unknown"
        treatment_area = _dominant([item.get("treatment_area") for item in context], None)
        for role in ("before", "after"):
            if roles[role]:
                continue
            gaps.append(
                {
                    "key": f"{view}-{role}",
                    "kind": "render_slot",
                    "view": view,
                    "view_label": {"front": "正面", "oblique": "45°", "side": "侧面"}[view],
                    "role": role,
                    "phase": role,
                    "role_label": "术前" if role == "before" else "术后",
                    "body_part": body_part,
                    "treatment_area": treatment_area,
                    "current_count": 0,
                    "required_count": 1,
                }
            )
    return gaps


def _candidate_score(
    *,
    candidate: dict[str, Any],
    target_row: sqlite3.Row,
    gap: dict[str, Any],
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = float(candidate.get("confidence") or 0) * 20
    if candidate.get("customer_id") and target_row["customer_id"] and candidate.get("customer_id") == target_row["customer_id"]:
        score += 80
        reasons.append("同客户")
    elif candidate.get("customer_raw") and target_row["customer_raw"] and candidate.get("customer_raw") == target_row["customer_raw"]:
        score += 60
        reasons.append("同客户名")
    if gap.get("treatment_area") and candidate.get("treatment_area") and gap["treatment_area"] in str(candidate["treatment_area"]):
        score += 30
        reasons.append("同治疗部位")
    gap_body = str(gap.get("body_part") or "unknown")
    candidate_body = str(candidate.get("body_part") or "unknown")
    if gap_body != "unknown" and candidate_body == gap_body:
        score += 20
        reasons.append("同身体/面部类型")
    review_state = candidate.get("review_state") if isinstance(candidate.get("review_state"), dict) else {}
    verdict = str((review_state or {}).get("verdict") or "")
    if verdict == "usable":
        score += 25
        reasons.append("已确认可用")
    elif verdict == "deferred":
        score -= 8
        reasons.append("低优先")
    if candidate.get("manual"):
        score += 16
        reasons.append("已人工分类")
    if candidate.get("used_in_render"):
        score += 8
        reasons.append("曾用于正式出图")
    if not reasons:
        reasons.append("阶段角度匹配")
    return score, reasons


@router.get("/api/image-workbench/supplement-candidates")
def list_supplement_candidates(
    target_case_id: int = Query(..., ge=1),
    limit_per_gap: int = Query(8, ge=1, le=30),
) -> dict[str, Any]:
    with db.connect() as conn:
        target_rows = _workbench_case_rows(conn, target_case_id)
        if not target_rows:
            raise HTTPException(404, f"case {target_case_id} not found")
        target_row = target_rows[0]
        target_items = _workbench_items_for_rows(conn, target_rows)
        gaps = _render_slot_gaps(target_row, target_items)
        all_rows = _workbench_case_rows(conn)
        all_items = _workbench_items_for_rows(conn, all_rows)

    excluded_states = {"needs_manual", "low_confidence", "needs_repick", "render_excluded", "copied_review"}
    out_gaps: list[dict[str, Any]] = []
    for gap in gaps:
        ranked: list[dict[str, Any]] = []
        for item in all_items:
            if int(item["case_id"]) == target_case_id:
                continue
            if item.get("queue_state") in excluded_states or item.get("render_excluded"):
                continue
            if str(item.get("phase")) != str(gap["phase"]) or str(item.get("view")) != str(gap["view"]):
                continue
            gap_body = str(gap.get("body_part") or "unknown")
            item_body = str(item.get("body_part") or "unknown")
            if gap_body != "unknown" and item_body != "unknown" and gap_body != item_body:
                continue
            score, reasons = _candidate_score(candidate=item, target_row=target_row, gap=gap)
            ranked.append(
                {
                    "case_id": item["case_id"],
                    "filename": item["filename"],
                    "preview_url": item["preview_url"],
                    "case_url": item["case_url"],
                    "case_title": item["case_title"],
                    "customer_raw": item["customer_raw"],
                    "phase": item["phase"],
                    "view": item["view"],
                    "body_part": item["body_part"],
                    "treatment_area": item["treatment_area"],
                    "confidence": item["confidence"],
                    "queue_state": item["queue_state"],
                    "manual": item["manual"],
                    "used_in_render": item["used_in_render"],
                    "review_state": item["review_state"],
                    "score": round(score, 3),
                    "match_reasons": reasons,
                }
            )
        ranked.sort(key=lambda item: (-float(item["score"]), -float(item["confidence"] or 0), int(item["case_id"]), item["filename"]))
        out_gaps.append({**gap, "candidates": ranked[:limit_per_gap], "candidate_count": len(ranked)})
    return {
        "target_case_id": target_case_id,
        "gaps": out_gaps,
        "summary": {
            "gap_count": len(out_gaps),
            "candidate_count": sum(int(gap["candidate_count"]) for gap in out_gaps),
        },
    }


class ImageWorkbenchTarget(BaseModel):
    case_id: int
    filename: str


class ImageWorkbenchBatchPayload(BaseModel):
    items: list[ImageWorkbenchTarget] = Field(min_length=1, max_length=300)
    manual_phase: Literal["before", "after", "clear"] | None = None
    manual_view: Literal["front", "oblique", "side", "clear"] | None = None
    body_part: Literal["face", "body", "unknown", "clear"] | None = None
    treatment_area: str | None = Field(default=None, max_length=200)
    verdict: Literal["usable", "deferred", "needs_repick", "excluded", "reopen"] | None = None
    reviewer: str | None = "operator"
    note: str | None = Field(default=None, max_length=1000)


class ImageWorkbenchConfirmSuggestionsPayload(BaseModel):
    items: list[ImageWorkbenchTarget] = Field(min_length=1, max_length=300)
    min_confidence: float = Field(default=SAFE_CONFIRM_CONFIDENCE, ge=LOW_CONFIDENCE_BELOW, le=1.0)
    reviewer: str | None = "operator"
    note: str | None = Field(default=None, max_length=1000)
    mark_usable: bool = True


class ImageTransferPayload(BaseModel):
    items: list[ImageWorkbenchTarget] = Field(min_length=1, max_length=100)
    target_case_id: int
    mode: Literal["copy"] = "copy"
    inherit_manual: bool = True
    inherit_review: bool = True
    require_target_review: bool = False
    reviewer: str | None = "operator"
    note: str | None = Field(default=None, max_length=1000)


def _case_dir(conn: sqlite3.Connection, case_id: int) -> Path:
    row = conn.execute("SELECT abs_path, trashed_at FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"case {case_id} not found")
    if row["trashed_at"]:
        raise HTTPException(410, f"case {case_id} has been moved to trash")
    path = Path(row["abs_path"]).resolve()
    if not path.is_dir():
        raise HTTPException(404, f"case directory missing: {path}")
    return path


def _validate_filename(case_dir: Path, filename: str) -> str:
    if not filename or filename in {".", ".."} or "\\" in filename:
        raise HTTPException(400, "invalid image filename")
    if TRASH_DIR_NAME in Path(filename).parts:
        raise HTTPException(400, "trashed images cannot be transferred")
    target = (case_dir / filename).resolve()
    try:
        target.relative_to(case_dir)
    except ValueError:
        raise HTTPException(400, "invalid image path")
    if not target.is_file():
        raise HTTPException(404, f"image not found: {filename}")
    if target.suffix.lower() not in scanner.IMAGE_EXTS:
        raise HTTPException(400, f"unsupported image extension: {target.suffix}")
    return filename


def _target_copy_name(target_dir: Path, source: Path, source_case_id: int) -> str:
    suffix = source.suffix.lower()
    stem = source.stem.strip() or "image"
    candidate = f"{stem}{suffix}"
    if not (target_dir / candidate).exists():
        return candidate
    base = f"{stem}-来自case{source_case_id}"
    candidate = f"{base}{suffix}"
    i = 2
    while (target_dir / candidate).exists():
        candidate = f"{base}-{i}{suffix}"
        i += 1
    return candidate


def _source_review_state(conn: sqlite3.Connection, case_id: int, filename: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT meta_json FROM cases WHERE id = ?", (case_id,)).fetchone()
    meta = _case_meta(row["meta_json"] if row else None)
    states = meta.get(IMAGE_REVIEW_META_KEY)
    if not isinstance(states, dict):
        return None
    state = _review_for_filename(states, filename)
    return dict(state) if isinstance(state, dict) else None


def _copy_review_state(
    conn: sqlite3.Connection,
    *,
    source_case_id: int,
    source_filename: str,
    target_case_id: int,
    target_filename: str,
    reviewer: str,
    note: str | None,
    now: str,
    require_target_review: bool = False,
) -> None:
    source_state = _source_review_state(conn, source_case_id, source_filename)
    row = conn.execute("SELECT meta_json FROM cases WHERE id = ?", (target_case_id,)).fetchone()
    meta = _case_meta(row["meta_json"] if row else None)
    states = meta.get(IMAGE_REVIEW_META_KEY)
    if not isinstance(states, dict):
        states = {}
    inherited = dict(source_state or {})
    if require_target_review:
        state = {
            key: value
            for key, value in inherited.items()
            if key in {"body_part", "treatment_area"}
        }
        if inherited.get("verdict"):
            state["inherited_verdict"] = inherited.get("verdict")
            state["inherited_label"] = inherited.get("label")
        state["copied_requires_review"] = True
        state["label"] = "补图待确认"
        state["render_excluded"] = False
    else:
        state = inherited
    state.update(
        {
            "copied_from_case_id": source_case_id,
            "copied_from_filename": source_filename,
            "copied_at": now,
            "reviewer": reviewer,
        }
    )
    if note:
        state["note"] = note
    states[target_filename] = state
    meta[IMAGE_REVIEW_META_KEY] = states
    conn.execute(
        "UPDATE cases SET meta_json = ? WHERE id = ?",
        (json.dumps(meta, ensure_ascii=False), target_case_id),
    )


def _write_image_override(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    filename: str,
    manual_phase: str | None,
    manual_view: str | None,
    updated_at: str,
) -> None:
    if manual_phase is None and manual_view is None:
        conn.execute(
            "DELETE FROM case_image_overrides WHERE case_id = ? AND filename = ?",
            (case_id, filename),
        )
        return
    conn.execute(
        """INSERT INTO case_image_overrides
             (case_id, filename, manual_phase, manual_view, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(case_id, filename) DO UPDATE SET
             manual_phase = excluded.manual_phase,
             manual_view = excluded.manual_view,
             updated_at = excluded.updated_at""",
        (case_id, filename, manual_phase, manual_view, updated_at),
    )


def _current_override(conn: sqlite3.Connection, case_id: int, filename: str) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT manual_phase, manual_view FROM case_image_overrides WHERE case_id = ? AND filename = ?",
        (case_id, filename),
    ).fetchone()
    if not row:
        return None, None
    return row["manual_phase"], row["manual_view"]


def _update_review_state(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    filename: str,
    payload: ImageWorkbenchBatchPayload,
    now: str,
) -> None:
    touches_state = (
        payload.verdict is not None
        or payload.body_part is not None
        or payload.treatment_area is not None
        or payload.note is not None
    )
    if not touches_state:
        return
    row = conn.execute("SELECT meta_json FROM cases WHERE id = ?", (case_id,)).fetchone()
    meta = _case_meta(row["meta_json"] if row else None)
    states = meta.get(IMAGE_REVIEW_META_KEY)
    if not isinstance(states, dict):
        states = {}
    state = states.get(filename)
    if not isinstance(state, dict):
        state = {}
    if payload.verdict == "reopen":
        state = {}
    elif payload.verdict:
        state.update(
            {
                "verdict": payload.verdict,
                "label": VERDICT_LABEL[payload.verdict],
                "render_excluded": payload.verdict == "excluded",
                "reviewed_at": now,
            }
        )
    if payload.body_part is not None:
        if payload.body_part == "clear":
            state.pop("body_part", None)
        else:
            state["body_part"] = payload.body_part
    if payload.treatment_area is not None:
        value = payload.treatment_area.strip()
        if value:
            state["treatment_area"] = value
        else:
            state.pop("treatment_area", None)
    reviewer = (payload.reviewer or "operator").strip() or "operator"
    if state:
        state["reviewer"] = reviewer
        if payload.note is not None:
            state["note"] = payload.note.strip() or None
        states[filename] = state
    else:
        states.pop(filename, None)
    if states:
        meta[IMAGE_REVIEW_META_KEY] = states
    else:
        meta.pop(IMAGE_REVIEW_META_KEY, None)
    conn.execute(
        "UPDATE cases SET meta_json = ? WHERE id = ?",
        (json.dumps(meta, ensure_ascii=False), case_id),
    )


@router.post("/api/image-workbench/batch")
def update_image_workbench_batch(payload: ImageWorkbenchBatchPayload) -> dict[str, Any]:
    if payload.manual_phase == "clear":
        requested_phase: str | None | Literal["__clear__"] = "__clear__"
    else:
        requested_phase = payload.manual_phase
    if payload.manual_view == "clear":
        requested_view: str | None | Literal["__clear__"] = "__clear__"
    else:
        requested_view = payload.manual_view
    updated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    now = _now_iso()
    case_ids = sorted({item.case_id for item in payload.items})
    with db.connect() as conn:
        befores = audit.snapshot_before(conn, case_ids)
        for item in payload.items:
            try:
                case_dir = _case_dir(conn, item.case_id)
                filename = _validate_filename(case_dir, item.filename)
                current_phase, current_view = _current_override(conn, item.case_id, filename)
                new_phase = None if requested_phase == "__clear__" else (requested_phase if requested_phase is not None else current_phase)
                new_view = None if requested_view == "__clear__" else (requested_view if requested_view is not None else current_view)
                if requested_phase is not None or requested_view is not None:
                    _write_image_override(
                        conn,
                        case_id=item.case_id,
                        filename=filename,
                        manual_phase=new_phase,
                        manual_view=new_view,
                        updated_at=now,
                    )
                _update_review_state(conn, case_id=item.case_id, filename=filename, payload=payload, now=now)
                updated.append({"case_id": item.case_id, "filename": filename})
            except HTTPException as e:
                skipped.append({"case_id": item.case_id, "filename": item.filename, "reason": str(e.detail)})
        audit.record_after(
            conn,
            case_ids,
            befores,
            op="image_workbench_batch",
            source_route="/api/image-workbench/batch",
        )
    return {
        "updated": len(updated),
        "items": updated,
        "skipped": skipped,
    }


@router.post("/api/image-workbench/confirm-suggestions")
def confirm_image_workbench_suggestions(payload: ImageWorkbenchConfirmSuggestionsPayload) -> dict[str, Any]:
    """Confirm only high-confidence existing labels.

    This is intentionally conservative: images with missing phase/view, low
    confidence, source-directory blockers, existing manual labels, or repick /
    copied-review states are skipped. The endpoint writes the current suggested
    phase/view as a manual override and can mark the image usable; it never
    invents labels.
    """
    updated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    now = _now_iso()
    case_ids = sorted({item.case_id for item in payload.items})
    reviewer = (payload.reviewer or "operator").strip() or "operator"
    note = payload.note if payload.note is not None else "高置信分类建议人工确认"
    with db.connect() as conn:
        case_rows = _workbench_case_rows(conn)
        target_case_rows = [row for row in case_rows if int(row["id"]) in set(case_ids)]
        current_items = _workbench_items_for_rows(conn, target_case_rows)
        items_by_key = {
            (int(item["case_id"]), str(item["filename"])): item
            for item in current_items
        }
        befores = audit.snapshot_before(conn, case_ids)
        for target in payload.items:
            try:
                case_dir = _case_dir(conn, target.case_id)
                filename = _validate_filename(case_dir, target.filename)
                item = items_by_key.get((target.case_id, filename))
                if item is None:
                    raise HTTPException(404, "image not found in workbench queue")
                safe = _safe_confirm_status(
                    item,
                    item.get("case_preflight") if isinstance(item.get("case_preflight"), dict) else None,
                    payload.min_confidence,
                )
                if not safe.get("eligible"):
                    skipped.append(
                        {
                            "case_id": target.case_id,
                            "filename": filename,
                            "reason": str(safe.get("reason") or "not_eligible"),
                        }
                    )
                    continue
                phase = str(item.get("phase") or "")
                view = str(item.get("view") or "")
                _write_image_override(
                    conn,
                    case_id=target.case_id,
                    filename=filename,
                    manual_phase=phase,
                    manual_view=view,
                    updated_at=now,
                )
                if payload.mark_usable:
                    review_payload = ImageWorkbenchBatchPayload(
                        items=[ImageWorkbenchTarget(case_id=target.case_id, filename=filename)],
                        verdict="usable",
                        reviewer=reviewer,
                        note=note,
                    )
                    _update_review_state(conn, case_id=target.case_id, filename=filename, payload=review_payload, now=now)
                updated.append(
                    {
                        "case_id": target.case_id,
                        "filename": filename,
                        "manual_phase": phase,
                        "manual_view": view,
                    }
                )
            except HTTPException as e:
                skipped.append({"case_id": target.case_id, "filename": target.filename, "reason": str(e.detail)})
        audit.record_after(
            conn,
            case_ids,
            befores,
            op="image_workbench_confirm_suggestions",
            source_route="/api/image-workbench/confirm-suggestions",
        )
    return {
        "updated": len(updated),
        "items": updated,
        "skipped": skipped,
        "min_confidence": payload.min_confidence,
    }


@router.post("/api/image-workbench/transfer")
def transfer_image_workbench_images(payload: ImageTransferPayload) -> dict[str, Any]:
    if payload.mode != "copy":
        raise HTTPException(400, "only copy mode is supported")
    copied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    now = _now_iso()
    reviewer = (payload.reviewer or "operator").strip() or "operator"
    source_case_ids = sorted({item.case_id for item in payload.items})
    audit_case_ids = sorted({*source_case_ids, payload.target_case_id})
    inheritance: list[dict[str, Any]] = []
    with db.connect() as conn:
        target_dir = _case_dir(conn, payload.target_case_id)
        befores = audit.snapshot_before(conn, audit_case_ids)
        for item in payload.items:
            try:
                if item.case_id == payload.target_case_id:
                    raise HTTPException(400, "source and target case are the same")
                source_dir = _case_dir(conn, item.case_id)
                source_filename = _validate_filename(source_dir, item.filename)
                source_path = (source_dir / source_filename).resolve()
                target_filename = _target_copy_name(target_dir, source_path, item.case_id)
                target_path = (target_dir / target_filename).resolve()
                try:
                    target_path.relative_to(target_dir)
                except ValueError:
                    raise HTTPException(400, "invalid target image path")
                shutil.copy2(source_path, target_path)
                phase, view = _current_override(conn, item.case_id, source_filename)
                inheritance.append(
                    {
                        "source_case_id": item.case_id,
                        "source_filename": source_filename,
                        "target_filename": target_filename,
                        "manual_phase": phase,
                        "manual_view": view,
                    }
                )
                copied.append(
                    {
                        "source_case_id": item.case_id,
                        "source_filename": source_filename,
                        "target_case_id": payload.target_case_id,
                        "target_filename": target_filename,
                    }
                )
            except HTTPException as e:
                skipped.append({"case_id": item.case_id, "filename": item.filename, "reason": str(e.detail)})
            except OSError as e:
                skipped.append({"case_id": item.case_id, "filename": item.filename, "reason": str(e)})
        if copied:
            try:
                scanner.rescan_one(conn, payload.target_case_id)
            except ValueError as e:
                raise HTTPException(400, str(e))
            for entry in inheritance:
                if payload.inherit_manual:
                    if entry["manual_phase"] is not None or entry["manual_view"] is not None:
                        _write_image_override(
                            conn,
                            case_id=payload.target_case_id,
                            filename=entry["target_filename"],
                            manual_phase=entry["manual_phase"],
                            manual_view=entry["manual_view"],
                            updated_at=now,
                        )
                if payload.inherit_review:
                    _copy_review_state(
                        conn,
                        source_case_id=entry["source_case_id"],
                        source_filename=entry["source_filename"],
                        target_case_id=payload.target_case_id,
                        target_filename=entry["target_filename"],
                        reviewer=reviewer,
                        note=payload.note.strip() if payload.note else None,
                        now=now,
                        require_target_review=payload.require_target_review,
                    )
        audit.record_after(
            conn,
            audit_case_ids,
            befores,
            op="image_workbench_transfer",
            source_route="/api/image-workbench/transfer",
            actor=reviewer,
        )
    return {
        "mode": "copy",
        "target_case_id": payload.target_case_id,
        "copied": len(copied),
        "items": copied,
        "skipped": skipped,
    }
