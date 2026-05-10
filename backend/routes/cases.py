"""Case endpoints: list, detail, files, rename-suggestion, manual edit."""
from __future__ import annotations

import base64
import binascii
import json
import shlex
import sqlite3
import shutil
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import _upgrade_executor, ai_generation_adapter, audit, db, issue_translator, render_executor, scanner, simulation_quality, skill_bridge, source_images, source_selection, stress
from ..models import (
    CaseBatchUpdate,
    CaseDetail,
    CaseListResponse,
    CaseRevealRequest,
    CaseRevealResponse,
    CaseSummary,
    CaseTrashRequest,
    CaseTrashResponse,
    CaseTrashSkipped,
    CaseUpdate,
    ImageOverride,
    ImageOverridePayload,
    ImageReviewPayload,
    ImageReviewResponse,
    ImageRestoreRequest,
    ImageRestoreResponse,
    ImageTrashRequest,
    ImageTrashResponse,
    ManualRenderImageInput,
    ManualRenderPreviewRequest,
    ManualRenderPreviewResponse,
    ManualRenderSourcesRequest,
    ManualRenderSourcesResponse,
    PsImageModelOptionsResponse,
    SimulateAfterRequest,
    SimulateAfterResponse,
    SimulationJob,
    SimulationJobReviewRequest,
    SourceBlockerActionRequest,
    SourceDirectoryBindRequest,
)

# Stage B: 单张图 phase / view 手动覆盖允许的取值。
# phase 与 skill manifest 输出对齐('before' / 'after');None 表示未覆盖。
# view 与 _extract_per_image_metadata 输出的 view_bucket / angle 对齐。
_ALLOWED_OVERRIDE_PHASES = {"before", "after"}
_ALLOWED_OVERRIDE_VIEWS = {"front", "oblique", "side"}
_MANUAL_VIEW_LABEL = {
    "front": "正面",
    "oblique": "右45侧",
    "side": "右侧面",
}
_MANUAL_PHASE_LABEL = {
    "before": "术前",
    "after": "术后",
}
_PREFLIGHT_VIEWS = ("front", "oblique", "side")
_PREFLIGHT_VIEW_LABEL = {
    "front": "正面",
    "oblique": "45°",
    "side": "侧面",
}
_PREFLIGHT_LAYER_LABEL = {
    "classification": "补齐分类",
    "confidence": "分类低置信",
    "copied_review": "补图待确认",
    "profile_expected": "侧面预期噪声",
    "candidate_noise": "未选候选噪声",
    "profile_quality": "侧面质量确认",
    "replace_image": "需换片",
    "render_excluded": "已排除出图",
    "image_quality": "图片质量确认",
    "render_pose": "成品姿态复核",
    "render_face": "成品面检复核",
    "render_other": "其他成品复核",
}
_PREFLIGHT_LAYER_ACTION = {
    "classification": "补齐阶段和角度后再出图",
    "confidence": "确认自动角度是否正确，必要时人工覆盖",
    "copied_review": "跨案例复制补图需要在目标案例中人工确认后再正式出图",
    "profile_expected": "侧面照正脸检测失败属预期；已走侧脸兜底，通常只需低优先确认",
    "candidate_noise": "未被正式出图选中的候选图检测噪声；保留审计，不阻断发布",
    "profile_quality": "侧面未定位到面部；确认下颌线/口角轮廓是否清晰，必要时换片",
    "replace_image": "已标记需换片；回到源图选择更清晰的同阶段同角度照片",
    "render_excluded": "不会参与下一次正式出图；保留文件和审计记录",
    "image_quality": "检查图片是否清晰、无遮挡、可用于正式出图",
    "render_pose": "检查术前术后姿态差，必要时回到人工整理重选配对",
    "render_face": "检查成品面部定位，必要时换片或调整正式出图策略",
    "render_other": "查看最新成品问题摘要后决定是否复核或重出",
}
_PREFLIGHT_CONFIDENCE_REVIEW_BELOW = 0.55
_MANUAL_UPLOAD_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/bmp": ".bmp",
}
_MAX_MANUAL_UPLOAD_BYTES = 25 * 1024 * 1024
_TRASH_DIR_NAME = ".case-workbench-trash"
_CASE_TRASH_SUBDIR = "cases"
_PREVIEW_DIR_NAME = ".case-workbench-preview"
_SIMULATION_INPUT_DIR_NAME = ".case-workbench-simulation-inputs"
_PREVIEW_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SIMULATION_PROVIDER = "ps_model_router"
_SIMULATION_FILE_LABELS = {
    "original_after": "原术后图",
    "before_reference": "术前姿态参考",
    "ai_after_simulation": "AI 增强图",
    "difference_heatmap": "差异热区",
    "controlled_policy_comparison": "对比图",
}
_SIMULATION_FILE_ALIASES = {
    "comparison": "controlled_policy_comparison",
    "five_way_comparison": "controlled_policy_comparison",
}
_SIMULATION_FILE_FALLBACKS = {
    "original_after": ("after-original.*",),
    "before_reference": ("before-reference.*",),
    "ai_after_simulation": ("after-ai-enhanced.*",),
    "difference_heatmap": ("difference-heatmap.png",),
    "controlled_policy_comparison": ("controlled-policy-five-way-comparison.png",),
}


class SourceGroupLockImage(BaseModel):
    case_id: int
    filename: str = Field(..., min_length=1, max_length=500)


class SourceGroupSlotLockRequest(BaseModel):
    view: str = Field(..., min_length=1, max_length=32)
    before: SourceGroupLockImage
    after: SourceGroupLockImage
    reviewer: str | None = Field(default="operator", max_length=64)
    reason: str | None = Field(default=None, max_length=500)


class SourceGroupWarningAcceptanceRequest(BaseModel):
    slot: str = Field(..., min_length=1, max_length=32)
    code: str = Field(..., min_length=1, max_length=80)
    job_id: int | None = None
    message_contains: str | None = Field(default=None, max_length=200)
    reviewer: str | None = Field(default="operator", max_length=64)
    note: str | None = Field(default=None, max_length=500)
_IMAGE_REVIEW_META_KEY = "image_review_states"
_IMAGE_REVIEW_VERDICT_LABEL = {
    "usable": "已确认可用",
    "deferred": "低优先",
    "needs_repick": "需换片",
    "excluded": "不用于出图",
}

# Stage A: brand 与 template 的默认值,用于 manifest fallback 路径推导。
# 这里硬编码 fumei + tri-compare 是因为 case_detail 不知道用户当前选了哪个 brand;
# 如果未来需要按品牌切换,前端应通过 query string 传入,这里再读 query。
_FALLBACK_BRAND = "fumei"
_FALLBACK_TEMPLATE = "tri-compare"
_UNSET = object()


def _fallback_skill_from_manifest(case_dir: str) -> dict[str, list[Any]]:
    """Stage A: 当 cases.skill_image_metadata_json 列为空(case 在新增列前已经
    upgrade 过)时,尝试直接读最近一次渲染的 manifest.final.json,实时透传
    image_metadata / blocking / warnings。

    返回 {"image_metadata": [...], "blocking_detail": [...], "warnings": [...]},
    任意错误条件全部空列表 — manifest 缺失/破损不阻塞 case 详情。
    """
    empty = {"image_metadata": [], "blocking_detail": [], "warnings": []}
    if not case_dir:
        return empty
    try:
        p = (
            Path(case_dir)
            / ".case-layout-output"
            / _FALLBACK_BRAND
            / _FALLBACK_TEMPLATE
            / "render"
            / "manifest.final.json"
        )
        if not p.is_file():
            return empty
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return empty
        groups = data.get("groups") or []
        return {
            "image_metadata": skill_bridge._extract_per_image_metadata(groups),
            "blocking_detail": [str(x) for x in (data.get("blocking_issues") or [])],
            "warnings": [str(x) for x in (data.get("warnings") or [])],
        }
    except (OSError, ValueError, TypeError):
        return empty

