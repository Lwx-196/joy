"""Render artifact quality evaluation and persistence."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from backend.render_pixel_metrics import compute_pixel_metrics


QUALITY_EVALUATION_VERSION = 2026063001


_TRANSIENT_ERROR_PATTERN = re.compile(
    r"(?:"
    # Status-code anchors: HTTP 403, HTTP 429, "403 forbidden", "429 too many", etc.
    r"\bHTTP[\s_-]*(?:403|429)\b"
    r"|\b(?:403|429)\s+(?:forbidden|too[\s_-]*many|rate|quota|client[\s_-]*error)"
    # Explicit quota / rate-limit phrasing
    r"|quota[\s_-]*(?:exceed|exhaust|limit)"
    r"|rate[\s_-]*limit"
    r"|api[\s_-]*error"
    # Bare "forbidden" only when not embedded in unrelated text
    r"|^\s*forbidden\b"
    r")",
    re.IGNORECASE,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _split_transient_errors(messages: list[str]) -> tuple[list[str], list[str]]:
    """Partition messages into (kept, transient) using `_TRANSIENT_ERROR_PATTERN`.

    Transient = upstream API hiccups (HTTP 403/429, quota, rate limit) that say
    nothing about the render artifact's quality and should not surface as
    user-facing warnings.
    """
    kept: list[str] = []
    transient: list[str] = []
    for msg in messages:
        if msg and _TRANSIENT_ERROR_PATTERN.search(msg):
            transient.append(msg)
        else:
            kept.append(msg)
    return kept, transient


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any((r["name"] if isinstance(r, sqlite3.Row) else r[1]) == column for r in rows)


def _json_load(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _quality_evaluation_version(metrics: dict[str, Any] | None) -> int:
    if not isinstance(metrics, dict):
        return 0
    raw = metrics.get("quality_evaluation_version")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _composition_alerts(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    alerts: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        code = str(item.get("code") or "").strip()
        if not message and not code:
            continue
        alerts.append(
            {
                "slot": str(item.get("slot") or ""),
                "slot_label": str(item.get("slot_label") or item.get("slot") or ""),
                "code": code,
                "severity": str(item.get("severity") or "warning"),
                "message": message,
                "recommended_action": str(item.get("recommended_action") or "manual_review"),
                "metrics": item.get("metrics") if isinstance(item.get("metrics"), dict) else {},
            }
        )
    return alerts


IMAGE_REF_RE = re.compile(r"\.(?:jpg|jpeg|png|heic|webp|bmp)\b", re.IGNORECASE)

_TEMPLATE_TIER_BY_NAME = {
    "tri": "tri",
    "tri-compare": "tri",
    "composite": "tri",
    "before-after-pair": "tri",
    "bi": "bi",
    "bi-compare": "bi",
    "single": "single",
    "single-compare": "single",
}

_TEMPLATE_POLICIES = {
    "tri": {
        "label": "tri_preferred",
        "min_score": 80.0,
        "max_cv_penalty": 15.0,
    },
    "bi": {
        "label": "bi_publishable",
        "min_score": 80.0,
        "max_cv_penalty": 15.0,
    },
    "single": {
        "label": "single_publishable_strict",
        "min_score": 85.0,
        "max_cv_penalty": 8.0,
    },
}


def _selected_files_from_manifest(manifest_path: str | None) -> set[str]:
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
    selected: set[str] = set()
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        slots = group.get("selected_slots")
        if not isinstance(slots, dict):
            continue
        for slot in slots.values():
            if not isinstance(slot, dict):
                continue
            for role in ("before", "after"):
                item = slot.get(role)
                if not isinstance(item, dict):
                    continue
                for key in ("name", "relative_path", "group_relative_path"):
                    value = item.get(key)
                    if value:
                        text = str(value)
                        selected.add(text)
                        selected.add(Path(text).name)
    return selected


def _manifest_dict(manifest_path: str | None) -> dict[str, Any]:
    if not manifest_path:
        return {}
    path = Path(manifest_path)
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _resolve_manifest_path(result: dict[str, Any]) -> str | None:
    manifest_path = result.get("manifest_path")
    if manifest_path and Path(str(manifest_path)).is_file():
        return str(manifest_path)
    output_path = result.get("output_path")
    if output_path:
        inferred = Path(str(output_path)).parent / "manifest.final.json"
        if inferred.is_file():
            return str(inferred)
    return str(manifest_path) if manifest_path else None


def _selected_pair_count_from_manifest(manifest_path: str | None) -> int:
    manifest = _manifest_dict(manifest_path)
    count = 0
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        slots = group.get("selected_slots")
        if not isinstance(slots, dict):
            continue
        for slot in slots.values():
            if not isinstance(slot, dict):
                continue
            if isinstance(slot.get("before"), dict) and isinstance(slot.get("after"), dict):
                count += 1
    return count


def _template_tier(result: dict[str, Any]) -> tuple[str, str]:
    candidates: list[str] = []
    effective_templates = result.get("effective_templates")
    if isinstance(effective_templates, list):
        candidates.extend(str(item) for item in effective_templates if item)
    elif isinstance(effective_templates, dict):
        candidates.extend(str(item) for item in effective_templates.values() if item)
    for key in ("effective_template", "template", "requested_template"):
        value = result.get(key)
        if value:
            candidates.append(str(value))
    for candidate in candidates:
        normalized = _TEMPLATE_TIER_BY_NAME.get(candidate.strip())
        if normalized:
            return normalized, candidate
    return "tri", ""


def _source_profile_from_result(result: dict[str, Any], ai_usage: dict[str, Any]) -> dict[str, Any]:
    profile = ai_usage.get("source_profile")
    if isinstance(profile, dict):
        return profile
    profile = result.get("source_profile")
    return profile if isinstance(profile, dict) else {}


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _has_single_explanation(result: dict[str, Any]) -> bool:
    for key in ("explanatory_copy", "copy_text", "caption", "board_title", "title", "project"):
        if str(result.get(key) or "").strip():
            return True
    manifest = _manifest_dict(result.get("manifest_path"))
    for key in ("title", "board_title", "project", "treatment"):
        if str(manifest.get(key) or "").strip():
            return True
    meta = manifest.get("meta")
    if isinstance(meta, dict):
        for key in ("title", "project", "treatment"):
            if str(meta.get(key) or "").strip():
                return True
    title_lines = manifest.get("title_lines")
    return isinstance(title_lines, list) and any(str(item or "").strip() for item in title_lines)


_STAGING_TITLE_RE = re.compile(r"(?:\.case-workbench-bound-render|\bjob-\d+\b)", re.IGNORECASE)
_TITLE_STAGE_TOKEN_RE = re.compile(r"(?:术前|术后|治疗前|治疗后|\b(?:before|after|pre|post)\b)", re.IGNORECASE)
_TITLE_DATE_RE = re.compile(r"(?:19|20)\d{2}[./-]\d{1,2}[./-]\d{1,2}")
SIDE_SOURCE_HEIGHT_RATIO_MAX = 1.22
SIDE_SOURCE_AREA_RATIO_MAX = 1.28


def _title_payload_candidates(payload: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in ("board_title", "title"):
        value = str(payload.get(key) or "").strip()
        if value:
            candidates.append(value)
    title_lines = payload.get("title_lines")
    if isinstance(title_lines, list):
        joined = " ".join(str(item or "").strip() for item in title_lines if str(item or "").strip())
        if joined:
            candidates.append(joined)
    return candidates


def _project_title_candidates(payload: dict[str, Any], *, include_treatment: bool = False) -> list[str]:
    candidates: list[str] = []
    keys = ("project", "case_project", "treatment") if include_treatment else ("project", "case_project")
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            candidates.append(value)
    for key in ("board_title_context", "title_context"):
        context = payload.get(key)
        if not isinstance(context, dict):
            continue
        value = str(context.get("project") or context.get("case_project") or context.get("treatment") or "").strip()
        if value:
            candidates.append(value)
    for title in _title_payload_candidates(payload):
        match = _TITLE_DATE_RE.search(title)
        if match:
            project = title[match.end() :].strip(" \t._-—－/，,、:：")
            if project:
                candidates.append(project)
    return candidates


def _title_integrity_policy(result: dict[str, Any], ai_usage: dict[str, Any]) -> dict[str, Any]:
    candidates = _title_payload_candidates(result) + _title_payload_candidates(ai_usage)
    project_candidates = _project_title_candidates(result) + _project_title_candidates(ai_usage)
    evidence_payload = ai_usage.get("enhancement_evidence")
    if not isinstance(evidence_payload, dict):
        evidence_payload = result.get("enhancement_evidence") if isinstance(result.get("enhancement_evidence"), dict) else {}
    candidates.extend(_title_payload_candidates(evidence_payload))
    project_candidates.extend(_project_title_candidates(evidence_payload))
    manifest = _manifest_dict(result.get("manifest_path"))
    candidates.extend(_title_payload_candidates(manifest))
    project_candidates.extend(_project_title_candidates(manifest))
    meta = manifest.get("meta")
    if isinstance(meta, dict):
        project_candidates.extend(_project_title_candidates(meta))

    board_title = next((item for item in candidates if item), "")
    bad_title = next((item for item in candidates if _STAGING_TITLE_RE.search(item)), "")
    title_blocker = f"正式板标题包含工作台 staging/job 标识：{bad_title}" if bad_title else ""
    bad_project = next((item for item in project_candidates if _TITLE_STAGE_TOKEN_RE.search(item)), "")
    if not title_blocker and bad_project:
        title_blocker = f"正式标题项目含阶段词，需清理项目标题后重出：{bad_project}"
    command_payload = ai_usage.get("ai_enhance_command")
    if not isinstance(command_payload, dict):
        command_payload = evidence_payload.get("ai_enhance_command")
    if not title_blocker and isinstance(command_payload, dict):
        project_candidates.extend(_project_title_candidates(command_payload))
        bad_project = next((item for item in project_candidates if _TITLE_STAGE_TOKEN_RE.search(item)), "")
        if bad_project:
            title_blocker = f"正式标题项目含阶段词，需清理项目标题后重出：{bad_project}"
        args = command_payload.get("args")
        args_text = " ".join(str(item) for item in args) if isinstance(args, list) else ""
        title_context = command_payload.get("title_context")
        has_title_override = "--customer-name" in args_text or (
            isinstance(title_context, dict) and str(title_context.get("customer_name") or "").strip()
        )
        if not board_title and _STAGING_TITLE_RE.search(args_text) and not has_title_override:
            title_blocker = "正式板标题缺少真实标题证据，且 AI 子进程使用 staging/job 目录"
    return {
        "board_title": board_title,
        "title_integrity": "blocked" if title_blocker else "ok",
        "title_policy_blocker": title_blocker,
    }


def _single_info_policy(result: dict[str, Any], ai_usage: dict[str, Any]) -> dict[str, Any]:
    source_profile = _source_profile_from_result(result, ai_usage)
    before_count = _int_value(source_profile.get("before_count") or ai_usage.get("before_count"))
    after_count = _int_value(source_profile.get("after_count") or ai_usage.get("after_count"))
    selected_pair_count = _int_value(
        ai_usage.get("render_selection_slot_count")
        or result.get("render_selection_slot_count")
        or _selected_pair_count_from_manifest(result.get("manifest_path"))
    )
    info_complete = selected_pair_count >= 1 or (before_count > 0 and after_count > 0)
    explanation_present = _has_single_explanation(result)
    return {
        "single_info_complete": info_complete,
        "single_explanation_present": explanation_present,
        "single_before_count": before_count,
        "single_after_count": after_count,
        "single_selected_pair_count": selected_pair_count,
    }


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _external_call_values(data: dict[str, Any]) -> list[str]:
    keys = (
        "external_call_id",
        "external_call_ids",
        "external_request_id",
        "external_request_ids",
        "relay_call_id",
        "relay_call_ids",
        "md_ai_call_id",
        "md_ai_call_ids",
        "provider_call_id",
        "provider_call_ids",
        "request_id",
        "request_ids",
        "call_id",
        "call_ids",
    )
    values: list[str] = []
    for key in keys:
        raw = data.get(key)
        if isinstance(raw, list):
            values.extend(str(item).strip() for item in raw if str(item).strip())
        elif str(raw or "").strip():
            values.append(str(raw).strip())
    return values


def _ai_evidence_policy(result: dict[str, Any], ai_usage: dict[str, Any]) -> dict[str, Any]:
    enhance = result.get("enhance") if isinstance(result.get("enhance"), dict) else {}
    evidence_payload = ai_usage.get("enhancement_evidence")
    if not isinstance(evidence_payload, dict):
        evidence_payload = result.get("enhancement_evidence") if isinstance(result.get("enhancement_evidence"), dict) else {}
    formal_required = (
        _truthy(result.get("require_formal_ai_enhancement"))
        or _truthy(ai_usage.get("require_formal_ai_enhancement"))
        or str(result.get("case_mode") or "") == "ai_enhanced_board"
        or bool(enhance)
        or _truthy(ai_usage.get("formal_ai_enhancement_run"))
    )
    fresh_required = (
        _truthy(result.get("require_fresh_ai_enhancement"))
        or _truthy(ai_usage.get("require_fresh_ai_enhancement"))
        or _truthy(ai_usage.get("require_external_ai_call"))
    )
    generated_count = _int_value(
        ai_usage.get("enhanced_artifact_count")
        or evidence_payload.get("generated_count")
        or evidence_payload.get("enhanced_artifact_count")
        or (ai_usage.get("generated_artifact_count") if formal_required else 0)
    )
    external_count = _int_value(ai_usage.get("external_call_count") or evidence_payload.get("external_call_count"))
    cache_hit_count = _int_value(ai_usage.get("cache_hit_count") or evidence_payload.get("cache_hit_count"))
    evidence: list[str] = []
    fresh_evidence: list[str] = []
    if _truthy(ai_usage.get("used_after_enhancement")):
        evidence.append("ai_usage.used_after_enhancement")
    if generated_count > 0:
        evidence.append("generated_artifact_count")
    external_values = _external_call_values(ai_usage) + _external_call_values(evidence_payload)
    if external_values:
        evidence.append("external_call_id")
        fresh_evidence.append("external_call_id")
    if external_count > 0:
        evidence.append("external_call_count")
        fresh_evidence.append("external_call_count")
    provider_counts = evidence_payload.get("provider_counts")
    if isinstance(provider_counts, dict):
        for provider, count in provider_counts.items():
            if str(provider).strip().lower() != "cache" and _int_value(count) > 0:
                fresh_evidence.append(f"provider:{provider}")
    verified = bool(evidence)
    fresh_verified = bool(fresh_evidence)
    if not formal_required:
        gate = "not_required"
    elif verified and (not fresh_required or fresh_verified):
        gate = "verified"
    elif verified and fresh_required and not fresh_verified:
        gate = "missing_external_call_evidence"
    else:
        gate = "missing_evidence"
    return {
        "formal_ai_enhancement_required": formal_required,
        "formal_ai_enhancement_verified": verified,
        "formal_ai_enhancement_evidence": sorted(set(evidence)),
        "formal_ai_enhancement_gate": gate,
        "fresh_ai_enhancement_required": fresh_required,
        "fresh_ai_enhancement_verified": fresh_verified,
        "fresh_ai_enhancement_evidence": sorted(set(fresh_evidence)),
        "ai_enhanced_artifact_count": generated_count,
        "ai_external_call_count": external_count,
        "ai_cache_hit_count": cache_hit_count,
        "ai_enhancement_evidence_payload": evidence_payload,
    }


def _warning_mentions_selected_file(text: str, selected_files: set[str]) -> bool:
    if not selected_files:
        return True
    return any(name and name in text for name in selected_files)


def _warning_buckets(warnings: list[str], selected_files: set[str] | None = None) -> dict[str, int]:
    buckets = {
        "candidate_noise": 0,
        "profile_expected": 0,
        "profile_quality": 0,
        "pose_delta": 0,
        "pose_candidates": 0,
        "other": 0,
    }
    for warning in warnings:
        text = str(warning)
        if selected_files and IMAGE_REF_RE.search(text) and not _warning_mentions_selected_file(text, selected_files):
            buckets["candidate_noise"] += 1
        elif "正脸检测失败，已使用侧脸检测兜底" in text:
            buckets["profile_expected"] += 1
        elif "面部检测失败" in text or "正脸检测失败" in text:
            buckets["profile_quality"] += 1
        elif "姿态差过大" in text:
            buckets["pose_delta"] += 1
        elif "姿态推断候选" in text:
            buckets["pose_candidates"] += 1
        else:
            buckets["other"] += 1
    buckets["noise_count"] = buckets["candidate_noise"] + buckets["profile_expected"] + buckets["pose_candidates"]
    buckets["actionable_count"] = sum(
        v
        for k, v in buckets.items()
        if k not in {"candidate_noise", "profile_expected", "pose_candidates", "noise_count", "actionable_count"}
    )
    return buckets


def _warning_buckets_from_layers(layers: dict[str, Any]) -> dict[str, int]:
    selected_actionable = layers.get("selected_actionable") if isinstance(layers.get("selected_actionable"), list) else []
    selected_expected = layers.get("selected_expected_profile") if isinstance(layers.get("selected_expected_profile"), list) else []
    candidate_noise = layers.get("candidate_noise") if isinstance(layers.get("candidate_noise"), list) else []
    stale_pose_noise = layers.get("stale_pose_noise") if isinstance(layers.get("stale_pose_noise"), list) else []
    dropped_slot_noise = layers.get("dropped_slot_noise") if isinstance(layers.get("dropped_slot_noise"), list) else []
    pose_delta = sum(1 for item in selected_actionable if "姿态差过大" in str(item))
    profile_quality = sum(1 for item in selected_actionable if "面部检测失败" in str(item) or "正脸检测失败" in str(item))
    other = max(0, len(selected_actionable) - pose_delta - profile_quality)
    return {
        "candidate_noise": len(candidate_noise),
        "profile_expected": len(selected_expected),
        "profile_quality": profile_quality,
        "pose_delta": pose_delta,
        "pose_candidates": 0,
        "stale_pose_noise": len(stale_pose_noise),
        "dropped_slot_noise": len(dropped_slot_noise),
        "other": other,
        "noise_count": len(candidate_noise) + len(selected_expected),
        "audit_noise_count": len(candidate_noise) + len(selected_expected) + len(stale_pose_noise) + len(dropped_slot_noise),
        "actionable_count": len(selected_actionable),
    }


def _display_warnings_from_layers(layers: dict[str, Any] | None, raw_warnings: list[str]) -> list[str]:
    if not isinstance(layers, dict):
        return raw_warnings
    selected_actionable = layers.get("selected_actionable")
    if not isinstance(selected_actionable, list):
        return raw_warnings
    return [str(item) for item in selected_actionable if str(item).strip()]


def _action_suggestions(blocking_issues: list[str], display_warnings: list[str], composition_alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []

    def add(code: str, label: str, source: str) -> None:
        if any(item["code"] == code for item in suggestions):
            return
        suggestions.append({"code": code, "label": label, "source": source})

    for text in [*blocking_issues, *display_warnings]:
        value = str(text)
        if "未闭环" in value or "待补充" in value or "低置信" in value:
            add("open_classification_workbench", "回到照片分类工作台补齐阶段、角度和可用性", "classification")
        elif "缺槽位" in value or "三联" in value or "配齐" in value:
            add("complete_source_group_slots", "补齐正面、45°、侧面术前术后槽位", "source_group")
        elif "姿态差" in value or "方向不一致" in value:
            add("reselect_pair", "回到源组候选重选姿态更接近的术前术后配对", "pair_quality")
        elif "面部检测" in value or "正脸检测" in value:
            add("review_face_detection", "复核面检/轮廓定位，必要时换片", "face_quality")
        elif "源照片" in value or "源文件" in value or "真实源图" in value:
            add("fix_source_directory", "恢复真实源图或处理源目录阻断", "source_directory")
    for alert in composition_alerts:
        if not isinstance(alert, dict):
            continue
        action = str(alert.get("recommended_action") or "").strip()
        add("review_composition", action or "复核构图，必要时重新选片或重出", "composition")
    if not suggestions and display_warnings:
        add("manual_quality_review", "人工复核 warning 后决定通过、复检或拒绝", "quality")
    return suggestions[:8]


def _warning_audit_from_layers(layers: dict[str, Any] | None, raw_warnings: list[str]) -> dict[str, Any]:
    if not isinstance(layers, dict):
        return {
            "raw_warning_count": len(raw_warnings),
            "raw_warnings": raw_warnings,
            "suppressed_layers": {},
            "suppressed_counts": {},
        }
    suppressed_layers: dict[str, list[str]] = {}
    for key in ("selected_expected_profile", "candidate_noise", "stale_pose_noise", "dropped_slot_noise"):
        items = layers.get(key)
        if isinstance(items, list):
            suppressed_layers[key] = [str(item) for item in items]
    return {
        "raw_warning_count": len(raw_warnings),
        "raw_warnings": raw_warnings,
        "suppressed_layers": suppressed_layers,
        "suppressed_counts": {key: len(value) for key, value in suppressed_layers.items()},
    }


def _crop_box_dimensions(item: dict[str, Any]) -> tuple[float, float] | None:
    crop = item.get("crop_box")
    if not isinstance(crop, dict):
        return None
    try:
        width = float(crop["x2"]) - float(crop["x1"])
        height = float(crop["y2"]) - float(crop["y1"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _image_dimensions(path_value: Any) -> tuple[float, float] | None:
    if not path_value:
        return None
    path = Path(str(path_value))
    if not path.is_file():
        return None
    try:
        with Image.open(path) as image:
            width, height = image.size
    except (OSError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return float(width), float(height)


def _selected_crop_metrics(item: dict[str, Any]) -> dict[str, float] | None:
    crop_size = _crop_box_dimensions(item)
    if not crop_size:
        return None
    crop_w, crop_h = crop_size
    image_size = _image_dimensions(item.get("path"))
    if image_size:
        image_w, image_h = image_size
        norm_h = crop_h / image_h
        norm_area = (crop_w * crop_h) / max(1.0, image_w * image_h)
    else:
        norm_h = crop_h
        norm_area = crop_w * crop_h
    return {
        "crop_w": round(crop_w, 3),
        "crop_h": round(crop_h, 3),
        "norm_h": round(norm_h, 6),
        "norm_area": round(norm_area, 6),
    }


def _ratio_pair(left: float, right: float) -> float:
    low = min(abs(left), abs(right))
    high = max(abs(left), abs(right))
    if low <= 0:
        return 1.0
    return high / low


def _source_scale_policy(result: dict[str, Any]) -> dict[str, Any]:
    manifest = _manifest_dict(result.get("manifest_path"))
    alerts: list[dict[str, Any]] = []
    evaluated = 0
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        slots = group.get("selected_slots")
        if not isinstance(slots, dict):
            continue
        for slot_name, slot in slots.items():
            if str(slot_name) not in {"side", "profile"} or not isinstance(slot, dict):
                continue
            before = slot.get("before")
            after = slot.get("after")
            if not isinstance(before, dict) or not isinstance(after, dict):
                continue
            before_metrics = _selected_crop_metrics(before)
            after_metrics = _selected_crop_metrics(after)
            if not before_metrics or not after_metrics:
                continue
            evaluated += 1
            height_ratio = _ratio_pair(before_metrics["norm_h"], after_metrics["norm_h"])
            area_ratio = _ratio_pair(before_metrics["norm_area"], after_metrics["norm_area"])
            if height_ratio >= SIDE_SOURCE_HEIGHT_RATIO_MAX and area_ratio >= SIDE_SOURCE_AREA_RATIO_MAX:
                alerts.append({
                    "slot": str(slot_name),
                    "slot_label": str(slot.get("label") or "侧面"),
                    "code": "side_source_scale_mismatch",
                    "message": (
                        f"侧面对比人物尺度不一致：面框高比 {height_ratio:.2f}、面积比 {area_ratio:.2f}"
                    ),
                    "metrics": {
                        "height_ratio": round(height_ratio, 3),
                        "area_ratio": round(area_ratio, 3),
                        "before": before_metrics,
                        "after": after_metrics,
                        "thresholds": {
                            "height_ratio_max": SIDE_SOURCE_HEIGHT_RATIO_MAX,
                            "area_ratio_max": SIDE_SOURCE_AREA_RATIO_MAX,
                        },
                    },
                })
    return {
        "status": "blocked" if alerts else "ok",
        "evaluated_pair_count": evaluated,
        "alerts": alerts,
        "fail_open": evaluated == 0 and not alerts,
    }


def evaluate_render_result(result: dict[str, Any]) -> dict[str, Any]:
    """Map a skill render result into a workbench quality envelope.

    The render subprocess can write a `final-board.jpg` even when the manifest
    status is `error`. That artifact is useful for review, but it must not be
    treated as a clean `done` result.
    """
    manifest_status = str(result.get("status") or "")
    # The standard renderer reports a clean manifest as "ok"; the AI-board CLI
    # reports a successful board result as "done". Treat both as clean render
    # outcomes while preserving the raw status in persisted audit fields.
    manifest_ok = manifest_status in {"ok", "done"}
    blocking = int(result.get("blocking_issue_count") or 0)
    warnings = int(result.get("warning_count") or 0)
    output_path = result.get("output_path")
    resolved_manifest_path = _resolve_manifest_path(result)
    if resolved_manifest_path and resolved_manifest_path != result.get("manifest_path"):
        result = {**result, "manifest_path": resolved_manifest_path}
    output_exists = bool(output_path and Path(str(output_path)).is_file())
    pixel_metrics = compute_pixel_metrics(str(output_path)) if output_exists else {"available": False, "flags": [], "cv_penalty": 0.0}
    ai_usage = result.get("ai_usage") or {}
    if not isinstance(ai_usage, dict):
        ai_usage = {}
    used_ai_enhancement = bool(ai_usage.get("used_after_enhancement"))
    used_ai_padfill = bool(ai_usage.get("used_ai_padfill"))
    template_tier, template_source = _template_tier(result)
    template_policy = _TEMPLATE_POLICIES[template_tier]
    single_policy = _single_info_policy(result, ai_usage) if template_tier == "single" else {
        "single_info_complete": None,
        "single_explanation_present": None,
        "single_before_count": 0,
        "single_after_count": 0,
        "single_selected_pair_count": 0,
    }
    ai_policy = _ai_evidence_policy(result, ai_usage)
    title_policy = _title_integrity_policy(result, ai_usage)
    render_error = str(result.get("render_error") or "").strip()
    # WP2（aligned-render-pipeline）：AI 增强板的 G1/G2 质量门 HELD 信号。
    held_gate = str(result.get("held_gate") or "").strip()
    held_reason = str(result.get("held_reason") or "").strip()
    # F2：cache-miss 待用户确认烧 API（执行器返回 status='needs_confirmation' + 缺槽/预估）。
    needs_confirmation = str(result.get("status") or "") == "needs_confirmation"
    cache_miss_count = int(result.get("cache_miss_count") or 0)
    cache_miss_total = int(result.get("cache_miss_total") or 0)
    cache_miss_est_cost_usd = result.get("cache_miss_est_cost_usd")
    cache_miss_est_seconds = result.get("cache_miss_est_seconds")
    blocking_issues = [str(item) for item in (result.get("blocking_issues") or [])]
    raw_warning_items = [str(item) for item in (result.get("warnings") or [])]
    # Transient API errors (HTTP 403/429, quota, rate limit) are upstream
    # hiccups, not artifact-quality signals — pull them out before any
    # bucketing / display layer / score calculation sees them.
    warning_items, system_transient_errors = _split_transient_errors(raw_warning_items)
    composition_alerts = _composition_alerts(result.get("composition_alerts"))
    selected_files = _selected_files_from_manifest(result.get("manifest_path"))

    warning_layers = result.get("warning_layers") if isinstance(result.get("warning_layers"), dict) else None
    warning_buckets = _warning_buckets_from_layers(warning_layers) if warning_layers else _warning_buckets(warning_items, selected_files)
    display_warnings = _display_warnings_from_layers(warning_layers, warning_items)
    warning_audit = result.get("warning_audit") if isinstance(result.get("warning_audit"), dict) else _warning_audit_from_layers(warning_layers, warning_items)
    actionable_warnings = int(warning_buckets.get("actionable_count") or 0)
    noisy_warnings = int(warning_buckets.get("noise_count") or 0)
    source_scale_policy = _source_scale_policy(result)
    action_suggestions = _action_suggestions(blocking_issues, display_warnings, composition_alerts)

    score = 100.0
    if not manifest_ok:
        score -= 35
    score -= min(blocking * 25, 60)
    score -= min(actionable_warnings * 3, 30)
    score -= min(noisy_warnings * 0.5, 4)
    score -= min(int(warning_buckets.get("pose_delta") or 0) * 5, 15)
    score -= min(int(warning_buckets.get("profile_quality") or 0) * 2, 18)
    score -= min(len(composition_alerts) * 8, 24)
    score -= max(0.0, float(pixel_metrics.get("cv_penalty") or 0.0))
    score = max(0.0, round(score, 1))
    if held_gate:
        score = min(score, 60.0)

    policy_blockers: list[str] = []
    cv_penalty = float(pixel_metrics.get("cv_penalty") or 0.0)
    cv_flags = set(pixel_metrics.get("flags") if isinstance(pixel_metrics.get("flags"), list) else [])
    artifact_policy_applicable = output_exists and not needs_confirmation and not held_gate
    cv_penalty_blocked = False
    cutout_blank_blocked = False
    if artifact_policy_applicable:
        if {"cutout_artifact", "blank_region"}.issubset(cv_flags):
            cutout_blank_blocked = True
            policy_blockers.append("抠图白边/空白背景质量未达发布标准（cutout_artifact + blank_region）")
        if "postop_cyan_cast" in cv_flags:
            policy_blockers.append("术后肤色偏青/偏冷，需重新调色或重出后复核")
            score = min(score, 70.0)
        if "side_scale_mismatch" in cv_flags:
            side_scale = pixel_metrics.get("side_scale_mismatch") if isinstance(pixel_metrics, dict) else None
            ratios = side_scale.get("ratios") if isinstance(side_scale, dict) else None
            if isinstance(ratios, dict):
                policy_blockers.append(
                    "侧面对比人物尺度不一致，需重新对齐或重选源图"
                    f"（肤区高比 {float(ratios.get('skin_height') or 0):.2f}、"
                    f"面积比 {float(ratios.get('skin_area') or 0):.2f}）"
                )
            else:
                policy_blockers.append("侧面对比人物尺度不一致，需重新对齐或重选源图")
            score = min(score, 70.0)
        if cv_penalty >= float(template_policy["max_cv_penalty"]):
            cv_penalty_blocked = True
            policy_blockers.append(
                f"{template_tier} 模板 CV penalty {cv_penalty:.1f} 达到或超过 {template_policy['max_cv_penalty']:.1f}"
            )
        for alert in source_scale_policy.get("alerts") or []:
            if isinstance(alert, dict):
                message = str(alert.get("message") or "侧面对比人物尺度不一致，需重选源图")
                policy_blockers.append(message)
                score = min(score, 70.0)
        if template_tier == "single":
            if not single_policy["single_info_complete"]:
                policy_blockers.append("single-compare 缺少可证明的术前/术后完整信息")
            if not single_policy["single_explanation_present"]:
                policy_blockers.append("single-compare 缺少说明文案/项目标题")
        if ai_policy["formal_ai_enhancement_required"] and not ai_policy["formal_ai_enhancement_verified"]:
            policy_blockers.append("正式 AI 增强成品缺少真实增强产物证据")
        if ai_policy["fresh_ai_enhancement_required"] and not ai_policy["fresh_ai_enhancement_verified"]:
            policy_blockers.append("本次重新深图缺少外部模型调用证据")
        if title_policy["title_policy_blocker"]:
            policy_blockers.append(title_policy["title_policy_blocker"])
            score = min(score, 60.0)
    effective_blocking = blocking + len(policy_blockers)

    if needs_confirmation:
        # F2：cache-miss 待用户确认烧钱 = 独立终态，≠ blocked/failed/done。前端弹确认卡，
        # 用户确认后以 allow_burn=True 重入即真烧。无诊断板（output_path=None）。
        quality_status = "needs_confirmation"
    elif held_gate:
        # WP2：质量门 HELD = 质量保留态，即使保留了诊断板（output 存在）也判 blocked，
        # 区别于 done_with_issues（出板可复核）与 failed（真渲染异常）。
        quality_status = "blocked"
    elif not output_exists:
        quality_status = "blocked"
    elif policy_blockers:
        quality_status = "blocked"
    elif (
        manifest_ok
        and blocking == 0
        and score >= float(template_policy["min_score"])
        and not composition_alerts
        and actionable_warnings == 0
    ):
        quality_status = "done"
    else:
        quality_status = "done_with_issues"

    can_publish = quality_status == "done"
    if cutout_blank_blocked or cv_penalty_blocked:
        edge_integrity = "blocked"
    elif cv_flags.intersection({"cutout_artifact", "blank_region"}) or cv_penalty > 0 or actionable_warnings >= 3:
        edge_integrity = "review"
    else:
        edge_integrity = "ok"
    color_integrity = "blocked" if "postop_cyan_cast" in cv_flags else "ok"
    artifact_mode = "ai_after_simulation" if used_ai_enhancement else "ai_edge_padfill" if used_ai_padfill else "real_layout"
    return {
        "quality_status": quality_status,
        "quality_score": score,
        "can_publish": can_publish,
        "artifact_mode": artifact_mode,
        "manifest_status": manifest_status,
        "blocking_count": effective_blocking,
        "warning_count": warnings,
        "metrics": {
            "quality_evaluation_version": QUALITY_EVALUATION_VERSION,
            "phase_integrity": "blocked" if blocking else "ok",
            "angle_match": "review" if actionable_warnings or composition_alerts or source_scale_policy.get("alerts") or "side_scale_mismatch" in cv_flags else "ok",
            "source_scale_policy": source_scale_policy,
            "background_fill": "ai" if used_ai_padfill else "standard",
            "edge_integrity": edge_integrity,
            "color_integrity": color_integrity,
            "ai_after_enhancement": used_ai_enhancement,
            "ai_edge_padfill": used_ai_padfill,
            "render_error": render_error,
            "held_gate": held_gate,
            "held_reason": held_reason,
            "cache_miss_count": cache_miss_count,
            "cache_miss_total": cache_miss_total,
            "cache_miss_est_cost_usd": cache_miss_est_cost_usd,
            "cache_miss_est_seconds": cache_miss_est_seconds,
            "blocking_issues": [*blocking_issues, *policy_blockers],
            "policy_blockers": policy_blockers,
            "warnings": display_warnings,
            "display_warnings": display_warnings,
            "audit_warnings": warning_items,
            "warning_audit": warning_audit,
            "warning_buckets": warning_buckets,
            "warning_layers": warning_layers or {},
            "warning_display_layers": result.get("warning_display_layers") if isinstance(result.get("warning_display_layers"), dict) else {"selected_actionable": display_warnings},
            "system_transient_errors": system_transient_errors,
            "actionable_warning_count": actionable_warnings,
            "noise_warning_count": noisy_warnings,
            "audit_warning_count": len(warning_items),
            "selected_file_count": len(selected_files),
            "composition_alerts": composition_alerts,
            "composition": "review" if composition_alerts else "ok",
            "action_suggestions": action_suggestions,
            "pixel_metrics": pixel_metrics,
            "cv_flags": pixel_metrics.get("flags") if isinstance(pixel_metrics.get("flags"), list) else [],
            "quality_template_tier": template_tier,
            "quality_template_source": template_source,
            "template_quality_policy": template_policy,
            "artifact_policy_applicable": artifact_policy_applicable,
            "template_policy_blocker_count": len(policy_blockers),
            "effective_blocking_count": effective_blocking,
            **single_policy,
            **ai_policy,
            **title_policy,
        },
    }


def build_quality_summary(quality: dict[str, Any] | None) -> dict[str, Any]:
    """Compact quality projection embedded in ``render_jobs.meta_json`` and the
    job-detail API response.

    A ``quality_status == "blocked"`` must always travel with a visible cause.
    Policy blockers (single-compare 信息不全 / AI 增强证据缺失 / 抠图白边 /
    术后偏色 / 侧面尺度不一致 …) already drive ``blocking_count`` (== effective
    blocking) and are listed in ``metrics.blocking_issues``. This projection used
    to carry only ``actionable_warning_count``, so a policy-blocked board surfaced
    as "score 100 / 0 warning / 0 blocking / 却 blocked" —— an unactionable state.
    Always include ``blocking_count`` + ``blocking_issues`` so the block is
    self-explaining wherever the compact summary is consumed.
    """
    quality = quality if isinstance(quality, dict) else {}
    metrics = quality.get("metrics") if isinstance(quality.get("metrics"), dict) else {}
    blocking_issues = metrics.get("blocking_issues")
    return {
        "quality_status": quality.get("quality_status"),
        "quality_score": quality.get("quality_score"),
        "can_publish": bool(quality.get("can_publish")) if "can_publish" in quality else False,
        "actionable_warning_count": metrics.get("actionable_warning_count"),
        "blocking_count": quality.get("blocking_count"),
        "blocking_issues": list(blocking_issues) if isinstance(blocking_issues, list) else [],
    }


def persist_render_quality(conn: sqlite3.Connection, job_id: int, quality: dict[str, Any]) -> None:
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO render_quality
          (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
           manifest_status, blocking_count, warning_count, metrics_json,
           created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(render_job_id) DO UPDATE SET
          quality_status = excluded.quality_status,
          quality_score = excluded.quality_score,
          can_publish = excluded.can_publish,
          artifact_mode = excluded.artifact_mode,
          manifest_status = excluded.manifest_status,
          blocking_count = excluded.blocking_count,
          warning_count = excluded.warning_count,
          metrics_json = excluded.metrics_json,
          updated_at = excluded.updated_at
        """,
        (
            job_id,
            quality["quality_status"],
            quality["quality_score"],
            1 if quality["can_publish"] else 0,
            quality["artifact_mode"],
            quality["manifest_status"],
            quality["blocking_count"],
            quality["warning_count"],
            json.dumps(quality["metrics"], ensure_ascii=False),
            now,
            now,
        ),
    )
    # Optional column written only when the schema has been migrated.
    transient = quality.get("metrics", {}).get("system_transient_errors") or []
    if transient and _column_exists(conn, "render_quality", "system_transient_errors_json"):
        conn.execute(
            "UPDATE render_quality SET system_transient_errors_json = ? WHERE render_job_id = ?",
            (json.dumps(transient, ensure_ascii=False), job_id),
        )


