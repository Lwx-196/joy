"""Render job queue — async-friendly singleton with SSE broadcast.

Design:
- Worker pool comes from `_job_pool` (shared with upgrade_queue, max_workers=2)
  so render and upgrade compete for the same 2 slots and never run 4 mediapipe
  subprocesses (~150MB each) in parallel.
- Job lifecycle: queued → running → (done | failed | cancelled).
  Cancellation is only honored while still queued; running jobs run to completion
  to avoid leaving mediapipe / cv2 in undefined states.
- SSE: each /api/render/stream subscriber owns an asyncio.Queue. Worker threads
  publish events via loop.call_soon_threadsafe(queue.put_nowait, payload). The
  publish loop captures the FastAPI event loop on first subscription.
- Recovery: on import / startup, residual `status='running'` rows are demoted to
  'queued' (they were interrupted by a previous crash) and resubmitted to the
  pool. queued rows from a clean shutdown are also resubmitted.

Public API used by routes/render.py:
- enqueue(case_id, brand, template, semantic_judge) -> job_id
- enqueue_batch(case_ids, brand, template, semantic_judge) -> (batch_id, job_ids)
- cancel(job_id) -> bool (only if status == 'queued')
- undo_render(case_id) -> dict (delete output file + audit op=undo_render)
- subscribe() -> async iterator yielding event dicts (for SSE)
- recover() -> resubmits queued + running jobs (called once at startup)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from . import _job_pool, ai_generation_adapter, audit, db, render_executor, render_quality, skill_bridge, source_images, source_selection, stress

DEFAULT_TEMPLATE = "tri-compare"
DEFAULT_SEMANTIC_JUDGE = "auto"
LOGGER = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


SEMANTIC_AUTO_SOURCE_LIMIT = _env_int("CASE_WORKBENCH_SEMANTIC_AUTO_SOURCE_LIMIT", 18)


def _code_version_summary() -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            timeout=1,
            check=False,
        ).stdout.strip()
    except Exception:
        commit = ""
    return {
        "commit": commit or "unknown",
        "dirty": True,
    }


def _source_manifest_hash(result: dict[str, Any]) -> str | None:
    plan = result.get("render_selection_plan")
    if not isinstance(plan, dict):
        plan = {
            "provenance": result.get("render_selection_source_provenance") or [],
            "dropped": result.get("render_selection_dropped_slots") or [],
            "missing": result.get("render_selection_missing_slots") or [],
        }
    try:
        payload = json.dumps(plan, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return "sha256:" + hashlib.sha256(payload).hexdigest()
BEFORE_TOKENS = ("术前", "治疗前", "before", "pre")
AFTER_TOKENS = ("术后", "治疗后", "after", "post")
IMAGE_REVIEW_META_KEY = "image_review_states"
LOW_CONFIDENCE_REVIEW_BELOW = 0.55


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_case_meta(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _case_source_info(raw_meta: str | None, abs_path: str | None = None) -> tuple[int, list[str]]:
    meta = _parse_case_meta(raw_meta)
    raw_files = [str(item) for item in (meta.get("image_files") or []) if item]
    if abs_path:
        split = source_images.existing_source_image_files(abs_path, raw_files)
        image_files = [str(item) for item in split["existing"]]
    else:
        image_files = source_images.filter_source_image_files(raw_files)
    return len(image_files), image_files


def _case_source_profile(raw_meta: str | None, abs_path: str | None = None) -> dict[str, object]:
    meta = _parse_case_meta(raw_meta)
    image_files = [str(item) for item in (meta.get("image_files") or []) if item]
    if abs_path:
        return source_images.classify_existing_case_source_profile(abs_path, image_files)
    return source_images.classify_source_profile(image_files)


def _json_list(raw: str | None) -> list[object]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _source_binding_case_ids(raw_meta: str | None) -> list[int]:
    meta = _parse_case_meta(raw_meta)
    bindings = meta.get(source_images.SOURCE_BINDINGS_META_KEY)
    raw_ids = bindings.get("case_ids") if isinstance(bindings, dict) else bindings if isinstance(bindings, list) else []
    out: list[int] = []
    for item in raw_ids or []:
        try:
            cid = int(item)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in out:
            out.append(cid)
    return out


def _merged_bound_profile(rows: list[dict[str, Any]]) -> dict[str, object]:
    merged_files: list[str] = []
    missing_files: list[str] = []
    raw_meta_image_count = 0
    for row in rows:
        case_name = Path(str(row.get("abs_path") or "")).name or f"case-{row.get('id')}"
        meta = _parse_case_meta(str(row.get("meta_json") or ""))
        raw_files = [str(item) for item in (meta.get("image_files") or []) if item]
        raw_meta_image_count += len(raw_files)
        split = source_images.existing_source_image_files(str(row.get("abs_path") or ""), raw_files)
        for filename in [str(item) for item in split["existing"]]:
            merged_files.append(str(Path(f"case{row.get('id')}-{case_name}") / filename))
        for filename in [str(item) for item in raw_files if not source_images.is_source_image_file(str(item))]:
            merged_files.append(str(Path(f"case{row.get('id')}-{case_name}") / filename))
        for filename in [str(item) for item in split["missing"]]:
            missing_files.append(str(Path(f"case{row.get('id')}-{case_name}") / filename))
    profile = source_images.classify_source_profile(merged_files)
    profile["raw_meta_image_count"] = raw_meta_image_count
    profile["missing_source_count"] = len(missing_files)
    profile["missing_source_samples"] = missing_files[:8]
    profile["file_integrity_status"] = "missing_source_files" if missing_files else "ok"
    if missing_files and not merged_files:
        profile["source_kind"] = "missing_source_files"
    return profile


def _safe_link_name(case_id: int, case_path: str, filename: str) -> str:
    case_name = Path(case_path).name or f"case-{case_id}"
    rel = str(filename).replace("\\", "/").strip("/")
    safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", f"case{case_id}-{case_name}-{rel}")
    return safe[:180] or f"case{case_id}-{Path(filename).name}"


def _build_bound_source_staging(
    job_id: int,
    rows: list[dict[str, Any]],
    primary_case_dir: str,
) -> tuple[Path, list[str], dict[tuple[int, str], str]]:
    staging = Path(primary_case_dir) / ".case-workbench-bound-render" / f"job-{job_id}"
    staging.mkdir(parents=True, exist_ok=True)
    linked: list[str] = []
    render_names: dict[tuple[int, str], str] = {}
    used_names: set[str] = set()
    for row in rows:
        case_id = int(row["id"])
        case_dir = Path(str(row["abs_path"]))
        meta = _parse_case_meta(str(row.get("meta_json") or ""))
        for filename in [str(item) for item in (meta.get("image_files") or []) if item]:
            if not source_images.is_source_image_file(filename):
                continue
            rel = Path(filename)
            if rel.is_absolute() or ".." in rel.parts:
                continue
            source = (case_dir / rel).resolve()
            if not source.is_file():
                continue
            link_name = _safe_link_name(case_id, str(case_dir), filename)
            stem = Path(link_name).stem
            suffix = Path(link_name).suffix
            candidate = link_name
            idx = 2
            while candidate in used_names or (staging / candidate).exists():
                candidate = f"{stem}-{idx}{suffix}"
                idx += 1
            used_names.add(candidate)
            target = staging / candidate
            try:
                os.symlink(source, target)
            except FileExistsError:
                pass
            linked.append(candidate)
            render_names[(case_id, filename)] = candidate
    return staging, linked, render_names


def _cleanup_bound_source_staging(job_id: int, staging_dir: Path | None) -> None:
    if staging_dir is None:
        return
    staging = Path(staging_dir)
    if staging.name != f"job-{job_id}" or staging.parent.name != ".case-workbench-bound-render":
        LOGGER.warning("skip unsafe bound source staging cleanup: job=%s path=%s", job_id, staging)
        return
    if staging.is_symlink():
        LOGGER.warning("skip symlink bound source staging cleanup: job=%s path=%s", job_id, staging)
        return
    try:
        shutil.rmtree(staging)
    except FileNotFoundError:
        return
    except OSError as exc:
        LOGGER.warning("failed to clean bound source staging: job=%s path=%s error=%s", job_id, staging, exc)


def _render_excluded_files(raw_meta: str | None) -> set[str]:
    meta = _parse_case_meta(raw_meta)
    states = meta.get(IMAGE_REVIEW_META_KEY)
    if not isinstance(states, dict):
        return set()
    out: set[str] = set()
    for filename, state in states.items():
        if not isinstance(state, dict):
            continue
        if state.get("render_excluded") or state.get("verdict") == "excluded":
            out.add(str(filename))
    return out


def _image_review_states(raw_meta: str | None) -> dict[str, dict[str, Any]]:
    meta = _parse_case_meta(raw_meta)
    states = meta.get(IMAGE_REVIEW_META_KEY)
    if not isinstance(states, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for filename, state in states.items():
        if isinstance(state, dict):
            out[str(filename)] = dict(state)
    return out


def _state_for_filename(states: dict[str, dict[str, Any]], filename: str) -> dict[str, Any] | None:
    return states.get(filename) or states.get(Path(filename).name)


def _skill_metadata_by_file(raw: str | None) -> dict[str, dict[str, Any]]:
    data = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                data = parsed
        except (TypeError, ValueError):
            data = []
    out: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        for key in (item.get("filename"), item.get("relative_path")):
            if key:
                out[str(key)] = item
                out[Path(str(key)).name] = item
    return out


_METADATA_FALLBACK_FIELDS = {
    "angle_confidence",
    "rejection_reason",
    "issues",
    "pose",
    "direction",
    "sharpness_score",
}


def _empty_metadata_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _selection_metadata_with_fallback(
    metadata: dict[str, Any] | None,
    fallback: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(fallback, dict):
        return metadata if isinstance(metadata, dict) else None
    if not isinstance(metadata, dict):
        merged = dict(fallback)
        merged["selection_metadata_source"] = "primary_render_history"
        return merged
    merged = dict(metadata)
    used = False
    for key in _METADATA_FALLBACK_FIELDS:
        if _empty_metadata_value(merged.get(key)) and not _empty_metadata_value(fallback.get(key)):
            merged[key] = fallback.get(key)
            used = True
    if used:
        merged["selection_metadata_source"] = "primary_render_history"
    return merged


def _fetch_case_image_overrides(conn: sqlite3.Connection, case_id: int) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """SELECT filename, manual_phase, manual_view, manual_transform_json, reason_json, reviewer
           FROM case_image_overrides WHERE case_id = ?""",
        (case_id,),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        item: dict[str, Any] = {"phase": row["manual_phase"], "view": row["manual_view"]}
        if row["manual_transform_json"]:
            try:
                transform = json.loads(row["manual_transform_json"])
            except (TypeError, ValueError):
                transform = None
            if isinstance(transform, dict):
                item["transform"] = transform
        if row["reviewer"]:
            item["reviewer"] = row["reviewer"]
        reason_json = row["reason_json"]
        if reason_json:
            item["reason_json"] = reason_json
            try:
                reason_payload = json.loads(reason_json)
            except (TypeError, ValueError):
                reason_payload = None
            if isinstance(reason_payload, dict):
                reason = str(reason_payload.get("reason") or "").strip()
                if reason:
                    item["reason"] = reason
            else:
                reason = str(reason_json).strip()
                if reason:
                    item["reason"] = reason
        out[str(row["filename"])] = item
    return out


def _phase_from_filename(filename: str) -> str | None:
    lowered = filename.lower()
    if any(token.lower() in lowered for token in BEFORE_TOKENS):
        return "before"
    if any(token.lower() in lowered for token in AFTER_TOKENS):
        return "after"
    return None


def _view_from_filename(filename: str) -> str | None:
    lowered = filename.lower()
    if re.search(r"3/4|34|45|45°|45度|45侧|四分之三|微侧|斜侧|斜面|半侧|oblique", filename, re.I):
        return "oblique"
    if "正面" in filename or "正脸" in filename or "front" in lowered:
        return "front"
    if re.search(r"侧面|侧脸|左侧(?!背景)|右侧(?!背景)|左脸|右脸|side|profile", filename, re.I):
        return "side"
    return None


def _metadata_phase_view(
    filename: str,
    manual_override: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
) -> tuple[str | None, str | None, bool]:
    manual_phase = (manual_override or {}).get("phase")
    manual_view = (manual_override or {}).get("view")
    phase = manual_phase if manual_phase in {"before", "after"} else None
    view = manual_view if manual_view in {"front", "oblique", "side"} else None
    manual = (
        (phase is not None and str((manual_override or {}).get("phase_source") or "manual") == "manual")
        or (view is not None and str((manual_override or {}).get("view_source") or "manual") == "manual")
    )
    if phase is None and isinstance(metadata, dict):
        meta_phase = metadata.get("phase")
        if meta_phase in {"before", "after"}:
            phase = str(meta_phase)
    if view is None and isinstance(metadata, dict):
        view_bucket = metadata.get("view_bucket") or metadata.get("angle")
        if view_bucket in {"front", "oblique", "side"}:
            view = str(view_bucket)
    return phase or _phase_from_filename(filename), view or _view_from_filename(filename), manual


def _selection_phase_view(
    filename: str,
    case_title: str,
    manual_override: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
) -> tuple[str | None, str, str | None, str, bool]:
    manual_phase = (manual_override or {}).get("phase")
    manual_view = (manual_override or {}).get("view")
    if manual_phase in {"before", "after"}:
        phase = str(manual_phase)
        phase_source = "manual"
    elif isinstance(metadata, dict) and metadata.get("phase") in {"before", "after"}:
        phase = str(metadata.get("phase"))
        phase_source = str(metadata.get("phase_source") or "skill")
    else:
        phase = _phase_from_filename(filename)
        phase_source = "filename" if phase else "unknown"
        if not phase:
            phase = _phase_from_filename(str(Path(case_title) / filename))
            phase_source = "directory" if phase else "unknown"

    if manual_view in {"front", "oblique", "side"}:
        view = str(manual_view)
        view_source = "manual"
    elif isinstance(metadata, dict) and (metadata.get("view_bucket") or metadata.get("angle")) in {"front", "oblique", "side"}:
        view = str(metadata.get("view_bucket") or metadata.get("angle"))
        view_source = str(metadata.get("angle_source") or "skill")
    else:
        view = _view_from_filename(filename)
        view_source = "filename" if view else "unknown"

    manual = phase_source == "manual" or view_source == "manual"
    return phase, phase_source, view, view_source, manual


def _selection_override_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "phase": candidate.get("phase"),
        "view": candidate.get("view"),
        "phase_source": candidate.get("phase_source"),
        "view_source": candidate.get("view_source"),
        "angle_confidence": candidate.get("angle_confidence"),
        "selection_score": candidate.get("selection_score"),
        "selection_reasons": candidate.get("selection_reasons") or [],
        "quality_warnings": candidate.get("quality_warnings") or [],
        "risk_level": candidate.get("risk_level"),
        "source_case_id": candidate.get("case_id"),
        "source_filename": candidate.get("filename"),
        "source_role": candidate.get("source_role"),
        "selection_source": "source_selection",
    }
    if candidate.get("review_verdict"):
        item["review_verdict"] = candidate.get("review_verdict")
    if candidate.get("body_part"):
        item["body_part"] = candidate.get("body_part")
    if candidate.get("treatment_area"):
        item["treatment_area"] = candidate.get("treatment_area")
    if candidate.get("render_feedback"):
        item["render_feedback"] = candidate.get("render_feedback")
    if candidate.get("source_group_lock"):
        item["source_group_lock"] = candidate.get("source_group_lock")
    if candidate.get("selection_metadata_source"):
        item["selection_metadata_source"] = candidate.get("selection_metadata_source")
    transform = candidate.get("manual_transform")
    if isinstance(transform, dict):
        item["transform"] = transform
    return item


def _selection_plan_candidate(candidate: dict[str, Any] | None) -> dict[str, Any] | None:
    if not candidate:
        return None
    return {
        "case_id": candidate.get("case_id"),
        "source_role": candidate.get("source_role"),
        "filename": candidate.get("filename"),
        "render_filename": candidate.get("render_filename"),
        "phase": candidate.get("phase"),
        "phase_source": candidate.get("phase_source"),
        "view": candidate.get("view"),
        "view_source": candidate.get("view_source"),
        "review_verdict": candidate.get("review_verdict"),
        "angle_confidence": candidate.get("angle_confidence"),
        "selection_score": candidate.get("selection_score"),
        "selection_reasons": candidate.get("selection_reasons") or [],
        "quality_warnings": candidate.get("quality_warnings") or [],
        "risk_level": candidate.get("risk_level"),
        "render_feedback": candidate.get("render_feedback"),
        "source_group_lock": candidate.get("source_group_lock"),
        "selection_metadata_source": candidate.get("selection_metadata_source"),
        "pose": candidate.get("pose"),
        "direction": candidate.get("direction"),
        "sharpness_score": candidate.get("sharpness_score"),
        "brightness": candidate.get("brightness"),
        "mean_luma": candidate.get("mean_luma"),
        "luma": candidate.get("luma"),
        "exposure_score": candidate.get("exposure_score"),
        "exposure": candidate.get("exposure"),
        "crop_touches_frame": candidate.get("crop_touches_frame"),
        "face_crop_touches_frame": candidate.get("face_crop_touches_frame"),
        "crop_margin": candidate.get("crop_margin"),
        "face_crop_margin": candidate.get("face_crop_margin"),
        "identity_similarity": candidate.get("identity_similarity"),
        "same_person_similarity": candidate.get("same_person_similarity"),
        "arcface_similarity": candidate.get("arcface_similarity"),
        "face_count": candidate.get("face_count"),
        "identity_provider": candidate.get("identity_provider"),
    }


def _build_render_selection_context(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    render_names: dict[tuple[int, str], str] | None = None,
) -> dict[str, Any]:
    slots: dict[str, dict[str, Any]] = {
        view: {"before_candidates": [], "after_candidates": []}
        for view in ("front", "oblique", "side")
    }
    overrides_by_render_name: dict[str, dict[str, Any]] = {}
    render_names = render_names or {}
    primary_case_id = int(rows[0]["id"]) if rows else 0
    render_feedback = source_selection.render_feedback_from_history(conn, primary_case_id) if primary_case_id else None
    primary_render_metadata = _skill_metadata_by_file(str(rows[0].get("skill_image_metadata_json") or "")) if rows else {}
    selection_controls = source_selection.selection_controls_from_meta(
        _parse_case_meta(str(rows[0].get("meta_json") or "")) if rows else {}
    )
    locked_slots = selection_controls.get("locked_slots") if isinstance(selection_controls.get("locked_slots"), dict) else {}
    for index, source_row in enumerate(rows):
        case_id = int(source_row["id"])
        role = str(source_row.get("role") or ("primary" if index == 0 else "bound"))
        case_dir = str(source_row.get("abs_path") or "")
        case_title = Path(case_dir).name or f"case {case_id}"
        meta_json = str(source_row.get("meta_json") or "")
        meta = _parse_case_meta(meta_json)
        raw_files = [str(item) for item in (meta.get("image_files") or []) if item]
        existing_files = [
            str(item)
            for item in source_images.existing_source_image_files(case_dir, raw_files)["existing"]
            if source_images.is_source_image_file(str(item))
        ]
        metadata_by_file = _skill_metadata_by_file(str(source_row.get("skill_image_metadata_json") or ""))
        review_states = _image_review_states(meta_json)
        manual_overrides = _fetch_case_image_overrides(conn, case_id)
        for filename in existing_files:
            render_filename = render_names.get((case_id, filename), filename)
            state = _state_for_filename(review_states, filename) or {}
            if bool(state.get("render_excluded") or state.get("verdict") == "excluded"):
                overrides_by_render_name[render_filename] = {"render_excluded": True, "review_verdict": "excluded"}
                continue
            metadata = metadata_by_file.get(filename) or metadata_by_file.get(Path(filename).name)
            fallback_metadata = (
                primary_render_metadata.get(render_filename)
                or primary_render_metadata.get(Path(render_filename).name)
                or primary_render_metadata.get(filename)
                or primary_render_metadata.get(Path(filename).name)
            )
            metadata = _selection_metadata_with_fallback(metadata, fallback_metadata)
            manual_override = manual_overrides.get(filename) or manual_overrides.get(Path(filename).name)
            phase, phase_source, view, view_source, manual = _selection_phase_view(
                filename,
                case_title,
                manual_override,
                metadata,
            )
            if phase not in {"before", "after"} or view not in {"front", "oblique", "side"}:
                continue
            candidate: dict[str, Any] = {
                "case_id": case_id,
                "source_role": role,
                "filename": filename,
                "render_filename": render_filename,
                "phase": phase,
                "phase_source": phase_source,
                "view": view,
                "view_source": view_source,
                "manual": manual,
                "review_verdict": state.get("verdict"),
                "body_part": state.get("body_part"),
                "treatment_area": state.get("treatment_area"),
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
                "selection_metadata_source": (metadata or {}).get("selection_metadata_source") if isinstance(metadata, dict) else None,
            }
            if isinstance(manual_override, dict) and isinstance(manual_override.get("transform"), dict):
                candidate["manual_transform"] = manual_override["transform"]
            candidate.update(source_selection.candidate_quality(candidate, role))
            if candidate.get("selection_metadata_source") == "primary_render_history":
                reasons = [str(item) for item in (candidate.get("selection_reasons") or []) if item]
                if "复用最近渲染姿态画像" not in reasons:
                    candidate["selection_reasons"] = [*reasons, "复用最近渲染姿态画像"][:6]
            source_selection.apply_render_feedback(candidate, render_feedback)
            slots[str(view)][f"{phase}_candidates"].append(candidate)
            overrides_by_render_name[render_filename] = _selection_override_from_candidate(candidate)

    plan_slots: dict[str, dict[str, Any]] = {}
    dropped_slots: list[dict[str, Any]] = []
    selected_candidates: list[dict[str, Any]] = []
    for view, slot in slots.items():
        slot["before_candidates"].sort(key=source_selection.candidate_rank)
        slot["after_candidates"].sort(key=source_selection.candidate_rank)
        before, after, pair_quality = source_selection.select_best_pair(
            view,
            slot["before_candidates"],
            slot["after_candidates"],
            lock=locked_slots.get(view) if isinstance(locked_slots, dict) else None,
        )
        if before:
            selected_candidates.append(before)
        if after:
            selected_candidates.append(after)
        if before or after:
            plan_slots[view] = {
                "before": _selection_plan_candidate(before),
                "after": _selection_plan_candidate(after),
                "pair_quality": pair_quality,
                "before_candidate_count": len(slot["before_candidates"]),
                "after_candidate_count": len(slot["after_candidates"]),
            }
        elif isinstance(pair_quality, dict) and pair_quality.get("render_slot_status") == "dropped":
            dropped_slots.append(
                {
                    "view": view,
                    "label": {"front": "正面", "oblique": "45°", "side": "侧面"}.get(view, view),
                    "reason": pair_quality.get("drop_reason"),
                    "pair_quality": pair_quality,
                    "before_candidate_count": len(slot["before_candidates"]),
                    "after_candidate_count": len(slot["after_candidates"]),
                }
            )
    for candidate in selected_candidates:
        render_filename = str(candidate.get("render_filename") or "")
        if render_filename:
            overrides_by_render_name[render_filename] = _selection_override_from_candidate(candidate)
    missing_slots = []
    dropped_views = {str(item.get("view") or "") for item in dropped_slots if isinstance(item, dict)}
    for view in ("front", "oblique", "side"):
        if view in dropped_views:
            continue
        slot = plan_slots.get(view) or {}
        missing = [
            *([] if isinstance(slot.get("before"), dict) else ["before"]),
            *([] if isinstance(slot.get("after"), dict) else ["after"]),
        ]
        if missing:
            missing_slots.append({"view": view, "missing": missing})
    renderable_slots = [
        view
        for view in ("front", "oblique", "side")
        if isinstance((plan_slots.get(view) or {}).get("before"), dict)
        and isinstance((plan_slots.get(view) or {}).get("after"), dict)
    ]
    return {
        "plan": {
            "version": 1,
            "policy": "source_selection_v1",
            "feedback_source_job_id": (render_feedback or {}).get("source_job_id") if isinstance(render_feedback, dict) else None,
            "feedback_applied": bool(
                isinstance(render_feedback, dict)
                and (render_feedback.get("candidate_penalties") or render_feedback.get("pair_penalties"))
            ),
            "selection_controls": selection_controls,
            "accepted_warnings": selection_controls.get("accepted_warnings") or [],
            "required_slots": ["front", "oblique", "side"],
            "missing_slots": missing_slots,
            "dropped_slots": dropped_slots,
            "renderable_slots": renderable_slots,
            "effective_template_hint": (
                "tri-compare"
                if len(renderable_slots) >= 3
                else "bi-compare"
                if "front" in renderable_slots and len(renderable_slots) >= 2
                else "single-compare"
                if "front" in renderable_slots
                else None
            ),
            "selected_count": len(selected_candidates),
            "slots": plan_slots,
            "source_provenance": [
                {
                    "case_id": candidate.get("case_id"),
                    "source_role": candidate.get("source_role"),
                    "filename": candidate.get("filename"),
                    "render_filename": candidate.get("render_filename"),
                    "phase": candidate.get("phase"),
                    "view": candidate.get("view"),
                    "render_feedback": candidate.get("render_feedback"),
                    "selection_metadata_source": candidate.get("selection_metadata_source"),
                }
                for candidate in selected_candidates
            ],
            "feedback_summary": render_feedback if isinstance(render_feedback, dict) else None,
        },
        "overrides_by_render_name": overrides_by_render_name,
        "selected_count": len(selected_candidates),
    }


def _apply_inferred_phase_view_overrides(
    image_files: list[str],
    manual_overrides: dict[str, dict[str, Any]],
    metadata_by_file: dict[str, dict[str, Any]],
) -> int:
    """Pass deterministic filename/skill labels into the renderer.

    The render skill is conservative and misses some English `front-before.jpg`
    style names. Queue preflight already knows those labels, so we feed them as
    ephemeral overrides without writing any manual override rows.
    """
    applied = 0
    for filename in image_files:
        current = manual_overrides.get(filename) or manual_overrides.get(Path(filename).name)
        metadata = metadata_by_file.get(filename) or metadata_by_file.get(Path(filename).name)
        phase, view, _manual = _metadata_phase_view(filename, current, metadata)
        if phase not in {"before", "after"} or view not in {"front", "oblique", "side"}:
            continue
        target = manual_overrides.setdefault(filename, {})
        before = (target.get("phase"), target.get("view"))
        target.setdefault("phase", phase)
        target.setdefault("view", view)
        if before != (target.get("phase"), target.get("view")):
            target.setdefault("inferred_from_queue", True)
            applied += 1
        elif (
            target.get("selection_source") == "source_selection"
            and target.get("phase_source") != "manual"
            and target.get("view_source") != "manual"
            and not target.get("inferred_from_queue")
        ):
            target["inferred_from_queue"] = True
            applied += 1
    return applied


def _classification_blocking_preflight(
    *,
    case_meta_json: str | None,
    skill_image_metadata_json: str | None,
    image_files: list[str],
    manual_overrides: dict[str, dict[str, Any]],
    semantic_judge: str,
) -> dict[str, Any] | None:
    states = _image_review_states(case_meta_json)
    metadata_by_file = _skill_metadata_by_file(skill_image_metadata_json)
    blockers: list[str] = []
    missing_count = 0
    low_confidence_count = 0
    untraced_manual_override_count = 0
    needs_repick_count = 0
    copied_review_count = 0
    unresolved_samples: list[str] = []
    for filename in image_files:
        state = _state_for_filename(states, filename)
        verdict = str((state or {}).get("verdict") or "")
        if bool((state or {}).get("render_excluded") or verdict == "excluded"):
            continue
        if verdict in {"usable", "deferred"}:
            continue
        if bool((state or {}).get("copied_requires_review")):
            copied_review_count += 1
            unresolved_samples.append(filename)
            continue
        override = manual_overrides.get(filename) or manual_overrides.get(Path(filename).name)
        metadata = metadata_by_file.get(filename) or metadata_by_file.get(Path(filename).name)
        phase, view, manual = _metadata_phase_view(filename, override, metadata)
        if verdict == "needs_repick":
            needs_repick_count += 1
            unresolved_samples.append(filename)
            continue
        if phase not in {"before", "after"} or view not in {"front", "oblique", "side"}:
            missing_count += 1
            unresolved_samples.append(filename)
            continue
        angle_confidence = None
        if isinstance(metadata, dict) and metadata.get("angle_confidence") is not None:
            try:
                angle_confidence = float(metadata.get("angle_confidence"))
            except (TypeError, ValueError):
                angle_confidence = None
        if angle_confidence is not None and angle_confidence < LOW_CONFIDENCE_REVIEW_BELOW:
            if not manual:
                low_confidence_count += 1
                unresolved_samples.append(filename)
            elif not _manual_override_traceable(override):
                untraced_manual_override_count += 1
                unresolved_samples.append(filename)
    if not any((missing_count, low_confidence_count, untraced_manual_override_count, needs_repick_count, copied_review_count)):
        return None
    pieces = []
    if missing_count:
        pieces.append(f"待补充 {missing_count} 张")
    if low_confidence_count:
        pieces.append(f"低置信 {low_confidence_count} 张")
    if untraced_manual_override_count:
        pieces.append(f"人工覆盖缺少原因 {untraced_manual_override_count} 张")
    if needs_repick_count:
        pieces.append(f"需换片 {needs_repick_count} 张")
    if copied_review_count:
        pieces.append(f"补图待确认 {copied_review_count} 张")
    message = "正式出图已阻断：还有未闭环的照片分类任务（" + "，".join(pieces) + "）。请先到照片分类工作台批量补齐、确认可用或标记不用于出图。"
    blockers.append(message)
    for sample in unresolved_samples[:8]:
        blockers.append(f"未闭环图片：{sample}")
    return {
        "output_path": None,
        "manifest_path": None,
        "status": "error",
        "blocking_issue_count": len(blockers),
        "warning_count": 0,
        "case_mode": "",
        "effective_templates": [],
        "manual_overrides_applied": list(manual_overrides.keys()),
        "ai_usage": {
            "semantic_judge_requested": semantic_judge,
            "semantic_judge_effective": "blocked-classification-preflight",
            "classification_missing_count": missing_count,
            "classification_low_confidence_count": low_confidence_count,
            "classification_untraced_manual_override_count": untraced_manual_override_count,
            "classification_needs_repick_count": needs_repick_count,
            "classification_copied_review_count": copied_review_count,
            "source_count": len(image_files),
        },
        "render_error": message,
        "blocking_issues": blockers,
        "warnings": [],
    }


def _manual_override_traceable(override: dict[str, Any] | None) -> bool:
    if not isinstance(override, dict):
        return False
    reviewer = str(override.get("reviewer") or "").strip()
    reason = str(override.get("reason") or "").strip()
    if not reason:
        raw = override.get("reason_json")
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                reason = raw.strip()
            else:
                if isinstance(parsed, dict):
                    reason = str(parsed.get("reason") or parsed.get("note") or "").strip()
    return bool(reviewer and reason)


def _source_readiness_preflight(
    *,
    semantic_judge: str,
    source_profile: dict[str, object],
    allow_only_generated: bool = False,
) -> dict[str, Any] | None:
    source_kind = str(source_profile.get("source_kind") or "")
    if (
        source_kind != "manual_not_case_source_directory"
        and (int(source_profile.get("missing_source_count") or 0) > 0 or source_kind == "missing_source_files")
    ):
        if allow_only_generated:
            return None
        missing_count = int(source_profile.get("missing_source_count") or 0)
        samples = [str(item) for item in (source_profile.get("missing_source_samples") or []) if item]
        message = (
            "正式出图已阻断：源组里有历史图片记录在当前磁盘不可读"
            f"（缺失 {missing_count} 张）。请恢复源文件、重新扫描目录，或清除失效绑定后再出图。"
        )
        blockers = [message]
        for sample in samples[:6]:
            blockers.append(f"缺失源文件：{sample}")
        effective = "blocked-missing-source-files"
        return {
            "output_path": None,
            "manifest_path": None,
            "status": "error",
            "blocking_issue_count": len(blockers),
            "warning_count": 0,
            "case_mode": "",
            "effective_templates": [],
            "manual_overrides_applied": [],
            "ai_usage": {
                "semantic_judge_requested": semantic_judge,
                "semantic_judge_effective": effective,
                "source_profile": source_profile,
                "source_count": int(source_profile.get("source_count") or 0),
                "missing_source_count": missing_count,
                "missing_source_samples": samples[:8],
                "generated_artifact_count": int(source_profile.get("generated_artifact_count") or 0),
                "generated_artifact_samples": source_profile.get("generated_artifact_samples") or [],
            },
            "render_error": message,
            "blocking_issues": blockers,
            "warnings": [],
        }
    if source_kind == "unknown_not_scanned":
        return None
    if allow_only_generated and source_kind not in {"generated_output_collection", "manual_not_case_source_directory"}:
        return None
    if source_kind == "manual_not_case_source_directory":
        message = "正式出图已阻断：该案例已人工标记为素材归档/非源照片目录，不进入正式出图。"
        blockers = [message, "如需要正式出图，请先恢复为待检查，并补充真实术前/术后源照片。"]
        effective = "blocked-manual-not-source-directory"
    elif source_kind == "generated_output_collection":
        generated_count = int(source_profile.get("generated_artifact_count") or 0)
        samples = [str(item) for item in (source_profile.get("generated_artifact_samples") or []) if item]
        message = "正式出图已阻断：这更像成品图/海报集合，不是案例源照片目录。请选择包含原始术前/术后照片的案例目录。"
        blockers = [message, f"已过滤生成图/海报/成品图 {generated_count} 张，不作为源照片参与出图。"]
        for sample in samples[:6]:
            blockers.append(f"已过滤生成图：{sample}")
        effective = "blocked-generated-output-collection"
    elif source_kind == "empty":
        message = "正式出图已阻断：没有可用于正式出图的真实源照片。请补充术前/术后原始照片后再出图。"
        blockers = [message]
        effective = "blocked-no-source"
    elif source_kind == "insufficient_source_photos":
        source_count = int(source_profile.get("source_count") or 0)
        samples = [str(item) for item in (source_profile.get("source_samples") or []) if item]
        message = f"正式出图已阻断：真实源照片不足（当前 {source_count} 张），至少需要术前和术后各 1 张。"
        blockers = [message, *[f"当前源图：{sample}" for sample in samples[:6]]]
        effective = "blocked-insufficient-source-photos"
    elif source_kind == "missing_before_after_pair":
        before_count = int(source_profile.get("before_count") or 0)
        after_count = int(source_profile.get("after_count") or 0)
        unlabeled_count = int(source_profile.get("unlabeled_source_count") or 0)
        message = (
            "正式出图已阻断：真实源照片目录缺少术前/术后配对"
            f"（术前 {before_count} 张，术后 {after_count} 张，未标注 {unlabeled_count} 张）。"
            "请先补齐阶段分类或补充缺失阶段照片。"
        )
        blockers = [message]
        for sample in [str(item) for item in (source_profile.get("source_samples") or []) if item][:6]:
            blockers.append(f"待配对源图：{sample}")
        effective = "blocked-missing-before-after-pair"
    else:
        return None
    return {
        "output_path": None,
        "manifest_path": None,
        "status": "error",
        "blocking_issue_count": len(blockers),
        "warning_count": 0,
        "case_mode": "",
        "effective_templates": [],
        "manual_overrides_applied": [],
        "ai_usage": {
            "semantic_judge_requested": semantic_judge,
            "semantic_judge_effective": effective,
            "source_profile": source_profile,
            "source_count": int(source_profile.get("source_count") or 0),
            "generated_artifact_count": int(source_profile.get("generated_artifact_count") or 0),
            "generated_artifact_samples": source_profile.get("generated_artifact_samples") or [],
        },
        "render_error": message,
        "blocking_issues": blockers,
        "warnings": [],
    }


def _tri_slot_preflight(
    *,
    template: str,
    semantic_judge: str,
    source_profile: dict[str, object],
    selection_plan: dict[str, Any],
) -> dict[str, Any] | None:
    if template != "tri-compare":
        return None
    slots = selection_plan.get("slots") if isinstance(selection_plan, dict) else {}
    if not isinstance(slots, dict):
        slots = {}
    dropped_slots = selection_plan.get("dropped_slots") if isinstance(selection_plan, dict) else []
    if not isinstance(dropped_slots, list):
        dropped_slots = []
    dropped_by_view = {
        str(item.get("view") or ""): item
        for item in dropped_slots
        if isinstance(item, dict) and item.get("view")
    }
    renderable_slots = [
        view
        for view in ("front", "oblique", "side")
        if isinstance((slots.get(view) or {}).get("before"), dict)
        and isinstance((slots.get(view) or {}).get("after"), dict)
    ]
    missing: list[dict[str, Any]] = []
    for view, label in (("front", "正面"), ("oblique", "45°"), ("side", "侧面")):
        slot = slots.get(view)
        before = slot.get("before") if isinstance(slot, dict) else None
        after = slot.get("after") if isinstance(slot, dict) else None
        missing_roles = [
            *([] if isinstance(before, dict) else ["before"]),
            *([] if isinstance(after, dict) else ["after"]),
        ]
        if missing_roles:
            if view in dropped_by_view and "front" in renderable_slots and len(renderable_slots) >= 2:
                continue
            missing.append({"view": view, "label": label, "missing": missing_roles})
    if not missing:
        if "front" in renderable_slots and len(renderable_slots) >= 2:
            return None
        if dropped_by_view:
            missing.append(
                {
                    "view": "renderable_slots",
                    "label": "可对比联",
                    "missing": ["before", "after"],
                }
            )
        else:
            return None
    if missing == [{"view": "renderable_slots", "label": "可对比联", "missing": ["before", "after"]}]:
        message = "正式出图已阻断：低价值槽位移除后不足双联，请先补齐至少正面加一个可对比角度。"
        blockers = [message]
        return {
            "output_path": None,
            "manifest_path": None,
            "status": "error",
            "blocking_issue_count": len(blockers),
            "warning_count": 0,
            "case_mode": "",
            "effective_templates": [],
            "manual_overrides_applied": [],
            "ai_usage": {
                "semantic_judge_requested": semantic_judge,
                "semantic_judge_effective": "blocked-source-group-slot-preflight",
                "source_profile": source_profile,
                "render_selection_policy": selection_plan.get("policy") if isinstance(selection_plan, dict) else None,
                "render_selection_slot_count": len(slots),
                "required_slots": ["front", "oblique", "side"],
                "missing_slots": missing,
                "dropped_slots": dropped_slots,
            },
            "render_error": message,
            "blocking_issues": blockers,
            "warnings": [],
            "render_selection_audit": {
                "policy": selection_plan.get("policy") if isinstance(selection_plan, dict) else None,
                "hard_blockers": [
                    {
                        "code": "insufficient_renderable_slots_after_downgrade",
                        "severity": "block",
                        "message": message,
                        "dropped_slots": dropped_slots,
                        "recommended_action": "补齐至少正面加一个可对比角度后再正式出图",
                    }
                ],
                "selection_plan": selection_plan,
            },
        }
    if not missing:
        return None
    slot_text = "；".join(
        f"{item['label']}缺{'/'.join('术前' if role == 'before' else '术后' for role in item['missing'])}"
        for item in missing
    )
    message = (
        "正式出图已阻断：三联正式出图槽位未配齐"
        f"（{slot_text}）。请先在源组预检或照片分类工作台补齐正面、45°、侧面术前术后配对。"
    )
    blockers = [message]
    for item in missing:
        blockers.append(
            f"缺槽位：{item['label']} {','.join('术前' if role == 'before' else '术后' for role in item['missing'])}"
        )
    return {
        "output_path": None,
        "manifest_path": None,
        "status": "error",
        "blocking_issue_count": len(blockers),
        "warning_count": 0,
        "case_mode": "",
        "effective_templates": [],
        "manual_overrides_applied": [],
        "ai_usage": {
            "semantic_judge_requested": semantic_judge,
            "semantic_judge_effective": "blocked-source-group-slot-preflight",
            "source_profile": source_profile,
            "render_selection_policy": selection_plan.get("policy") if isinstance(selection_plan, dict) else None,
            "render_selection_slot_count": len(slots),
            "required_slots": ["front", "oblique", "side"],
            "missing_slots": missing,
            "dropped_slots": dropped_slots,
        },
        "render_error": message,
        "blocking_issues": blockers,
        "warnings": [],
        "render_selection_audit": {
            "policy": selection_plan.get("policy") if isinstance(selection_plan, dict) else None,
            "hard_blockers": [
                {
                    "code": "missing_render_slots",
                    "severity": "block",
                    "message": message,
                    "slots": missing,
                    "recommended_action": "补齐三联槽位后再正式出图",
                }
            ],
            "selection_plan": selection_plan,
        },
    }


def _has_phase_pair(
    image_files: list[str],
    manual_overrides: dict[str, dict[str, Any]],
) -> bool:
    has_before = any(
        str(item.get("phase") or "").lower() == "before"
        for item in manual_overrides.values()
        if isinstance(item, dict) and not item.get("render_excluded")
    )
    has_after = any(
        str(item.get("phase") or "").lower() == "after"
        for item in manual_overrides.values()
        if isinstance(item, dict) and not item.get("render_excluded")
    )
    if has_before and has_after:
        return True

    for name in image_files:
        lowered = name.lower()
        if any(token.lower() in lowered for token in BEFORE_TOKENS):
            has_before = True
        if any(token.lower() in lowered for token in AFTER_TOKENS):
            has_after = True
        if has_before and has_after:
            return True
    return False


def _semantic_auto_preflight(
    *,
    semantic_judge: str,
    source_count: int,
    image_files: list[str],
    manual_overrides: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, Any] | None]:
    """Protect large unlabeled cases from spending minutes on per-image VLM.

    `semantic_judge=auto` is useful when only a handful of candidates need a
    visual tie-breaker. On large unlabeled cases the current skill screens every
    image serially, so a case like 88 (35 sources) exceeds the 180s render
    timeout before it can write a manifest. The queue knows the source count and
    manual overrides, so it can fail early with an actionable blocked state.
    """
    if semantic_judge != "auto" or source_count <= SEMANTIC_AUTO_SOURCE_LIMIT:
        return semantic_judge, None

    if _has_phase_pair(image_files, manual_overrides):
        # A usable before/after pair already exists through filenames or manual
        # overrides. Let the normal renderer pair those deterministic labels
        # and skip expensive per-image VLM screening for the rest of the set.
        return "off", None

    message = (
        f"视觉补判需要处理 {source_count} 张源图，已超过自动出图上限 "
        f"{SEMANTIC_AUTO_SOURCE_LIMIT} 张；请先在“人工整理与出图”中拖入术前/术后照片，"
        "或把无用图片移入废弃区后再重试。"
    )
    result = {
        "output_path": None,
        "manifest_path": None,
        "status": "error",
        "blocking_issue_count": 1,
        "warning_count": 0,
        "case_mode": "",
        "effective_templates": [],
        "manual_overrides_applied": list(manual_overrides.keys()),
        "ai_usage": {
            "semantic_judge_requested": semantic_judge,
            "semantic_judge_effective": "blocked-preflight",
            "semantic_auto_source_limit": SEMANTIC_AUTO_SOURCE_LIMIT,
            "source_count": source_count,
        },
        "render_error": message,
        "blocking_issues": [message],
        "warnings": [],
    }
    return semantic_judge, result


def _render_case_metadata(manifest_path: str | None) -> dict[str, Any] | None:
    if not manifest_path:
        return None
    path = Path(manifest_path)
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    groups = manifest.get("groups") or []
    if not isinstance(groups, list):
        groups = []
    pose_max, sharp_min = skill_bridge._extract_geometric_aggregates(groups)
    return {
        "skill_image_metadata_json": json.dumps(
            skill_bridge._extract_per_image_metadata(groups),
            ensure_ascii=False,
        ),
        "skill_blocking_detail_json": json.dumps(
            [str(x) for x in (manifest.get("blocking_issues") or [])],
            ensure_ascii=False,
        ),
        "skill_warnings_json": json.dumps(
            [str(x) for x in (manifest.get("warnings") or [])],
            ensure_ascii=False,
        ),
        "pose_delta_max": pose_max,
        "sharp_ratio_min": sharp_min,
        "meta_extras": {
            "source": "skill_v3",
            "skill_template": (manifest.get("effective_templates") or [None])[0],
            "skill_case_mode": manifest.get("case_mode"),
            "skill_status": manifest.get("status"),
            "skill_warning_count": manifest.get("warning_count"),
            "skill_blocking_issue_count": manifest.get("blocking_issue_count"),
            "skill_upgraded_at": manifest.get("created_at"),
            "render_synced_at": _now_iso(),
        },
    }


class _Subscriber:
    """One SSE listener. Bound to the event loop where subscribe() was called."""

    __slots__ = ("queue", "loop", "closed")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.loop = loop
        self.closed = False


class RenderQueue:
    """Single global queue. Construct once at import time."""

    def __init__(self) -> None:
        self._subscribers: list[_Subscriber] = []
        self._sub_lock = threading.Lock()

    # ------------------------------------------------------------------
    # SSE broadcast
    # ------------------------------------------------------------------

    def _publish(self, event: dict[str, Any]) -> None:
        """Push event to every active subscriber. Safe to call from any thread.

        Always tags events with `job_type='render'` so the unified
        /api/jobs/stream consumer (and any future fan-in router) can route
        without sniffing payload shape.
        """
        event.setdefault("job_type", "render")
        with self._sub_lock:
            subs = list(self._subscribers)
        for sub in subs:
            if sub.closed:
                continue
            try:
                sub.loop.call_soon_threadsafe(sub.queue.put_nowait, event)
            except RuntimeError:
                # Loop closed — mark sub for removal on next iteration.
                sub.closed = True

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Async generator yielding render events. Caller iterates with async for."""
        loop = asyncio.get_running_loop()
        sub = _Subscriber(loop)
        with self._sub_lock:
            self._subscribers.append(sub)
        try:
            while True:
                event = await sub.queue.get()
                yield event
        finally:
            sub.closed = True
            with self._sub_lock:
                if sub in self._subscribers:
                    self._subscribers.remove(sub)

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    def enqueue(
        self,
        case_id: int,
        brand: str,
        template: str = DEFAULT_TEMPLATE,
        semantic_judge: str = DEFAULT_SEMANTIC_JUDGE,
        batch_id: str | None = None,
        render_mode: str = "ai",
        best_pair_selection_id: int | None = None,
        candidates_fingerprint_snapshot: str | None = None,
        draft_preview: bool = False,
        model: str | None = None,
        system_prompt: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> int:
        """Insert job row + submit to thread pool. Returns job_id."""
        if not brand:
            raise ValueError("brand required")
        if render_mode not in {"ai", "best-pair"}:
            raise ValueError("render_mode must be ai or best-pair")
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM cases WHERE id = ? AND trashed_at IS NULL", (case_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"case {case_id} not found")
            cur = conn.execute(
                """
                INSERT INTO render_jobs
                    (case_id, brand, template, status, batch_id, enqueued_at,
                     semantic_judge, render_mode, best_pair_selection_id,
                     candidates_fingerprint_snapshot, draft_preview, meta_json)
                VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    brand,
                    template,
                    batch_id,
                    _now_iso(),
                    semantic_judge,
                    render_mode,
                    best_pair_selection_id,
                    candidates_fingerprint_snapshot,
                    1 if draft_preview else 0,
                    json.dumps(
                        stress.tag_payload(
                            {
                                "enqueue_source": "render_queue",
                                "render_mode": render_mode,
                                "best_pair_selection_id": best_pair_selection_id,
                                "candidates_fingerprint_snapshot": candidates_fingerprint_snapshot,
                                "draft_preview": bool(draft_preview),
                                "model": model,
                                "system_prompt": system_prompt,
                                "options": options,
                            }
                        ),
                        ensure_ascii=False,
                    ),
                ),
            )
            job_id = cur.lastrowid or 0
        self._publish(
            {
                "type": "job_update",
                "job_id": job_id,
                "case_id": case_id,
                "batch_id": batch_id,
                "brand": brand,
                "template": template,
                "status": "queued",
                "render_mode": render_mode,
                "best_pair_selection_id": best_pair_selection_id,
                "draft_preview": bool(draft_preview),
            }
        )
        _job_pool.submit(self._execute_safe, job_id)
        return job_id

    def enqueue_batch(
        self,
        case_ids: list[int],
        brand: str,
        template: str = DEFAULT_TEMPLATE,
        semantic_judge: str = DEFAULT_SEMANTIC_JUDGE,
        draft_preview: bool = False,
        model: str | None = None,
        system_prompt: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[str, list[int]]:
        """Insert N job rows sharing one batch_id. Returns (batch_id, job_ids)."""
        if not case_ids:
            raise ValueError("case_ids cannot be empty")
        batch_id = f"batch-{uuid.uuid4().hex[:12]}"
        job_ids: list[int] = []
        for case_id in case_ids:
            try:
                jid = self.enqueue(
                    case_id,
                    brand,
                    template,
                    semantic_judge,
                    batch_id=batch_id,
                    draft_preview=draft_preview,
                    model=model,
                    system_prompt=system_prompt,
                    options=options,
                )
                job_ids.append(jid)
            except ValueError:
                # Skip missing cases but continue the batch.
                continue
        return batch_id, job_ids

    def cancel(self, job_id: int) -> bool:
        """Cancel a queued job. No-op if job is already running/done/failed."""
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id, status, case_id, batch_id FROM render_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if not row:
                return False
            if row["status"] != "queued":
                return False
            conn.execute(
                """
                UPDATE render_jobs
                SET status = 'cancelled',
                    finished_at = ?,
                    recovery_token = NULL,
                    recovery_claimed_at = NULL
                WHERE id = ?
                """,
                (_now_iso(), job_id),
            )
        self._publish(
            {
                "type": "job_update",
                "job_id": job_id,
                "case_id": row["case_id"],
                "batch_id": row["batch_id"],
                "status": "cancelled",
            }
        )
        return True

    # ------------------------------------------------------------------
    # Render execution (runs in worker thread)
    # ------------------------------------------------------------------

    def _execute_safe(self, job_id: int) -> None:
        """Wrapper that always marks failure on unexpected exceptions."""
        try:
            self._execute_render(job_id)
        except Exception as e:  # noqa: BLE001 — final safety net
            self._mark_failed(job_id, f"unexpected: {e}")

    def _execute_render(self, job_id: int) -> None:
        bound_staging_dir: Path | None = None

        def remember_bound_staging(path: Path) -> None:
            nonlocal bound_staging_dir
            bound_staging_dir = path

        try:
            self._execute_render_impl(job_id, remember_bound_staging)
        finally:
            _cleanup_bound_source_staging(job_id, bound_staging_dir)

    def _automate_md_ai_clinical_enhancements(
        self,
        render_case_dir: str,
        brand: str,
        case_tags_json: str | None,
        manual_phase_lookup: dict[str, str] | None = None,
    ) -> None:
        """Scan render_case_dir for 'after' images and trigger AI enhancement for md_ai.

        Phase resolution priority:
          1. manual_phase_lookup[entry.name] — operator override from case_image_overrides
          2. source_images._phase_from_filename(entry.name) — filename token fallback
        """
        if brand not in ("md_ai", "meiji_ai"):
            return

        tags = []
        if case_tags_json:
            try:
                tags = json.loads(case_tags_json)
            except (ValueError, TypeError):
                tags = []

        # Extract focus targets from tags by cross-referencing with anatomical keywords
        focus_targets = []
        if isinstance(tags, list):
            for tag in tags:
                tag_str = str(tag)
                for key in ai_generation_adapter.MD_ANATOMICAL_KEYWORDS:
                    if key in tag_str:
                        focus_targets.append(key)

        # Deduplicate
        focus_targets = list(dict.fromkeys(focus_targets))

        # Scan for 'after' images in the staging directory
        staging_path = Path(render_case_dir)
        if not staging_path.is_dir():
            return

        for entry in staging_path.iterdir():
            if not entry.is_file():
                continue

            # Phase resolution: manual override wins over filename token.
            phase: str | None = None
            if manual_phase_lookup:
                override = manual_phase_lookup.get(entry.name)
                if override in ("before", "after"):
                    phase = override
            if phase is None:
                phase = source_images._phase_from_filename(entry.name)
            if phase == "after":
                LOGGER.info("Triggering automated MD-AI clinical enhancement for %s", entry.name)
                try:
                    # Enhance image (returns path to enhanced file)
                    enhanced_path = ai_generation_adapter.run_direct_clinical_enhancement(
                        entry,
                        brand=brand,
                        focus_targets=focus_targets
                    )
                    if enhanced_path != entry and enhanced_path.is_file():
                        # Replace original in staging with enhanced version
                        temp_enhanced = staging_path / f"enhanced_{entry.name}"
                        shutil.copyfile(enhanced_path, temp_enhanced)
                        os.replace(temp_enhanced, entry)
                        LOGGER.info("MD-AI enhancement applied to %s", entry.name)
                except Exception as exc:
                    LOGGER.warning("MD-AI enhancement failed for %s: %s", entry.name, exc)

    def _execute_render_impl(
        self,
        job_id: int,
        remember_bound_staging: Callable[[Path], None],
    ) -> None:
        # 1. Read job row + transition to running (skip if cancelled meanwhile).
        with db.connect() as conn:
            row = conn.execute(
                """
                SELECT j.*, c.abs_path AS case_dir, c.meta_json AS case_meta_json
                     , c.skill_image_metadata_json AS skill_image_metadata_json
                     , c.tags_json AS case_tags_json
                     , c.manual_blocking_issues_json AS case_manual_blocking_issues_json
                FROM render_jobs j
                JOIN cases c ON c.id = j.case_id
                WHERE j.id = ?
                """,
                (job_id,),
            ).fetchone()
            if not row:
                return
            if row["status"] != "queued":
                return  # Already cancelled or being handled elsewhere.
            claimed = conn.execute(
                """
                UPDATE render_jobs
                SET status = 'running',
                    started_at = ?,
                    recovery_token = NULL,
                    recovery_claimed_at = NULL
                WHERE id = ? AND status = 'queued'
                """,
                (_now_iso(), job_id),
            ).rowcount
            if claimed != 1:
                return
            case_dir = row["case_dir"]
            brand = row["brand"]
            template = row["template"]
            semantic_judge = row["semantic_judge"]
            case_id = row["case_id"]
            batch_id = row["batch_id"]

            # Stage B: pull manual phase/view overrides for this case.
            manual_overrides: dict[str, dict[str, Any]] = _fetch_case_image_overrides(conn, int(case_id))
            raw_meta_image_files = [
                str(item)
                for item in ((_parse_case_meta(row["case_meta_json"]).get("image_files") or []))
                if item
            ]
            generated_artifact_files = [
                filename for filename in raw_meta_image_files if not source_images.is_source_image_file(filename)
            ]
            source_count, image_files = _case_source_info(row["case_meta_json"], case_dir)
            source_profile = _case_source_profile(row["case_meta_json"], case_dir)
            binding_ids = _source_binding_case_ids(row["case_meta_json"])
            bound_source_rows: list[dict[str, Any]] = []
            if binding_ids:
                placeholders = ",".join("?" * len(binding_ids))
                fetched = conn.execute(
                    f"SELECT id, abs_path, meta_json, skill_image_metadata_json FROM cases WHERE trashed_at IS NULL AND id IN ({placeholders})",
                    binding_ids,
                ).fetchall()
                bound_source_rows = [
                    {
                        "id": int(item["id"]),
                        "abs_path": item["abs_path"],
                        "meta_json": item["meta_json"],
                        "skill_image_metadata_json": item["skill_image_metadata_json"],
                        "role": "bound",
                    }
                    for item in fetched
                ]
            render_case_dir = case_dir
            bound_staging_files: list[str] = []
            bound_render_names: dict[tuple[int, str], str] = {}
            primary_source_row = {
                "id": int(case_id),
                "abs_path": case_dir,
                "meta_json": row["case_meta_json"],
                "skill_image_metadata_json": row["skill_image_metadata_json"],
                "role": "primary",
            }
            # Always use staging for MD-AI to avoid mutating original source during enhancement
            use_staging = bool(bound_source_rows or brand in ("md_ai", "meiji_ai"))
            if use_staging:
                source_profile = _merged_bound_profile([primary_source_row, *bound_source_rows])
                source_profile["bound_case_ids"] = [int(item["id"]) for item in bound_source_rows]
                staging_dir, bound_staging_files, bound_render_names = _build_bound_source_staging(
                    job_id,
                    [primary_source_row, *bound_source_rows],
                    case_dir,
                )
                remember_bound_staging(staging_dir)
                if bound_staging_files:
                    render_case_dir = str(staging_dir)
                    image_files = bound_staging_files
                    source_count = len(bound_staging_files)
            if source_images.case_marked_not_source(
                _json_list(row["case_tags_json"]),
                _json_list(row["case_manual_blocking_issues_json"]),
            ):
                source_profile = {
                    **source_profile,
                    "source_kind": "manual_not_case_source_directory",
                    "manual_not_source": True,
                }
            source_filter = source_images.source_filter_summary(raw_meta_image_files)
            selection_context = _build_render_selection_context(
                conn,
                [primary_source_row, *bound_source_rows],
                bound_render_names,
            )
            render_selection_plan = selection_context["plan"]
            for filename, selection_override in selection_context["overrides_by_render_name"].items():
                target = manual_overrides.setdefault(filename, {})
                for key, value in selection_override.items():
                    if value is not None:
                        target.setdefault(key, value)
            excluded_files = _render_excluded_files(row["case_meta_json"])
            review_states = _image_review_states(row["case_meta_json"])
            for filename, state in review_states.items():
                if not isinstance(state, dict):
                    continue
                item = manual_overrides.setdefault(filename, {})
                verdict = state.get("verdict")
                if verdict:
                    item["review_verdict"] = verdict
                    item["selection_review_verdict"] = verdict
                if state.get("body_part"):
                    item["body_part"] = state.get("body_part")
                if state.get("treatment_area"):
                    item["treatment_area"] = state.get("treatment_area")
                if state.get("render_excluded") or verdict == "excluded":
                    item["render_excluded"] = True
            if excluded_files:
                for filename in excluded_files:
                    item = manual_overrides.setdefault(filename, {})
                    item["render_excluded"] = True
                    item["review_verdict"] = "excluded"
                image_files = [filename for filename in image_files if filename not in excluded_files]
                source_count = len(image_files)
            for filename in generated_artifact_files:
                item = manual_overrides.setdefault(filename, {})
                item["render_excluded"] = True
                item["review_verdict"] = "excluded"
                item["selection_review_verdict"] = "excluded_generated_artifact"

        self._publish(
            {
                "type": "job_update",
                "job_id": job_id,
                "case_id": case_id,
                "batch_id": batch_id,
                "status": "running",
            }
        )

        requested_semantic_judge = semantic_judge
        no_source_preflight = _source_readiness_preflight(
            semantic_judge=semantic_judge,
            source_profile=source_profile,
            allow_only_generated=True,
        )
        if no_source_preflight is not None:
            self._finish_result(
                job_id,
                case_id=case_id,
                batch_id=batch_id,
                brand=brand,
                template=template,
                result=no_source_preflight,
            )
            return
        classification_preflight = _classification_blocking_preflight(
            case_meta_json=row["case_meta_json"],
            skill_image_metadata_json=row["skill_image_metadata_json"],
            image_files=image_files,
            manual_overrides=manual_overrides,
            semantic_judge=semantic_judge,
        )
        if classification_preflight is not None:
            ai_usage = dict(classification_preflight.get("ai_usage") or {})
            ai_usage["generated_artifact_count"] = int(source_filter.get("generated_artifact_count") or 0)
            ai_usage["generated_artifact_samples"] = source_filter.get("generated_artifact_samples") or []
            classification_preflight["ai_usage"] = ai_usage
            self._finish_result(
                job_id,
                case_id=case_id,
                batch_id=batch_id,
                brand=brand,
                template=template,
                result=classification_preflight,
            )
            return
        source_readiness_preflight = _source_readiness_preflight(
            semantic_judge=semantic_judge,
            source_profile=source_profile,
        )
        if source_readiness_preflight is not None:
            self._finish_result(
                job_id,
                case_id=case_id,
                batch_id=batch_id,
                brand=brand,
                template=template,
                result=source_readiness_preflight,
            )
            return
        slot_preflight = _tri_slot_preflight(
            template=template,
            semantic_judge=semantic_judge,
            source_profile=source_profile,
            selection_plan=render_selection_plan,
        )
        if slot_preflight is not None:
            self._finish_result(
                job_id,
                case_id=case_id,
                batch_id=batch_id,
                brand=brand,
                template=template,
                result=slot_preflight,
            )
            return
        metadata_by_file = _skill_metadata_by_file(row["skill_image_metadata_json"])
        inferred_override_count = _apply_inferred_phase_view_overrides(image_files, manual_overrides, metadata_by_file)
        semantic_judge, preflight_result = _semantic_auto_preflight(
            semantic_judge=semantic_judge,
            source_count=source_count,
            image_files=image_files,
            manual_overrides=manual_overrides,
        )
        if preflight_result is not None:
            ai_usage = dict(preflight_result.get("ai_usage") or {})
            ai_usage["generated_artifact_count"] = int(source_filter.get("generated_artifact_count") or 0)
            ai_usage["generated_artifact_samples"] = source_filter.get("generated_artifact_samples") or []
            ai_usage["inferred_override_count"] = inferred_override_count
            preflight_result["ai_usage"] = ai_usage
            self._finish_result(
                job_id,
                case_id=case_id,
                batch_id=batch_id,
                brand=brand,
                template=template,
                result=preflight_result,
            )
            return

        # 1.5. MD-AI clinical enhancement (automation hook)
        if brand in ("md_ai", "meiji_ai"):
            # Translate operator manual_phase overrides (keyed by original filename
            # in the primary case dir) into staging link-names so the enhancement
            # scanner respects user-corrected phase labels.
            #
            # NOTE: _fetch_case_image_overrides normalizes the column ``manual_phase``
            # into the dict key ``phase``. The SQL column is ``manual_phase`` but
            # the in-memory representation drops the prefix — see line ~340.
            manual_phase_lookup: dict[str, str] = {}
            for (mapped_case_id, original_filename), link_name in bound_render_names.items():
                if int(mapped_case_id) != int(case_id):
                    # _fetch_case_image_overrides only loaded current case; bound
                    # source overrides are out of scope for this hot-fix.
                    continue
                override_entry = manual_overrides.get(original_filename) or {}
                manual_phase = override_entry.get("phase")
                if isinstance(manual_phase, str) and manual_phase in ("before", "after"):
                    manual_phase_lookup[link_name] = manual_phase
            self._automate_md_ai_clinical_enhancements(
                render_case_dir=render_case_dir,
                brand=brand,
                case_tags_json=row["case_tags_json"],
                manual_phase_lookup=manual_phase_lookup,
            )

        # 2. Run the heavy subprocess.
        try:
            result = render_executor.run_render(
                render_case_dir,
                brand=brand,
                template=template,
                semantic_judge=semantic_judge,
                manual_overrides=manual_overrides,
                selection_plan=render_selection_plan,
            )
        except FileNotFoundError as e:
            self._mark_failed(job_id, f"missing: {e}")
            return
        except subprocess.TimeoutExpired as e:
            self._mark_failed(job_id, f"timeout after {e.timeout}s")
            return
        except RuntimeError as e:
            self._mark_failed(job_id, f"render failed: {e}")
            return
        ai_usage = dict(result.get("ai_usage") or {})
        ai_usage.setdefault("semantic_judge_requested", requested_semantic_judge)
        ai_usage.setdefault("semantic_judge_effective", semantic_judge)
        ai_usage.setdefault("semantic_auto_source_limit", SEMANTIC_AUTO_SOURCE_LIMIT)
        ai_usage.setdefault("source_count", source_count)
        ai_usage.setdefault("source_profile", source_profile)
        ai_usage.setdefault("generated_artifact_count", int(source_filter.get("generated_artifact_count") or 0))
        ai_usage.setdefault("generated_artifact_samples", source_filter.get("generated_artifact_samples") or [])
        ai_usage.setdefault("inferred_override_count", inferred_override_count)
        ai_usage.setdefault("render_selection_policy", render_selection_plan.get("policy"))
        ai_usage.setdefault("render_selection_slot_count", len(render_selection_plan.get("slots") or {}))
        if bound_source_rows:
            ai_usage.setdefault("bound_source_case_ids", [int(item["id"]) for item in bound_source_rows])
            ai_usage.setdefault("bound_source_staging_dir", render_case_dir)
            ai_usage.setdefault("bound_source_staging_count", len(bound_staging_files))
        ai_usage.setdefault(
            "render_excluded_count",
            sum(1 for item in manual_overrides.values() if isinstance(item, dict) and item.get("render_excluded")),
        )
        result["ai_usage"] = ai_usage

        self._finish_result(
            job_id,
            case_id=case_id,
            batch_id=batch_id,
            brand=brand,
            template=template,
            result=result,
        )

    def _finish_result(
        self,
        job_id: int,
        *,
        case_id: int,
        batch_id: str | None,
        brand: str,
        template: str,
        result: dict[str, Any],
    ) -> None:
        # 3. Persist artifact state + quality audit + broadcast. A skill
        # manifest with status=error can still produce a visible board; that
        # must be reviewed as `done_with_issues`, not silently treated as clean.
        quality = render_quality.evaluate_render_result(result)
        final_status = quality["quality_status"]
        render_error = str(result.get("render_error") or "").strip()
        case_meta = _render_case_metadata(result.get("manifest_path"))
        with db.connect() as conn:
            job_context_row = conn.execute(
                """SELECT render_mode, best_pair_selection_id, candidates_fingerprint_snapshot, draft_preview
                   FROM render_jobs WHERE id = ?""",
                (job_id,),
            ).fetchone()
            job_context = {
                "render_mode": job_context_row["render_mode"] if job_context_row else None,
                "best_pair_selection_id": job_context_row["best_pair_selection_id"] if job_context_row else None,
                "candidates_fingerprint_snapshot": job_context_row["candidates_fingerprint_snapshot"] if job_context_row else None,
                "draft_preview": bool(job_context_row["draft_preview"]) if job_context_row and "draft_preview" in job_context_row.keys() else False,
            }
            if job_context["draft_preview"]:
                quality["can_publish"] = False
                quality["artifact_mode"] = "draft_preview"
                metrics = quality.get("metrics") if isinstance(quality.get("metrics"), dict) else {}
                metrics["draft_preview"] = True
                metrics.setdefault("action_suggestions", [])
                quality["metrics"] = metrics
            conn.execute(
                """
                UPDATE render_jobs
                SET status = ?,
                    finished_at = ?,
                    output_path = ?,
                    manifest_path = ?,
                    error_message = ?,
                    meta_json = ?
                WHERE id = ?
                """,
                (
                    final_status,
                    _now_iso(),
                    result.get("output_path"),
                    result.get("manifest_path"),
                    render_error if final_status == "blocked" and render_error else None,
	                    json.dumps(
	                        stress.tag_payload(
		                            {
		                                "run_id": stress.stress_run_id() or result.get("run_id"),
		                                "code_version": _code_version_summary(),
		                                "source_manifest_hash": _source_manifest_hash(result),
		                                **job_context,
		                                "quality_summary": {
	                                    "quality_status": quality.get("quality_status"),
	                                    "quality_score": quality.get("quality_score"),
	                                    "can_publish": quality.get("can_publish"),
	                                    "actionable_warning_count": (
	                                        (quality.get("metrics") or {}).get("actionable_warning_count")
	                                        if isinstance(quality.get("metrics"), dict)
	                                        else None
	                                    ),
	                                },
	                                **{
	                                    k: result.get(k)
	                                    for k in (
	                                        "status",
	                                        "blocking_issue_count",
	                                        "warning_count",
	                                        "case_mode",
	                                        "effective_templates",
	                                        "ai_usage",
	                                        "render_error",
	                                        "blocking_issues",
	                                        "warnings",
	                                        "composition_alerts",
	                                        "selection_quality",
	                                        "render_selection_audit",
	                                        "render_selection_plan",
	                                        "render_selection_source_provenance",
	                                        "render_selection_missing_slots",
	                                        "render_selection_dropped_slots",
	                                        "warning_layers",
	                                        "warning_display_layers",
	                                        "warning_audit",
	                                        "warning_layer_counts",
	                                    )
	                                },
	                            }
	                        ),
	                        ensure_ascii=False,
	                    ),
                    job_id,
                ),
            )
            render_quality.persist_render_quality(conn, job_id, quality)
            if case_meta:
                case_row = conn.execute("SELECT meta_json FROM cases WHERE id = ?", (case_id,)).fetchone()
                current_meta: dict[str, Any] = {}
                if case_row and case_row["meta_json"]:
                    try:
                        parsed_meta = json.loads(case_row["meta_json"])
                        if isinstance(parsed_meta, dict):
                            current_meta = parsed_meta
                    except (TypeError, ValueError):
                        current_meta = {}
                current_meta.update(case_meta["meta_extras"])
                conn.execute(
                    """
                    UPDATE cases
                    SET skill_image_metadata_json = ?,
                        skill_blocking_detail_json = ?,
                        skill_warnings_json = ?,
                        pose_delta_max = ?,
                        sharp_ratio_min = ?,
                        meta_json = ?
                    WHERE id = ?
                    """,
                    (
                        case_meta["skill_image_metadata_json"],
                        case_meta["skill_blocking_detail_json"],
                        case_meta["skill_warnings_json"],
                        case_meta["pose_delta_max"],
                        case_meta["sharp_ratio_min"],
                        json.dumps(current_meta, ensure_ascii=False),
                        case_id,
                    ),
                )

        try:
            with db.connect() as conn:
                audit.record_revision(
                    conn,
                    case_id,
                    op="render",
                    before={"render_output_path": None},
                    after={
                        "render_output_path": result.get("output_path"),
                        "render_job_id": job_id,
                        "brand": brand,
                        "template": template,
                        "quality_status": final_status,
                        "render_selection_audit": result.get("render_selection_audit"),
                    },
                    source_route=f"/api/cases/{case_id}/render",
                    actor="render",
                )
        except sqlite3.Error as exc:
            LOGGER.warning("render audit revision failed for job %s: %s", job_id, exc)

        self._publish(
            {
                "type": "job_update",
                "job_id": job_id,
                "case_id": case_id,
                "batch_id": batch_id,
                "status": final_status,
                "output_path": result.get("output_path"),
                "manifest_path": result.get("manifest_path"),
                "error_message": render_error if final_status == "blocked" and render_error else None,
                "summary": {
                    "status": result.get("status"),
                    "blocking_issue_count": result.get("blocking_issue_count"),
                    "warning_count": result.get("warning_count"),
                    "case_mode": result.get("case_mode"),
                    "effective_templates": result.get("effective_templates"),
                    "quality": quality,
                },
            }
        )

    def _mark_failed(self, job_id: int, message: str) -> None:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT case_id, batch_id FROM render_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            conn.execute(
                """
                UPDATE render_jobs
                SET status = 'failed',
                    finished_at = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (_now_iso(), message[:4000], job_id),
            )
        self._publish(
            {
                "type": "job_update",
                "job_id": job_id,
                "case_id": row["case_id"] if row else None,
                "batch_id": row["batch_id"] if row else None,
                "status": "failed",
                "error_message": message[:1000],
            }
        )

    # ------------------------------------------------------------------
    # Undo
    # ------------------------------------------------------------------

    def undo_render(self, case_id: int, source_route: str | None = None) -> dict[str, Any]:
        """Delete the most recent done render's output file + write op=undo_render audit.

        Also flips the corresponding render_jobs row to status='undone' so the
        UI can reflect the change (UndoToast / RenderStatusCard rely on the
        latest job's status to render their state).

        Returns {"undone": bool, "output_path": str | None, "revision_id": int | None}.
        Raises ValueError if no recent render to undo.
        """
        with db.connect() as conn:
            rev = conn.execute(
                """
                SELECT * FROM case_revisions
                WHERE case_id = ?
                  AND op = 'render'
                  AND undone_at IS NULL
                ORDER BY changed_at DESC, id DESC
                LIMIT 1
                """,
                (case_id,),
            ).fetchone()
            if rev is None:
                raise ValueError("nothing to undo")
            after = json.loads(rev["after_json"] or "{}")
            output_path = after.get("render_output_path")
            job_id = after.get("render_job_id")
            removed = False
            if output_path:
                p = Path(output_path)
                if p.exists() and p.is_file():
                    try:
                        p.unlink()
                        removed = True
                    except OSError:
                        # Permission/race — leave file but mark revision undone anyway.
                        pass
            # Flip the corresponding job row to status='undone' so the UI
            # surfaces the new state. We keep output_path for forensic purposes
            # (the UI checks status, not file existence, to decide what to show).
            if isinstance(job_id, int):
                conn.execute(
                    """
                    UPDATE render_jobs
                    SET status = 'undone'
                    WHERE id = ? AND status IN ('done', 'done_with_issues', 'blocked')
                    """,
                    (job_id,),
                )
            # Write the undo revision.
            undo_id = audit.record_revision(
                conn,
                case_id,
                op="undo_render",
                before=after,
                after={"render_output_path": None, "removed_file": removed},
                source_route=source_route,
                actor="user",
            )
            # Mark source revision as undone.
            conn.execute(
                "UPDATE case_revisions SET undone_at = ? WHERE id = ?",
                (_now_iso(), rev["id"]),
            )
            # Cascade: any active evaluation pointing at this render job is also
            # invalidated — the artifact file is gone, so the evaluation has no
            # subject to refer back to. Reverse direction (evaluation undo) does
            # NOT cascade here; that's intentional.
            if isinstance(job_id, int):
                conn.execute(
                    """
                    UPDATE evaluations
                    SET undone_at = ?
                    WHERE subject_kind = 'render'
                      AND subject_id = ?
                      AND undone_at IS NULL
                    """,
                    (_now_iso(), job_id),
                )
        self._publish(
            {
                "type": "job_update",
                "job_id": job_id if isinstance(job_id, int) else None,
                "case_id": case_id,
                "status": "undone",
                "output_path": output_path,
                "revision_id": undo_id,
            }
        )
        return {"undone": True, "output_path": output_path, "revision_id": undo_id, "removed_file": removed}

    # ------------------------------------------------------------------
    # Recovery (call once at process start)
    # ------------------------------------------------------------------

    def recover(self) -> dict[str, int]:
        """Demote residual 'running' jobs to 'queued' and resubmit all queued jobs.

        Called from main.py module top-level after init_schema.
        """
        token = f"render-recover:{os.getpid()}:{uuid.uuid4().hex}"
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            running_to_queued = conn.execute(
                """
                UPDATE render_jobs
                SET status = 'queued',
                    started_at = NULL,
                    recovery_token = NULL,
                    recovery_claimed_at = NULL
                WHERE status = 'running'
                """
            ).rowcount
            queued_rows: list[sqlite3.Row] = conn.execute(
                """
                SELECT id
                FROM render_jobs
                WHERE status = 'queued'
                  AND recovery_token IS NULL
                ORDER BY enqueued_at
                """
            ).fetchall()
            if queued_rows:
                placeholders = ",".join("?" for _ in queued_rows)
                conn.execute(
                    f"""
                    UPDATE render_jobs
                    SET recovery_token = ?,
                        recovery_claimed_at = ?
                    WHERE id IN ({placeholders})
                      AND status = 'queued'
                      AND recovery_token IS NULL
                    """,
                    (token, _now_iso(), *[r["id"] for r in queued_rows]),
                )
                queued_rows = conn.execute(
                    "SELECT id FROM render_jobs WHERE recovery_token = ? ORDER BY enqueued_at",
                    (token,),
                ).fetchall()
        for r in queued_rows:
            _job_pool.submit(self._execute_safe, r["id"])
        return {"requeued_running": running_to_queued, "resubmitted_queued": len(queued_rows)}


# Module-level singleton. Imported by routes/render.py and main.py.
RENDER_QUEUE = RenderQueue()