def _decode_manual_transform(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        offset_x = float(data.get("offset_x_pct") or 0)
        offset_y = float(data.get("offset_y_pct") or 0)
        scale = float(data.get("scale") or 1)
    except (TypeError, ValueError):
        return None
    offset_x = max(-0.25, min(0.25, offset_x))
    offset_y = max(-0.25, min(0.25, offset_y))
    scale = max(0.85, min(1.15, scale))
    if abs(offset_x) < 0.0005 and abs(offset_y) < 0.0005 and abs(scale - 1) < 0.0005:
        return None
    return {
        "enabled": True,
        "offset_x_pct": round(offset_x, 4),
        "offset_y_pct": round(offset_y, 4),
        "scale": round(scale, 4),
    }


def _manual_transform_to_json(transform: Any) -> str | None:
    if transform is None:
        return None
    if hasattr(transform, "model_dump"):
        data = transform.model_dump()
    elif isinstance(transform, dict):
        data = transform
    else:
        return None
    normalized = _decode_manual_transform(json.dumps(data, ensure_ascii=False))
    if not normalized:
        return None
    return json.dumps(normalized, ensure_ascii=False)


def _image_review_states_from_meta(meta: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    raw = (meta or {}).get(_IMAGE_REVIEW_META_KEY)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for filename, state in raw.items():
        if not filename or not isinstance(state, dict):
            continue
        verdict = str(state.get("verdict") or "")
        copied_requires_review = bool(state.get("copied_requires_review"))
        if verdict not in _IMAGE_REVIEW_VERDICT_LABEL and not copied_requires_review:
            continue
        item = dict(state)
        if verdict in _IMAGE_REVIEW_VERDICT_LABEL:
            item["verdict"] = verdict
        else:
            item.pop("verdict", None)
        item["render_excluded"] = bool(state.get("render_excluded") or verdict == "excluded")
        if verdict in _IMAGE_REVIEW_VERDICT_LABEL:
            item.setdefault("label", _IMAGE_REVIEW_VERDICT_LABEL[verdict])
        elif copied_requires_review:
            item.setdefault("label", "补图待确认")
        out[str(filename)] = item
    return out


def _apply_review_states_to_metadata(
    image_metadata: list[dict[str, Any]],
    review_states: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not review_states:
        return image_metadata
    for entry in image_metadata or []:
        filename = str(entry.get("filename") or entry.get("relative_path") or "")
        state = review_states.get(filename)
        if state:
            entry["review_state"] = state
    return image_metadata


def _fetch_image_overrides(conn: sqlite3.Connection, case_id: int) -> dict[str, dict[str, Any]]:
    """Stage B: 读 case_image_overrides 表,返回 {filename: {phase, view, transform}}。

    任一字段是 None 表示该维度未覆盖。返回空 dict 表示该 case 完全没有手动覆盖。
    """
    out: dict[str, dict[str, Any]] = {}
    rows = conn.execute(
        "SELECT filename, manual_phase, manual_view, manual_transform_json FROM case_image_overrides WHERE case_id = ?",
        (case_id,),
    ).fetchall()
    for r in rows:
        item: dict[str, Any] = {
            "phase": r["manual_phase"],
            "view": r["manual_view"],
        }
        transform = _decode_manual_transform(r["manual_transform_json"])
        if transform:
            item["transform"] = transform
        out[r["filename"]] = item
    return out


def _apply_overrides_to_metadata(
    image_metadata: list[dict[str, Any]],
    overrides: dict[str, dict[str, Any]],
    image_files: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Stage B: 把 case_image_overrides 合并到 skill_image_metadata。

    每条 entry 新增 `phase_source` / `view_source` 标 'manual' 或 'skill';manual 优先。
    在前端这两字段决定 chip 颜色或图标提示。
    """
    known = {str(entry.get("filename")) for entry in image_metadata or [] if entry.get("filename")}
    # Older cases can have manual overrides but no persisted per-image skill
    # metadata yet. Surface those rows so the source wall still shows the
    # human classification instead of hiding it behind filename fallback.
    for fname in image_files or []:
        if fname in known or fname not in overrides:
            continue
        image_metadata.append(
            {
                "filename": fname,
                "relative_path": fname,
                "phase": None,
                "phase_source": None,
                "angle": None,
                "angle_source": None,
                "angle_confidence": None,
                "direction": None,
                "view_bucket": None,
                "pose": None,
                "sharpness_score": None,
                "sharpness_level": None,
                "issues": [],
                "rejection_reason": None,
            }
        )
        known.add(fname)

    if not overrides and image_metadata is not None:
        # No overrides — annotate every entry as skill-sourced for UI determinism.
        for entry in image_metadata:
            entry.setdefault("phase_override_source", None)
            entry.setdefault("view_override_source", None)
        return image_metadata
    for entry in image_metadata or []:
        fname = entry.get("filename")
        ov = overrides.get(fname) if fname else None
        if ov and ov.get("phase"):
            entry["phase"] = ov["phase"]
            entry["phase_override_source"] = "manual"
        else:
            entry["phase_override_source"] = None
        if ov and ov.get("view"):
            entry["view_bucket"] = ov["view"]
            entry["angle"] = ov["view"]
            entry["view_override_source"] = "manual"
        else:
            entry["view_override_source"] = None
        if ov and ov.get("transform"):
            entry["manual_transform"] = ov["transform"]
            entry["manual_transform_source"] = "manual"
        else:
            entry.pop("manual_transform", None)
            entry.pop("manual_transform_source", None)
    return image_metadata


def _infer_phase_from_filename(filename: str) -> str | None:
    lower = filename.lower()
    if re.search(r"术前|before|pre", lower):
        return "before"
    if re.search(r"术后|after|post", lower):
        return "after"
    return None


def _infer_view_from_filename(filename: str) -> str | None:
    if re.search(r"3/4|34|45|45°|微侧|斜侧|半侧", filename, re.I):
        return "oblique"
    if re.search(r"侧面|侧脸", filename, re.I):
        return "side"
    if re.search(r"正面|正脸|front", filename, re.I):
        return "front"
    return None


def _metadata_phase(item: dict[str, Any] | None, filename: str) -> tuple[str | None, str]:
    if item:
        phase = item.get("phase")
        if phase in _ALLOWED_OVERRIDE_PHASES:
            return str(phase), str(item.get("phase_override_source") or item.get("phase_source") or "skill")
        if "phase" in item and phase is None:
            inferred = _infer_phase_from_filename(filename)
            if inferred:
                return inferred, "filename"
            return None, str(item.get("phase_source") or "skill")
    inferred = _infer_phase_from_filename(filename)
    return inferred, "filename" if inferred else "unknown"


def _metadata_view(item: dict[str, Any] | None, filename: str) -> tuple[str | None, str]:
    if item:
        view = item.get("view_bucket") or item.get("angle")
        if view in _ALLOWED_OVERRIDE_VIEWS:
            return str(view), str(item.get("view_override_source") or item.get("angle_source") or "skill")
        if ("view_bucket" in item or "angle" in item) and not view:
            inferred = _infer_view_from_filename(filename)
            if inferred:
                return inferred, "filename"
            return None, str(item.get("angle_source") or "skill")
    inferred = _infer_view_from_filename(filename)
    return inferred, "filename" if inferred else "unknown"


def _metadata_body_part(item: dict[str, Any] | None, default: str = "unknown") -> str:
    review_state = (item or {}).get("review_state") if isinstance(item, dict) else None
    if isinstance(review_state, dict):
        body_part = str(review_state.get("body_part") or "").strip()
        if body_part in {"face", "body", "unknown"}:
            return body_part
    return default


def _metadata_treatment_area(item: dict[str, Any] | None) -> str | None:
    review_state = (item or {}).get("review_state") if isinstance(item, dict) else None
    if not isinstance(review_state, dict):
        return None
    value = str(review_state.get("treatment_area") or "").strip()
    return value or None


def _dominant(values: list[str | None], fallback: str | None = None) -> str | None:
    counts: dict[str, int] = {}
    for value in values:
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return fallback
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _review_layer(layer: str, count: int = 0, filenames: list[str] | None = None) -> dict[str, Any]:
    severity = "info" if layer in {"profile_expected", "candidate_noise", "render_excluded"} else "block" if layer in {"classification", "copied_review"} else "review"
    return {
        "key": layer,
        "label": _PREFLIGHT_LAYER_LABEL.get(layer, layer),
        "severity": severity,
        "count": count,
        "action": _PREFLIGHT_LAYER_ACTION.get(layer, "人工复核"),
        "filenames": filenames or [],
    }


def _issue_is_profile_fallback(text: str) -> bool:
    return "正脸检测失败" in text and "侧脸检测兜底" in text


def _issue_is_face_failure(text: str) -> bool:
    return "面部检测失败" in text or "正脸检测失败" in text


def _review_layer_for_image(
    *,
    reasons: list[str],
    view: str | None,
    issues: list[str],
    rejection_reason: str,
    review_state: dict[str, Any] | None = None,
) -> str:
    verdict = str((review_state or {}).get("verdict") or "")
    if verdict == "excluded":
        return "render_excluded"
    if verdict == "needs_repick":
        return "replace_image"
    if bool((review_state or {}).get("copied_requires_review")):
        return "copied_review"
    if "missing_phase" in reasons or "missing_view" in reasons:
        return "classification"
    if "low_view_confidence" in reasons:
        return "confidence"
    issue_text = "\n".join(issues)
    if view == "side" and _issue_is_profile_fallback(issue_text):
        return "profile_expected"
    if view == "side" and (rejection_reason == "face_detection_failure" or _issue_is_face_failure(issue_text)):
        return "profile_quality"
    if "image_issues" in reasons or "rejected_by_skill" in reasons:
        return "image_quality"
    return "image_quality"


_IMAGE_REF_RE = re.compile(r"\.(?:jpg|jpeg|png|heic|webp|bmp)\b", re.IGNORECASE)


def _selected_files_from_manifest(manifest_path: str | None) -> set[str]:
    if not manifest_path:
        return set()
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return set()
    if not isinstance(manifest, dict):
        return set()
    selected: set[str] = set()
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        selected_slots = group.get("selected_slots")
        if not isinstance(selected_slots, dict):
            continue
        for slot in selected_slots.values():
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


def _mentions_selected_file(text: str, selected_files: set[str]) -> bool:
    if not selected_files:
        return True
    return any(name and name in text for name in selected_files)


def _warning_kind(
    warning: str,
    metadata_by_file: dict[str, dict[str, Any]] | None = None,
    selected_files: set[str] | None = None,
) -> str:
    text = str(warning)
    if selected_files and _IMAGE_REF_RE.search(text) and not _mentions_selected_file(text, selected_files):
        return "candidate_noise"
    matched = [
        item
        for filename, item in (metadata_by_file or {}).items()
        if filename and filename in text
    ]
    matched_views = {str(item.get("view_bucket") or item.get("angle") or "") for item in matched}
    all_side = bool(matched_views) and matched_views <= {"side"}
    if _issue_is_profile_fallback(text) and all_side:
        return "profile_expected"
    if _issue_is_face_failure(text):
        return "profile_quality" if all_side else "render_face"
    if "姿态差过大" in text or "多个姿态推断候选" in text:
        return "render_pose"
    return "render_other"


def _warning_slot(warning: str) -> str | None:
    text = str(warning)
    if "45°" in text or "45度" in text or "45侧" in text or "45" in text:
        return "oblique"
    if "侧面" in text or "侧脸" in text:
        return "side"
    if "正面" in text or "正脸" in text:
        return "front"
    return None


def _selected_filename(selection: dict[str, Any], role: str) -> str | None:
    item = selection.get(role)
    if not isinstance(item, dict):
        return None
    return str(
        item.get("group_relative_path")
        or item.get("relative_path")
        or item.get("name")
        or ""
    ) or None


def _selected_pair_scope_from_manifest(manifest_path: str | None, slot: str) -> dict[str, Any]:
    if not manifest_path:
        return {}
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    if not isinstance(manifest, dict):
        return {}
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        selected_slots = group.get("selected_slots")
        if not isinstance(selected_slots, dict):
            continue
        selection = selected_slots.get(slot)
        if not isinstance(selection, dict):
            continue
        selected_files: list[str] = []
        selected_pair: dict[str, str] = {}
        for role in ("before", "after"):
            item = selection.get(role)
            if not isinstance(item, dict):
                continue
            role_tokens = []
            for key in ("group_relative_path", "relative_path", "name"):
                value = item.get(key)
                text = str(value or "").strip()
                if text:
                    role_tokens.append(text)
                    selected_files.append(text)
            if role_tokens:
                selected_pair[role] = role_tokens[0]
        files = list(dict.fromkeys(selected_files))
        if files:
            return {"selected_files": files, "selected_pair": selected_pair}
    return {}


def _render_pose_slot_summaries(
    warnings: list[str],
    manifest_path: str | None,
) -> list[dict[str, Any]]:
    slot_counts: dict[str, int] = {view: 0 for view in _PREFLIGHT_VIEWS}
    for warning in warnings:
        if _warning_kind(str(warning)) != "render_pose":
            continue
        slot = _warning_slot(str(warning))
        if slot in slot_counts:
            slot_counts[slot] += 1
    if not any(slot_counts.values()) or not manifest_path:
        return []
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return []
    if not isinstance(manifest, dict):
        return []
    out: list[dict[str, Any]] = []
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        selected_slots = group.get("selected_slots")
        if not isinstance(selected_slots, dict):
            continue
        for slot in _PREFLIGHT_VIEWS:
            count = slot_counts.get(slot, 0)
            if count <= 0:
                continue
            selection = selected_slots.get(slot)
            if not isinstance(selection, dict):
                continue
            before = _selected_filename(selection, "before")
            after = _selected_filename(selection, "after")
            filenames = [name for name in (before, after) if name]
            out.append(
                {
                    "key": slot,
                    "label": _PREFLIGHT_VIEW_LABEL.get(slot, slot),
                    "count": count,
                    "before": before,
                    "after": after,
                    "filenames": filenames,
                    "pose_delta": selection.get("pose_delta") if isinstance(selection.get("pose_delta"), dict) else None,
                }
            )
    return out


def _warning_buckets(
    warnings: list[str],
    metadata_by_file: dict[str, dict[str, Any]] | None = None,
    selected_files: set[str] | None = None,
) -> dict[str, int]:
    buckets = {
        "candidate_noise": 0,
        "face_detection": 0,
        "profile_expected": 0,
        "profile_quality": 0,
        "pose_delta": 0,
        "pose_candidates": 0,
        "other": 0,
        "noise_count": 0,
        "actionable_count": 0,
    }
    for warning in warnings:
        text = str(warning)
        kind = _warning_kind(text, metadata_by_file, selected_files)
        if kind == "candidate_noise":
            buckets["candidate_noise"] += 1
            buckets["noise_count"] += 1
        elif kind == "profile_expected":
            buckets["profile_expected"] += 1
            buckets["noise_count"] += 1
        elif kind == "profile_quality":
            buckets["face_detection"] += 1
            buckets["profile_quality"] += 1
            buckets["actionable_count"] += 1
        elif kind == "render_face":
            buckets["face_detection"] += 1
            buckets["actionable_count"] += 1
        elif "姿态差过大" in text:
            buckets["pose_delta"] += 1
            buckets["actionable_count"] += 1
        elif "多个姿态推断候选" in text:
            buckets["pose_candidates"] += 1
            buckets["noise_count"] += 1
        else:
            buckets["other"] += 1
            buckets["actionable_count"] += 1
    return buckets


def _warning_layers(
    warnings: list[str],
    metadata_by_file: dict[str, dict[str, Any]] | None = None,
    manifest_path: str | None = None,
) -> list[dict[str, Any]]:
    selected_files = _selected_files_from_manifest(manifest_path)
    order = ["profile_quality", "render_pose", "render_face", "profile_expected", "candidate_noise", "render_other"]
    counts: dict[str, int] = {key: 0 for key in order}
    filenames_by_layer: dict[str, set[str]] = {key: set() for key in order}
    pose_slots = _render_pose_slot_summaries(warnings, manifest_path)
    for warning in warnings:
        layer = _warning_kind(str(warning), metadata_by_file, selected_files)
        if layer not in counts:
            layer = "render_other"
        counts[layer] += 1
        for filename in metadata_by_file or {}:
            if filename and filename in str(warning):
                filenames_by_layer[layer].add(filename)
    out: list[dict[str, Any]] = []
    for layer in order:
        if counts[layer] <= 0:
            continue
        filenames = sorted(filenames_by_layer[layer])
        item = _review_layer(layer, counts[layer], filenames)
        if layer == "render_pose" and pose_slots:
            pose_files = sorted({name for slot in pose_slots for name in slot.get("filenames", [])})
            item["filenames"] = pose_files
            item["slots"] = pose_slots
        out.append(item)
    return out


def _latest_render_display_warnings(metrics: dict[str, Any], meta: dict[str, Any]) -> list[str]:
    layers = metrics.get("warning_layers") if isinstance(metrics.get("warning_layers"), dict) else None
    if layers and isinstance(layers.get("selected_actionable"), list):
        return [str(item) for item in layers.get("selected_actionable") or [] if str(item).strip()]
    if isinstance(metrics.get("display_warnings"), list):
        return [str(item) for item in metrics.get("display_warnings") or [] if str(item).strip()]
    if isinstance(metrics.get("warnings"), list):
        return [str(item) for item in metrics.get("warnings") or [] if str(item).strip()]
    return [str(item) for item in meta.get("warnings") or [] if str(item).strip()]


def _latest_render_review_summary(
    conn: sqlite3.Connection,
    case_id: int,
    metadata_by_file: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
          j.id AS job_id,
          j.status AS job_status,
          j.manifest_path AS manifest_path,
          j.meta_json AS job_meta_json,
          rq.quality_status AS quality_status,
          rq.quality_score AS quality_score,
          rq.can_publish AS can_publish,
          rq.blocking_count AS blocking_count,
          rq.warning_count AS warning_count,
          rq.metrics_json AS metrics_json,
          rq.review_verdict AS review_verdict
        FROM render_jobs j
        LEFT JOIN render_quality rq ON rq.render_job_id = j.id
        WHERE j.case_id = ?
          AND j.status != 'undone'
        ORDER BY j.enqueued_at DESC, j.id DESC
        LIMIT 1
        """,
        (case_id,),
    ).fetchone()
    if not row:
        return None
    try:
        meta = json.loads(row["job_meta_json"] or "{}")
    except (TypeError, ValueError):
        meta = {}
    try:
        metrics = json.loads(row["metrics_json"] or "{}")
    except (TypeError, ValueError):
        metrics = {}
    warnings = _latest_render_display_warnings(metrics, meta)
    ai_usage = meta.get("ai_usage") if isinstance(meta.get("ai_usage"), dict) else {}
    selected_files = _selected_files_from_manifest(row["manifest_path"])
    metrics_buckets = metrics.get("warning_buckets") if isinstance(metrics.get("warning_buckets"), dict) else None
    buckets = dict(metrics_buckets) if metrics_buckets else _warning_buckets(warnings, metadata_by_file, selected_files)
    layers = _warning_layers(warnings, metadata_by_file, row["manifest_path"])
    acceptable_warning_count = int(buckets.get("noise_count") or 0)
    blocking_warning_count = int(buckets.get("actionable_count") or 0) + int(row["blocking_count"] or 0)
    publish_blockers: list[str] = []
    if row["quality_status"] == "done_with_issues":
        publish_blockers.append("quality_review_required")
    if row["can_publish"] is not None and not bool(row["can_publish"]):
        publish_blockers.append("not_publishable")
    if blocking_warning_count:
        publish_blockers.append("actionable_warnings")
    return {
        "job_id": row["job_id"],
        "job_status": row["job_status"],
        "quality_status": row["quality_status"],
        "quality_score": row["quality_score"],
        "can_publish": bool(row["can_publish"]) if row["can_publish"] is not None else None,
        "blocking_count": int(row["blocking_count"] or 0),
        "warning_count": int(row["warning_count"] or len(warnings) or 0),
        "warning_buckets": buckets,
        "warning_layers": layers,
        "acceptable_warning_count": acceptable_warning_count,
        "blocking_warning_count": blocking_warning_count,
        "publish_blockers": publish_blockers,
        "review_verdict": row["review_verdict"],
        "ai_usage": {
            "used_after_enhancement": bool(
                metrics.get("ai_after_enhancement")
                or ai_usage.get("used_after_enhancement")
            ),
            "used_ai_padfill": bool(
                metrics.get("ai_edge_padfill")
                or ai_usage.get("used_ai_padfill")
            ),
            "semantic_judge_requested": ai_usage.get("semantic_judge_requested"),
            "semantic_judge_effective": ai_usage.get("semantic_judge_effective"),
        },
    }


def _build_classification_preflight(
    *,
    image_files: list[str],
    image_metadata: list[dict[str, Any]],
    case_id: int,
    raw_image_files: list[str] | None = None,
    case_category: str | None = None,
) -> dict[str, Any]:
    source_profile = source_images.classify_source_profile(raw_image_files or image_files)
    metadata_by_file: dict[str, dict[str, Any]] = {}
    for item in image_metadata or []:
        key = str(item.get("filename") or item.get("relative_path") or "")
        if key:
            metadata_by_file[key] = item

    slots: dict[str, dict[str, Any]] = {
        view: {
            "view": view,
            "label": _PREFLIGHT_VIEW_LABEL[view],
            "before_count": 0,
            "after_count": 0,
            "manual_before_count": 0,
            "manual_after_count": 0,
            "ready": False,
        }
        for view in _PREFLIGHT_VIEWS
    }
    review_items: list[dict[str, Any]] = []
    classification_gaps: list[dict[str, Any]] = []
    slot_context: dict[str, list[dict[str, Any]]] = {view: [] for view in _PREFLIGHT_VIEWS}
    manual_count = 0
    low_confidence_count = 0
    classified_count = 0
    reviewed_count = 0
    deferred_review_count = 0
    render_excluded_count = 0
    needs_repick_count = 0
    default_body_part = "body" if case_category == "body" else "face" if case_category == "standard_face" else "unknown"

    for filename in image_files:
        item = metadata_by_file.get(filename)
        review_state = (item or {}).get("review_state") if isinstance(item, dict) else None
        review_state = review_state if isinstance(review_state, dict) else None
        review_verdict = str((review_state or {}).get("verdict") or "")
        is_render_excluded = bool((review_state or {}).get("render_excluded") or review_verdict == "excluded")
        copied_requires_review = bool((review_state or {}).get("copied_requires_review"))
        if review_verdict in {"usable", "deferred", "needs_repick", "excluded"}:
            reviewed_count += 1
        if review_verdict == "deferred":
            deferred_review_count += 1
        if review_verdict == "needs_repick":
            needs_repick_count += 1
        if is_render_excluded:
            render_excluded_count += 1
        phase, phase_source = _metadata_phase(item, filename)
        view, view_source = _metadata_view(item, filename)
        is_manual = (
            (item or {}).get("phase_override_source") == "manual"
            or (item or {}).get("view_override_source") == "manual"
        )
        if is_manual:
            manual_count += 1
        if phase in _ALLOWED_OVERRIDE_PHASES and view in _ALLOWED_OVERRIDE_VIEWS:
            classified_count += 1
            if not is_render_excluded:
                slot = slots[view]
                key = f"{phase}_count"
                slot[key] = int(slot[key]) + 1
                if is_manual:
                    manual_key = f"manual_{phase}_count"
                    slot[manual_key] = int(slot[manual_key]) + 1
                slot_context[view].append(
                    {
                        "filename": filename,
                        "phase": phase,
                        "body_part": _metadata_body_part(item, default_body_part),
                        "treatment_area": _metadata_treatment_area(item),
                    }
                )

        reasons: list[str] = []
        severity = "review"
        if is_render_excluded:
            reasons.append("render_excluded")
            severity = "info"
        if review_verdict == "needs_repick":
            reasons.append("review_needs_repick")
        if copied_requires_review and review_verdict not in {"usable", "deferred", "needs_repick", "excluded"}:
            reasons.append("copied_requires_review")
            severity = "block"
        if phase not in _ALLOWED_OVERRIDE_PHASES:
            reasons.append("missing_phase")
            severity = "block"
        if view not in _ALLOWED_OVERRIDE_VIEWS:
            reasons.append("missing_view")
            severity = "block"
        angle_confidence = None
        if item and item.get("angle_confidence") is not None:
            try:
                angle_confidence = float(item.get("angle_confidence"))
            except (TypeError, ValueError):
                angle_confidence = None
        if (
            angle_confidence is not None
            and angle_confidence < _PREFLIGHT_CONFIDENCE_REVIEW_BELOW
            and (item or {}).get("view_override_source") != "manual"
        ):
            reasons.append("low_view_confidence")
            low_confidence_count += 1
        issues = [str(x) for x in ((item or {}).get("issues") or []) if str(x)]
        if issues:
            reasons.append("image_issues")
        rejection_reason = str((item or {}).get("rejection_reason") or "").strip()
        if rejection_reason:
            reasons.append("rejected_by_skill")
        if review_verdict in {"usable", "deferred"} and "missing_phase" not in reasons and "missing_view" not in reasons:
            reasons = []
        if is_render_excluded:
            severity = "info"
        if reasons:
            if "missing_phase" in reasons or "missing_view" in reasons:
                classification_gaps.append(
                    {
                        "kind": "classification",
                        "filename": filename,
                        "phase": phase,
                        "view": view,
                        "missing": [
                            key
                            for key, value in (("phase", phase), ("view", view))
                            if value not in (_ALLOWED_OVERRIDE_PHASES if key == "phase" else _ALLOWED_OVERRIDE_VIEWS)
                        ],
                        "body_part": _metadata_body_part(item, default_body_part),
                        "treatment_area": _metadata_treatment_area(item),
                    }
                )
            layer = _review_layer_for_image(
                reasons=reasons,
                view=view,
                issues=issues,
                rejection_reason=rejection_reason,
                review_state=review_state,
            )
            review_items.append(
                {
                    "filename": filename,
                    "phase": phase,
                    "phase_source": phase_source,
                    "view": view,
                    "view_source": view_source,
                    "manual": is_manual,
                    "severity": severity,
                    "layer": layer,
                    "layer_label": _PREFLIGHT_LAYER_LABEL.get(layer, layer),
                    "action": _PREFLIGHT_LAYER_ACTION.get(layer, "人工复核"),
                    "reasons": reasons,
                    "angle_confidence": angle_confidence,
                    "issues": issues,
                    "rejection_reason": rejection_reason or None,
                    "review_state": review_state,
                }
            )

    slot_list = []
    slot_blockers: list[dict[str, Any]] = []
    render_gaps: list[dict[str, Any]] = []
    for slot in slots.values():
        slot["ready"] = int(slot["before_count"]) > 0 and int(slot["after_count"]) > 0
        slot_list.append(slot)
        if not slot["ready"]:
            missing = []
            if int(slot["before_count"]) == 0:
                missing.append("before")
            if int(slot["after_count"]) == 0:
                missing.append("after")
            slot_blockers.append(
                {
                    "code": "missing_render_pair",
                    "view": slot["view"],
                    "label": slot["label"],
                    "missing": missing,
                }
            )
            context = slot_context.get(str(slot["view"]), [])
            body_part = _dominant([str(item.get("body_part") or "") for item in context], default_body_part)
            treatment_area = _dominant([str(item.get("treatment_area") or "") for item in context], None)
            for role in missing:
                render_gaps.append(
                    {
                        "key": f"{slot['view']}-{role}",
                        "kind": "render_slot",
                        "view": slot["view"],
                        "view_label": slot["label"],
                        "role": role,
                        "phase": role,
                        "role_label": "术前" if role == "before" else "术后",
                        "body_part": body_part or "unknown",
                        "treatment_area": treatment_area,
                        "current_count": int(slot[f"{role}_count"]),
                        "required_count": 1,
                    }
                )

    blocking_items = [item for item in review_items if item["severity"] == "block"]
    layer_order = [
        "classification",
        "confidence",
        "profile_quality",
        "replace_image",
        "image_quality",
        "render_excluded",
        "profile_expected",
    ]
    layer_counts: dict[str, int] = {layer: 0 for layer in layer_order}
    layer_files: dict[str, list[str]] = {layer: [] for layer in layer_order}
    for item in review_items:
        layer = str(item.get("layer") or "image_quality")
        if layer not in layer_counts:
            layer_counts[layer] = 0
            layer_files[layer] = []
        layer_counts[layer] += 1
        layer_files[layer].append(str(item["filename"]))
    review_layers = [
        _review_layer(layer, layer_counts[layer], layer_files[layer])
        for layer in layer_order
        if layer_counts.get(layer, 0) > 0
    ]

    with db.connect() as conn:
        latest_render = _latest_render_review_summary(conn, case_id, metadata_by_file)
    latest_warning_buckets = (latest_render or {}).get("warning_buckets") or {}
    latest_has_review = bool(
        (latest_render or {}).get("quality_status") == "done_with_issues"
        or (latest_render or {}).get("can_publish") is False
        or int((latest_render or {}).get("blocking_warning_count") or 0) > 0
    )
    render_blocked = bool(blocking_items or slot_blockers)
    preflight_blocking = {
        "classification": len(blocking_items),
        "render_pairs": len(slot_blockers),
        "low_confidence": low_confidence_count,
        "needs_repick": needs_repick_count,
    }
    acceptable_review = {
        "profile_expected": sum(1 for item in review_items if item.get("layer") == "profile_expected"),
        "render_excluded": sum(1 for item in review_items if item.get("layer") == "render_excluded"),
        "latest_warning_noise": int(latest_warning_buckets.get("noise_count") or 0),
    }
    render_status = "blocked" if render_blocked else "review" if latest_has_review or low_confidence_count else "ready"
    suggested_action = (
        "complete_classification"
        if blocking_items
        else "complete_render_pairs"
        if slot_blockers
        else "review_latest_quality"
        if latest_has_review
        else "ready_to_render"
    )
    return {
        "classification": {
            "source_count": len(image_files),
            "source_profile": source_profile,
            "metadata_count": len(image_metadata or []),
            "classified_count": classified_count,
            "needs_manual_count": len(blocking_items),
            "actionable_review_count": sum(
                1 for item in review_items if item.get("layer") not in {"profile_expected", "render_excluded"}
            ),
            "expected_profile_noise_count": sum(
                1 for item in review_items if item.get("layer") == "profile_expected"
            ),
            "reviewed_count": reviewed_count,
            "deferred_review_count": deferred_review_count,
            "needs_repick_count": needs_repick_count,
            "render_excluded_count": render_excluded_count,
            "manual_override_count": manual_count,
            "low_confidence_count": low_confidence_count,
            "review_count": len(review_items),
            "gaps": classification_gaps[:80],
            "review_layers": review_layers,
            "review_items": review_items[:80],
        },
        "render": {
            "status": render_status,
            "ready": not render_blocked,
            "slots": slot_list,
            "blocking": slot_blockers,
            "gaps": render_gaps,
            "blocking_summary": preflight_blocking,
            "acceptable_review": acceptable_review,
            "suggested_action": suggested_action,
        },
        "latest_render": latest_render,
    }


def _latest_render_requires_review(latest_render: dict[str, Any] | None) -> bool:
    latest = latest_render if isinstance(latest_render, dict) else {}
    return bool(
        latest.get("quality_status") == "done_with_issues"
        or latest.get("can_publish") is False
        or int(latest.get("blocking_warning_count") or 0) > 0
    )


def _apply_source_group_authority_to_preflight(
    preflight: dict[str, Any],
    source_group: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(preflight, dict) or not isinstance(source_group, dict):
        return preflight
    bound_case_ids = source_group.get("bound_case_ids")
    if not bound_case_ids:
        return preflight
    group_preflight = source_group.get("preflight")
    if not isinstance(group_preflight, dict):
        return preflight
    render = dict(preflight.get("render") or {})
    original_status = str(render.get("status") or "")
    original_blocking_summary = dict(render.get("blocking_summary") or {})
    group_status = str(group_preflight.get("status") or "blocked")
    group_blocked = group_status == "blocked"
    latest_has_review = _latest_render_requires_review(preflight.get("latest_render"))
    effective_status = "blocked" if group_blocked else "review" if latest_has_review else "ready"

    source_blocking_summary = {
        "classification": int(group_preflight.get("needs_manual_count") or 0),
        "render_pairs": len(group_preflight.get("missing_slots") or []),
        "low_confidence": 0,
        "needs_repick": 0,
        "primary_classification": int(original_blocking_summary.get("classification") or 0),
        "primary_render_pairs": int(original_blocking_summary.get("render_pairs") or 0),
    }
    render.update(
        {
            "status": effective_status,
            "ready": not group_blocked,
            "slots": group_preflight.get("slots") or render.get("slots") or [],
            "blocking": group_preflight.get("missing_slots") or [],
            "gaps": [] if not group_blocked else render.get("gaps") or [],
            "blocking_summary": source_blocking_summary,
            "suggested_action": (
                "complete_source_group_slots"
                if group_blocked and group_preflight.get("missing_slots")
                else "complete_classification"
                if group_blocked and int(group_preflight.get("needs_manual_count") or 0) > 0
                else "review_latest_quality"
                if latest_has_review
                else "ready_to_render"
            ),
            "source_group_authority": {
                "active": True,
                "reason": "source_group_ready_overrides_primary_directory"
                if not group_blocked
                else "source_group_blocks_formal_render",
                "status": group_status,
                "readiness_score": group_preflight.get("readiness_score"),
                "bound_case_ids": bound_case_ids,
                "missing_source_count": int(group_preflight.get("missing_source_count") or 0),
                "missing_slot_count": len(group_preflight.get("missing_slots") or []),
                "needs_manual_count": int(group_preflight.get("needs_manual_count") or 0),
                "original_status": original_status,
                "original_blocking_summary": original_blocking_summary,
            },
        }
    )
    preflight = dict(preflight)
    preflight["render"] = render
    return preflight


def _write_image_override(
    conn: sqlite3.Connection,
    case_id: int,
    filename: str,
    manual_phase: str | None,
    manual_view: str | None,
    updated_at: str,
    manual_transform: Any = _UNSET,
) -> None:
    transform_touched = manual_transform is not _UNSET
    transform_json = _manual_transform_to_json(manual_transform) if transform_touched else None
    if manual_phase is None and manual_view is None and (not transform_touched or transform_json is None):
        conn.execute(
            "DELETE FROM case_image_overrides WHERE case_id = ? AND filename = ?",
            (case_id, filename),
        )
        return
    if transform_touched:
        conn.execute(
            """INSERT INTO case_image_overrides
                   (case_id, filename, manual_phase, manual_view, manual_transform_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(case_id, filename) DO UPDATE SET
                   manual_phase = excluded.manual_phase,
                   manual_view = excluded.manual_view,
                   manual_transform_json = excluded.manual_transform_json,
                   updated_at = excluded.updated_at""",
            (case_id, filename, manual_phase, manual_view, transform_json, updated_at),
        )
    else:
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


def _case_dir_for_update(conn: sqlite3.Connection, case_id: int) -> Path:
    row = conn.execute("SELECT abs_path, trashed_at FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(404, "case not found")
    if row["trashed_at"]:
        raise HTTPException(410, "case has been moved to trash")
    base = Path(row["abs_path"]).resolve()
    if not base.exists() or not base.is_dir():
        raise HTTPException(404, f"case directory missing: {base}")
    return base


def _resolve_existing_source(case_dir: Path, filename: str | None) -> Path:
    if not filename or filename in {".", ".."}:
        raise HTTPException(400, "existing image filename is required")
    if _TRASH_DIR_NAME in Path(filename).parts:
        raise HTTPException(400, "trashed images cannot be used as active source images")
    target = (case_dir / filename).resolve()
    try:
        target.relative_to(case_dir)
    except ValueError:
        raise HTTPException(400, "invalid image path")
    if not target.is_file():
        raise HTTPException(404, f"image not found: {filename}")
    if target.suffix.lower() not in scanner.IMAGE_EXTS:
        raise HTTPException(400, f"unsupported image extension: {target.suffix}")
    return target


def _validate_relative_image_name(case_dir: Path, filename: str | None) -> str:
    if not filename or filename in {".", ".."}:
        raise HTTPException(400, "image filename is required")
    if "\\" in filename:
        raise HTTPException(400, "invalid image path")
    if _TRASH_DIR_NAME in Path(filename).parts:
        raise HTTPException(400, "trashed images cannot be used as active source images")
    target = (case_dir / filename).resolve()
    try:
        target.relative_to(case_dir)
    except ValueError:
        raise HTTPException(400, "invalid image path")
    if target.suffix.lower() not in scanner.IMAGE_EXTS:
        raise HTTPException(400, f"unsupported image extension: {target.suffix}")
    return filename


def _decode_upload_image(item: ManualRenderImageInput) -> tuple[bytes, str]:
    if not item.data_url:
        raise HTTPException(400, "upload data_url is required")
    header = ""
    payload = item.data_url
    if "," in item.data_url and item.data_url.startswith("data:"):
        header, payload = item.data_url.split(",", 1)
    mime = ""
    if header.startswith("data:"):
        mime = header[5:].split(";", 1)[0].lower()
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "upload data_url is not valid base64")
    if not raw:
        raise HTTPException(400, "upload image is empty")
    if len(raw) > _MAX_MANUAL_UPLOAD_BYTES:
        raise HTTPException(413, "upload image exceeds 25MB")

    ext = Path(item.upload_name or "").suffix.lower()
    if ext not in scanner.IMAGE_EXTS:
        ext = _MANUAL_UPLOAD_MIME_EXT.get(mime, "")
    if ext not in scanner.IMAGE_EXTS:
        raise HTTPException(400, "unsupported upload image type")
    return raw, ext


def _manual_filename(phase: str, view: str, suffix: str, stamp: str, case_dir: Path) -> str:
    phase_label = _MANUAL_PHASE_LABEL[phase]
    view_label = _MANUAL_VIEW_LABEL[view]
    base = f"{phase_label}-{view_label}-手动-{stamp}"
    candidate = f"{base}{suffix.lower()}"
    i = 2
    while (case_dir / candidate).exists():
        candidate = f"{base}-{i}{suffix.lower()}"
        i += 1
    return candidate


def _materialize_manual_image(
    case_dir: Path,
    item: ManualRenderImageInput,
    phase: str,
    view: str,
    stamp: str,
) -> str:
    if item.kind == "existing":
        source = _resolve_existing_source(case_dir, item.filename)
        filename = _manual_filename(phase, view, source.suffix, stamp, case_dir)
        shutil.copyfile(source, case_dir / filename)
        return filename
    raw, suffix = _decode_upload_image(item)
    filename = _manual_filename(phase, view, suffix, stamp, case_dir)
    (case_dir / filename).write_bytes(raw)
    return filename


def _preview_root(case_dir: Path) -> Path:
    return case_dir / _PREVIEW_DIR_NAME


def _prune_preview_dirs(case_dir: Path, keep: int = 12) -> None:
    root = _preview_root(case_dir)
    if not root.is_dir():
        return
    previews = [p for p in root.iterdir() if p.is_dir() and _PREVIEW_ID_RE.match(p.name)]
    previews.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in previews[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


def _materialize_preview_image(
    case_dir: Path,
    preview_dir: Path,
    item: ManualRenderImageInput,
    role: str,
) -> Path:
    if item.kind == "existing":
        return _resolve_existing_source(case_dir, item.filename)
    raw, suffix = _decode_upload_image(item)
    target = preview_dir / f"{role}{suffix.lower()}"
    target.write_bytes(raw)
    return target


def _simulation_input_root(case_dir: Path) -> Path:
    return case_dir / _SIMULATION_INPUT_DIR_NAME


def _materialize_simulation_image(
    case_dir: Path,
    item: ManualRenderImageInput,
    role: str,
    stamp: str,
) -> Path:
    if item.kind == "existing":
        return _resolve_existing_source(case_dir, item.filename)
    raw, suffix = _decode_upload_image(item)
    target_dir = _simulation_input_root(case_dir) / stamp
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{role}{suffix.lower()}"
    target.write_bytes(raw)
    return target


def _resolve_simulation_image_input(
    case_dir: Path,
    *,
    path: str | None,
    image: ManualRenderImageInput | None,
    role: str,
    stamp: str,
    required: bool,
) -> Path | None:
    if image is not None:
        return _materialize_simulation_image(case_dir, image, role, stamp)
    if path:
        return _resolve_existing_source(case_dir, path)
    if required:
        raise HTTPException(400, f"{role} image is required")
    return None


def _simulation_input_ref(case_dir: Path, role: str, path: Path) -> dict[str, Any]:
    ref: dict[str, Any] = {"role": role, "path": str(path)}
    try:
        ref["case_relative_path"] = str(path.resolve().relative_to(case_dir))
    except ValueError:
        pass
    return ref


def _resolve_style_reference_path(raw: str) -> Path:
    """Resolve a user-supplied absolute (or ~-prefixed) image path for style guidance.

    Style references may live anywhere on disk (e.g. ~/Downloads/对比照模板.jpg),
    so case-dir containment is intentionally NOT enforced. We still validate
    existence and image extension to fail fast.
    """
    if not raw or not raw.strip():
        raise HTTPException(400, "style_reference path must be non-empty")
    expanded = Path(raw.strip()).expanduser()
    if not expanded.is_absolute():
        raise HTTPException(400, f"style_reference path must be absolute: {raw}")
    target = expanded.resolve()
    if not target.is_file():
        raise HTTPException(404, f"style_reference image not found: {raw}")
    if target.suffix.lower() not in scanner.IMAGE_EXTS:
        raise HTTPException(400, f"unsupported style_reference extension: {target.suffix}")
    return target


def _trash_root(case_dir: Path) -> Path:
    return case_dir / _TRASH_DIR_NAME


def _trash_image(conn: sqlite3.Connection, case_id: int, case_dir: Path, filename: str) -> tuple[str, str]:
    source = _resolve_existing_source(case_dir, filename)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    rel = source.relative_to(case_dir)
    dest = (_trash_root(case_dir) / stamp / rel).resolve()
    try:
        dest.relative_to(_trash_root(case_dir).resolve())
    except ValueError:
        raise HTTPException(400, "invalid trash path")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
    conn.execute(
        "DELETE FROM case_image_overrides WHERE case_id = ? AND filename = ?",
        (case_id, str(rel)),
    )
    return str(rel), str(dest.relative_to(_trash_root(case_dir)))


def _unique_restore_name(case_dir: Path, requested: str) -> str:
    target = (case_dir / requested).resolve()
    try:
        rel = target.relative_to(case_dir)
    except ValueError:
        raise HTTPException(400, "invalid restore target")
    if target.suffix.lower() not in scanner.IMAGE_EXTS:
        raise HTTPException(400, f"unsupported image extension: {target.suffix}")
    if not target.exists():
        return str(rel)
    parent = target.parent
    stem = target.stem
    suffix = target.suffix
    for i in range(2, 1000):
        candidate = (parent / f"{stem}-恢复-{i}{suffix}").resolve()
        try:
            crel = candidate.relative_to(case_dir)
        except ValueError:
            continue
        if not candidate.exists():
            return str(crel)
    raise HTTPException(409, "cannot find free restore filename")


def _resolve_trash_item(case_dir: Path, trash_path: str | None) -> Path:
    if not trash_path or trash_path in {".", ".."} or "\\" in trash_path:
        raise HTTPException(400, "trash_path is required")
    root = _trash_root(case_dir).resolve()
    target = (root / trash_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(400, "invalid trash path")
    if not target.is_file():
        raise HTTPException(404, "trash item not found")
    if target.suffix.lower() not in scanner.IMAGE_EXTS:
        raise HTTPException(400, f"unsupported image extension: {target.suffix}")
    return target


def _restore_trashed_image(case_dir: Path, trash_path: str, restore_to: str | None = None) -> str:
    source = _resolve_trash_item(case_dir, trash_path)
    if restore_to:
        requested = _validate_relative_image_name(case_dir, restore_to)
    else:
        parts = Path(trash_path).parts
        requested = str(Path(*parts[1:])) if len(parts) > 1 else source.name
    restored_name = _unique_restore_name(case_dir, requested)
    dest = (case_dir / restored_name).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
    # Best-effort cleanup of empty timestamp folders.
    trash_root = _trash_root(case_dir).resolve()
    for parent in [source.parent, *source.parent.parents]:
        if parent == trash_root or trash_root not in parent.parents:
            break
        try:
            parent.rmdir()
        except OSError:
            break
    return restored_name


def _unique_case_trash_dest(case_dir: Path, case_id: int, stamp: str) -> Path:
    name = case_dir.name.strip() or f"case-{case_id}"
    root = (case_dir.parent / _TRASH_DIR_NAME / _CASE_TRASH_SUBDIR).resolve()
    base = f"{stamp}-case-{case_id}-{name}"
    dest = (root / base).resolve()
    i = 2
    while dest.exists():
        dest = (root / f"{base}-{i}").resolve()
        i += 1
    try:
        dest.relative_to(root)
    except ValueError:
        raise HTTPException(400, "invalid case trash path")
    return dest


def _trash_case_directory(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    abs_path: str,
    reason: str | None,
    stamp: str,
    trashed_at: str,
) -> str:
    source = Path(abs_path).resolve()
    if not source.exists() or not source.is_dir():
        raise FileNotFoundError(str(source))
    dest = _unique_case_trash_dest(source, case_id, stamp)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
    conn.execute(
        """
        UPDATE cases
        SET original_abs_path = COALESCE(original_abs_path, ?),
            abs_path = ?,
            trashed_at = ?,
            trash_reason = ?
        WHERE id = ?
        """,
        (str(source), str(dest), trashed_at, reason, case_id),
    )
    return str(dest)


def _simulation_policy(focus_regions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    has_regions = bool(focus_regions)
    return {
        "artifact_mode": "ai_after_simulation",
        "focus_scope": "region-locked-light" if has_regions else "whole-image-light",
        "focus_region_required": False,
        "target_input_requirement": "focus_regions_optional",
        "non_target_policy": "preserve-no-global-retouch" if has_regions else "whole-image-light-retouch",
        "watermark_required": True,
        "mix_with_real_case": False,
        "can_publish_default": False,
    }


def _normalize_focus_regions(regions: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for idx, region in enumerate(regions, start=1):
        data = region.model_dump() if hasattr(region, "model_dump") else dict(region)
        try:
            x = float(data.get("x", 0))
            y = float(data.get("y", 0))
            width = float(data.get("width", 0))
            height = float(data.get("height", 0))
        except (TypeError, ValueError):
            raise HTTPException(400, f"focus_regions[{idx}] must use numeric normalized coordinates")
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x > 1 or y > 1:
            raise HTTPException(400, f"focus_regions[{idx}] must be within normalized 0-1 coordinates")
        if x + width > 1.001 or y + height > 1.001:
            raise HTTPException(400, f"focus_regions[{idx}] exceeds the normalized image bounds")
        label = str(data.get("label") or "").strip() or None
        normalized.append(
            {
                "x": round(x, 4),
                "y": round(y, 4),
                "width": round(width, 4),
                "height": round(height, 4),
                "label": label,
            }
        )
    return normalized


def _insert_simulation_job(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    focus_targets: list[str],
    focus_regions: list[dict[str, Any]],
    input_refs: list[dict[str, Any]],
    provider: str,
    model_name: str | None,
    note: str | None,
    status: str = "running",
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO simulation_jobs
          (group_id, case_id, status, focus_targets_json, policy_json,
           model_plan_json, input_refs_json, output_refs_json, watermarked,
           audit_json, error_message, created_at, updated_at)
        VALUES (NULL, ?, ?, ?, ?, ?, ?, '[]', 1, ?, NULL, ?, ?)
        """,
        (
            case_id,
            status,
            json.dumps(focus_targets, ensure_ascii=False),
            json.dumps(_simulation_policy(focus_regions), ensure_ascii=False),
            json.dumps(
                {
                    "provider": provider,
                    "model_name": model_name,
                    "focus_regions": focus_regions,
                },
                ensure_ascii=False,
            ),
            json.dumps(input_refs, ensure_ascii=False),
            json.dumps(
                {
                    "note": note,
                    "focus_regions": focus_regions,
                    "created_via": "/api/cases/{id}/simulate-after",
                },
                ensure_ascii=False,
            ),
            now,
            now,
        ),
    )
    return int(cur.lastrowid or 0)


def _insert_ai_run(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    provider: str,
    model_name: str | None,
    focus_targets: list[str],
    focus_regions: list[dict[str, Any]],
    input_refs: list[dict[str, Any]],
    status: str,
    error_message: str | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO ai_runs
          (subject_kind, subject_id, model_role, provider, model_name,
           input_summary_json, output_json, status, error_message, started_at, finished_at)
        VALUES ('simulation_job', ?, 'image_generation', ?, ?, ?, '{}', ?, ?, ?, NULL)
        """,
        (
            job_id,
            provider,
            model_name,
            json.dumps(
                {
                    "focus_targets": focus_targets,
                    "focus_regions": focus_regions,
                    "input_refs": input_refs,
                },
                ensure_ascii=False,
            ),
            status,
            error_message,
            now,
        ),
    )
    return int(cur.lastrowid or 0)


def _json_field(row: sqlite3.Row, key: str, fallback: Any) -> Any:
    try:
        raw = row[key]
    except (IndexError, KeyError):
        return fallback
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _simulation_kind(kind: str) -> str:
    raw = (kind or "").strip() or "ai_after_simulation"
    return _SIMULATION_FILE_ALIASES.get(raw, raw)


def _safe_simulation_image_path(job_id: int, path: Path) -> Path | None:
    target = path.resolve()
    safe_root = ai_generation_adapter.simulation_job_dir(job_id).resolve()
    try:
        target.relative_to(safe_root)
    except ValueError:
        return None
    if not target.is_file() or target.suffix.lower() not in scanner.IMAGE_EXTS:
        return None
    return target


def _simulation_fallback_file(job_id: int, kind: str) -> Path | None:
    safe_root = ai_generation_adapter.simulation_job_dir(job_id).resolve()
    for pattern in _SIMULATION_FILE_FALLBACKS.get(kind, ()):
        for candidate in sorted(safe_root.glob(pattern)):
            safe = _safe_simulation_image_path(job_id, candidate)
            if safe is not None:
                return safe
    return None


def _simulation_available_files(row: sqlite3.Row) -> list[dict[str, Any]]:
    job_id = int(row["id"])
    refs = _json_field(row, "output_refs_json", [])
    files: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(kind: str, path: Path, watermarked: bool = False) -> None:
        canonical = _simulation_kind(kind)
        if canonical in seen:
            return
        safe = _safe_simulation_image_path(job_id, path)
        if safe is None:
            return
        seen.add(canonical)
        files.append(
            {
                "kind": canonical,
                "label": _SIMULATION_FILE_LABELS.get(canonical, canonical),
                "filename": safe.name,
                "path": str(safe),
                "watermarked": bool(watermarked),
            }
        )

    if isinstance(refs, list):
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            kind = str(ref.get("kind") or "")
            path = str(ref.get("path") or "")
            if not kind or not path:
                continue
            add(kind, Path(path), bool(ref.get("watermarked")))

    for kind in _SIMULATION_FILE_LABELS:
        fallback = _simulation_fallback_file(job_id, kind)
        if fallback is not None:
            add(kind, fallback, kind == "ai_after_simulation" and bool(row["watermarked"]))
    return files


def _simulation_file_exists(job: SimulationJob, kind: str) -> bool:
    canonical = _simulation_kind(kind)
    if any(ref.get("kind") == canonical for ref in job.available_files):
        return True
    return False


def _float_metric(raw: dict[str, Any], key: str) -> float | None:
    value = raw.get(key)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _simulation_review_decision(job: SimulationJob, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = policy or simulation_quality.load_ai_review_policy()
    thresholds = dict(policy["thresholds"])
    blockers: list[str] = []
    warnings: list[str] = []
    passes: list[str] = []
    metrics: dict[str, float] = {}

    if job.status == "failed":
        blockers.append("任务失败，没有可审核的 AI 增强图")
    if job.status not in {"done", "done_with_issues", "failed"}:
        warnings.append(f"任务状态为 {job.status}，等待生成完成后再审核")
    if not _simulation_file_exists(job, "ai_after_simulation"):
        blockers.append("缺少 AI 增强输出图")
    if not job.watermarked:
        blockers.append("增强图未确认水印，不能通过审核")

    focus_regions = None
    if isinstance(job.model_plan, dict):
        focus_regions = job.model_plan.get("focus_regions")
    if not focus_regions and isinstance(job.audit, dict):
        focus_regions = job.audit.get("focus_regions")
    if not focus_regions:
        blockers.append("缺少框选区域，不符合受控增强策略")

    diff = job.audit.get("difference_analysis") if isinstance(job.audit, dict) else None
    if isinstance(diff, dict):
        mapping = {
            "full_frame_change_score": "full_frame_change_score",
            "target_region_change_score": "target_region_change_score",
            "non_target_change_score": "non_target_change_score",
            "p95_change_score": "p95_change_score",
            "changed_pixel_ratio_8pct": "changed_pixel_ratio_8pct",
        }
        for source, target in mapping.items():
            value = _float_metric(diff, source)
            if value is not None:
                metrics[target] = value
    else:
        warnings.append("缺少差异热区/变化评分，建议重新生成或人工复核")

    full = metrics.get("full_frame_change_score")
    target = metrics.get("target_region_change_score")
    non_target = metrics.get("non_target_change_score")
    p95 = metrics.get("p95_change_score")
    changed_ratio = metrics.get("changed_pixel_ratio_8pct")

    if non_target is not None:
        if non_target >= thresholds["reject_non_target_min"]:
            blockers.append(f"框外变化 {non_target:.1f} 过高，疑似全脸或背景被改动")
        elif non_target >= thresholds["approve_non_target_max"]:
            warnings.append(f"框外变化 {non_target:.1f} 偏高，需要确认非目标区域是否被美化")
        else:
            passes.append(f"框外变化 {non_target:.1f}，低于可通过阈值")
    if full is not None:
        if full >= thresholds["reject_full_min"]:
            blockers.append(f"全脸/全图变化 {full:.1f} 过高，不建议使用")
        elif full >= thresholds["approve_full_max"]:
            warnings.append(f"全脸/全图变化 {full:.1f} 偏高，需要人工复核")
        else:
            passes.append(f"全脸/全图变化 {full:.1f}，整体稳定")
    if p95 is not None:
        if p95 >= thresholds["reject_p95_min"]:
            blockers.append(f"P95 变化 {p95:.1f} 过高，存在局部大幅改动")
        elif p95 >= thresholds["approve_p95_max"]:
            warnings.append(f"P95 变化 {p95:.1f} 偏高，请查看差异热区")
        else:
            passes.append(f"P95 变化 {p95:.1f}，未见大面积异常")
    if changed_ratio is not None:
        pct = changed_ratio * 100
        if changed_ratio >= thresholds["reject_changed_ratio_min"]:
            blockers.append(f"8%+ 变化像素占比 {pct:.1f}% 过高，疑似非目标区域扩散")
        elif changed_ratio >= thresholds["approve_changed_ratio_max"]:
            warnings.append(f"8%+ 变化像素占比 {pct:.1f}% 偏高，需要复核")
        else:
            passes.append(f"8%+ 变化像素占比 {pct:.1f}%，扩散较低")
    if target is not None:
        if target < thresholds["approve_target_min"]:
            warnings.append(f"目标区域变化 {target:.1f} 偏低，增强效果可能不明显")
        else:
            passes.append(f"目标区域变化 {target:.1f}，能看到局部处理")

    if blockers:
        verdict = "rejected"
        label = "建议拒绝"
        severity = "block"
    elif warnings:
        verdict = "needs_recheck"
        label = "建议复核"
        severity = "review"
    else:
        verdict = "approved"
        label = "通过候选"
        severity = "ok"

    return {
        "recommended_verdict": verdict,
        "label": label,
        "severity": severity,
        "policy_version": policy["version"],
        "policy_name": policy["name"],
        "can_approve": not blockers and job.status in {"done", "done_with_issues"} and _simulation_file_exists(job, "ai_after_simulation"),
        "blocking_reasons": blockers,
        "warning_reasons": warnings,
        "passing_reasons": passes,
        "metrics": metrics,
        "thresholds": thresholds,
    }


def _simulation_row_to_model(row: sqlite3.Row, policy: dict[str, Any] | None = None) -> SimulationJob:
    job = SimulationJob(
        id=int(row["id"]),
        group_id=row["group_id"],
        case_id=row["case_id"],
        status=row["status"],
        focus_targets=_json_field(row, "focus_targets_json", []),
        policy=_json_field(row, "policy_json", {}),
        model_plan=_json_field(row, "model_plan_json", {}),
        input_refs=_json_field(row, "input_refs_json", []),
        output_refs=_json_field(row, "output_refs_json", []),
        available_files=_simulation_available_files(row),
        watermarked=bool(row["watermarked"]),
        audit=_json_field(row, "audit_json", {}),
        error_message=row["error_message"],
        review_status=row["review_status"] if "review_status" in row.keys() else None,
        reviewer=row["reviewer"] if "reviewer" in row.keys() else None,
        review_note=row["review_note"] if "review_note" in row.keys() else None,
        reviewed_at=row["reviewed_at"] if "reviewed_at" in row.keys() else None,
        can_publish=bool(row["can_publish"]) if "can_publish" in row.keys() else False,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
    job.review_decision = _simulation_review_decision(job, policy)
    return job


_SIMULATION_QUEUE_STATUSES = {
    "review_required",
    "all",
    "done",
    "done_with_issues",
    "failed",
    "reviewed",
    "approved",
    "needs_recheck",
    "rejected",
    "publishable",
    "not_publishable",
}


def _simulation_queue_condition(status: str) -> tuple[str, list[Any]]:
    base = [
        "(c.id IS NULL OR c.trashed_at IS NULL)",
        "s.status IN ('done', 'done_with_issues', 'failed')",
    ]
    params: list[Any] = []
    if status == "review_required":
        base.append(
            """
            (
              s.status IN ('done', 'done_with_issues', 'failed')
              OR COALESCE(s.can_publish, 0) = 0
            )
            """
        )
        base.append("(s.review_status IS NULL OR s.review_status = 'needs_recheck')")
    elif status in {"done", "done_with_issues", "failed"}:
        base.append("s.status = ?")
        params.append(status)
    elif status == "reviewed":
        base.append("s.review_status IS NOT NULL")
    elif status in {"approved", "needs_recheck", "rejected"}:
        base.append("s.review_status = ?")
        params.append(status)
    elif status == "publishable":
        base.append("s.can_publish = 1")
    elif status == "not_publishable":
        base.append("COALESCE(s.can_publish, 0) = 0")
    elif status != "all":
        raise HTTPException(400, f"status must be one of {sorted(_SIMULATION_QUEUE_STATUSES)}")
    return " AND ".join(f"({item})" for item in base), params


def _simulation_issue_summary(job: SimulationJob) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    warnings: list[str] = []
    if job.error_message:
        issues.append(job.error_message)
    if job.status == "done_with_issues":
        warnings.append("AI 增强已生成，但存在水印、审计或模型链路问题，需要人工复核")
    if not job.watermarked:
        warnings.append("AI 增强图未确认水印，不能直接发布")
    focus_regions = job.model_plan.get("focus_regions") if isinstance(job.model_plan, dict) else None
    if not focus_regions:
        warnings.append("缺少框选区域，增强依据不符合受控策略")
    diff = job.audit.get("difference_analysis") if isinstance(job.audit, dict) else None
    if isinstance(diff, dict):
        non_target = diff.get("non_target_change_score")
        try:
            if non_target is not None and float(non_target) >= 8:
                warnings.append(f"非目标区域变化评分 {float(non_target):.1f}，需复核是否过度美化")
        except (TypeError, ValueError):
            pass
    decision = job.review_decision if isinstance(job.review_decision, dict) else {}
    issues.extend(str(x) for x in decision.get("blocking_reasons", []) if str(x))
    warnings.extend(str(x) for x in decision.get("warning_reasons", []) if str(x))
    if job.review_status == "needs_recheck":
        warnings.append("上次审核要求复检")
    return list(dict.fromkeys(issues))[:6], list(dict.fromkeys(warnings))[:8]


def _inc(counter: dict[str, int], key: Any) -> None:
    name = str(key or "unknown")
    counter[name] = counter.get(name, 0) + 1


def _code_version_summary() -> dict[str, Any]:
    repo = Path(__file__).resolve().parents[2]
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            timeout=1.5,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--short"],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            timeout=1.5,
        ).stdout.strip()
        return {
            "repo": str(repo),
            "commit": rev or "unknown",
            "dirty": bool(dirty),
            "dirty_file_count": len([line for line in dirty.splitlines() if line.strip()]),
        }
    except Exception:
        return {"repo": str(repo), "commit": "unknown", "dirty": None, "dirty_file_count": None}


def _skill_metadata_by_file_for_report(raw: str | None) -> dict[str, dict[str, Any]]:
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        parsed = []
    if not isinstance(parsed, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        for key in (item.get("filename"), item.get("relative_path")):
            if key:
                out[str(key)] = item
                out[Path(str(key)).name] = item
    return out


def _classification_baseline(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id, abs_path, meta_json, skill_image_metadata_json
        FROM cases
        WHERE trashed_at IS NULL
        """
    ).fetchall()
    totals = {
        "case_count": len(rows),
        "source_image_count": 0,
        "classified_count": 0,
        "needs_manual_count": 0,
        "low_confidence_count": 0,
        "manual_override_count": 0,
        "reviewed_count": 0,
        "render_excluded_count": 0,
        "needs_repick_count": 0,
        "completion_rate": 0.0,
    }
    for row in rows:
        meta = _json_field(row, "meta_json", {})
        if not isinstance(meta, dict):
            meta = {}
        raw_files = _case_image_files_from_meta(meta)
        image_files = source_images.existing_source_image_files(str(row["abs_path"] or ""), raw_files)["existing"]
        if not image_files:
            continue
        overrides = _fetch_image_overrides(conn, int(row["id"]))
        review_states = _image_review_states_from_meta(meta)
        metadata_by_file = _skill_metadata_by_file_for_report(row["skill_image_metadata_json"])
        for filename in [str(item) for item in image_files]:
            totals["source_image_count"] += 1
            override = overrides.get(filename) or overrides.get(Path(filename).name)
            metadata = metadata_by_file.get(filename) or metadata_by_file.get(Path(filename).name)
            phase, _phase_source = _metadata_phase(metadata, filename)
            view, _view_source = _metadata_view(metadata, filename)
            if override and override.get("phase") in _ALLOWED_OVERRIDE_PHASES:
                phase = str(override["phase"])
            if override and override.get("view") in _ALLOWED_OVERRIDE_VIEWS:
                view = str(override["view"])
            manual = bool(override and (override.get("phase") or override.get("view")))
            state = review_states.get(filename) or review_states.get(Path(filename).name) or {}
            if manual:
                totals["manual_override_count"] += 1
            if state.get("verdict"):
                totals["reviewed_count"] += 1
            if state.get("render_excluded") or state.get("verdict") == "excluded":
                totals["render_excluded_count"] += 1
                continue
            if state.get("verdict") == "needs_repick":
                totals["needs_repick_count"] += 1
            if phase in _ALLOWED_OVERRIDE_PHASES and view in _ALLOWED_OVERRIDE_VIEWS:
                totals["classified_count"] += 1
            else:
                totals["needs_manual_count"] += 1
            confidence = None
            if isinstance(metadata, dict) and metadata.get("angle_confidence") is not None:
                try:
                    confidence = float(metadata.get("angle_confidence"))
                except (TypeError, ValueError):
                    confidence = None
            if confidence is not None and confidence < 0.65 and not manual:
                totals["low_confidence_count"] += 1
    total_images = int(totals["source_image_count"])
    if total_images:
        totals["completion_rate"] = round(int(totals["classified_count"]) / total_images, 4)
    return totals


def _render_artifact_metrics(render_rows: list[sqlite3.Row]) -> dict[str, Any]:
    output_rows = [row for row in render_rows if row["status"] in {"done", "done_with_issues"}]
    visible = 0
    missing = 0
    manifest_visible = 0
    for row in output_rows:
        output_path = str(row["output_path"] or "")
        manifest_path = str(row["manifest_path"] or "")
        if output_path and Path(output_path).is_file():
            visible += 1
        else:
            missing += 1
        if manifest_path and Path(manifest_path).is_file():
            manifest_visible += 1
    total = len(output_rows)
    return {
        "output_artifact_count": total,
        "final_board_visible_count": visible,
        "final_board_missing_count": missing,
        "final_board_visible_rate": round(visible / total, 4) if total else None,
        "manifest_visible_count": manifest_visible,
    }


def _render_row_recency_key(row: sqlite3.Row) -> tuple[str, int]:
    return (str(row["finished_at"] or row["enqueued_at"] or ""), int(row["id"] or 0))


def _current_latest_render_rows(render_rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    latest_by_case: dict[int, sqlite3.Row] = {}
    for row in render_rows:
        case_id = int(row["case_id"])
        current = latest_by_case.get(case_id)
        if current is None or _render_row_recency_key(row) > _render_row_recency_key(current):
            latest_by_case[case_id] = row
    return sorted(latest_by_case.values(), key=_render_row_recency_key, reverse=True)


def _archived_render_rows(render_rows: list[sqlite3.Row], current_rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    current_ids = {int(row["id"]) for row in current_rows}
    return [row for row in render_rows if int(row["id"]) not in current_ids]


def _render_actionable_warning_count(row: sqlite3.Row) -> int:
    try:
        metrics = json.loads(row["metrics_json"] or "{}")
    except (TypeError, ValueError):
        return 0
    if not isinstance(metrics, dict):
        return 0
    buckets = metrics.get("warning_buckets")
    if isinstance(buckets, dict):
        try:
            return int(buckets.get("actionable_count") or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(metrics.get("actionable_warning_count") or 0)
    except (TypeError, ValueError):
        return 0


def _current_version_baseline(render_rows: list[sqlite3.Row]) -> dict[str, Any]:
    current_rows = _current_latest_render_rows(render_rows)
    archived_rows = _archived_render_rows(render_rows, current_rows)
    recent = current_rows[:30]
    by_status: dict[str, int] = {}
    by_quality_status: dict[str, int] = {}
    for row in recent:
        _inc(by_status, row["status"])
        _inc(by_quality_status, row["quality_status"] or row["status"])
    executable_total = sum(by_status.get(key, 0) for key in ("done", "done_with_issues", "failed"))
    generated_total = by_status.get("done", 0) + by_status.get("done_with_issues", 0)
    publishable = sum(1 for row in recent if row["can_publish"])
    review_required = sum(
        1
        for row in recent
        if row["status"] in {"failed", "blocked", "done_with_issues"}
        or row["quality_status"] in {"blocked", "done_with_issues"}
        or (row["can_publish"] == 0 and row["review_verdict"] != "approved")
    )
    actionable = sum(_render_actionable_warning_count(row) for row in recent)
    artifact_metrics = _render_artifact_metrics(recent)
    return {
        "scope": "current_latest_per_case_recent_30",
        "sample_size": len(recent),
        "current_latest_case_count": len(current_rows),
        "historical_archived_count": len(archived_rows),
        "by_status": by_status,
        "by_quality_status": by_quality_status,
        "blocked_as_guardrail": by_status.get("blocked", 0),
        "generated_total": generated_total,
        "renderer_failure_count": by_status.get("failed", 0),
        "review_required_count": review_required,
        "actionable_warning_count": actionable,
        "renderer_success_rate_excluding_blocked": round(generated_total / executable_total, 4) if executable_total else None,
        "clean_done_rate": round(by_status.get("done", 0) / generated_total, 4) if generated_total else None,
        "done_with_issues_rate": round(by_status.get("done_with_issues", 0) / generated_total, 4) if generated_total else None,
        "publishable_rate": round(publishable / len(recent), 4) if recent else None,
        "artifact_visibility": artifact_metrics,
    }


def _delivery_baseline(
    *,
    render_rows: list[sqlite3.Row],
    classification_counts: dict[str, Any],
    root_causes: dict[str, Any],
) -> dict[str, Any]:
    current_rows = _current_latest_render_rows(render_rows)
    recent = current_rows[:30]
    by_status: dict[str, int] = {}
    by_quality_status: dict[str, int] = {}
    for row in recent:
        _inc(by_status, row["status"])
        _inc(by_quality_status, row["quality_status"] or row["status"])
    blocked = int(by_status.get("blocked", 0))
    failed = int(by_status.get("failed", 0))
    done = int(by_status.get("done", 0))
    done_with_issues = int(by_status.get("done_with_issues", 0))
    generated = done + done_with_issues
    executable = max(0, len(recent) - blocked)
    publishable = sum(1 for row in recent if row["can_publish"])
    artifact_metrics = _render_artifact_metrics(recent)
    top_causes = root_causes.get("top_causes") if isinstance(root_causes.get("top_causes"), list) else []
    return {
        "scope": "current_latest_per_case_delivery_v1",
        "sample_size": len(recent),
        "current_latest_case_count": len(current_rows),
        "renderer": {
            "terminal_count": len(recent),
            "generated_count": generated,
            "failed_count": failed,
            "blocked_guardrail_count": blocked,
            "blocked_is_guardrail": True,
            "failed_rate_excluding_blocked": round(failed / executable, 4) if executable else None,
            "success_rate_excluding_blocked": round(generated / executable, 4) if executable else None,
        },
        "publishability": {
            "publishable_count": publishable,
            "publishable_rate": round(publishable / len(recent), 4) if recent else None,
            "final_board_visible_rate": artifact_metrics.get("final_board_visible_rate"),
            "final_board_missing_count": artifact_metrics.get("final_board_missing_count"),
        },
        "quality": {
            "done_count": done,
            "done_with_issues_count": done_with_issues,
            "done_with_issues_rate": round(done_with_issues / generated, 4) if generated else None,
            "actionable_warning_count": sum(_render_actionable_warning_count(row) for row in recent),
        },
        "classification": {
            "source_image_count": classification_counts.get("source_image_count", 0),
            "classified_count": classification_counts.get("classified_count", 0),
            "needs_manual_count": classification_counts.get("needs_manual_count", 0),
            "low_confidence_count": classification_counts.get("low_confidence_count", 0),
            "completion_rate": classification_counts.get("completion_rate"),
        },
        "root_causes": {
            "scope": root_causes.get("scope"),
            "top_causes": top_causes[:8],
            "by_category": root_causes.get("by_category") or {},
        },
    }


_ROOT_CAUSE_META = {
    "classification_open": {
        "label": "分类未闭环",
        "category": "classification",
        "severity": "block",
        "action": "进入照片分类队列补齐阶段、角度和可用性",
        "href": "/images?status=review_needed",
    },
    "low_confidence": {
        "label": "低置信待复核",
        "category": "classification",
        "severity": "review",
        "action": "复核低置信图片，不能静默进入正式出图",
        "href": "/images?status=low_confidence",
    },
    "missing_render_slots": {
        "label": "缺三联槽位",
        "category": "source_group",
        "severity": "block",
        "action": "补齐正面、45°、侧面术前术后配对",
        "href": "/images?status=missing_view",
    },
    "source_directory": {
        "label": "源目录阻断",
        "category": "source_directory",
        "severity": "block",
        "action": "处理非案例源目录、真实源图不足或源文件缺失",
        "href": "/source-blockers",
    },
    "output_invisible": {
        "label": "成品图不可见",
        "category": "artifact",
        "severity": "block",
        "action": "修复输出路径或 final-board HTTP 展示链路",
        "href": "/quality",
    },
    "renderer_failed": {
        "label": "renderer 失败",
        "category": "renderer",
        "severity": "block",
        "action": "查看失败 job 日志并修复 renderer 异常",
        "href": "/quality",
    },
    "pair_quality": {
        "label": "配对姿态/方向风险",
        "category": "quality",
        "severity": "review",
        "action": "回到源组候选重选姿态更接近的术前术后配对",
        "href": "/quality",
    },
    "face_quality": {
        "label": "面检/轮廓复核",
        "category": "quality",
        "severity": "review",
        "action": "复核面部检测或侧面轮廓，必要时换片",
        "href": "/quality",
    },
    "composition_review": {
        "label": "构图需复核",
        "category": "quality",
        "severity": "review",
        "action": "复核构图，必要时重新选片或重出",
        "href": "/quality",
    },
    "ai_review": {
        "label": "AI 增强审核待处理",
        "category": "ai_review",
        "severity": "review",
        "action": "进入 AI 增强审核队列，按策略通过/复核/拒绝",
        "href": "/quality",
    },
}


def _root_cause_bucket(
    buckets: dict[str, dict[str, Any]],
    code: str,
    *,
    count: int = 1,
    unit: str = "job",
    case_id: int | None = None,
    job_id: int | None = None,
    example: str | None = None,
) -> None:
    meta = _ROOT_CAUSE_META.get(code, {})
    bucket = buckets.setdefault(
        code,
        {
            "code": code,
            "label": meta.get("label", code),
            "category": meta.get("category", "other"),
            "severity": meta.get("severity", "review"),
            "action": meta.get("action", "人工复核"),
            "href": meta.get("href", "/quality"),
            "count": 0,
            "unit": unit,
            "job_impact_count": 0,
            "case_ids": [],
            "job_ids": [],
            "examples": [],
        },
    )
    if bucket.get("unit") == "image" and unit == "job":
        bucket["job_impact_count"] += int(count)
    else:
        bucket["count"] += int(count)
    if unit == "image" and bucket.get("unit") != "image":
        bucket["unit"] = unit
    if case_id is not None and case_id not in bucket["case_ids"]:
        bucket["case_ids"].append(case_id)
    if job_id is not None and job_id not in bucket["job_ids"]:
        bucket["job_ids"].append(job_id)
    if example and example not in bucket["examples"]:
        bucket["examples"].append(example)


def _root_cause_from_action(code: str) -> str | None:
    mapping = {
        "open_classification_workbench": "classification_open",
        "complete_source_group_slots": "missing_render_slots",
        "reselect_pair": "pair_quality",
        "review_face_detection": "face_quality",
        "fix_source_directory": "source_directory",
        "review_composition": "composition_review",
    }
    return mapping.get(code)


def _root_cause_from_text(text: str) -> str | None:
    value = text.strip()
    if not value:
        return None
    if any(token in value for token in ("未闭环", "待补充", "低置信", "分类")):
        return "classification_open"
    if any(token in value for token in ("缺槽位", "三联", "配齐")):
        return "missing_render_slots"
    if any(token in value for token in ("源照片", "源文件", "真实源图", "源目录", "no_real_source", "missing_source")):
        return "source_directory"
    if any(token in value for token in ("姿态差", "方向不一致", "pose_delta")):
        return "pair_quality"
    if any(token in value for token in ("面部检测", "正脸检测", "face_detection")):
        return "face_quality"
    if any(token in value for token in ("构图", "composition")):
        return "composition_review"
    return None


def _quality_root_causes(
    *,
    classification_counts: dict[str, Any],
    render_rows: list[sqlite3.Row],
    sim_counts: dict[str, Any],
) -> dict[str, Any]:
    buckets: dict[str, dict[str, Any]] = {}
    needs_manual = int(classification_counts.get("needs_manual_count") or 0)
    low_conf = int(classification_counts.get("low_confidence_count") or 0)
    if needs_manual:
        _root_cause_bucket(buckets, "classification_open", count=needs_manual, unit="image")
    if low_conf:
        _root_cause_bucket(buckets, "low_confidence", count=low_conf, unit="image")

    latest_render_rows: list[sqlite3.Row] = []
    seen_render_cases: set[int] = set()
    for row in render_rows:
        case_id = int(row["case_id"])
        if case_id in seen_render_cases:
            continue
        seen_render_cases.add(case_id)
        latest_render_rows.append(row)

    for row in latest_render_rows[:30]:
        job_id = int(row["id"])
        case_id = int(row["case_id"])
        metrics = _json_field(row, "metrics_json", {})
        if not isinstance(metrics, dict):
            metrics = {}
        row_causes: set[str] = set()
        if row["status"] == "failed":
            row_causes.add("renderer_failed")
        if row["status"] in {"done", "done_with_issues"} and not (row["output_path"] and Path(str(row["output_path"])).is_file()):
            row_causes.add("output_invisible")
        for action in metrics.get("action_suggestions") or []:
            if isinstance(action, dict):
                mapped = _root_cause_from_action(str(action.get("code") or ""))
                if mapped:
                    row_causes.add(mapped)
        display_warnings = metrics.get("display_warnings")
        if display_warnings is None:
            display_warnings = metrics.get("warnings") or []
        text_sources = [
            row["error_message"],
            *(metrics.get("blocking_issues") or []),
            *(display_warnings or []),
        ]
        for text in text_sources:
            mapped = _root_cause_from_text(str(text or ""))
            if mapped:
                row_causes.add(mapped)
        for code in sorted(row_causes):
            _root_cause_bucket(
                buckets,
                code,
                case_id=case_id,
                job_id=job_id,
                example=f"case #{case_id} job #{job_id}",
            )

    if int(sim_counts.get("pending") or 0):
        _root_cause_bucket(buckets, "ai_review", count=int(sim_counts.get("pending") or 0), unit="job")

    top = sorted(
        buckets.values(),
        key=lambda item: (
            0 if item.get("severity") == "block" else 1,
            -int(item.get("count") or 0),
            str(item.get("code") or ""),
        ),
    )
    for item in top:
        item["case_ids"] = item["case_ids"][:12]
        item["job_ids"] = item["job_ids"][:12]
        item["examples"] = item["examples"][:6]
    by_category: dict[str, int] = {}
    for item in top:
        _inc(by_category, item["category"])
    return {
        "scope": "current_latest_per_case_plus_classification_backlog",
        "top_causes": top[:12],
        "by_category": by_category,
    }


def _quality_report(conn: sqlite3.Connection, limit: int) -> dict[str, Any]:
    render_rows = conn.execute(
        """
        SELECT
          j.id,
          j.case_id,
          j.status,
          j.brand,
          j.template,
          j.enqueued_at,
          j.finished_at,
          j.output_path,
          j.manifest_path,
          j.error_message,
          c.abs_path,
          c.customer_raw,
          rq.quality_status,
          rq.quality_score,
          rq.can_publish,
          rq.review_verdict,
          rq.metrics_json
        FROM render_jobs j
        JOIN cases c ON c.id = j.case_id
        LEFT JOIN render_quality rq ON rq.render_job_id = j.id
        WHERE c.trashed_at IS NULL
          AND j.status IN ('done', 'done_with_issues', 'blocked', 'failed')
        ORDER BY COALESCE(j.finished_at, j.enqueued_at) DESC, j.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    render_counts: dict[str, Any] = {
        "total": len(render_rows),
        "by_status": {},
        "by_quality_status": {},
        "by_review_verdict": {},
        "publishable": 0,
        "not_publishable": 0,
        "reviewed": 0,
        "avg_quality_score": None,
        "artifact_visibility": {},
        "current_version_baseline": {},
    }
    render_score_sum = 0.0
    render_score_count = 0
    render_recent: list[dict[str, Any]] = []
    for row in render_rows:
        _inc(render_counts["by_status"], row["status"])
        _inc(render_counts["by_quality_status"], row["quality_status"] or row["status"])
        if row["review_verdict"]:
            _inc(render_counts["by_review_verdict"], row["review_verdict"])
            render_counts["reviewed"] += 1
        if row["can_publish"]:
            render_counts["publishable"] += 1
        else:
            render_counts["not_publishable"] += 1
        if row["quality_score"] is not None:
            render_score_sum += float(row["quality_score"])
            render_score_count += 1
        if len(render_recent) < 8:
            render_recent.append(
                {
                    "id": row["id"],
                    "case_id": row["case_id"],
                    "status": row["status"],
                    "quality_status": row["quality_status"],
                    "quality_score": row["quality_score"],
                    "can_publish": bool(row["can_publish"]),
                    "review_verdict": row["review_verdict"],
                    "customer_raw": row["customer_raw"],
                    "finished_at": row["finished_at"],
                }
            )
    if render_score_count:
        render_counts["avg_quality_score"] = round(render_score_sum / render_score_count, 1)
    render_counts["artifact_visibility"] = _render_artifact_metrics(render_rows)
    render_counts["current_version_baseline"] = _current_version_baseline(render_rows)

    simulation_rows = conn.execute(
        """
        SELECT s.*, c.abs_path AS case_abs_path, c.customer_raw AS case_customer_raw
        FROM simulation_jobs s
        LEFT JOIN cases c ON c.id = s.case_id
        WHERE (c.id IS NULL OR c.trashed_at IS NULL)
          AND s.status IN ('done', 'done_with_issues', 'failed')
        ORDER BY s.updated_at DESC, s.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    sim_counts: dict[str, Any] = {
        "total": len(simulation_rows),
        "by_status": {},
        "by_review_status": {},
        "by_system_recommendation": {},
        "reviewed": 0,
        "pending": 0,
        "aligned_with_system": 0,
        "manual_override": 0,
        "publishable": 0,
        "not_publishable": 0,
        "avg_full_frame_change": None,
        "avg_non_target_change": None,
        "risk_reasons": {},
    }
    full_sum = 0.0
    full_count = 0
    non_target_sum = 0.0
    non_target_count = 0
    sim_recent: list[dict[str, Any]] = []
    for row in simulation_rows:
        job = _simulation_row_to_model(row)
        decision = job.review_decision if isinstance(job.review_decision, dict) else {}
        recommended = str(decision.get("recommended_verdict") or "unknown")
        _inc(sim_counts["by_status"], job.status)
        _inc(sim_counts["by_system_recommendation"], recommended)
        if job.review_status:
            sim_counts["reviewed"] += 1
            _inc(sim_counts["by_review_status"], job.review_status)
            if job.review_status == recommended:
                sim_counts["aligned_with_system"] += 1
            else:
                sim_counts["manual_override"] += 1
        else:
            sim_counts["pending"] += 1
        if job.can_publish:
            sim_counts["publishable"] += 1
        else:
            sim_counts["not_publishable"] += 1
        metrics = decision.get("metrics") if isinstance(decision.get("metrics"), dict) else {}
        full = metrics.get("full_frame_change_score")
        non_target = metrics.get("non_target_change_score")
        if isinstance(full, (int, float)):
            full_sum += float(full)
            full_count += 1
        if isinstance(non_target, (int, float)):
            non_target_sum += float(non_target)
            non_target_count += 1
        for reason in [*(decision.get("blocking_reasons") or []), *(decision.get("warning_reasons") or [])]:
            _inc(sim_counts["risk_reasons"], reason)
        if len(sim_recent) < 8:
            sim_recent.append(
                {
                    "id": job.id,
                    "case_id": job.case_id,
                    "status": job.status,
                    "review_status": job.review_status,
                    "recommended_verdict": recommended,
                    "decision_label": decision.get("label"),
                    "can_publish": job.can_publish,
                    "customer_raw": row["case_customer_raw"],
                    "updated_at": job.updated_at,
                }
            )
    if full_count:
        sim_counts["avg_full_frame_change"] = round(full_sum / full_count, 3)
    if non_target_count:
        sim_counts["avg_non_target_change"] = round(non_target_sum / non_target_count, 3)

    classification_counts = _classification_baseline(conn)
    root_causes = _quality_root_causes(
        classification_counts=classification_counts,
        render_rows=render_rows,
        sim_counts=sim_counts,
    )
    delivery_baseline = _delivery_baseline(
        render_rows=render_rows,
        classification_counts=classification_counts,
        root_causes=root_causes,
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limit": limit,
        "code_version": _code_version_summary(),
        "policy": simulation_quality.load_ai_review_policy(),
        "delivery_baseline": delivery_baseline,
        "classification": classification_counts,
        "root_causes": root_causes,
        "render": {**render_counts, "recent": render_recent},
        "simulation": {**sim_counts, "recent": sim_recent},
        "totals": {
            "artifacts": len(render_rows) + len(simulation_rows),
            "reviewed": int(render_counts["reviewed"]) + int(sim_counts["reviewed"]),
            "publishable": int(render_counts["publishable"]) + int(sim_counts["publishable"]),
            "not_publishable": int(render_counts["not_publishable"]) + int(sim_counts["not_publishable"]),
            "classification_completion_rate": classification_counts["completion_rate"],
            "final_board_visible_rate": render_counts["artifact_visibility"].get("final_board_visible_rate"),
        },
    }


def _simulation_policy_preview(conn: sqlite3.Connection, payload: dict[str, Any], limit: int) -> dict[str, Any]:
    current_policy = simulation_quality.load_ai_review_policy()
    preview_policy = simulation_quality.preview_ai_review_policy(payload)
    rows = conn.execute(
        """
        SELECT s.*, c.abs_path AS case_abs_path, c.customer_raw AS case_customer_raw
        FROM simulation_jobs s
        LEFT JOIN cases c ON c.id = s.case_id
        WHERE (c.id IS NULL OR c.trashed_at IS NULL)
          AND s.status IN ('done', 'done_with_issues', 'failed')
        ORDER BY s.updated_at DESC, s.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    summary: dict[str, Any] = {
        "total": len(rows),
        "changed_count": 0,
        "review_conflict_count": 0,
        "manual_override_count": 0,
        "by_current": {},
        "by_preview": {},
        "changed_transitions": {},
    }
    items: list[dict[str, Any]] = []
    for row in rows:
        current_job = _simulation_row_to_model(row, current_policy)
        preview_job = _simulation_row_to_model(row, preview_policy)
        current_decision = current_job.review_decision if isinstance(current_job.review_decision, dict) else {}
        preview_decision = preview_job.review_decision if isinstance(preview_job.review_decision, dict) else {}
        current_verdict = str(current_decision.get("recommended_verdict") or "unknown")
        preview_verdict = str(preview_decision.get("recommended_verdict") or "unknown")
        _inc(summary["by_current"], current_verdict)
        _inc(summary["by_preview"], preview_verdict)
        changed = current_verdict != preview_verdict
        if changed:
            summary["changed_count"] += 1
            _inc(summary["changed_transitions"], f"{current_verdict}->{preview_verdict}")
        review_status = current_job.review_status
        if review_status and review_status != preview_verdict:
            summary["review_conflict_count"] += 1
        if review_status and review_status != current_verdict:
            summary["manual_override_count"] += 1
        if changed or len(items) < 8:
            items.append(
                {
                    "id": current_job.id,
                    "case_id": current_job.case_id,
                    "customer_raw": row["case_customer_raw"],
                    "status": current_job.status,
                    "review_status": review_status,
                    "can_publish": current_job.can_publish,
                    "changed": changed,
                    "current": {
                        "recommended_verdict": current_verdict,
                        "label": current_decision.get("label"),
                        "severity": current_decision.get("severity"),
                        "metrics": current_decision.get("metrics") or {},
                        "blocking_reasons": current_decision.get("blocking_reasons") or [],
                        "warning_reasons": current_decision.get("warning_reasons") or [],
                        "passing_reasons": current_decision.get("passing_reasons") or [],
                    },
                    "preview": {
                        "recommended_verdict": preview_verdict,
                        "label": preview_decision.get("label"),
                        "severity": preview_decision.get("severity"),
                        "metrics": preview_decision.get("metrics") or {},
                        "blocking_reasons": preview_decision.get("blocking_reasons") or [],
                        "warning_reasons": preview_decision.get("warning_reasons") or [],
                        "passing_reasons": preview_decision.get("passing_reasons") or [],
                    },
                }
            )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limit": limit,
        "current_policy": current_policy,
        "preview_policy": preview_policy,
        "summary": summary,
        "items": items[:limit],
    }


def _review_simulation_job_by_id(
    job_id: int,
    payload: SimulationJobReviewRequest,
    *,
    case_id: int | None = None,
) -> SimulationJob:
    reviewer = payload.reviewer.strip()
    if not reviewer:
        raise HTTPException(400, "reviewer cannot be blank")
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        if case_id is None:
            row = conn.execute("SELECT * FROM simulation_jobs WHERE id = ?", (job_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM simulation_jobs WHERE id = ? AND case_id = ?",
                (job_id, case_id),
            ).fetchone()
        if not row:
            raise HTTPException(404, "simulation job not found")
        if row["status"] not in {"done", "done_with_issues"}:
            raise HTTPException(400, f"simulation job is not reviewable: {row['status']}")
        job = _simulation_row_to_model(row)
        decision = job.review_decision if isinstance(job.review_decision, dict) else {}
        blocking_reasons = [str(x) for x in decision.get("blocking_reasons", []) if str(x)]
        if payload.verdict == "approved" and not bool(decision.get("can_approve")):
            reason = "；".join(blocking_reasons[:3]) or "AI 增强任务未满足通过条件"
            raise HTTPException(400, f"AI enhancement cannot be approved: {reason}")
        note = payload.note.strip() if payload.note else None
        if not note and payload.verdict != "approved":
            note = "；".join(
                [
                    *blocking_reasons[:3],
                    *[str(x) for x in decision.get("warning_reasons", []) if str(x)][:3],
                ]
            ) or None
        can_publish = (
            payload.verdict == "approved"
            and bool(row["watermarked"])
            and bool(decision.get("can_approve"))
        )
        audit_payload = job.audit if isinstance(job.audit, dict) else {}
        history = audit_payload.get("review_history")
        if not isinstance(history, list):
            history = []
        review_event = {
            "verdict": payload.verdict,
            "reviewer": reviewer,
            "note": note,
            "reviewed_at": now,
            "can_publish": can_publish,
            "decision_snapshot": decision,
        }
        audit_payload = {
            **audit_payload,
            "review_decision": decision,
            "review_history": [*history[-19:], review_event],
        }
        audit_payload = stress.tag_payload(audit_payload)
        conn.execute(
            """
            UPDATE simulation_jobs
            SET review_status = ?,
                reviewer = ?,
                review_note = ?,
                reviewed_at = ?,
                can_publish = ?,
                audit_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                payload.verdict,
                reviewer,
                note,
                now,
                1 if can_publish else 0,
                json.dumps(audit_payload, ensure_ascii=False),
                now,
                job_id,
            ),
        )
        updated = conn.execute("SELECT * FROM simulation_jobs WHERE id = ?", (job_id,)).fetchone()
    return _simulation_row_to_model(updated)


def _simulation_output_file(job: sqlite3.Row, kind: str) -> Path:
    kind = _simulation_kind(kind)
    refs = _json_field(job, "output_refs_json", [])
    match = next((ref for ref in refs if isinstance(ref, dict) and ref.get("kind") == kind), None)
    if match:
        target = Path(str(match.get("path") or "")).resolve()
        safe = _safe_simulation_image_path(int(job["id"]), target)
        if safe is None:
            safe_root = ai_generation_adapter.simulation_job_dir(int(job["id"])).resolve()
            try:
                target.relative_to(safe_root)
            except ValueError:
                raise HTTPException(403, "simulation output is not served from the audit directory")
            if not target.is_file():
                raise HTTPException(404, "simulation output file missing")
            raise HTTPException(400, "simulation output is not an image")
        return safe
    target = _simulation_fallback_file(int(job["id"]), kind)
    if target is None:
        raise HTTPException(404, "simulation output not found")
    return target


router = APIRouter(prefix="/api/cases", tags=["cases"])


@router.get("/ps-image-model-options", response_model=PsImageModelOptionsResponse)
def ps_image_model_options() -> PsImageModelOptionsResponse:
    return PsImageModelOptionsResponse(**ai_generation_adapter.get_ps_image_model_options())


@router.get("/simulation-jobs/quality-queue")
def list_simulation_quality_queue(
    status: str = Query("review_required"),
    recommendation: str | None = Query(None),
    limit: int = Query(100, ge=1, le=200),
) -> dict[str, Any]:
    """Central queue for AI after-image simulation QA.

    This is intentionally separate from render_quality: AI-enhanced artifacts
    are never treated as real case renders, even when shown in the same QA UI.
    """
    status = status.strip() or "review_required"
    where_sql, params = _simulation_queue_condition(status)
    recommendation = (recommendation or "").strip() or None
    allowed_recommendations = {"approved", "needs_recheck", "rejected", "manual_override", "aligned"}
    if recommendation and recommendation not in allowed_recommendations:
        raise HTTPException(400, f"recommendation must be one of {sorted(allowed_recommendations)}")
    scan_limit = 2000 if recommendation else limit
    with db.connect() as conn:
        total = conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM simulation_jobs s
            LEFT JOIN cases c ON c.id = s.case_id
            WHERE {where_sql}
            """,
            params,
        ).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT
              s.*,
              c.abs_path AS case_abs_path,
              c.customer_raw AS case_customer_raw,
              cu.canonical_name AS case_customer_canonical
            FROM simulation_jobs s
            LEFT JOIN cases c ON c.id = s.case_id
            LEFT JOIN customers cu ON cu.id = c.customer_id
            WHERE {where_sql}
            ORDER BY
              CASE
                WHEN s.status = 'failed' THEN 0
                WHEN s.status = 'done_with_issues' THEN 1
                ELSE 2
              END,
              s.updated_at DESC,
              s.id DESC
            LIMIT ?
            """,
            [*params, scan_limit],
        ).fetchall()
        counts: dict[str, int] = {}
        for row in conn.execute(
            """
            SELECT s.status AS status, COUNT(*) AS n
            FROM simulation_jobs s
            LEFT JOIN cases c ON c.id = s.case_id
            WHERE (c.id IS NULL OR c.trashed_at IS NULL)
              AND s.status IN ('done', 'done_with_issues', 'failed')
            GROUP BY s.status
            """
        ).fetchall():
            counts[str(row["status"])] = int(row["n"])
        for row in conn.execute(
            """
            SELECT s.review_status AS status, COUNT(*) AS n
            FROM simulation_jobs s
            LEFT JOIN cases c ON c.id = s.case_id
            WHERE (c.id IS NULL OR c.trashed_at IS NULL)
              AND s.review_status IS NOT NULL
            GROUP BY s.review_status
            """
        ).fetchall():
            counts[str(row["status"])] = int(row["n"])
        counts["reviewed"] = sum(
            counts.get(key, 0) for key in ("approved", "needs_recheck", "rejected")
        )

    items: list[dict[str, Any]] = []
    matched_total = 0
    for row in rows:
        job = _simulation_row_to_model(row)
        decision = job.review_decision if isinstance(job.review_decision, dict) else {}
        recommended = str(decision.get("recommended_verdict") or "unknown")
        if recommendation == "manual_override":
            if not job.review_status or job.review_status == recommended:
                continue
        elif recommendation == "aligned":
            if not job.review_status or job.review_status != recommended:
                continue
        elif recommendation and recommended != recommendation:
            continue
        matched_total += 1
        if len(items) >= limit:
            continue
        issues, warnings = _simulation_issue_summary(job)
        case_id = row["case_id"]
        items.append(
            {
                "job": job.model_dump(),
                "case": (
                    {
                        "id": case_id,
                        "abs_path": row["case_abs_path"],
                        "customer_raw": row["case_customer_raw"],
                        "customer_canonical": row["case_customer_canonical"],
                    }
                    if case_id is not None
                    else None
                ),
                "reviewable": job.status in {"done", "done_with_issues"},
                "issue_summary": issues,
                "warning_summary": warnings,
            }
        )
    if recommendation:
        total = matched_total
    return {
        "items": items,
        "total": total,
        "counts": counts,
        "status": status,
        "recommendation": recommendation,
        "limit": limit,
    }


@router.get("/simulation-jobs/review-policy")
def get_simulation_review_policy() -> dict[str, Any]:
    return simulation_quality.load_ai_review_policy()


@router.put("/simulation-jobs/review-policy")
def put_simulation_review_policy(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return simulation_quality.save_ai_review_policy(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/simulation-jobs/review-policy/preview")
def preview_simulation_review_policy(
    payload: dict[str, Any],
    limit: int = Query(500, ge=1, le=2000),
) -> dict[str, Any]:
    try:
        simulation_quality.preview_ai_review_policy(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with db.connect() as conn:
        return _simulation_policy_preview(conn, payload, limit)


@router.get("/quality-report")
def quality_report(limit: int = Query(300, ge=1, le=2000)) -> dict[str, Any]:
    with db.connect() as conn:
        return _quality_report(conn, limit)


@router.get("/simulation-jobs/{job_id}/file")
def simulation_job_file_by_id(
    job_id: int,
    kind: str = Query("ai_after_simulation"),
) -> FileResponse:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM simulation_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "simulation job not found")
    return FileResponse(_simulation_output_file(row, kind))


@router.post("/simulation-jobs/{job_id}/review", response_model=SimulationJob)
def review_simulation_job_by_id(
    job_id: int,
    payload: SimulationJobReviewRequest,
) -> SimulationJob:
    return _review_simulation_job_by_id(job_id, payload)


def _row_to_summary(row: sqlite3.Row, customer_canonical: str | None = None) -> CaseSummary:
    # B2: blocking_issues_json may be v1 strings or v2 objects; merge_codes handles both.
    auto_raw = json.loads(row["blocking_issues_json"] or "[]")
    manual_raw = json.loads(row["manual_blocking_issues_json"] or "[]") if "manual_blocking_issues_json" in row.keys() else []
    effective_blocking = issue_translator.merge_codes([*auto_raw, *manual_raw])
    auto_cat = row["category"]
    auto_tier = row["template_tier"]
    manual_cat = row["manual_category"] if "manual_category" in row.keys() else None
    manual_tier = row["manual_template_tier"] if "manual_template_tier" in row.keys() else None
    tags = json.loads(row["tags_json"] or "[]") if "tags_json" in row.keys() and row["tags_json"] else []
    return CaseSummary(
        id=row["id"],
        abs_path=row["abs_path"],
        customer_raw=row["customer_raw"],
        customer_id=row["customer_id"],
        customer_canonical=customer_canonical,
        auto_category=auto_cat,
        auto_template_tier=auto_tier,
        manual_category=manual_cat,
        manual_template_tier=manual_tier,
        category=manual_cat or auto_cat,
        template_tier=manual_tier or auto_tier,
        source_count=row["source_count"],
        labeled_count=row["labeled_count"],
        blocking_issue_count=len(effective_blocking),
        notes=row["notes"] if "notes" in row.keys() else None,
        tags=tags,
        review_status=row["review_status"] if "review_status" in row.keys() else None,
        reviewed_at=row["reviewed_at"] if "reviewed_at" in row.keys() else None,
        held_until=row["held_until"] if "held_until" in row.keys() else None,
        hold_reason=row["hold_reason"] if "hold_reason" in row.keys() else None,
        latest_render_status=row["latest_render_status"] if "latest_render_status" in row.keys() else None,
        latest_render_quality_status=row["latest_render_quality_status"] if "latest_render_quality_status" in row.keys() else None,
        latest_render_quality_score=row["latest_render_quality_score"] if "latest_render_quality_score" in row.keys() else None,
        last_modified=row["last_modified"],
        indexed_at=row["indexed_at"],
    )


@router.get("", response_model=CaseListResponse)
def list_cases(
    category: str | None = None,
    tier: str | None = None,
    customer_id: int | None = None,
    review_status: str | None = None,
    q: str | None = None,
    tag: str | None = None,
    since: str | None = None,
    blocking: str | None = None,
    include_held: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=2000),
) -> CaseListResponse:
    where: list[str] = ["c.trashed_at IS NULL"]
    params: list[Any] = []
    if category:
        where.append("COALESCE(c.manual_category, c.category) = ?")
        params.append(category)
    if tier:
        where.append("COALESCE(c.manual_template_tier, c.template_tier) = ?")
        params.append(tier)
    if customer_id is not None:
        where.append("c.customer_id = ?")
        params.append(customer_id)
    if review_status:
        if review_status == "unreviewed":
            where.append("(c.review_status IS NULL OR c.review_status = 'pending')")
        else:
            where.append("c.review_status = ?")
            params.append(review_status)
    if q and q.strip():
        like = f"%{q.strip()}%"
        where.append(
            "("
            "c.abs_path LIKE ? OR "
            "COALESCE(cu.canonical_name, '') LIKE ? OR "
            "COALESCE(c.customer_raw, '') LIKE ? OR "
            "COALESCE(c.notes, '') LIKE ?"
            ")"
        )
        params.extend([like, like, like, like])

    if tag and tag.strip():
        # tags_json 是 JSON 数组,LIKE '%"<tag>"%' 精确匹配 token(避免子串误匹配)
        where.append("c.tags_json LIKE ?")
        params.append(f'%"{tag.strip()}"%')

    if since == "today":
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        where.append("c.indexed_at >= ?")
        params.append(today_start.isoformat())

    if blocking == "open":
        # 用 json_array_length 判定数组非空,避免依赖 "无空白序列化" 的隐式不变量
        where.append("(c.blocking_issues_json IS NOT NULL AND json_array_length(c.blocking_issues_json) > 0)")

    if not include_held:
        # held_until 未来 = 挂起;NULL or 过去 = 不挂起
        now_iso = datetime.now(timezone.utc).isoformat()
        where.append("(c.held_until IS NULL OR c.held_until < ?)")
        params.append(now_iso)

    where_sql = " WHERE " + " AND ".join(where) if where else ""

    with db.connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM cases c LEFT JOIN customers cu ON cu.id = c.customer_id{where_sql}",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT c.*, cu.canonical_name AS canonical_name,
                   latest.status AS latest_render_status,
                   rq.quality_status AS latest_render_quality_status,
                   rq.quality_score AS latest_render_quality_score
            FROM cases c
            LEFT JOIN customers cu ON cu.id = c.customer_id
            LEFT JOIN render_jobs latest ON latest.id = (
                SELECT j.id FROM render_jobs j
                WHERE j.case_id = c.id
                ORDER BY j.enqueued_at DESC, j.id DESC
                LIMIT 1
            )
            LEFT JOIN render_quality rq ON rq.render_job_id = latest.id
            {where_sql}
            ORDER BY c.id DESC
            LIMIT ? OFFSET ?
            """,
            params + [page_size, (page - 1) * page_size],
        ).fetchall()

    items = [_row_to_summary(r, r["canonical_name"]) for r in rows]
    return CaseListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/stats")
def stats() -> dict:
    with db.connect() as conn:
        cat_rows = conn.execute(
            "SELECT COALESCE(manual_category, category) AS cat, COUNT(*) AS n "
            "FROM cases WHERE trashed_at IS NULL GROUP BY cat"
        ).fetchall()
        tier_rows = conn.execute(
            "SELECT COALESCE(manual_template_tier, template_tier) AS tier, COUNT(*) AS n "
            "FROM cases WHERE trashed_at IS NULL "
            "AND COALESCE(manual_template_tier, template_tier) IS NOT NULL GROUP BY tier"
        ).fetchall()
        review_rows = conn.execute(
            "SELECT COALESCE(review_status, 'unreviewed') AS status, COUNT(*) AS n "
            "FROM cases WHERE trashed_at IS NULL GROUP BY status"
        ).fetchall()
        manual_count = conn.execute(
            "SELECT COUNT(*) FROM cases WHERE trashed_at IS NULL "
            "AND (manual_category IS NOT NULL OR manual_template_tier IS NOT NULL)"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM cases WHERE trashed_at IS NULL").fetchone()[0]
    return {
        "total": total,
        "by_category": {r["cat"]: r["n"] for r in cat_rows},
        "by_tier": {r["tier"]: r["n"] for r in tier_rows},
        "by_review_status": {r["status"]: r["n"] for r in review_rows},
        "manual_override_count": manual_count,
    }


def _source_blocker_reason(profile: dict[str, Any], marked_not_source: bool) -> str | None:
    if marked_not_source:
        return "no_real_source_photos"
    source_kind = str(profile.get("source_kind") or "")
    if int(profile.get("missing_source_count") or 0) > 0 or source_kind == "missing_source_files":
        return "missing_source_files"
    if source_kind in {"generated_output_collection", "empty"}:
        return "no_real_source_photos"
    if source_kind == "insufficient_source_photos":
        return "insufficient_source_photos"
    if source_kind == "missing_before_after_pair":
        return "missing_before_after_pair"
    return None


def _case_image_files_from_meta(meta: Any) -> list[str]:
    if not isinstance(meta, dict):
        return []
    return [str(item) for item in (meta.get("image_files") or []) if item]


def _source_binding_case_ids(meta: Any) -> list[int]:
    if not isinstance(meta, dict):
        return []
    bindings = meta.get(source_images.SOURCE_BINDINGS_META_KEY)
    raw_ids: list[Any] = []
    if isinstance(bindings, dict):
        raw_ids = bindings.get("case_ids") or []
    elif isinstance(bindings, list):
        raw_ids = bindings
    out: list[int] = []
    for item in raw_ids:
        try:
            cid = int(item)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in out:
            out.append(cid)
    return out


def _bound_rows(conn: sqlite3.Connection, case_ids: list[int]) -> list[sqlite3.Row]:
    if not case_ids:
        return []
    placeholders = ",".join("?" * len(case_ids))
    return conn.execute(
        f"""
        SELECT id, abs_path, customer_raw, customer_id, meta_json, skill_image_metadata_json
        FROM cases
        WHERE trashed_at IS NULL AND id IN ({placeholders})
        ORDER BY id
        """,
        case_ids,
    ).fetchall()


def _profile_for_case_row(row: sqlite3.Row) -> dict[str, Any]:
    meta = _json_field(row, "meta_json", {})
    return source_images.classify_existing_case_source_profile(row["abs_path"], _case_image_files_from_meta(meta))


def _merged_profile_for_rows(rows: list[sqlite3.Row]) -> dict[str, Any]:
    merged_files: list[str] = []
    missing_files: list[str] = []
    raw_meta_image_count = 0
    for row in rows:
        meta = _json_field(row, "meta_json", {})
        case_name = Path(str(row["abs_path"] or "")).name or f"case-{row['id']}"
        raw_files = _case_image_files_from_meta(meta)
        raw_meta_image_count += len(raw_files)
        split = source_images.existing_source_image_files(row["abs_path"], raw_files)
        for filename in [str(item) for item in split["existing"]]:
            merged_files.append(str(Path(f"case{row['id']}-{case_name}") / filename))
        for filename in [str(item) for item in raw_files if not source_images.is_source_image_file(str(item))]:
            merged_files.append(str(Path(f"case{row['id']}-{case_name}") / filename))
        for filename in [str(item) for item in split["missing"]]:
            missing_files.append(str(Path(f"case{row['id']}-{case_name}") / filename))
    profile = source_images.classify_source_profile(merged_files)
    profile["raw_meta_image_count"] = raw_meta_image_count
    profile["missing_source_count"] = len(missing_files)
    profile["missing_source_samples"] = missing_files[:8]
    profile["file_integrity_status"] = "missing_source_files" if missing_files else "ok"
    if missing_files and not merged_files:
        profile["source_kind"] = "missing_source_files"
    return profile


def _source_group_row_payload(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    role: str,
) -> dict[str, Any]:
    meta = _json_field(row, "meta_json", {})
    if not isinstance(meta, dict):
        meta = {}
    raw_files = _case_image_files_from_meta(meta)
    case_id = int(row["id"])
    abs_path = str(row["abs_path"] or "")
    case_title = Path(abs_path).name or f"case {case_id}"
    existing_split = source_images.existing_source_image_files(abs_path, raw_files)
    image_files = [str(item) for item in existing_split["existing"]]
    missing_files = [str(item) for item in existing_split["missing"]]
    image_review_states = _image_review_states_from_meta(meta)
    skill_image_metadata = _json_field(row, "skill_image_metadata_json", [])
    if not isinstance(skill_image_metadata, list):
        skill_image_metadata = []
    active_files = set(image_files)
    skill_image_metadata = [
        item
        for item in skill_image_metadata
        if isinstance(item, dict)
        and str(item.get("filename") or item.get("relative_path") or "") in active_files
    ]
    overrides = _fetch_image_overrides(conn, int(row["id"]))
    skill_image_metadata = _apply_overrides_to_metadata(
        skill_image_metadata,
        overrides,
        image_files=image_files,
    )
    skill_image_metadata = _apply_review_states_to_metadata(skill_image_metadata, image_review_states)
    metadata_by_file: dict[str, dict[str, Any]] = {}
    for item in skill_image_metadata:
        filename = str(item.get("filename") or item.get("relative_path") or "")
        if filename:
            metadata_by_file[filename] = item
    default_body_part = "unknown"
    images: list[dict[str, Any]] = []
    for filename in image_files:
        item = metadata_by_file.get(filename)
        phase, phase_source = _metadata_phase(item, filename)
        if phase is None:
            contextual_phase, _ = _metadata_phase(None, str(Path(case_title) / filename))
            if contextual_phase:
                phase = contextual_phase
                phase_source = "directory"
        view, view_source = _metadata_view(item, filename)
        review_state = (item or {}).get("review_state") if isinstance(item, dict) else None
        if review_state is None:
            review_state = image_review_states.get(filename) or image_review_states.get(Path(filename).name)
        review_state = review_state if isinstance(review_state, dict) else None
        images.append({
            "case_id": case_id,
            "filename": filename,
            "preview_url": f"/api/cases/{case_id}/files?name={quote(filename, safe='')}",
            "phase": phase,
            "phase_source": phase_source,
            "view": view,
            "view_source": view_source,
            "manual": phase_source == "manual" or view_source == "manual",
            "needs_manual": phase is None or view is None,
            "review_state": review_state,
            "review_verdict": (review_state or {}).get("verdict") if review_state else None,
            "render_excluded": bool((review_state or {}).get("render_excluded")) if review_state else False,
            "copied_requires_review": bool((review_state or {}).get("copied_requires_review")) if review_state else False,
            "body_part": _metadata_body_part(item, default_body_part),
            "treatment_area": _metadata_treatment_area(item),
            "angle_confidence": (item or {}).get("angle_confidence") if isinstance(item, dict) else None,
            "rejection_reason": (item or {}).get("rejection_reason") if isinstance(item, dict) else None,
            "issues": [str(issue) for issue in ((item or {}).get("issues") or [])] if isinstance(item, dict) else [],
            "pose": (item or {}).get("pose") if isinstance(item, dict) else None,
            "direction": (item or {}).get("direction") if isinstance(item, dict) else None,
            "sharpness_score": (item or {}).get("sharpness_score") if isinstance(item, dict) else None,
        })
    return {
        "case_id": case_id,
        "case_title": case_title,
        "abs_path": abs_path,
        "customer_raw": row["customer_raw"],
        "customer_id": row["customer_id"],
        "role": role,
        "source_profile": source_images.classify_existing_case_source_profile(abs_path, raw_files),
        "raw_meta_image_count": len(raw_files),
        "missing_image_count": len(missing_files),
        "missing_image_samples": missing_files[:12],
        "image_count": len(images),
        "images": images,
    }


def _merged_profile_for_source_payloads(sources: list[dict[str, Any]]) -> dict[str, Any]:
    merged_files: list[str] = []
    missing_files: list[str] = []
    raw_meta_image_count = 0
    for source in sources:
        case_id = int(source.get("case_id") or 0)
        case_title = str(source.get("case_title") or f"case-{case_id}")
        raw_meta_image_count += int(source.get("raw_meta_image_count") or 0)
        for image in source.get("images") or []:
            filename = str(image.get("filename") or "")
            if filename:
                merged_files.append(str(Path(f"case{case_id}-{case_title}") / filename))
        for filename in source.get("missing_image_samples") or []:
            if filename:
                missing_files.append(str(Path(f"case{case_id}-{case_title}") / str(filename)))
    profile = source_images.classify_source_profile(merged_files)
    profile["raw_meta_image_count"] = raw_meta_image_count
    profile["missing_source_count"] = sum(int(source.get("missing_image_count") or 0) for source in sources)
    profile["missing_source_samples"] = missing_files[:8]
    profile["file_integrity_status"] = "missing_source_files" if profile["missing_source_count"] else "ok"
    if int(profile["missing_source_count"]) > 0 and not merged_files:
        profile["source_kind"] = "missing_source_files"
    return profile


def _source_group_metadata_index(raw: Any) -> dict[str, dict[str, Any]]:
    items = raw if isinstance(raw, list) else []
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in (item.get("filename"), item.get("relative_path")):
            if key:
                out[str(key)] = item
                out[Path(str(key)).name] = item
    return out


def _source_group_render_name(case_id: int, case_title: str, filename: str) -> str:
    rel = str(filename).replace("\\", "/").strip("/")
    safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", f"case{case_id}-{case_title}-{rel}")
    return safe[:180] or f"case{case_id}-{Path(filename).name}"


_SOURCE_GROUP_METADATA_FALLBACK_FIELDS = {
    "angle_confidence",
    "rejection_reason",
    "issues",
    "pose",
    "direction",
    "sharpness_score",
}


def _source_group_empty_metadata_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _apply_source_group_metadata_fallback(
    image: dict[str, Any],
    fallback: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(fallback, dict):
        return image
    merged = dict(image)
    used = False
    for key in _SOURCE_GROUP_METADATA_FALLBACK_FIELDS:
        if _source_group_empty_metadata_value(merged.get(key)) and not _source_group_empty_metadata_value(fallback.get(key)):
            merged[key] = fallback.get(key)
            used = True
    if used:
        merged["selection_metadata_source"] = "primary_render_history"
    return merged


_candidate_quality = source_selection.candidate_quality
_candidate_rank = source_selection.candidate_rank
_slot_pair_quality = source_selection.slot_pair_quality


def _source_group_hard_blockers(
    *,
    effective_profile: dict[str, Any],
    missing_slots: list[dict[str, Any]],
    needs_manual: list[dict[str, Any]],
    slots: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    source_kind = str(effective_profile.get("source_kind") or "")
    missing_source_count = int(effective_profile.get("missing_source_count") or 0)
    if missing_source_count > 0 or source_kind == "missing_source_files":
        blockers.append(
            {
                "code": "missing_source_files",
                "severity": "block",
                "message": f"源组里有 {missing_source_count} 个历史源文件在当前磁盘不可读",
                "recommended_action": "恢复源文件、重新扫描目录，或清除失效绑定",
            }
        )
    elif source_kind in {"generated_output_collection", "empty"}:
        blockers.append(
            {
                "code": "no_real_source_photos",
                "severity": "block",
                "message": "当前目录不是可用于正式出图的真实术前/术后源照片目录",
                "recommended_action": "切换到真实案例源目录，或标记为素材归档",
            }
        )
    elif source_kind == "insufficient_source_photos":
        blockers.append(
            {
                "code": "insufficient_source_photos",
                "severity": "block",
                "message": "真实源照片不足，至少需要术前和术后各 1 张",
                "recommended_action": "补充真实术前/术后源照片",
            }
        )
    elif source_kind == "missing_before_after_pair":
        blockers.append(
            {
                "code": "missing_before_after_pair",
                "severity": "block",
                "message": "真实源照片缺少术前/术后配对",
                "recommended_action": "补齐阶段分类，或绑定互补源目录",
            }
        )
    if needs_manual:
        blockers.append(
            {
                "code": "classification_open",
                "severity": "block",
                "message": f"还有 {len(needs_manual)} 张源图未补齐阶段或角度",
                "recommended_action": "进入照片分类工作台批量补齐阶段、角度和可用性",
                "samples": needs_manual[:8],
            }
        )
    if missing_slots:
        slot_text = "；".join(
            f"{slot['label']}缺{'/'.join('术前' if role == 'before' else '术后' for role in slot['missing'])}"
            for slot in missing_slots
        )
        blockers.append(
            {
                "code": "missing_render_slots",
                "severity": "block",
                "message": f"三联正式出图槽位未配齐：{slot_text}",
                "recommended_action": "从补图候选或源组整理中补齐正面、45°、侧面术前术后配对",
                "slots": missing_slots,
            }
        )
    risky_pairs = []
    for view, slot in slots.items():
        pair = slot.get("pair_quality")
        if isinstance(pair, dict) and pair.get("severity") == "block":
            risky_pairs.append({"view": view, "label": slot.get("label"), "pair_quality": pair})
    if risky_pairs:
        blockers.append(
            {
                "code": "selected_pair_block_risk",
                "severity": "block",
                "message": f"有 {len(risky_pairs)} 个已配齐槽位的首选配对仍含阻断级风险",
                "recommended_action": "回到源组候选重选更清晰、姿态更接近的术前术后配对",
                "slots": risky_pairs,
            }
        )
    return blockers


def _source_group_readiness_score(
    *,
    hard_blockers: list[dict[str, Any]],
    missing_slots: list[dict[str, Any]],
    needs_manual_count: int,
    missing_source_count: int,
    slots: dict[str, dict[str, Any]],
) -> int:
    score = 100
    score -= min(missing_source_count * 20, 60)
    score -= min(needs_manual_count * 6, 42)
    score -= min(len(missing_slots) * 14, 42)
    for slot in slots.values():
        pair = slot.get("pair_quality")
        if not isinstance(pair, dict):
            continue
        if pair.get("severity") == "block":
            score -= 18
        elif pair.get("severity") == "review":
            score -= 6
    if any(item.get("code") in {"no_real_source_photos", "insufficient_source_photos", "missing_before_after_pair"} for item in hard_blockers):
        score = min(score, 40)
    return max(0, min(100, int(round(score))))


def _candidate_angle_confidence(candidate: dict[str, Any] | None) -> float | None:
    if not isinstance(candidate, dict):
        return None
    value = candidate.get("angle_confidence")
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    if candidate.get("view_source") == "manual":
        return 1.0
    if candidate.get("view_source") in {"filename", "directory"}:
        return 0.7
    return None


def _slot_quality_prediction(view: str, slot: dict[str, Any]) -> dict[str, Any]:
    before = slot.get("selected_before")
    after = slot.get("selected_after")
    pair_quality = slot.get("pair_quality") if isinstance(slot.get("pair_quality"), dict) else {}
    warnings = pair_quality.get("warnings") if isinstance(pair_quality.get("warnings"), list) else []
    warning_codes = [
        str(item.get("code") or "")
        for item in warnings
        if isinstance(item, dict) and item.get("code")
    ]
    if pair_quality.get("render_slot_status") == "dropped":
        decision = "drop"
        recommended_action = "该角度无稳定对比价值，正式出图自动降级"
        blocks_render = False
    elif isinstance(before, dict) and isinstance(after, dict):
        decision = "render"
        recommended_action = "进入正式出图候选"
        blocks_render = False
    else:
        decision = "block"
        recommended_action = "补齐该角度术前/术后配对"
        blocks_render = True
    pose_delta = None
    metrics = pair_quality.get("metrics") if isinstance(pair_quality.get("metrics"), dict) else {}
    if isinstance(metrics.get("pose_delta"), dict):
        pose_delta = metrics.get("pose_delta")
    return {
        "slot": view,
        "decision": decision,
        "blocks_render": blocks_render,
        "pair_score": pair_quality.get("score"),
        "pair_label": pair_quality.get("label"),
        "pose_delta": pose_delta,
        "angle_confidence": {
            "before": _candidate_angle_confidence(before),
            "after": _candidate_angle_confidence(after),
        },
        "warning_codes": warning_codes,
        "drop_reason": pair_quality.get("drop_reason"),
        "recommended_action": recommended_action,
    }


def _formal_candidate_manifest_summary(
    slots: dict[str, dict[str, Any]],
    *,
    readiness_score: int,
    missing_slots: list[dict[str, Any]],
    hard_blockers: list[dict[str, Any]],
) -> dict[str, Any]:
    out_slots: dict[str, Any] = {}
    selected_count = 0
    renderable_slot_count = 0
    provenance: list[dict[str, Any]] = []
    for view, slot in slots.items():
        before = slot.get("selected_before")
        after = slot.get("selected_after")
        quality_prediction = _slot_quality_prediction(view, slot)
        if quality_prediction["decision"] == "render":
            renderable_slot_count += 1
        if before:
            selected_count += 1
            provenance.append(
                {
                    "slot": view,
                    "role": "before",
                    "case_id": before.get("case_id"),
                    "filename": before.get("filename"),
                    "source_role": before.get("source_role"),
                }
            )
        if after:
            selected_count += 1
            provenance.append(
                {
                    "slot": view,
                    "role": "after",
                    "case_id": after.get("case_id"),
                    "filename": after.get("filename"),
                    "source_role": after.get("source_role"),
                }
            )
        out_slots[view] = {
            "label": slot.get("label"),
            "ready": bool(slot.get("ready")),
            "before": {
                "case_id": before.get("case_id"),
                "filename": before.get("filename"),
                "selection_score": before.get("selection_score"),
                "source_role": before.get("source_role"),
                "render_feedback": before.get("render_feedback"),
                "source_group_lock": before.get("source_group_lock"),
                "selection_metadata_source": before.get("selection_metadata_source"),
            } if isinstance(before, dict) else None,
            "after": {
                "case_id": after.get("case_id"),
                "filename": after.get("filename"),
                "selection_score": after.get("selection_score"),
                "source_role": after.get("source_role"),
                "render_feedback": after.get("render_feedback"),
                "source_group_lock": after.get("source_group_lock"),
                "selection_metadata_source": after.get("selection_metadata_source"),
            } if isinstance(after, dict) else None,
            "pair_quality": slot.get("pair_quality"),
            "quality_prediction": quality_prediction,
            "selection_lock": slot.get("selection_lock"),
            "candidate_counts": {
                "before": int(slot.get("before_count") or 0),
                "after": int(slot.get("after_count") or 0),
            },
        }
    blocking_reasons: list[dict[str, Any]] = []
    for blocker in hard_blockers:
        if isinstance(blocker, dict):
            blocking_reasons.append(
                {
                    "code": blocker.get("code"),
                    "message": blocker.get("message"),
                    "recommended_action": blocker.get("recommended_action"),
                }
            )
    for item in missing_slots:
        blocking_reasons.append(
            {
                "code": "missing_render_slot",
                "view": item.get("view"),
                "message": f"{item.get('label') or item.get('view')} 缺 {','.join(item.get('missing') or [])}",
                "recommended_action": "补齐该角度术前/术后配对",
            }
        )
    return {
        "version": 1,
        "policy": "source_selection_v1",
        "required_slots": list(_PREFLIGHT_VIEWS),
        "readiness_score": readiness_score,
        "selected_count": selected_count,
        "renderable_slot_count": renderable_slot_count,
        "effective_template_hint": (
            "tri-compare"
            if renderable_slot_count >= 3
            else "bi-compare"
            if renderable_slot_count >= 2
            else None
        ),
        "blocking_reasons": blocking_reasons,
        "slots": out_slots,
        "source_provenance": provenance,
    }


def _source_group_preflight(
    sources: list[dict[str, Any]],
    effective_profile: dict[str, Any],
    render_feedback: dict[str, Any] | None = None,
    selection_controls: dict[str, Any] | None = None,
    primary_render_metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selection_controls = selection_controls if isinstance(selection_controls, dict) else {}
    locked_slots = selection_controls.get("locked_slots") if isinstance(selection_controls.get("locked_slots"), dict) else {}
    slots = {
        view: {
            "view": view,
            "label": _PREFLIGHT_VIEW_LABEL[view],
            "before_count": 0,
            "after_count": 0,
            "ready": False,
            "source_case_ids": [],
            "before_candidates": [],
            "after_candidates": [],
            "selected_before": None,
            "selected_after": None,
            "pair_quality": None,
            "selection_lock": None,
        }
        for view in _PREFLIGHT_VIEWS
    }
    needs_manual: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    case_ids_by_view: dict[str, set[int]] = {view: set() for view in _PREFLIGHT_VIEWS}
    for source in sources:
        source_case_id = int(source["case_id"])
        source_role = str(source.get("role") or "")
        source_title = str(source.get("case_title") or f"case {source_case_id}")
        for image in source.get("images") or []:
            filename = str(image.get("filename") or "")
            render_filename = _source_group_render_name(source_case_id, source_title, filename)
            fallback_metadata = None
            if isinstance(primary_render_metadata, dict):
                fallback_metadata = (
                    primary_render_metadata.get(render_filename)
                    or primary_render_metadata.get(Path(render_filename).name)
                    or primary_render_metadata.get(filename)
                    or primary_render_metadata.get(Path(filename).name)
                )
            image_for_quality = _apply_source_group_metadata_fallback(image, fallback_metadata)
            if image.get("render_excluded"):
                excluded.append({"case_id": source_case_id, "filename": filename})
                continue
            phase = image_for_quality.get("phase")
            view = image_for_quality.get("view")
            if phase not in _ALLOWED_OVERRIDE_PHASES or view not in _ALLOWED_OVERRIDE_VIEWS:
                needs_manual.append({
                    "case_id": source_case_id,
                    "filename": filename,
                    "missing": [
                        *([] if phase in _ALLOWED_OVERRIDE_PHASES else ["phase"]),
                        *([] if view in _ALLOWED_OVERRIDE_VIEWS else ["view"]),
                    ],
                })
                continue
            slot = slots[str(view)]
            key = f"{phase}_count"
            slot[key] = int(slot[key]) + 1
            case_ids_by_view[str(view)].add(source_case_id)
            candidate = {
                "case_id": source_case_id,
                "case_title": source_title,
                "source_role": source_role,
                "filename": filename,
                "render_filename": render_filename,
                "preview_url": image.get("preview_url"),
                "phase": phase,
                "phase_source": image_for_quality.get("phase_source"),
                "view": view,
                "view_source": image_for_quality.get("view_source"),
                "manual": bool(image_for_quality.get("manual")),
                "review_verdict": image_for_quality.get("review_verdict"),
                "review_label": (image_for_quality.get("review_state") or {}).get("label") if isinstance(image_for_quality.get("review_state"), dict) else None,
                "angle_confidence": image_for_quality.get("angle_confidence"),
                "rejection_reason": image_for_quality.get("rejection_reason"),
                "pose": image_for_quality.get("pose"),
                "direction": image_for_quality.get("direction"),
                "sharpness_score": image_for_quality.get("sharpness_score"),
                "selection_metadata_source": image_for_quality.get("selection_metadata_source"),
            }
            candidate.update(_candidate_quality(image_for_quality, source_role))
            if candidate.get("selection_metadata_source") == "primary_render_history":
                reasons = [str(item) for item in (candidate.get("selection_reasons") or []) if item]
                if "复用最近渲染姿态画像" not in reasons:
                    candidate["selection_reasons"] = [*reasons, "复用最近渲染姿态画像"][:6]
            source_selection.apply_render_feedback(candidate, render_feedback)
            slot[f"{phase}_candidates"].append(candidate)
    for view, slot in slots.items():
        slot["before_candidates"].sort(key=_candidate_rank)
        slot["after_candidates"].sort(key=_candidate_rank)
        selected_before, selected_after, pair_quality = source_selection.select_best_pair(
            str(view),
            slot["before_candidates"],
            slot["after_candidates"],
            lock=locked_slots.get(view) if isinstance(locked_slots, dict) else None,
        )
        slot["selected_before"] = selected_before
        slot["selected_after"] = selected_after
        slot["pair_quality"] = pair_quality
        slot["selection_lock"] = (pair_quality or {}).get("metrics", {}).get("source_group_lock") if isinstance(pair_quality, dict) else None
        slot["before_candidates"] = slot["before_candidates"][:5]
        slot["after_candidates"] = slot["after_candidates"][:5]
        slot["ready"] = int(slot["before_count"]) > 0 and int(slot["after_count"]) > 0
        slot["source_case_ids"] = sorted(case_ids_by_view[view])
    missing_slots = [
        {
            "view": view,
            "label": str(slot["label"]),
            "missing": [
                *([] if int(slot["before_count"]) > 0 else ["before"]),
                *([] if int(slot["after_count"]) > 0 else ["after"]),
            ],
        }
        for view, slot in slots.items()
        if not slot["ready"]
    ]
    hard_blockers = _source_group_hard_blockers(
        effective_profile=effective_profile,
        missing_slots=missing_slots,
        needs_manual=needs_manual,
        slots=slots,
    )
    readiness_score = _source_group_readiness_score(
        hard_blockers=hard_blockers,
        missing_slots=missing_slots,
        needs_manual_count=len(needs_manual),
        missing_source_count=int(effective_profile.get("missing_source_count") or 0),
        slots=slots,
    )
    candidate_manifest = _formal_candidate_manifest_summary(
        slots,
        readiness_score=readiness_score,
        missing_slots=missing_slots,
        hard_blockers=hard_blockers,
    )
    return {
        "status": "ready" if not hard_blockers else "blocked",
        "readiness_score": readiness_score,
        "feedback_source_job_id": (render_feedback or {}).get("source_job_id") if isinstance(render_feedback, dict) else None,
        "feedback_applied": bool(
            isinstance(render_feedback, dict)
            and (render_feedback.get("candidate_penalties") or render_feedback.get("pair_penalties"))
        ),
        "selection_controls": selection_controls,
        "accepted_warnings": selection_controls.get("accepted_warnings") or [],
        "hard_blockers": hard_blockers,
        "formal_candidate_manifest": candidate_manifest,
        "slots": list(slots.values()),
        "missing_slots": missing_slots,
        "needs_manual_count": len(needs_manual),
        "needs_manual_samples": needs_manual[:12],
        "render_excluded_count": len(excluded),
        "render_excluded_samples": excluded[:12],
        "missing_source_count": int(effective_profile.get("missing_source_count") or 0),
        "missing_source_samples": effective_profile.get("missing_source_samples") or [],
    }


def _effective_profile_for_case(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[dict[str, Any], list[int]]:
    meta = _json_field(row, "meta_json", {})
    base_profile = source_images.classify_existing_case_source_profile(
        row["abs_path"],
        _case_image_files_from_meta(meta),
    )
    binding_ids = _source_binding_case_ids(meta)
    bound = _bound_rows(conn, binding_ids)
    if not bound:
        return base_profile, []
    merged = _merged_profile_for_rows([row, *bound])
    merged["bound_case_ids"] = [int(r["id"]) for r in bound]
    return merged, [int(r["id"]) for r in bound]


def _complementary_phase_reason(base: dict[str, Any], candidate: dict[str, Any]) -> str | None:
    if int(base.get("before_count") or 0) == 0 and int(candidate.get("before_count") or 0) > 0:
        return "候选目录可补术前"
    if int(base.get("after_count") or 0) == 0 and int(candidate.get("after_count") or 0) > 0:
        return "候选目录可补术后"
    if int(base.get("unlabeled_source_count") or 0) > 0 and (
        int(candidate.get("before_count") or 0) > 0 or int(candidate.get("after_count") or 0) > 0
    ):
        return "候选目录带阶段线索，可辅助配对"
    return None


def _binding_candidate_rows(conn: sqlite3.Connection, row: sqlite3.Row, limit: int) -> list[dict[str, Any]]:
    base_meta = _json_field(row, "meta_json", {})
    base_profile = _profile_for_case_row(row)
    base_source = _source_group_row_payload(conn, row, role="primary")
    target_path = Path(str(row["abs_path"] or ""))
    parent = str(target_path.parent)
    grand_parent = str(target_path.parent.parent)
    like_parent = f"{parent}/%"
    like_grand = f"{grand_parent}/%"
    rows = conn.execute(
        """
        SELECT id, abs_path, customer_raw, customer_id, meta_json, skill_image_metadata_json
        FROM cases
        WHERE trashed_at IS NULL
          AND id != ?
          AND (abs_path LIKE ? OR abs_path LIKE ? OR customer_raw = ?)
        ORDER BY id DESC
        LIMIT 300
        """,
        (row["id"], like_parent, like_grand, row["customer_raw"]),
    ).fetchall()
    existing = set(_source_binding_case_ids(base_meta))
    candidates: list[dict[str, Any]] = []
    for cand in rows:
        cand_profile = _profile_for_case_row(cand)
        if int(cand_profile.get("source_count") or 0) == 0:
            continue
        merged = _merged_profile_for_rows([row, cand])
        can_complete = str(merged.get("source_kind")) == "ready_source"
        reasons: list[str] = []
        score = 0
        cand_path = Path(str(cand["abs_path"] or ""))
        if cand_path.parent == target_path.parent:
            score += 60
            reasons.append("同父目录")
        elif cand_path.parent.parent == target_path.parent.parent:
            score += 35
            reasons.append("同上级案例目录")
        if row["customer_raw"] and row["customer_raw"] == cand["customer_raw"]:
            score += 20
            reasons.append("同客户")
        phase_reason = _complementary_phase_reason(base_profile, cand_profile)
        if phase_reason:
            score += 35
            reasons.append(phase_reason)
        if can_complete:
            score += 50
            reasons.append("合并后术前术后配齐")
        if score < 35:
            continue
        cand_source = _source_group_row_payload(conn, cand, role="bound_preview")
        preview_sources = [base_source, cand_source]
        projected_profile = _merged_profile_for_source_payloads(preview_sources)
        projected_preflight = _source_group_preflight(preview_sources, projected_profile)
        candidates.append({
            "case_id": cand["id"],
            "case_title": cand_path.name or f"case {cand['id']}",
            "abs_path": cand["abs_path"],
            "customer_raw": cand["customer_raw"],
            "score": score,
            "match_reasons": reasons,
            "source_profile": cand_profile,
            "merged_source_profile": merged,
            "can_complete_pair": can_complete,
            "already_bound": int(cand["id"]) in existing,
            "case_url": f"/cases/{cand['id']}",
            "projected_preflight": _source_binding_preflight_preview(projected_preflight),
        })
    candidates.sort(key=lambda item: (not item["can_complete_pair"], -int(item["score"]), int(item["case_id"])))
    return candidates[:limit]


def _source_binding_preflight_preview(preflight: dict[str, Any]) -> dict[str, Any]:
    manifest = preflight.get("formal_candidate_manifest") if isinstance(preflight.get("formal_candidate_manifest"), dict) else {}
    return {
        "status": preflight.get("status"),
        "readiness_score": preflight.get("readiness_score"),
        "needs_manual_count": preflight.get("needs_manual_count"),
        "missing_source_count": preflight.get("missing_source_count"),
        "selected_count": manifest.get("selected_count", 0),
        "missing_slots": preflight.get("missing_slots") or [],
        "slots": [
            {
                "view": slot.get("view"),
                "label": slot.get("label"),
                "before_count": int(slot.get("before_count") or 0),
                "after_count": int(slot.get("after_count") or 0),
                "ready": bool(slot.get("ready")),
            }
            for slot in (preflight.get("slots") or [])
            if isinstance(slot, dict)
        ],
        "hard_blockers": [
            {
                "code": blocker.get("code"),
                "severity": blocker.get("severity"),
                "message": blocker.get("message"),
                "recommended_action": blocker.get("recommended_action"),
            }
            for blocker in (preflight.get("hard_blockers") or [])
            if isinstance(blocker, dict)
        ],
    }


_SOURCE_BLOCKER_LABEL = {
    "missing_source_files": "源文件缺失",
    "no_real_source_photos": "不是案例源目录",
    "insufficient_source_photos": "真实源图不足",
    "missing_before_after_pair": "缺术前/术后配对",
}
_SOURCE_BLOCKER_ACTION = {
    "missing_source_files": "恢复源文件、重新扫描目录，或清除失效绑定后再正式出图",
    "no_real_source_photos": "标记为素材归档，或绑定/补充真实术前术后源目录",
    "insufficient_source_photos": "补充至少一张缺失阶段源图，或用跨案例补图候选复制后人工确认",
    "missing_before_after_pair": "补齐术前/术后阶段分类，或复制缺失阶段候选图后人工确认",
}


def _source_blocker_item(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any] | None:
    meta = _json_field(row, "meta_json", {})
    tags = _json_field(row, "tags_json", [])
    manual_issues = _json_field(row, "manual_blocking_issues_json", [])
    profile = _profile_for_case_row(row)
    effective_profile, bound_case_ids = _effective_profile_for_case(conn, row)
    marked_not_source = source_images.case_marked_not_source(tags, manual_issues)
    if marked_not_source:
        profile = {
            **profile,
            "source_kind": "manual_not_case_source_directory",
            "manual_not_source": True,
        }
        effective_profile = profile
    reason = _source_blocker_reason(effective_profile, marked_not_source)
    if not reason:
        return None
    abs_path = str(row["abs_path"] or "")
    return {
        "case_id": row["id"],
        "case_title": Path(abs_path).name or f"case {row['id']}",
        "abs_path": abs_path,
        "customer_raw": row["customer_raw"],
        "customer_id": row["customer_id"],
        "reason": reason,
        "reason_label": _SOURCE_BLOCKER_LABEL.get(reason, reason),
        "recommended_action": _SOURCE_BLOCKER_ACTION.get(reason, "回到案例详情复核源图和分类"),
        "source_profile": profile,
        "effective_source_profile": effective_profile,
        "bound_case_ids": bound_case_ids,
        "marked_not_source": marked_not_source,
        "tags": tags if isinstance(tags, list) else [],
        "notes": row["notes"],
        "latest_render_status": row["latest_render_status"],
        "latest_render_quality_status": row["latest_render_quality_status"],
        "case_url": f"/cases/{row['id']}",
    }


@router.get("/source-blockers")
def list_source_blockers(
    reason: str = Query("all"),
    limit: int = Query(200, ge=1, le=2000),
) -> dict[str, Any]:
    allowed = {
        "all",
        "missing_source_files",
        "no_real_source_photos",
        "insufficient_source_photos",
        "missing_before_after_pair",
    }
    if reason not in allowed:
        raise HTTPException(400, f"reason must be one of {sorted(allowed)}")
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.abs_path, c.customer_raw, c.customer_id, c.meta_json, c.tags_json,
                   c.manual_blocking_issues_json, c.notes,
                   latest.status AS latest_render_status,
                   rq.quality_status AS latest_render_quality_status
            FROM cases c
            LEFT JOIN render_jobs latest ON latest.id = (
                SELECT j.id FROM render_jobs j
                WHERE j.case_id = c.id
                ORDER BY j.enqueued_at DESC, j.id DESC
                LIMIT 1
            )
            LEFT JOIN render_quality rq ON rq.render_job_id = latest.id
            WHERE c.trashed_at IS NULL
            ORDER BY c.id DESC
            """
        ).fetchall()
        items_all = [item for row in rows if (item := _source_blocker_item(conn, row)) is not None]
    counts = {
        "total": len(items_all),
        "missing_source_files": sum(1 for item in items_all if item["reason"] == "missing_source_files"),
        "no_real_source_photos": sum(1 for item in items_all if item["reason"] == "no_real_source_photos"),
        "insufficient_source_photos": sum(1 for item in items_all if item["reason"] == "insufficient_source_photos"),
        "missing_before_after_pair": sum(1 for item in items_all if item["reason"] == "missing_before_after_pair"),
        "marked_not_source": sum(1 for item in items_all if item["marked_not_source"]),
    }
    filtered = items_all if reason == "all" else [item for item in items_all if item["reason"] == reason]
    return {
        "items": filtered[:limit],
        "total": len(filtered),
        "counts": counts,
        "reason": reason,
        "limit": limit,
    }


@router.post("/source-blockers/{case_id}/action")
def apply_source_blocker_action(case_id: int, payload: SourceBlockerActionRequest) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT id, tags_json, manual_blocking_issues_json, notes
            FROM cases
            WHERE id = ? AND trashed_at IS NULL
            """,
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        tags = _json_field(row, "tags_json", [])
        manual_issues = _json_field(row, "manual_blocking_issues_json", [])
        if not isinstance(tags, list):
            tags = []
        if not isinstance(manual_issues, list):
            manual_issues = []
        codes: list[str] = []
        for item in manual_issues:
            raw_code = item.get("code") if isinstance(item, dict) else item
            code = str(raw_code or "").strip()
            if code:
                codes.append(code)
        note = row["notes"] or ""
        befores = audit.snapshot_before(conn, [case_id])
        if payload.action == "mark_not_source":
            if source_images.CASE_NOT_SOURCE_TAG not in tags:
                tags.append(source_images.CASE_NOT_SOURCE_TAG)
            if source_images.CASE_NOT_SOURCE_CODE not in codes:
                codes.append(source_images.CASE_NOT_SOURCE_CODE)
            extra = (payload.note or "").strip()
            reviewer = (payload.reviewer or "人工整理").strip() or "人工整理"
            line = f"[源目录治理 {now}] {reviewer} 标记为素材归档/非案例源目录"
            if extra:
                line += f"：{extra}"
            note = f"{note.rstrip()}\n{line}".strip()
        elif payload.action == "clear_not_source":
            tags = [str(item) for item in tags if str(item) != source_images.CASE_NOT_SOURCE_TAG]
            codes = [code for code in codes if code != source_images.CASE_NOT_SOURCE_CODE]
            extra = (payload.note or "").strip()
            reviewer = (payload.reviewer or "人工整理").strip() or "人工整理"
            line = f"[源目录治理 {now}] {reviewer} 恢复为待检查"
            if extra:
                line += f"：{extra}"
            note = f"{note.rstrip()}\n{line}".strip()
        conn.execute(
            """
            UPDATE cases
            SET tags_json = ?, manual_blocking_issues_json = ?, notes = ?
            WHERE id = ?
            """,
            (
                json.dumps(tags, ensure_ascii=False),
                json.dumps(codes, ensure_ascii=False),
                note or None,
                case_id,
            ),
        )
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="source_blocker_action",
            source_route=f"/api/cases/source-blockers/{case_id}/action",
        )
    return {
        "case_id": case_id,
        "action": payload.action,
        "marked_not_source": source_images.case_marked_not_source(tags, codes),
        "tags": tags,
        "manual_blocking_codes": codes,
    }


@router.get("/{case_id}/source-binding-candidates")
def source_binding_candidates(
    case_id: int,
    limit: int = Query(8, ge=1, le=50),
) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT id, abs_path, customer_raw, customer_id, meta_json, skill_image_metadata_json
            FROM cases
            WHERE id = ? AND trashed_at IS NULL
            """,
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        source_profile = _profile_for_case_row(row)
        effective_profile, bound_case_ids = _effective_profile_for_case(conn, row)
        candidates = _binding_candidate_rows(conn, row, limit)
    return {
        "case_id": case_id,
        "source_profile": source_profile,
        "effective_source_profile": effective_profile,
        "bound_case_ids": bound_case_ids,
        "candidates": candidates,
    }


@router.post("/{case_id}/source-bindings")
def bind_source_directories(case_id: int, payload: SourceDirectoryBindRequest) -> dict[str, Any]:
    deduped: list[int] = []
    for raw in payload.source_case_ids:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid == case_id:
            raise HTTPException(400, "cannot bind case to itself")
        if cid > 0 and cid not in deduped:
            deduped.append(cid)
    if not deduped:
        raise HTTPException(400, "source_case_ids cannot be empty")
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, abs_path, customer_raw, customer_id, meta_json FROM cases WHERE id = ? AND trashed_at IS NULL",
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        bound = _bound_rows(conn, deduped)
        found_ids = {int(item["id"]) for item in bound}
        missing = [cid for cid in deduped if cid not in found_ids]
        if missing:
            raise HTTPException(404, f"source cases not found: {missing}")
        meta = _json_field(row, "meta_json", {})
        if not isinstance(meta, dict):
            meta = {}
        befores = audit.snapshot_before(conn, [case_id])
        meta[source_images.SOURCE_BINDINGS_META_KEY] = {
            "case_ids": deduped,
            "reviewer": payload.reviewer or "source-binding-workbench",
            "note": payload.note,
            "updated_at": now,
        }
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), case_id),
        )
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="source_directory_bind",
            source_route=f"/api/cases/{case_id}/source-bindings",
        )
        effective_profile = _merged_profile_for_rows([row, *bound])
        effective_profile["bound_case_ids"] = deduped
    return {
        "case_id": case_id,
        "bound_case_ids": deduped,
        "effective_source_profile": effective_profile,
    }


@router.delete("/{case_id}/source-bindings")
def clear_source_directory_bindings(case_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, meta_json FROM cases WHERE id = ? AND trashed_at IS NULL",
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        meta = _json_field(row, "meta_json", {})
        if not isinstance(meta, dict):
            meta = {}
        befores = audit.snapshot_before(conn, [case_id])
        meta.pop(source_images.SOURCE_BINDINGS_META_KEY, None)
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), case_id),
        )
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="source_directory_unbind",
            source_route=f"/api/cases/{case_id}/source-bindings",
        )
    return {"case_id": case_id, "bound_case_ids": []}


def _source_group_allowed_case_ids(meta: dict[str, Any], case_id: int) -> list[int]:
    return [case_id, *_source_binding_case_ids(meta)]


def _validate_source_group_lock_image(
    conn: sqlite3.Connection,
    *,
    primary_case_id: int,
    primary_meta: dict[str, Any],
    image: SourceGroupLockImage,
) -> dict[str, Any]:
    try:
        source_case_id = int(image.case_id)
    except (TypeError, ValueError):
        raise HTTPException(400, "source case_id must be an integer")
    allowed_case_ids = _source_group_allowed_case_ids(primary_meta, primary_case_id)
    if source_case_id not in allowed_case_ids:
        raise HTTPException(400, "locked image must belong to the primary case or its bound source group")
    case_dir = _case_dir_for_update(conn, source_case_id)
    filename = _validate_relative_image_name(case_dir, image.filename)
    _resolve_existing_source(case_dir, filename)
    return {"case_id": source_case_id, "filename": filename}


@router.post("/{case_id}/source-group/slot-locks")
def lock_source_group_slot(case_id: int, payload: SourceGroupSlotLockRequest) -> dict[str, Any]:
    view = payload.view.strip()
    if view not in _ALLOWED_OVERRIDE_VIEWS:
        raise HTTPException(400, "view must be one of front, oblique, side")
    now = datetime.now(timezone.utc).isoformat()
    reviewer = (payload.reviewer or "operator").strip() or "operator"
    reason = payload.reason.strip() if payload.reason else None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, meta_json FROM cases WHERE id = ? AND trashed_at IS NULL",
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        meta = _json_field(row, "meta_json", {})
        if not isinstance(meta, dict):
            meta = {}
        before = _validate_source_group_lock_image(conn, primary_case_id=case_id, primary_meta=meta, image=payload.before)
        after = _validate_source_group_lock_image(conn, primary_case_id=case_id, primary_meta=meta, image=payload.after)
        controls = source_selection.selection_controls_from_meta(meta)
        locked_slots = controls.setdefault("locked_slots", {})
        locked_slots[view] = {
            "before": before,
            "after": after,
            "reviewer": reviewer,
            "reason": reason,
            "updated_at": now,
        }
        controls["accepted_warnings"] = controls.get("accepted_warnings") or []
        befores = audit.snapshot_before(conn, [case_id])
        meta[source_selection.SOURCE_GROUP_SELECTION_META_KEY] = controls
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), case_id),
        )
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="source_group_slot_lock",
            source_route=f"/api/cases/{case_id}/source-group/slot-locks",
        )
    return case_source_group(case_id)


@router.delete("/{case_id}/source-group/slot-locks/{view}")
def clear_source_group_slot_lock(case_id: int, view: str) -> dict[str, Any]:
    view = view.strip()
    if view not in _ALLOWED_OVERRIDE_VIEWS:
        raise HTTPException(400, "view must be one of front, oblique, side")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, meta_json FROM cases WHERE id = ? AND trashed_at IS NULL",
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        meta = _json_field(row, "meta_json", {})
        if not isinstance(meta, dict):
            meta = {}
        controls = source_selection.selection_controls_from_meta(meta)
        locked_slots = controls.setdefault("locked_slots", {})
        locked_slots.pop(view, None)
        befores = audit.snapshot_before(conn, [case_id])
        meta[source_selection.SOURCE_GROUP_SELECTION_META_KEY] = controls
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), case_id),
        )
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="source_group_slot_unlock",
            source_route=f"/api/cases/{case_id}/source-group/slot-locks/{view}",
        )
    return case_source_group(case_id)


@router.post("/{case_id}/source-group/accepted-warnings")
def accept_source_group_warning(case_id: int, payload: SourceGroupWarningAcceptanceRequest) -> dict[str, Any]:
    slot = payload.slot.strip()
    if slot not in _ALLOWED_OVERRIDE_VIEWS:
        raise HTTPException(400, "slot must be one of front, oblique, side")
    code = payload.code.strip()
    if not code:
        raise HTTPException(400, "code cannot be blank")
    now = datetime.now(timezone.utc).isoformat()
    reviewer = (payload.reviewer or "operator").strip() or "operator"
    acceptance = {
        "job_id": payload.job_id,
        "slot": slot,
        "code": code,
        "message_contains": (payload.message_contains or "").strip(),
        "reviewer": reviewer,
        "note": payload.note.strip() if payload.note else None,
        "accepted_at": now,
    }
    with db.connect() as conn:
        if payload.job_id is not None:
            job_row = conn.execute(
                """
                SELECT id, manifest_path
                FROM render_jobs
                WHERE id = ? AND case_id = ?
                """,
                (payload.job_id, case_id),
            ).fetchone()
            if not job_row:
                raise HTTPException(404, "render job not found")
            scope = _selected_pair_scope_from_manifest(job_row["manifest_path"], slot)
            if not scope.get("selected_files"):
                raise HTTPException(400, "selected pair not found in render manifest")
            acceptance.update(scope)
        row = conn.execute(
            "SELECT id, meta_json FROM cases WHERE id = ? AND trashed_at IS NULL",
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        meta = _json_field(row, "meta_json", {})
        if not isinstance(meta, dict):
            meta = {}
        controls = source_selection.selection_controls_from_meta(meta)
        accepted = [
            item
            for item in (controls.get("accepted_warnings") or [])
            if not (
                item.get("slot") == slot
                and item.get("code") == code
                and str(item.get("message_contains") or "") == acceptance["message_contains"]
            )
        ]
        accepted.append(acceptance)
        controls["accepted_warnings"] = accepted
        controls.setdefault("locked_slots", {})
        befores = audit.snapshot_before(conn, [case_id])
        meta[source_selection.SOURCE_GROUP_SELECTION_META_KEY] = controls
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), case_id),
        )
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="source_group_warning_accept",
            source_route=f"/api/cases/{case_id}/source-group/accepted-warnings",
        )
    return case_source_group(case_id)


@router.get("/{case_id}/source-group")
def case_source_group(case_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT id, abs_path, customer_raw, customer_id, meta_json, skill_image_metadata_json
            FROM cases
            WHERE id = ? AND trashed_at IS NULL
            """,
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        meta = _json_field(row, "meta_json", {})
        if not isinstance(meta, dict):
            meta = {}
        binding_ids = _source_binding_case_ids(meta)
        bound_rows = _bound_rows(conn, binding_ids)
        found_bound_ids = [int(item["id"]) for item in bound_rows]
        missing_bound_ids = [cid for cid in binding_ids if cid not in set(found_bound_ids)]
        sources = [
            _source_group_row_payload(conn, row, role="primary"),
            *[_source_group_row_payload(conn, bound_row, role="bound") for bound_row in bound_rows],
        ]
        source_profile = sources[0]["source_profile"]
        effective_profile = _merged_profile_for_source_payloads(sources) if bound_rows else source_profile
        if bound_rows:
            effective_profile["bound_case_ids"] = found_bound_ids
        total_images = sum(int(source.get("image_count") or 0) for source in sources)
        missing_image_count = sum(int(source.get("missing_image_count") or 0) for source in sources)
        binding_meta = meta.get(source_images.SOURCE_BINDINGS_META_KEY)
        binding_meta = binding_meta if isinstance(binding_meta, dict) else None
        render_feedback = source_selection.render_feedback_from_history(conn, case_id)
        selection_controls = source_selection.selection_controls_from_meta(meta)
        primary_render_metadata = _source_group_metadata_index(_json_field(row, "skill_image_metadata_json", []))
        preflight = _source_group_preflight(
            sources,
            effective_profile,
            render_feedback,
            selection_controls,
            primary_render_metadata,
        )
    return {
        "case_id": case_id,
        "source_profile": source_profile,
        "effective_source_profile": effective_profile,
        "bound_case_ids": found_bound_ids,
        "missing_bound_case_ids": missing_bound_ids,
        "binding": binding_meta,
        "source_count": len(sources),
        "image_count": total_images,
        "missing_image_count": missing_image_count,
        "sources": sources,
        "preflight": preflight,
        "audit": {
            "bound_source_case_ids": found_bound_ids,
            "binding_reviewer": (binding_meta or {}).get("reviewer") if binding_meta else None,
            "binding_updated_at": (binding_meta or {}).get("updated_at") if binding_meta else None,
            "binding_note": (binding_meta or {}).get("note") if binding_meta else None,
            "source_group_selection": selection_controls,
        },
    }


@router.post("/trash", response_model=CaseTrashResponse)
def trash_cases(payload: CaseTrashRequest) -> CaseTrashResponse:
    stress.assert_destructive_allowed("case trash")
    case_ids = list(dict.fromkeys(payload.case_ids))
    if not case_ids:
        raise HTTPException(400, "case_ids cannot be empty")
    reason = payload.reason.strip() if payload.reason else None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    trashed_at = datetime.now(timezone.utc).isoformat()
    trashed: list[int] = []
    skipped: list[CaseTrashSkipped] = []

    with db.connect() as conn:
        placeholders = ",".join("?" * len(case_ids))
        rows = conn.execute(
            f"SELECT id, abs_path, trashed_at FROM cases WHERE id IN ({placeholders})",
            case_ids,
        ).fetchall()
        by_id = {int(row["id"]): row for row in rows}

        for case_id in case_ids:
            row = by_id.get(case_id)
            if not row:
                skipped.append(CaseTrashSkipped(case_id=case_id, reason="case_not_found"))
                continue
            if row["trashed_at"]:
                skipped.append(CaseTrashSkipped(case_id=case_id, reason="already_trashed"))
                continue
            try:
                _trash_case_directory(
                    conn,
                    case_id=case_id,
                    abs_path=row["abs_path"],
                    reason=reason,
                    stamp=stamp,
                    trashed_at=trashed_at,
                )
            except FileNotFoundError:
                skipped.append(CaseTrashSkipped(case_id=case_id, reason="directory_missing"))
                continue
            except OSError as e:
                skipped.append(CaseTrashSkipped(case_id=case_id, reason=f"io_error: {e}"))
                continue
            trashed.append(case_id)

    return CaseTrashResponse(trashed=len(trashed), case_ids=trashed, skipped=skipped)


@router.get("/{case_id}", response_model=CaseDetail)
def case_detail(case_id: int) -> CaseDetail:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT c.*, cu.canonical_name AS canonical_name,
                   latest.status AS latest_render_status,
                   rq.quality_status AS latest_render_quality_status,
                   rq.quality_score AS latest_render_quality_score
            FROM cases c
            LEFT JOIN customers cu ON cu.id = c.customer_id
            LEFT JOIN render_jobs latest ON latest.id = (
                SELECT j.id FROM render_jobs j
                WHERE j.case_id = c.id
                ORDER BY j.enqueued_at DESC, j.id DESC
                LIMIT 1
            )
            LEFT JOIN render_quality rq ON rq.render_job_id = latest.id
            WHERE c.id = ? AND c.trashed_at IS NULL
            """,
            (case_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "case not found")

    auto_raw = json.loads(row["blocking_issues_json"] or "[]")
    manual_raw = json.loads(row["manual_blocking_issues_json"] or "[]") if row["manual_blocking_issues_json"] else []
    # manual_blocking_codes stays as bare strings (frontend chip-toggle UI uses code-only)
    manual_codes = [
        issue_translator.normalize_issue(it)["code"]
        for it in manual_raw
        if issue_translator.normalize_issue(it)["code"]
    ]
    effective = issue_translator.merge_codes([*auto_raw, *manual_raw])
    meta = json.loads(row["meta_json"] or "{}")

    # Stage A: skill 透传字段(只在 upgrade 后非空)
    def _safe_list(col: str) -> list[Any]:
        if col not in row.keys():
            return []
        raw = row[col]
        if not raw:
            return []
        try:
            val = json.loads(raw)
            return val if isinstance(val, list) else []
        except (TypeError, ValueError):
            return []

    skill_image_metadata = _safe_list("skill_image_metadata_json")
    skill_blocking_detail = _safe_list("skill_blocking_detail_json")
    skill_warnings = _safe_list("skill_warnings_json")
    # 兼容 Stage A 之前已升级的 case:db 列为空时直接读 manifest.final.json
    if not skill_image_metadata and not skill_blocking_detail and not skill_warnings:
        fb = _fallback_skill_from_manifest(row["abs_path"])
        skill_image_metadata = fb["image_metadata"]
        skill_blocking_detail = fb["blocking_detail"]
        skill_warnings = fb["warnings"]
    raw_image_files = [str(x) for x in (meta.get("image_files") or []) if x]
    image_files = source_images.filter_source_image_files(raw_image_files)
    with db.connect() as bind_conn:
        bound_rows = _bound_rows(bind_conn, _source_binding_case_ids(meta))
    if bound_rows:
        effective_raw_image_files: list[str] = []
        for source_row in [row, *bound_rows]:
            source_meta = _json_field(source_row, "meta_json", {})
            effective_raw_image_files.extend(_case_image_files_from_meta(source_meta))
        meta["source_case_bindings_effective_profile"] = source_images.classify_source_profile(effective_raw_image_files)
        meta["source_case_bindings_bound_case_ids"] = [int(item["id"]) for item in bound_rows]
    image_review_states = _image_review_states_from_meta(meta)
    if image_files:
        active_files = set(image_files)
        skill_image_metadata = [
            item
            for item in skill_image_metadata
            if str(item.get("filename") or item.get("relative_path") or "") in active_files
        ]

    # Stage B: 合并 case_image_overrides — 手动覆盖优先于 skill 自动判读。
    with db.connect() as ov_conn:
        overrides = _fetch_image_overrides(ov_conn, case_id)
    skill_image_metadata = _apply_overrides_to_metadata(
        skill_image_metadata,
        overrides,
        image_files=image_files,
    )
    skill_image_metadata = _apply_review_states_to_metadata(skill_image_metadata, image_review_states)
    summary = _row_to_summary(row, row["canonical_name"])
    classification_preflight = _build_classification_preflight(
        image_files=image_files,
        raw_image_files=raw_image_files,
        image_metadata=skill_image_metadata,
        case_id=case_id,
        case_category=summary.category,
    )
    if bound_rows:
        classification_preflight = _apply_source_group_authority_to_preflight(
            classification_preflight,
            case_source_group(case_id),
        )

    return CaseDetail(
        **summary.model_dump(),
        auto_blocking_issues=issue_translator.translate_list(auto_raw),
        manual_blocking_codes=manual_codes,
        blocking_issues=issue_translator.translate_list(effective),
        pose_delta_max=row["pose_delta_max"],
        sharp_ratio_min=row["sharp_ratio_min"],
        meta=meta,
        rename_suggestion=None,
        skill_image_metadata=skill_image_metadata,
        skill_blocking_detail=skill_blocking_detail,
        skill_warnings=skill_warnings,
        classification_preflight=classification_preflight,
    )


@router.patch("/{case_id}", response_model=CaseDetail)
def update_case(case_id: int, payload: CaseUpdate) -> CaseDetail:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM cases WHERE id = ? AND trashed_at IS NULL", (case_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        # Audit: snapshot before, apply, snapshot after.
        befores = audit.snapshot_before(conn, [case_id])
        _apply_update(conn, [case_id], payload)
        audit.record_after(
            conn, [case_id], befores, op="patch", source_route=f"/api/cases/{case_id}"
        )
    return case_detail(case_id)


@router.patch("/{case_id}/images/{filename:path}", response_model=ImageOverride)
def patch_image_override(
    case_id: int, filename: str, payload: ImageOverridePayload
) -> ImageOverride:
    """Stage B: 单张源图 phase / view 手动覆盖。

    - filename 是 case 目录下图片相对路径(不接受绝对路径或 ..);非法返回 400
    - manual_phase 必须 ∈ _ALLOWED_OVERRIDE_PHASES 或 ""(清除);其它返回 400
    - manual_view 必须 ∈ _ALLOWED_OVERRIDE_VIEWS 或 "";其它返回 400
    - 字段省略(None)= 不修改该维度;空字符串 = 清除该维度回到 skill 自动判读
    - 两个字段都清完 → 删除整行
    """
    def _norm(v: str | None, allowed: set[str], label: str) -> tuple[bool, str | None]:
        """Returns (touch?, value-to-write). Empty string → clear."""
        if v is None:
            return False, None
        if v == "":
            return True, None
        if v not in allowed:
            raise HTTPException(400, f"invalid {label}: {v!r}")
        return True, v

    touch_phase, phase_val = _norm(payload.manual_phase, _ALLOWED_OVERRIDE_PHASES, "manual_phase")
    touch_view, view_val = _norm(payload.manual_view, _ALLOWED_OVERRIDE_VIEWS, "manual_view")
    touch_transform = "manual_transform" in payload.model_fields_set
    if not touch_phase and not touch_view and not touch_transform:
        raise HTTPException(400, "no fields to update")

    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        case_row = conn.execute(
            "SELECT abs_path FROM cases WHERE id = ? AND trashed_at IS NULL", (case_id,)
        ).fetchone()
        if not case_row:
            raise HTTPException(404, "case not found")
        case_dir = Path(case_row["abs_path"]).resolve()
        filename = _validate_relative_image_name(case_dir, filename)
        existing = conn.execute(
            "SELECT manual_phase, manual_view, manual_transform_json FROM case_image_overrides WHERE case_id = ? AND filename = ?",
            (case_id, filename),
        ).fetchone()
        new_phase = phase_val if touch_phase else (existing["manual_phase"] if existing else None)
        new_view = view_val if touch_view else (existing["manual_view"] if existing else None)
        transform_arg = payload.manual_transform if touch_transform else _UNSET
        _write_image_override(conn, case_id, filename, new_phase, new_view, now, manual_transform=transform_arg)
        new_transform = (
            _decode_manual_transform(_manual_transform_to_json(payload.manual_transform))
            if touch_transform
            else (_decode_manual_transform(existing["manual_transform_json"]) if existing else None)
        )
    return ImageOverride(
        case_id=case_id,
        filename=filename,
        manual_phase=new_phase,
        manual_view=new_view,
        manual_transform=new_transform,
        updated_at=now,
    )


@router.post("/{case_id}/image-review/{filename:path}", response_model=ImageReviewResponse)
def review_case_image(
    case_id: int,
    filename: str,
    payload: ImageReviewPayload,
) -> ImageReviewResponse:
    """Record a source-image quality review decision without moving files.

    The decision lives under `cases.meta_json.image_review_states` and is
    tracked by the normal case revision audit. `excluded` is non-destructive:
    the file stays in place, but future formal renders skip it during scanning.
    """
    now = datetime.now(timezone.utc).isoformat()
    reviewer = (payload.reviewer or "operator").strip() or "operator"
    note = payload.note.strip() if payload.note else None
    layer = payload.layer.strip() if payload.layer else None
    with db.connect() as conn:
        case_dir = _case_dir_for_update(conn, case_id)
        filename = _validate_relative_image_name(case_dir, filename)
        _resolve_existing_source(case_dir, filename)
        befores = audit.snapshot_before(conn, [case_id])
        row = conn.execute("SELECT meta_json FROM cases WHERE id = ?", (case_id,)).fetchone()
        meta: dict[str, Any] = {}
        if row and row["meta_json"]:
            try:
                parsed = json.loads(row["meta_json"])
                if isinstance(parsed, dict):
                    meta = parsed
            except (TypeError, ValueError):
                meta = {}
        states = _image_review_states_from_meta(meta)
        if payload.verdict == "reopen":
            states.pop(filename, None)
            review_state = None
        else:
            review_state = {
                "verdict": payload.verdict,
                "label": _IMAGE_REVIEW_VERDICT_LABEL[payload.verdict],
                "reviewer": reviewer,
                "note": note,
                "layer": layer,
                "render_excluded": payload.verdict == "excluded",
                "reviewed_at": now,
            }
            states[filename] = review_state
        if states:
            meta[_IMAGE_REVIEW_META_KEY] = states
        else:
            meta.pop(_IMAGE_REVIEW_META_KEY, None)
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), case_id),
        )
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="image_review",
            source_route=f"/api/cases/{case_id}/image-review/{filename}",
        )
    return ImageReviewResponse(
        case_id=case_id,
        filename=filename,
        review_state=review_state,
        detail=case_detail(case_id),
    )


@router.post("/{case_id}/manual-render-sources", response_model=ManualRenderSourcesResponse)
def prepare_manual_render_sources(
    case_id: int,
    payload: ManualRenderSourcesRequest,
) -> ManualRenderSourcesResponse:
    """Materialize a user-selected before/after pair as standard named sources.

    The render skill pairs images while building the manifest. Writing only a
    late manual phase/view override is not enough for previously unlabeled
    cases, so this endpoint copies/saves the chosen images into the case
    directory using names the skill already understands, then rescans the case.
    """
    view = payload.view
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    created: list[str] = []
    try:
        with db.connect() as conn:
            case_dir = _case_dir_for_update(conn, case_id)
            befores = audit.snapshot_before(conn, [case_id])
            before_name = _materialize_manual_image(case_dir, payload.before, "before", view, stamp)
            created.append(before_name)
            after_name = _materialize_manual_image(case_dir, payload.after, "after", view, stamp)
            created.append(after_name)

            try:
                scanner.rescan_one(conn, case_id)
            except ValueError as e:
                raise HTTPException(400, str(e))

            # The scanner caps meta.image_files at 50. Keep the just-created
            # manual files visible in the detail page even for very large cases.
            row = conn.execute("SELECT meta_json FROM cases WHERE id = ?", (case_id,)).fetchone()
            meta = json.loads(row["meta_json"] or "{}") if row else {}
            image_files = source_images.filter_source_image_files(
                [str(x) for x in (meta.get("image_files") if isinstance(meta, dict) else []) if x]
            )
            if isinstance(image_files, list):
                merged = list(dict.fromkeys([*created, *[str(x) for x in image_files]]))
                meta["image_files"] = merged[:50]
                conn.execute(
                    "UPDATE cases SET meta_json = ? WHERE id = ?",
                    (json.dumps(meta, ensure_ascii=False), case_id),
                )

            now = datetime.now(timezone.utc).isoformat()
            before_transform = _decode_manual_transform(_manual_transform_to_json(payload.before_transform))
            _write_image_override(
                conn,
                case_id,
                before_name,
                "before",
                view,
                now,
                manual_transform=before_transform,
            )
            _write_image_override(conn, case_id, after_name, "after", view, now)
            audit.record_after(
                conn,
                [case_id],
                befores,
                op="rescan",
                source_route=f"/api/cases/{case_id}/manual-render-sources",
                actor="user",
            )
    except Exception:
        for name in created:
            try:
                (case_dir / name).unlink(missing_ok=True)  # type: ignore[possibly-undefined]
            except Exception:
                pass
        raise

    return ManualRenderSourcesResponse(
        case_id=case_id,
        view=view,
        created_files=created,
        manual_overrides=[
            ImageOverride(
                case_id=case_id,
                filename=created[0],
                manual_phase="before",
                manual_view=view,
                manual_transform=before_transform,
                updated_at=now,
            ),
            ImageOverride(
                case_id=case_id,
                filename=created[1],
                manual_phase="after",
                manual_view=view,
                manual_transform=None,
                updated_at=now,
            ),
        ],
        detail=case_detail(case_id),
    )


@router.post("/{case_id}/manual-render-preview", response_model=ManualRenderPreviewResponse)
def preview_manual_render(
    case_id: int,
    payload: ManualRenderPreviewRequest,
) -> ManualRenderPreviewResponse:
    """Generate a temporary one-view formal preview before saving sources.

    This intentionally does not copy files into the case source list, does not
    rescan, and does not enqueue a render job. The preview lives under a hidden
    workbench directory and is only used for quick visual comparison.
    """
    preview_id = uuid.uuid4().hex
    with db.connect() as conn:
        case_dir = _case_dir_for_update(conn, case_id)
    preview_dir = _preview_root(case_dir) / preview_id
    preview_dir.mkdir(parents=True, exist_ok=False)
    try:
        before_path = _materialize_preview_image(case_dir, preview_dir, payload.before, "before")
        after_path = _materialize_preview_image(case_dir, preview_dir, payload.after, "after")
        before_transform = _decode_manual_transform(_manual_transform_to_json(payload.before_transform))
        result = render_executor.run_manual_render_preview(
            case_dir=case_dir,
            preview_dir=preview_dir,
            brand=payload.brand or _FALLBACK_BRAND,
            view=payload.view,
            before_path=before_path,
            after_path=after_path,
            before_transform=before_transform,
        )
        _prune_preview_dirs(case_dir)
    except Exception:
        shutil.rmtree(preview_dir, ignore_errors=True)
        raise

    return ManualRenderPreviewResponse(
        case_id=case_id,
        preview_id=preview_id,
        view=payload.view,
        output_path=str(result.get("output_path") or ""),
        manifest_path=result.get("manifest_path"),
        render_plan=result.get("render_plan") or {},
        warnings=[str(x) for x in (result.get("warnings") or [])],
    )


@router.get("/{case_id}/manual-render-preview/{preview_id}/file")
def manual_render_preview_file(case_id: int, preview_id: str) -> FileResponse:
    if not _PREVIEW_ID_RE.match(preview_id):
        raise HTTPException(400, "invalid preview id")
    with db.connect() as conn:
        row = conn.execute("SELECT abs_path, trashed_at FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(404, "case not found")
    if row["trashed_at"]:
        raise HTTPException(410, "case has been moved to trash")
    case_dir = Path(row["abs_path"]).resolve()
    target = (_preview_root(case_dir) / preview_id / "preview.jpg").resolve()
    try:
        target.relative_to(_preview_root(case_dir).resolve())
    except ValueError:
        raise HTTPException(400, "invalid preview path")
    if not target.is_file():
        raise HTTPException(404, "preview not found")
    return FileResponse(target)


@router.post("/{case_id}/images/trash", response_model=ImageTrashResponse)
def trash_case_image(case_id: int, payload: ImageTrashRequest) -> ImageTrashResponse:
    stress.assert_destructive_allowed("image trash")
    with db.connect() as conn:
        case_dir = _case_dir_for_update(conn, case_id)
        befores = audit.snapshot_before(conn, [case_id])
        original, trash_path = _trash_image(conn, case_id, case_dir, payload.filename)
        try:
            scanner.rescan_one(conn, case_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="rescan",
            source_route=f"/api/cases/{case_id}/images/trash",
            actor="user",
        )
    return ImageTrashResponse(
        case_id=case_id,
        original_filename=original,
        trash_path=trash_path,
        detail=case_detail(case_id),
    )


@router.post("/{case_id}/images/restore", response_model=ImageRestoreResponse)
def restore_case_image(case_id: int, payload: ImageRestoreRequest) -> ImageRestoreResponse:
    stress.assert_destructive_allowed("image restore")
    with db.connect() as conn:
        case_dir = _case_dir_for_update(conn, case_id)
        befores = audit.snapshot_before(conn, [case_id])
        restored = _restore_trashed_image(case_dir, payload.trash_path, payload.restore_to)
        try:
            scanner.rescan_one(conn, case_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="rescan",
            source_route=f"/api/cases/{case_id}/images/restore",
            actor="user",
        )
    return ImageRestoreResponse(
        case_id=case_id,
        trash_path=payload.trash_path,
        restored_filename=restored,
        detail=case_detail(case_id),
    )


@router.post("/{case_id}/reveal", response_model=CaseRevealResponse)
def reveal_case_path(case_id: int, payload: CaseRevealRequest) -> CaseRevealResponse:
    """Open a local Finder window for a safe case-owned path."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT abs_path FROM cases WHERE id = ? AND trashed_at IS NULL",
            (case_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "case not found")

    case_dir = Path(row["abs_path"]).resolve()
    if not case_dir.is_dir():
        raise HTTPException(404, "case directory not found")

    if payload.target == "case_root":
        target = case_dir
    else:
        brand = payload.brand or _FALLBACK_BRAND
        template = payload.template or _FALLBACK_TEMPLATE
        target = (case_dir / ".case-layout-output" / brand / template / "render").resolve()
        try:
            target.relative_to(case_dir)
        except ValueError:
            raise HTTPException(400, "invalid path")
        if not target.is_dir():
            raise HTTPException(404, "render output directory not found")

    try:
        subprocess.run(["open", str(target)], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HTTPException(500, f"open failed: {exc}") from exc
    return CaseRevealResponse(opened=True, path=str(target))


@router.post("/{case_id}/simulate-after", response_model=SimulateAfterResponse)
def simulate_case_after(case_id: int, payload: SimulateAfterRequest) -> SimulateAfterResponse:
    focus_targets = [x.strip() for x in payload.focus_targets if x.strip()]
    focus_regions = _normalize_focus_regions(payload.focus_regions)
    if not payload.ai_generation_authorized:
        raise HTTPException(400, "ai_generation_authorized must be true")
    provider = (payload.provider or _SIMULATION_PROVIDER).strip()
    if provider != _SIMULATION_PROVIDER:
        raise HTTPException(400, f"provider must be {_SIMULATION_PROVIDER}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    with db.connect() as conn:
        case_dir = _case_dir_for_update(conn, case_id)
        after_path = _resolve_simulation_image_input(
            case_dir,
            path=payload.after_image_path,
            image=payload.after_image,
            role="after",
            stamp=stamp,
            required=True,
        )
        assert after_path is not None
        before_path = _resolve_simulation_image_input(
            case_dir,
            path=payload.before_image_path,
            image=payload.before_image,
            role="before",
            stamp=stamp,
            required=False,
        )
        style_reference_paths = [
            _resolve_style_reference_path(p) for p in payload.style_reference_paths
        ]
        input_refs = [
            _simulation_input_ref(case_dir, "after_source", after_path),
            *(
                [_simulation_input_ref(case_dir, "before_pose_reference", before_path)]
                if before_path
                else []
            ),
            *[
                _simulation_input_ref(case_dir, "style_reference", sp)
                for sp in style_reference_paths
            ],
        ]
        job_id = _insert_simulation_job(
            conn,
            case_id=case_id,
            focus_targets=focus_targets,
            focus_regions=focus_regions,
            input_refs=input_refs,
            provider=provider,
            model_name=payload.model_name,
            note=payload.note,
        )
        ai_run_id = _insert_ai_run(
            conn,
            job_id=job_id,
            provider=provider,
            model_name=payload.model_name,
            focus_targets=focus_targets,
            focus_regions=focus_regions,
            input_refs=input_refs,
            status="running",
        )

    try:
        result = ai_generation_adapter.run_ps_model_router_after_simulation(
            job_id=job_id,
            after_image_path=after_path,
            before_image_path=before_path,
            focus_targets=focus_targets,
            focus_regions=focus_regions,
            model_name=payload.model_name,
            note=payload.note,
            style_reference_image_paths=style_reference_paths,
        )
        status = str(result["status"])
        output_refs = result["output_refs"]
        audit_payload = {
            **result["audit"],
            "input_refs": input_refs,
            "output_refs": output_refs,
        }
        audit_payload = stress.tag_payload(audit_payload)
        error_message = result.get("error_message") if status != "done" else None
        with db.connect() as conn:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE simulation_jobs
                SET status = ?,
                    output_refs_json = ?,
                    watermarked = ?,
                    audit_json = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(output_refs, ensure_ascii=False),
                    1 if result.get("watermarked") else 0,
                    json.dumps(audit_payload, ensure_ascii=False),
                    error_message,
                    now,
                    job_id,
                ),
            )
            conn.execute(
                """
                UPDATE ai_runs
                SET output_json = ?,
                    status = ?,
                    error_message = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps({"output_refs": output_refs, "audit": audit_payload}, ensure_ascii=False),
                    status,
                    error_message,
                    now,
                    ai_run_id,
                ),
            )
    except Exception as exc:  # noqa: BLE001 - external model/router failure is recorded, not raised as 500
        status = "failed"
        output_refs = []
        audit_payload = {
            "provider": provider,
            "model_name": payload.model_name,
            "focus_targets": focus_targets,
            "focus_regions": focus_regions,
            "input_refs": input_refs,
            "output_refs": output_refs,
            "policy": _simulation_policy(focus_regions),
            "note": payload.note,
        }
        audit_payload = stress.tag_payload(audit_payload)
        error_message = str(exc)[:4000]
        with db.connect() as conn:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE simulation_jobs
                SET status = 'failed',
                    output_refs_json = '[]',
                    watermarked = 0,
                    audit_json = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(audit_payload, ensure_ascii=False), error_message, now, job_id),
            )
            conn.execute(
                """
                UPDATE ai_runs
                SET output_json = ?,
                    status = 'failed',
                    error_message = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (json.dumps({"error": error_message}, ensure_ascii=False), error_message, now, ai_run_id),
            )

    return SimulateAfterResponse(
        simulation_job_id=job_id,
        case_id=case_id,
        status=status,
        focus_targets=focus_targets,
        focus_regions=focus_regions,
        provider=provider,
        model_name=payload.model_name,
        input_refs=input_refs,
        output_refs=output_refs,
        audit=audit_payload,
        error_message=error_message,
    )


@router.get("/{case_id}/simulation-jobs", response_model=list[SimulationJob])
def list_case_simulation_jobs(
    case_id: int,
    limit: int = Query(10, ge=1, le=100),
) -> list[SimulationJob]:
    with db.connect() as conn:
        exists = conn.execute("SELECT id FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "case not found")
        rows = conn.execute(
            """
            SELECT * FROM simulation_jobs
            WHERE case_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (case_id, limit),
        ).fetchall()
    return [_simulation_row_to_model(row) for row in rows]


@router.get("/{case_id}/simulation-jobs/{job_id}/file")
def simulation_job_file(
    case_id: int,
    job_id: int,
    kind: str = Query("ai_after_simulation"),
) -> FileResponse:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM simulation_jobs WHERE id = ? AND case_id = ?",
            (job_id, case_id),
        ).fetchone()
    if not row:
        raise HTTPException(404, "simulation job not found")
    return FileResponse(_simulation_output_file(row, kind))


@router.post("/{case_id}/simulation-jobs/{job_id}/review", response_model=SimulationJob)
def review_simulation_job(
    case_id: int,
    job_id: int,
    payload: SimulationJobReviewRequest,
) -> SimulationJob:
    return _review_simulation_job_by_id(job_id, payload, case_id=case_id)


@router.post("/batch")
def batch_update(payload: CaseBatchUpdate) -> dict:
    if not payload.case_ids:
        raise HTTPException(400, "case_ids cannot be empty")
    with db.connect() as conn:
        placeholders = ",".join("?" * len(payload.case_ids))
        rows = conn.execute(
            f"SELECT id FROM cases WHERE trashed_at IS NULL AND id IN ({placeholders})", payload.case_ids
        ).fetchall()
        valid_ids = [r["id"] for r in rows]
        if not valid_ids:
            raise HTTPException(404, "no matching cases")
        # Audit: snapshot before, apply, snapshot after — one revision per case.
        befores = audit.snapshot_before(conn, valid_ids)
        _apply_update(conn, valid_ids, payload.update)
        audit.record_after(
            conn, valid_ids, befores, op="batch", source_route="/api/cases/batch"
        )
    return {"updated": len(valid_ids), "case_ids": valid_ids}


def _apply_update(conn: sqlite3.Connection, case_ids: list[int], payload: CaseUpdate) -> None:
    sets: list[str] = []
    values: list[Any] = []
    clear = set(payload.clear_fields or [])

    def set_or_clear(field_db: str, value: Any | None, clear_key: str, json_encode: bool = False):
        if clear_key in clear:
            sets.append(f"{field_db} = NULL")
            return
        if value is None:
            return
        sets.append(f"{field_db} = ?")
        values.append(json.dumps(value, ensure_ascii=False) if json_encode else value)

    set_or_clear("manual_category", payload.manual_category, "manual_category")
    set_or_clear("manual_template_tier", payload.manual_template_tier, "manual_template_tier")
    set_or_clear(
        "manual_blocking_issues_json",
        payload.manual_blocking_codes,
        "manual_blocking_codes",
        json_encode=True,
    )
    set_or_clear("notes", payload.notes, "notes")
    set_or_clear("tags_json", payload.tags, "tags", json_encode=True)
    set_or_clear("review_status", payload.review_status, "review_status")
    set_or_clear("customer_id", payload.customer_id, "customer_id")
    # 三态之 "挂起"
    set_or_clear("held_until", payload.held_until, "held_until")
    set_or_clear("hold_reason", payload.hold_reason, "hold_reason")

    if payload.review_status == "reviewed":
        sets.append("reviewed_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
    elif "review_status" in clear or payload.review_status in {"pending", "needs_recheck"}:
        sets.append("reviewed_at = NULL")

    if not sets:
        return

    placeholders = ",".join("?" * len(case_ids))
    sql = f"UPDATE cases SET {', '.join(sets)} WHERE id IN ({placeholders})"
    conn.execute(sql, [*values, *case_ids])


@router.get("/{case_id}/files")
def case_file(case_id: int, name: str):
    with db.connect() as conn:
        row = conn.execute("SELECT abs_path FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(404, "case not found")
    base = Path(row["abs_path"]).resolve()
    target = (base / name).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(400, "invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(target)


@router.get("/{case_id}/rename-suggestion")
def rename_suggestion(case_id: int, dry_run: bool = True) -> dict:
    """Return a rename hint for non_labeled cases.

    `dry_run=true` (default and currently the only mode) means the response includes
    the candidate filenames that *would* be touched but doesn't actually rename. We
    keep `dry_run` in the surface so the UI can promise "not applied yet" loudly.
    """
    with db.connect() as conn:
        row = conn.execute(
            """SELECT abs_path, COALESCE(manual_category, category) AS cat,
                      meta_json
               FROM cases WHERE id = ?""",
            (case_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "case not found")
    if row["cat"] != "non_labeled":
        return {
            "command": None,
            "note": "当前案例已有标准命名，无需重命名",
            "dry_run": True,
            "affected_count": 0,
            "affected_files": [],
        }
    base = shlex.quote(row["abs_path"])
    meta = json.loads(row["meta_json"] or "{}")
    image_files: list[str] = source_images.filter_source_image_files([str(x) for x in (meta.get("image_files") or []) if x])
    affected = [
        f for f in image_files
        if not any(tok in f for tok in scanner.LABELED_TOKENS)
    ]
    return {
        "command": f"# 在 {base} 下，把术前/术后图片改成：术前-正面.jpg / 术后-正面.jpg / 术前-右45侧.jpg / ...",
        "note": "本阶段不直接执行，仅给出建议命令模板",
        "dry_run": dry_run,
        "affected_count": len(affected),
        "affected_files": affected[:20],
    }


@router.post("/{case_id}/upgrade")
def upgrade_case(case_id: int, brand: str = "fumei") -> CaseDetail:
    """Run case-layout-board's `build_manifest()` and persist the v3 results.

    Synchronous path (5-30s blocking). The shared core lives in
    `_upgrade_executor.execute_upgrade` so the upgrade_queue worker uses the
    exact same logic.
    """
    try:
        _upgrade_executor.execute_upgrade(
            case_id, brand, source_route=f"/api/cases/{case_id}/upgrade"
        )
    except ValueError:
        raise HTTPException(404, "case not found")
    except FileNotFoundError as e:
        raise HTTPException(404, f"case directory missing: {e}")
    except RuntimeError as e:
        raise HTTPException(503, f"skill unavailable: {e}")
    except Exception as e:  # noqa: BLE001 — skill can raise many flavors
        raise HTTPException(500, f"skill upgrade failed: {e}")
    return case_detail(case_id)


@router.post("/{case_id}/rescan")
def rescan_case(case_id: int) -> CaseDetail:
    """Re-run lite scanner on a single case dir.

    Useful after the user manually renamed files on disk and wants the auto-judged
    category/tier/blocking refreshed without scanning the whole library.
    """
    with db.connect() as conn:
        existing = conn.execute("SELECT id FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "case not found")
        # Audit before/after the scanner write.
        befores = audit.snapshot_before(conn, [case_id])
        try:
            scanner.rescan_one(conn, case_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="rescan",
            source_route=f"/api/cases/{case_id}/rescan",
            actor="scan",
        )
    return case_detail(case_id)