def quality_row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    metrics = _json_load(row["metrics_json"], {})
    try:
        transient_raw = row["system_transient_errors_json"]
    except (IndexError, KeyError):
        transient_raw = None
    transient = _json_load(transient_raw, []) if transient_raw else metrics.get("system_transient_errors") or []
    return {
        "id": row["id"],
        "render_job_id": row["render_job_id"],
        "quality_status": row["quality_status"],
        "quality_score": row["quality_score"],
        "can_publish": bool(row["can_publish"]),
        "artifact_mode": row["artifact_mode"],
        "manifest_status": row["manifest_status"],
        "blocking_count": row["blocking_count"],
        "warning_count": row["warning_count"],
        "metrics": metrics,
        "system_transient_errors": transient,
        "review_verdict": row["review_verdict"],
        "reviewer": row["reviewer"],
        "review_note": row["review_note"],
        "reviewed_at": row["reviewed_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def backfill_existing_render_quality(conn: sqlite3.Connection) -> int:
    """Create or refresh quality rows for historical completed render jobs."""
    rows = conn.execute(
        """
        SELECT j.*, q.id AS quality_id, q.metrics_json AS quality_metrics_json
        FROM render_jobs j
        LEFT JOIN render_quality q ON q.render_job_id = j.id
        WHERE j.status IN ('done', 'done_with_issues', 'blocked')
           OR q.id IS NOT NULL
        """
    ).fetchall()
    count = 0
    for row in rows:
        quality_id = row["quality_id"]
        if quality_id is not None:
            existing_metrics = _json_load(row["quality_metrics_json"], {})
            if _quality_evaluation_version(existing_metrics) >= QUALITY_EVALUATION_VERSION:
                continue
        meta = _json_load(row["meta_json"], {})
        result = {
            "output_path": row["output_path"],
            "status": meta.get("status") or row["status"],
            "requested_template": row["template"],
            "template": row["template"],
            "effective_templates": meta.get("effective_templates") or [],
            "blocking_issue_count": meta.get("blocking_issue_count") or 0,
            "warning_count": meta.get("warning_count") or 0,
            "ai_usage": meta.get("ai_usage") or {},
            "composition_alerts": meta.get("composition_alerts") or [],
        }
        quality = evaluate_render_result(result)
        persist_render_quality(conn, row["id"], quality)
        if row["status"] == "done" and quality["quality_status"] != "done":
            conn.execute(
                "UPDATE render_jobs SET status = ? WHERE id = ?",
                (quality["quality_status"], row["id"]),
            )
        count += 1
    return count
