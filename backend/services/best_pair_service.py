"""Best-pair compute, selection, and render handoff service."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from fastapi import HTTPException

from .. import audit, db as _db
from .. import scanner, source_images
from .. import source_selection
from .case_files import resolve_existing_source
from .image_override_writer import write_image_override

TOP_N = 5
SLOTS = ("front", "oblique", "side")
_IMAGE_SUFFIXES = scanner.IMAGE_EXTS
_CASE_LAYOUT_BOARD = Path("/Users/a1234/Desktop/飞书Claude/skills/case-layout-board/scripts/case_layout_board.py")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_conn_factory() -> sqlite3.Connection:
    return _db.get_conn()


def _json_obj(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _connect(factory: Callable[[], sqlite3.Connection] | None) -> sqlite3.Connection:
    conn = (factory or _default_conn_factory)()
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_cache_row(conn: sqlite3.Connection, case_id: int) -> int:
    row = conn.execute(
        "SELECT source_version FROM case_best_pairs WHERE case_id = ?",
        (case_id,),
    ).fetchone()
    if row is not None:
        return int(row["source_version"])
    now = _now_iso()
    conn.execute(
        """INSERT INTO case_best_pairs
             (case_id, status, candidates_json, source_version, scanned_at, updated_at)
           VALUES (?, 'pending', '[]', 0, ?, ?)""",
        (case_id, now, now),
    )
    conn.commit()
    return 0


def _case_row(conn: sqlite3.Connection, case_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT id, abs_path, meta_json, skill_image_metadata_json
           FROM cases
           WHERE id = ? AND trashed_at IS NULL""",
        (case_id,),
    ).fetchone()


