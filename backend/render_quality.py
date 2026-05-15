"""Render artifact quality evaluation and persistence."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def evaluate_render_result(result: dict[str, Any]) -> dict[str, Any]:
    """Map a skill render result into a workbench quality envelope.

    The render subprocess can write a `final-board.jpg` even when the manifest
    status is `error`. That artifact is useful for review, but it must not be
    treated as a clean `done` result.
    """
    manifest_status = str(result.get("status") or "")
    blocking = int(result.get("blocking_issue_count") or 0)
    warnings = int(result.get("warning_count") or 0)
    output_path = result.get("output_path")
    output_exists = bool(output_path and Path(str(output_path)).is_file())
    ai_usage = result.get("ai_usage") or {}
    used_ai_enhancement = bool(ai_usage.get("used_after_enhancement"))
    used_ai_padfill = bool(ai_usage.get("used_ai_padfill"))
    render_error = str(result.get("render_error") or "").strip()
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
    action_suggestions = _action_suggestions(blocking_issues, display_warnings, composition_alerts)

    score = 100.0
    if manifest_status != "ok":
        score -= 35
    score -= min(blocking * 25, 60)
    score -= min(actionable_warnings * 3, 30)
    score -= min(noisy_warnings * 0.5, 4)
    score -= min(int(warning_buckets.get("pose_delta") or 0) * 5, 15)
    score -= min(int(warning_buckets.get("profile_quality") or 0) * 2, 18)
    score -= min(len(composition_alerts) * 8, 24)
    if used_ai_enhancement:
        score -= 10
    score = max(0.0, round(score, 1))

    if not output_exists:
        quality_status = "blocked"
    elif manifest_status == "ok" and blocking == 0 and score >= 80 and not composition_alerts and actionable_warnings == 0:
        quality_status = "done"
    else:
        quality_status = "done_with_issues"

    can_publish = quality_status == "done" and not used_ai_enhancement
    artifact_mode = "ai_after_simulation" if used_ai_enhancement else "ai_edge_padfill" if used_ai_padfill else "real_layout"
    return {
        "quality_status": quality_status,
        "quality_score": score,
        "can_publish": can_publish,
        "artifact_mode": artifact_mode,
        "manifest_status": manifest_status,
        "blocking_count": blocking,
        "warning_count": warnings,
        "metrics": {
            "phase_integrity": "blocked" if blocking else "ok",
            "angle_match": "review" if actionable_warnings or composition_alerts else "ok",
            "background_fill": "ai" if used_ai_padfill else "standard",
            "edge_integrity": "review" if actionable_warnings >= 3 else "ok",
            "ai_after_enhancement": used_ai_enhancement,
            "ai_edge_padfill": used_ai_padfill,
            "render_error": render_error,
            "blocking_issues": blocking_issues,
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
        },
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
    """Create quality rows for historical completed render jobs."""
    rows = conn.execute(
        """
        SELECT j.*
        FROM render_jobs j
        LEFT JOIN render_quality q ON q.render_job_id = j.id
        WHERE q.id IS NULL
          AND j.status IN ('done', 'done_with_issues', 'blocked')
        """
    ).fetchall()
    count = 0
    for row in rows:
        meta = _json_load(row["meta_json"], {})
        result = {
            "output_path": row["output_path"],
            "status": meta.get("status") or row["status"],
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