def _source_files(case_dir: Path, meta_json: str | None) -> list[str]:
    meta = _json_obj(meta_json)
    raw_files = [str(item) for item in (meta.get("image_files") or []) if item]
    if raw_files:
        split = source_images.existing_source_image_files(str(case_dir), raw_files)
        return [str(item) for item in (split.get("existing") or [])]
    out: list[str] = []
    for path in sorted(case_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        rel = path.relative_to(case_dir).as_posix()
        if not source_images.is_source_image_file(rel):
            continue
        out.append(rel)
    return out


def _read_overrides(conn: sqlite3.Connection, case_id: int) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """SELECT filename, manual_phase, manual_view, manual_transform_json, reason_json, reviewer, updated_at
           FROM case_image_overrides WHERE case_id = ?""",
        (case_id,),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = {
            "manual_phase": row["manual_phase"],
            "manual_view": row["manual_view"],
            "manual_transform_json": row["manual_transform_json"],
            "reason_json": row["reason_json"],
            "reviewer": row["reviewer"],
            "updated_at": row["updated_at"],
        }
        name = str(row["filename"])
        out[name] = item
        out.setdefault(Path(name).name, item)
    return out


def _coerce_pose(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    try:
        return {
            "yaw": float(raw.get("yaw") or 0.0),
            "pitch": float(raw.get("pitch") or 0.0),
            "roll": float(raw.get("roll") or 0.0),
        }
    except (TypeError, ValueError):
        return None


def _skill_index(skill_image_metadata_json: str | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in _json_list(skill_image_metadata_json):
        if not isinstance(item, dict):
            continue
        keys = {
            str(item.get("relative_path") or "").strip(),
            str(item.get("filename") or "").strip(),
        }
        keys.add(Path(str(item.get("relative_path") or item.get("filename") or "")).name)
        normalized = dict(item)
        pose = _coerce_pose(item.get("pose"))
        if pose:
            normalized["pose"] = pose
        for key in keys:
            if key:
                out[key] = normalized
    return out


def _phase_from_text(value: str) -> str | None:
    lowered = value.lower()
    if any(token.lower() in lowered for token in source_images.BEFORE_TOKENS):
        return "before"
    if any(token.lower() in lowered for token in source_images.AFTER_TOKENS):
        return "after"
    return None


def _phase_for_file(filename: str, overrides: dict[str, dict[str, Any]], skill_by_file: dict[str, dict[str, Any]]) -> str | None:
    override = overrides.get(filename) or overrides.get(Path(filename).name) or {}
    phase = override.get("manual_phase")
    if phase in {"before", "after"}:
        return str(phase)
    skill = skill_by_file.get(filename) or skill_by_file.get(Path(filename).name) or {}
    phase = skill.get("phase")
    if phase in {"before", "after"}:
        return str(phase)
    return _phase_from_text(filename)


def _view_from_text(value: str) -> str | None:
    lowered = value.lower()
    if any(token in value for token in ("正面", "面部正", "front")) or "front" in lowered:
        return "front"
    if any(token in value for token in ("45", "四分之三", "斜侧", "斜面")) or "oblique" in lowered:
        return "oblique"
    if any(token in value for token in ("侧面", "侧脸", "左侧", "右侧")) or "side" in lowered or "profile" in lowered:
        return "side"
    return None


def _view_for_file(filename: str, overrides: dict[str, dict[str, Any]], skill_by_file: dict[str, dict[str, Any]]) -> str | None:
    override = overrides.get(filename) or overrides.get(Path(filename).name) or {}
    view = override.get("manual_view")
    if view in SLOTS:
        return str(view)
    skill = skill_by_file.get(filename) or skill_by_file.get(Path(filename).name) or {}
    view = skill.get("view") or skill.get("view_bucket") or skill.get("angle")
    if view in SLOTS:
        return str(view)
    return _view_from_text(filename)


def _partition_phases(
    files: list[str],
    overrides: dict[str, dict[str, Any]],
    skill_by_file: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str]]:
    before: list[str] = []
    after: list[str] = []
    for filename in files:
        phase = _phase_for_file(filename, overrides, skill_by_file)
        if phase == "before":
            before.append(filename)
        elif phase == "after":
            after.append(filename)
    return before, after


def _partition_phase_views(
    files: list[str],
    overrides: dict[str, dict[str, Any]],
    skill_by_file: dict[str, dict[str, Any]],
) -> dict[str, dict[str, list[str]]]:
    slots: dict[str, dict[str, list[str]]] = {
        view: {"before": [], "after": []}
        for view in SLOTS
    }
    for filename in files:
        phase = _phase_for_file(filename, overrides, skill_by_file)
        view = _view_for_file(filename, overrides, skill_by_file)
        if phase in {"before", "after"} and view in SLOTS:
            slots[view][phase].append(filename)
    return slots


def _load_case_layout_board_module():
    if not _CASE_LAYOUT_BOARD.is_file():
        raise RuntimeError(f"case-layout-board script missing: {_CASE_LAYOUT_BOARD}")
    spec = importlib.util.spec_from_file_location("_case_layout_board_for_best_pair", _CASE_LAYOUT_BOARD)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load case_layout_board.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _analyze_faces(case_dir: Path, filenames: list[str]) -> dict[str, dict[str, float]]:
    """Analyze real source images and return pose by relative filename.

    Existing skill metadata is used before this function is called, so this is
    only a fallback for real source files without stored pose results.
    """
    module = _load_case_layout_board_module()
    out: dict[str, dict[str, float]] = {}
    for filename in filenames:
        try:
            item = module.analyze_image(case_dir / filename, case_dir, {}, case_dir, None)
        except Exception:
            continue
        pose = _coerce_pose((item or {}).get("pose") if isinstance(item, dict) else None)
        if pose:
            out[filename] = pose
    return out


def _collect_poses(case_dir: Path, files: list[str], skill_by_file: dict[str, dict[str, Any]]) -> dict[str, dict[str, float]]:
    poses: dict[str, dict[str, float]] = {}
    missing: list[str] = []
    for filename in files:
        skill = skill_by_file.get(filename) or skill_by_file.get(Path(filename).name) or {}
        pose = _coerce_pose(skill.get("pose"))
        if pose:
            poses[filename] = pose
        else:
            missing.append(filename)
    if missing:
        try:
            poses.update(_analyze_faces(case_dir, missing))
        except (ImportError, RuntimeError, ModuleNotFoundError):
            # The local pose backend is optional at runtime. Existing skill
            # metadata is still valid; callers will skip if no before/after
            # pose pair remains after this fallback fails.
            pass
    return poses


def _fingerprint(files: list[str], overrides: dict[str, dict[str, Any]], latest_mtime_ns: int) -> str:
    relevant_overrides = {
        key: overrides[key]
        for key in sorted(overrides)
        if key in files or Path(key).name in {Path(item).name for item in files}
    }
    payload = json.dumps(
        {
            "files": sorted(files),
            "overrides": relevant_overrides,
            "mtime": int(latest_mtime_ns),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _write_skip(conn: sqlite3.Connection, case_id: int, observed: int, reason: str) -> dict[str, Any]:
    now = _now_iso()
    cur = conn.execute(
        """UPDATE case_best_pairs
           SET status = 'skipped',
               skipped_reason = ?,
               candidates_json = '[]',
               candidates_fingerprint = NULL,
               scanned_at = ?,
               updated_at = ?
           WHERE case_id = ? AND source_version = ?""",
        (reason, now, now, case_id, observed),
    )
    conn.commit()
    if cur.rowcount != 1:
        return {"case_id": case_id, "status": "dirty", "candidates": [], "material_tasks": [], "fingerprint": None}
    return {"case_id": case_id, "status": "skipped", "skipped_reason": reason, "candidates": [], "material_tasks": [], "fingerprint": None}


def _pair_delta(before_pose: dict[str, float], after_pose: dict[str, float]) -> dict[str, float]:
    dy = float(after_pose["yaw"]) - float(before_pose["yaw"])
    dp = float(after_pose["pitch"]) - float(before_pose["pitch"])
    dr = float(after_pose["roll"]) - float(before_pose["roll"])
    return {
        "delta_deg": round(math.sqrt(dy * dy + dp * dp + dr * dr), 3),
        "delta_yaw": round(dy, 3),
        "delta_pitch": round(dp, 3),
        "delta_roll": round(dr, 3),
    }


def _abs_delta(candidate: dict[str, Any], key: str) -> float:
    try:
        return abs(float(candidate.get(key) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _metadata_for_file(skill_by_file: dict[str, dict[str, Any]], filename: str) -> dict[str, Any]:
    return dict(skill_by_file.get(filename) or skill_by_file.get(Path(filename).name) or {})


def _sharpness_component(before_meta: dict[str, Any], after_meta: dict[str, Any]) -> dict[str, Any]:
    values = [
        value
        for value in (
            _float_or_none(before_meta.get("sharpness_score")),
            _float_or_none(after_meta.get("sharpness_score")),
        )
        if value is not None
    ]
    if not values:
        return {"score": 8, "label": "unknown", "reason": "清晰度未验证"}
    minimum = min(values)
    if minimum >= 70:
        score = 20
        label = "strong"
    elif minimum >= 45:
        score = 10
        label = "review"
    elif minimum >= 15:
        score = -8
        label = "weak"
    else:
        score = -18
        label = "block"
    return {
        "score": score,
        "label": label,
        "before": values[0] if len(values) > 0 else None,
        "after": values[1] if len(values) > 1 else None,
        "minimum": minimum,
    }


def _face_component(before_meta: dict[str, Any], after_meta: dict[str, Any], view: str | None) -> dict[str, Any]:
    warnings: list[str] = []
    for role, meta in (("before", before_meta), ("after", after_meta)):
        rejection = str(meta.get("rejection_reason") or "")
        issues = [str(item) for item in (meta.get("issues") or []) if item]
        if rejection == "face_detection_failure" or any("面部检测失败" in item for item in issues):
            warnings.append(f"{role}_face_detection_failure")
        elif meta.get("face_detected") is False:
            warnings.append(f"{role}_face_not_detected")
    if warnings and view == "front":
        score = -30
        label = "block"
    elif warnings:
        score = -8
        label = "review"
    else:
        score = 20
        label = "strong"
    return {"score": score, "label": label, "warnings": warnings}


def _comparability_component(before_meta: dict[str, Any], after_meta: dict[str, Any]) -> dict[str, Any]:
    before_conf = _float_or_none(before_meta.get("angle_confidence"))
    after_conf = _float_or_none(after_meta.get("angle_confidence"))
    if before_conf is None or after_conf is None:
        return {"score": 8, "label": "unknown", "reason": "角度置信度未验证"}
    minimum = min(before_conf, after_conf)
    delta = round(abs(before_conf - after_conf), 3)
    if minimum >= 0.85 and delta <= 0.12:
        score = 20
        label = "strong"
    elif minimum >= 0.65 and delta <= 0.25:
        score = 10
        label = "review"
    elif minimum >= 0.55:
        score = 0
        label = "weak"
    else:
        score = -12
        label = "block"
    return {"score": score, "label": label, "before": before_conf, "after": after_conf, "delta": delta}


def _quality_breakdown(
    *,
    view: str | None,
    candidate: dict[str, Any],
    before_meta: dict[str, Any],
    after_meta: dict[str, Any],
) -> dict[str, Any]:
    audit_data = candidate.get("rank_audit") if isinstance(candidate.get("rank_audit"), dict) else {}
    warnings = audit_data.get("warnings") if isinstance(audit_data.get("warnings"), list) else []
    weighted = float(audit_data.get("weighted") or candidate.get("delta_deg") or 0.0)
    pose_score = max(0, round(35 - weighted * 1.8))
    if warnings:
        pose_score = max(0, pose_score - 10)
    sharpness = _sharpness_component(before_meta, after_meta)
    face = _face_component(before_meta, after_meta, view)
    comparability = _comparability_component(before_meta, after_meta)
    primary_judge = source_selection.pair_primary_judgment(str(view or ""), before_meta, after_meta)
    primary_adjustment = sum(
        int(component.get("score") or 0)
        for component in (
            primary_judge.get("identity"),
            primary_judge.get("exposure"),
            primary_judge.get("crop"),
        )
        if isinstance(component, dict)
    )
    total = max(
        0,
        min(
            100,
            round(
                pose_score
                + int(sharpness.get("score") or 0)
                + int(face.get("score") or 0)
                + int(comparability.get("score") or 0)
                + primary_adjustment
            ),
        ),
    )
    labels = {str(item.get("label") or "") for item in (sharpness, face, comparability)}
    component_statuses = {
        str(component.get("status") or "")
        for component in (
            primary_judge.get("identity"),
            primary_judge.get("exposure"),
            primary_judge.get("crop"),
        )
        if isinstance(component, dict)
    }
    hard_blocker = "block" in labels or "block" in component_statuses
    label = "block" if hard_blocker or total < 45 else "review" if total < 75 else "strong"
    quality_warnings: list[str] = []
    for component_name, component in (
        ("sharpness", sharpness),
        ("face_completeness", face),
        ("comparability", comparability),
    ):
        if component.get("label") in {"block", "weak"}:
            quality_warnings.append(f"{component_name}_{component.get('label')}")
        quality_warnings.extend(str(item) for item in (component.get("warnings") or []) if item)
    for component_name in ("identity", "exposure", "crop"):
        component = primary_judge.get(component_name)
        if isinstance(component, dict) and component.get("code"):
            quality_warnings.append(str(component["code"]))
    return {
        "pose": {"score": pose_score, "weighted": weighted, "warnings": warnings},
        "sharpness": sharpness,
        "face_completeness": face,
        "comparability": comparability,
        "identity": primary_judge.get("identity"),
        "exposure": primary_judge.get("exposure"),
        "crop": primary_judge.get("crop"),
        "primary_judge": primary_judge,
        "total": total,
        "label": label,
        "hard_blocker": hard_blocker,
        "warnings": quality_warnings[:6],
    }


def _ranking_audit(view: str | None, candidate: dict[str, Any]) -> dict[str, Any] | None:
    if view not in SLOTS:
        return None
    thresholds = source_selection.POSE_THRESHOLDS.get(view, {})
    yaw = _abs_delta(candidate, "delta_yaw")
    pitch = _abs_delta(candidate, "delta_pitch")
    roll = _abs_delta(candidate, "delta_roll")
    weighted = round(yaw + pitch + roll * 0.5, 3)
    pitch_threshold = float(thresholds.get("pitch", 99.0))
    weighted_threshold = float(thresholds.get("weighted", 99.0))
    yaw_threshold = float(thresholds.get("yaw", 99.0))
    roll_threshold = float(thresholds.get("roll", 99.0))
    pitch_over = max(0.0, round(pitch - pitch_threshold, 3))
    weighted_over = max(0.0, round(weighted - weighted_threshold, 3))
    axis_over = {
        "yaw": max(0.0, round(yaw - yaw_threshold, 3)),
        "pitch": pitch_over,
        "roll": max(0.0, round(roll - roll_threshold, 3)),
        "weighted": weighted_over,
    }
    reasons: list[str] = []
    warnings: list[str] = []
    if view == "front" and pitch_over > 0:
        warnings.append("front_pitch_over_threshold")
    elif view == "front":
        reasons.append("front_pitch_within_threshold")
    if weighted_over > 0:
        warnings.append("weighted_delta_over_threshold")
    else:
        reasons.append("weighted_delta_within_threshold")
    return {
        "view": view,
        "yaw_abs": round(yaw, 3),
        "pitch_abs": round(pitch, 3),
        "roll_abs": round(roll, 3),
        "weighted": weighted,
        "thresholds": {
            "yaw": yaw_threshold,
            "pitch": pitch_threshold,
            "roll": roll_threshold,
            "weighted": weighted_threshold,
        },
        "axis_over_threshold": axis_over,
        "reasons": reasons,
        "warnings": warnings,
    }


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    view = str(candidate.get("view") or "")
    delta_deg = float(candidate.get("delta_deg") or 0.0)
    breakdown = candidate.get("score_breakdown") if isinstance(candidate.get("score_breakdown"), dict) else {}
    quality_total = int(breakdown.get("total") or 0)
    quality_block = 1 if breakdown.get("hard_blocker") or breakdown.get("label") == "block" else 0
    if view == "front":
        audit_data = candidate.get("rank_audit") if isinstance(candidate.get("rank_audit"), dict) else {}
        axis_over = audit_data.get("axis_over_threshold") if isinstance(audit_data.get("axis_over_threshold"), dict) else {}
        weighted = float(audit_data.get("weighted") or 0.0)
        pitch_abs = float(audit_data.get("pitch_abs") or _abs_delta(candidate, "delta_pitch"))
        pitch_over = float(axis_over.get("pitch") or 0.0)
        weighted_over = float(axis_over.get("weighted") or 0.0)
        return (
            quality_block,
            1 if pitch_over > 0 else 0,
            round(pitch_over, 3),
            1 if weighted_over > 0 else 0,
            round(weighted_over, 3),
            -quality_total,
            round(pitch_abs, 3),
            round(weighted, 3),
            delta_deg,
            str(candidate.get("before") or ""),
            str(candidate.get("after") or ""),
        )
    return (
        quality_block,
        -quality_total,
        delta_deg,
        str(candidate.get("before") or ""),
        str(candidate.get("after") or ""),
    )


def _candidate(
    *,
    before_name: str,
    after_name: str,
    before_pose: dict[str, float],
    after_pose: dict[str, float],
    view: str | None = None,
    before_meta: dict[str, Any] | None = None,
    after_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "before": before_name,
        "after": after_name,
        **_pair_delta(before_pose, after_pose),
        "before_pose": before_pose,
        "after_pose": after_pose,
    }
    if view in SLOTS:
        item["view"] = view
        audit_data = _ranking_audit(view, item)
        if audit_data:
            item["rank_audit"] = audit_data
    breakdown = _quality_breakdown(
        view=view,
        candidate=item,
        before_meta=before_meta or {},
        after_meta=after_meta or {},
    )
    item["score_breakdown"] = breakdown
    rank_audit = item.setdefault("rank_audit", {})
    if isinstance(rank_audit, dict):
        rank_audit["quality_score"] = breakdown["total"]
        rank_audit["quality_label"] = breakdown["label"]
        if breakdown.get("warnings"):
            warnings = list(rank_audit.get("warnings") or [])
            warnings.extend(str(value) for value in breakdown["warnings"] if value not in warnings)
            rank_audit["warnings"] = warnings[:8]
    return item


def _group_candidates_by_slot(candidates: list[Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {view: [] for view in SLOTS}
    for item in candidates:
        if not isinstance(item, dict):
            continue
        view = str(item.get("view") or "")
        if view in grouped:
            grouped[view].append(item)
    return grouped


def _front_pitch_material_task(case_id: int, front_candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not front_candidates:
        return None
    risky: list[dict[str, Any]] = []
    for candidate in front_candidates:
        audit_data = candidate.get("rank_audit") if isinstance(candidate.get("rank_audit"), dict) else {}
        warnings = audit_data.get("warnings") if isinstance(audit_data.get("warnings"), list) else []
        if "front_pitch_over_threshold" in {str(item) for item in warnings}:
            risky.append(candidate)
    if len(risky) != len(front_candidates):
        return None
    best = front_candidates[0]
    best_audit = best.get("rank_audit") if isinstance(best.get("rank_audit"), dict) else {}
    thresholds = best_audit.get("thresholds") if isinstance(best_audit.get("thresholds"), dict) else {}
    seed_params: list[tuple[str, str]] = [
        ("manual_seed_source", "front_pitch_blocker"),
        ("manual_seed_view", "front"),
    ]
    for role in ("before", "after"):
        filename = str(best.get(role) or "").strip()
        if filename:
            seed_params.append(("file", filename))
    supplement_href = f"/cases/{case_id}?{urlencode(seed_params)}#manual-render"
    return {
        "code": "front_pitch_material_gap",
        "view": "front",
        "severity": "block_publish",
        "title": "正面 pitch 安全配对缺失",
        "message": "正面候选的 pitch 差均超过发布阈值，需要补一组更接近的正面术前/术后，或仅做人工风险接受。",
        "candidate_count": len(front_candidates),
        "thresholds": thresholds,
        "best_candidate": {
            "before": best.get("before"),
            "after": best.get("after"),
            "delta_deg": best.get("delta_deg"),
            "delta_yaw": best.get("delta_yaw"),
            "delta_pitch": best.get("delta_pitch"),
            "delta_roll": best.get("delta_roll"),
            "rank_audit": best_audit,
        },
        "recommended_actions": [
            {
                "code": "add_front_pitch_material_pair",
                "label": "补一组 pitch 更接近的正面术前/术后",
                "source": "material_loop",
                "href": supplement_href,
            },
            {
                "code": "accept_front_pitch_risk_keep_unpublishable",
                "label": "人工接受该正面姿态差，但继续保持不可发布门禁",
                "source": "quality_gate",
                "publish_gate": {
                    "can_publish_after_acceptance": False,
                    "reason": "front_pitch_over_threshold",
                },
            },
        ],
        "publish_gate": {
            "can_publish_after_acceptance": False,
            "requires_new_material_for_publish": True,
        },
    }


def _material_tasks(case_id: int, candidates_by_slot: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    front_task = _front_pitch_material_task(case_id, candidates_by_slot.get("front") or [])
    if front_task:
        tasks.append(front_task)
    return tasks


def compute_best_pair(
    case_id: int,
    *,
    db_conn_factory: Callable[[], sqlite3.Connection] | None = None,
) -> dict[str, Any]:
    conn = _connect(db_conn_factory)
    try:
        observed = _ensure_cache_row(conn, case_id)
        row = _case_row(conn, case_id)
        if row is None:
            return _write_skip(conn, case_id, observed, "dir_missing")
        case_dir = Path(str(row["abs_path"])).resolve()
        if not case_dir.is_dir():
            return _write_skip(conn, case_id, observed, "dir_missing")
        files = _source_files(case_dir, row["meta_json"])
        if not files:
            return _write_skip(conn, case_id, observed, "no_source_photos")
        overrides = _read_overrides(conn, case_id)
        skill_by_file = _skill_index(row["skill_image_metadata_json"])
        before, after = _partition_phases(files, overrides, skill_by_file)
        if not before or not after:
            return _write_skip(conn, case_id, observed, "no_phase_labels")
        poses = _collect_poses(case_dir, sorted(set(before + after)), skill_by_file)
        before_ready = [filename for filename in before if filename in poses]
        after_ready = [filename for filename in after if filename in poses]
        if not before_ready or not after_ready:
            return _write_skip(conn, case_id, observed, "no_face_detected")
        candidates: list[dict[str, Any]] = []
        slots = _partition_phase_views(files, overrides, skill_by_file)
        for view, slot in slots.items():
            for before_name in [filename for filename in slot["before"] if filename in poses]:
                for after_name in [filename for filename in slot["after"] if filename in poses]:
                    candidates.append(
                        _candidate(
                            before_name=before_name,
                            after_name=after_name,
                            before_pose=poses[before_name],
                            after_pose=poses[after_name],
                            view=view,
                            before_meta=_metadata_for_file(skill_by_file, before_name),
                            after_meta=_metadata_for_file(skill_by_file, after_name),
                        )
                    )
        if not candidates:
            for before_name in before_ready:
                for after_name in after_ready:
                    candidates.append(
                        _candidate(
                            before_name=before_name,
                            after_name=after_name,
                            before_pose=poses[before_name],
                            after_pose=poses[after_name],
                            before_meta=_metadata_for_file(skill_by_file, before_name),
                            after_meta=_metadata_for_file(skill_by_file, after_name),
                        )
                    )
        grouped = _group_candidates_by_slot(candidates)
        top_by_slot = {
            view: sorted(items, key=_candidate_sort_key)[:TOP_N]
            for view, items in grouped.items()
        }
        material_tasks = _material_tasks(case_id, top_by_slot)
        slot_top_candidates = [item for view in SLOTS for item in top_by_slot[view]]
        top = sorted(slot_top_candidates or candidates, key=_candidate_sort_key)
        stored_candidates = top if slot_top_candidates else top[:TOP_N]
        latest_mtime = max(resolve_existing_source(case_dir, filename).stat().st_mtime_ns for filename in sorted(set(before + after)))
        fp = _fingerprint(sorted(set(before + after)), overrides, latest_mtime)
        now = _now_iso()
        cur = conn.execute(
            """UPDATE case_best_pairs
               SET status = 'ready',
                   skipped_reason = NULL,
                   candidates_json = ?,
                   candidates_fingerprint = ?,
                   scanned_at = ?,
                   updated_at = ?
               WHERE case_id = ? AND source_version = ?""",
            (json.dumps(stored_candidates, ensure_ascii=False), fp, now, now, case_id, observed),
        )
        conn.commit()
        if cur.rowcount != 1:
            return {"case_id": case_id, "status": "dirty", "candidates": [], "material_tasks": [], "fingerprint": None}
        return {
            "case_id": case_id,
            "status": "ready",
            "candidates": stored_candidates,
            "candidates_by_slot": top_by_slot,
            "material_tasks": material_tasks,
            "fingerprint": fp,
            "scanned_at": now,
        }
    finally:
        conn.close()


def list_best_pair(
    case_id: int,
    *,
    db_conn_factory: Callable[[], sqlite3.Connection] | None = None,
) -> dict[str, Any]:
    conn = _connect(db_conn_factory)
    try:
        row = conn.execute(
            """SELECT status, skipped_reason, candidates_json, candidates_fingerprint,
                      source_version, scanned_at, updated_at
               FROM case_best_pairs WHERE case_id = ?""",
            (case_id,),
        ).fetchone()
        selection = conn.execute(
            """SELECT id, before_filename, after_filename, delta_deg, view,
                      COALESCE(candidates_fingerprint_snapshot, candidates_fingerprint) AS fingerprint,
                      selected_at, selected_by, selection_reason
               FROM case_best_pair_selections
               WHERE case_id = ?
               ORDER BY selected_at DESC, id DESC
               LIMIT 1""",
            (case_id,),
        ).fetchone()
        selection_rows = conn.execute(
            """SELECT id, before_filename, after_filename, delta_deg, view,
                      COALESCE(candidates_fingerprint_snapshot, candidates_fingerprint) AS fingerprint,
                      selected_at, selected_by, selection_reason
               FROM case_best_pair_selections
               WHERE case_id = ?
               ORDER BY selected_at DESC, id DESC""",
            (case_id,),
        ).fetchall()
        current_selection = None
        if selection is not None:
            current_selection = {
                "id": selection["id"],
                "before": selection["before_filename"],
                "after": selection["after_filename"],
                "delta_deg": selection["delta_deg"],
                "view": selection["view"],
                "fingerprint": selection["fingerprint"],
                "selected_at": selection["selected_at"],
                "selected_by": selection["selected_by"],
                "selection_reason": selection["selection_reason"],
            }
        current_selection_by_slot: dict[str, dict[str, Any]] = {}
        for selection_row in selection_rows:
            view = str(selection_row["view"] or "")
            if view not in SLOTS or view in current_selection_by_slot:
                continue
            current_selection_by_slot[view] = {
                "id": selection_row["id"],
                "before": selection_row["before_filename"],
                "after": selection_row["after_filename"],
                "delta_deg": selection_row["delta_deg"],
                "view": view,
                "fingerprint": selection_row["fingerprint"],
                "selected_at": selection_row["selected_at"],
                "selected_by": selection_row["selected_by"],
                "selection_reason": selection_row["selection_reason"],
            }
        if row is None:
            return {
                "case_id": case_id,
                "status": "pending",
                "skipped_reason": None,
                "candidates": [],
                "candidates_by_slot": {view: [] for view in SLOTS},
                "material_tasks": [],
                "fingerprint": None,
                "source_version": None,
                "current_selection": current_selection,
                "current_selection_by_slot": current_selection_by_slot,
            }
        try:
            candidates = json.loads(row["candidates_json"] or "[]")
        except (TypeError, ValueError):
            candidates = []
        candidates = candidates if isinstance(candidates, list) else []
        return {
            "case_id": case_id,
            "status": row["status"],
            "skipped_reason": row["skipped_reason"],
            "candidates": candidates,
            "candidates_by_slot": _group_candidates_by_slot(candidates),
            "material_tasks": _material_tasks(case_id, _group_candidates_by_slot(candidates)),
            "fingerprint": row["candidates_fingerprint"],
            "source_version": row["source_version"],
            "scanned_at": row["scanned_at"],
            "updated_at": row["updated_at"],
            "current_selection": current_selection,
            "current_selection_by_slot": current_selection_by_slot,
        }
    finally:
        conn.close()


def _snapshot_override(conn: sqlite3.Connection, case_id: int, filename: str) -> str | None:
    row = conn.execute(
        """SELECT filename, manual_phase, manual_view, manual_transform_json, reason_json, reviewer, updated_at
           FROM case_image_overrides
           WHERE case_id = ? AND filename = ?""",
        (case_id, filename),
    ).fetchone()
    return None if row is None else json.dumps(dict(row), ensure_ascii=False)


def select_best_pair_for_case(
    case_id: int,
    before: str,
    after: str,
    fingerprint: str,
    view: str | None = None,
    *,
    reviewer: str | None = None,
    reason: str | None = None,
    db_conn_factory: Callable[[], sqlite3.Connection] | None = None,
) -> int:
    if view is not None:
        view = str(view).strip()
        if view not in SLOTS:
            raise HTTPException(400, "view must be one of front, oblique, side")
    conn = _connect(db_conn_factory)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _case_row(conn, case_id)
        if row is None:
            raise HTTPException(404, "case not found")
        befores = audit.snapshot_before(conn, [case_id])
        case_dir = Path(str(row["abs_path"])).resolve()
        resolve_existing_source(case_dir, before)
        resolve_existing_source(case_dir, after)
        cache = conn.execute(
            """SELECT status, candidates_json, candidates_fingerprint
               FROM case_best_pairs WHERE case_id = ?""",
            (case_id,),
        ).fetchone()
        if cache is None or cache["status"] != "ready":
            raise HTTPException(409, "cache_not_ready")
        if str(cache["candidates_fingerprint"] or "") != fingerprint:
            raise HTTPException(409, "stale_fingerprint")
        try:
            candidates = json.loads(cache["candidates_json"] or "[]")
        except (TypeError, ValueError):
            candidates = []
        match = next(
            (
                item
                for item in candidates
                if isinstance(item, dict) and item.get("before") == before and item.get("after") == after
                and (view is None or item.get("view") == view)
            ),
            None,
        )
        if match is None:
            raise HTTPException(400, "pair_not_in_candidates")
        selected_view = view if view in SLOTS else str(match.get("view") or "").strip()
        selected_view = selected_view if selected_view in SLOTS else None
        now = _now_iso()
        reviewer_name = str(reviewer or "best-pair").strip() or "best-pair"
        selection_reason = str(reason or "").strip()
        if not selection_reason:
            selection_reason = (
                f"best-pair slot {selected_view} selection"
                if selected_view
                else "best-pair manual selection"
            )
        reason_json = json.dumps(
            {
                "reason": selection_reason,
                "source": "best_pair_select",
                "reviewer": reviewer_name,
                "selection_view": selected_view,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        before_snapshot = _snapshot_override(conn, case_id, before)
        after_snapshot = _snapshot_override(conn, case_id, after)
        cur = conn.execute(
            """INSERT INTO case_best_pair_selections
                 (case_id, before_filename, after_filename, delta_deg,
                  candidates_fingerprint, candidates_fingerprint_snapshot,
                  before_override_before_json, after_override_before_json,
                  view, selected_at, selected_by, selection_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                case_id,
                before,
                after,
                float(match.get("delta_deg") or 0.0),
                fingerprint,
                fingerprint,
                before_snapshot,
                after_snapshot,
                selected_view,
                now,
                reviewer_name,
                selection_reason,
            ),
        )
        selection_id = int(cur.lastrowid or 0)
        write_image_override(
            conn,
            case_id=case_id,
            filename=before,
            manual_phase="before",
            manual_view=selected_view,
            manual_transform_json=None,
            updated_at=now,
            reason_json=reason_json,
            reviewer=reviewer_name,
            skip_dirty_mark=True,
        )
        write_image_override(
            conn,
            case_id=case_id,
            filename=after,
            manual_phase="after",
            manual_view=selected_view,
            manual_transform_json=None,
            updated_at=now,
            reason_json=reason_json,
            reviewer=reviewer_name,
            skip_dirty_mark=True,
        )
        if selected_view:
            meta = _json_obj(row["meta_json"])
            controls = source_selection.selection_controls_from_meta(meta)
            locked_slots = controls.setdefault("locked_slots", {})
            locked_slots[selected_view] = {
                "before": {"case_id": case_id, "filename": before},
                "after": {"case_id": case_id, "filename": after},
                "reviewer": reviewer_name,
                "reason": selection_reason,
                "updated_at": now,
            }
            controls["accepted_warnings"] = controls.get("accepted_warnings") or []
            meta[source_selection.SOURCE_GROUP_SELECTION_META_KEY] = controls
            conn.execute(
                "UPDATE cases SET meta_json = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False), case_id),
            )
        conn.execute(
            """UPDATE case_best_pairs
               SET status = 'ready',
                   source_version = source_version + 1,
                   candidates_fingerprint = ?,
                   updated_at = ?
               WHERE case_id = ?""",
            (fingerprint, now, case_id),
        )
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="best_pair_slot_select" if selected_view else "best_pair_select",
            source_route=f"/api/cases/{case_id}/best-pair/select",
        )
        conn.commit()
        return selection_id
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def trigger_best_pair_render(
    case_id: int,
    *,
    brand: str = "fumei",
    template: str = "tri-compare",
    db_conn_factory: Callable[[], sqlite3.Connection] | None = None,
) -> int:
    conn = _connect(db_conn_factory)
    try:
        row = conn.execute(
            """SELECT id, COALESCE(candidates_fingerprint_snapshot, candidates_fingerprint) AS fingerprint
               FROM case_best_pair_selections
               WHERE case_id = ?
               ORDER BY selected_at DESC, id DESC
               LIMIT 1""",
            (case_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(400, "no_current_selection")
        selection_id = int(row["id"])
        fingerprint = row["fingerprint"]
    finally:
        conn.close()

    from ..render_queue import RENDER_QUEUE

    return RENDER_QUEUE.enqueue(
        case_id=case_id,
        brand=brand,
        template=template,
        render_mode="best-pair",
        best_pair_selection_id=selection_id,
        candidates_fingerprint_snapshot=fingerprint,
    )


def revert_selection_overrides(
    selection_id: int,
    *,
    db_conn_factory: Callable[[], sqlite3.Connection] | None = None,
) -> None:
    """Future hook for selection undo; snapshots are already persisted."""
    raise NotImplementedError("best-pair selection revert is not implemented yet")
