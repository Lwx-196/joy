"""Render endpoints — Phase 3 daily-render workbench.

Endpoints:
    POST   /api/cases/{id}/render             enqueue single
    POST   /api/cases/render/batch            enqueue batch
    GET    /api/cases/{id}/render/jobs        per-case history
    GET    /api/cases/{id}/render/latest      most recent done job
    POST   /api/cases/{id}/render/undo        undo last render
    GET    /api/render/jobs/{job_id}          one job
    GET    /api/render/batches/{batch_id}     batch summary
    POST   /api/render/jobs/{job_id}/cancel   cancel queued job
    GET    /api/render/stream                 SSE feed
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import audit, db, render_executor, render_quality, source_images, stress
from ..render_queue import RENDER_QUEUE
from ..services import pre_render_gate

router = APIRouter(tags=["render"])

ALLOWED_BRANDS = {"fumei", "shimei", "芙美", "莳美", "meiji_ai", "md_ai"}
DEFAULT_BRAND = "fumei"
DEFAULT_TEMPLATE = "tri-compare"
DEFAULT_SEMANTIC = "auto"
ALLOWED_SEMANTIC = {"off", "auto"}
MAX_BATCH_SIZE = 50


def datetime_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------
# Pydantic
# ----------------------------------------------------------------------


class RenderRequest(BaseModel):
    brand: str = Field(default=DEFAULT_BRAND)
    template: str = Field(default=DEFAULT_TEMPLATE)
    semantic_judge: str = Field(default=DEFAULT_SEMANTIC)
    draft_preview: bool = False
    force: bool = False
    model: str | None = None
    system_prompt: str | None = None
    options: dict[str, Any] | None = None


class RenderBatchRequest(BaseModel):
    case_ids: list[int]
    brand: str = Field(default=DEFAULT_BRAND)
    template: str = Field(default=DEFAULT_TEMPLATE)
    semantic_judge: str = Field(default=DEFAULT_SEMANTIC)
    draft_preview: bool = False
    force: bool = False
    model: str | None = None
    system_prompt: str | None = None
    options: dict[str, Any] | None = None


class PreRenderGateRequest(BaseModel):
    persist_tickets: bool = False
    template: str = Field(default=DEFAULT_TEMPLATE)
    semantic_judge: str = Field(default=DEFAULT_SEMANTIC)


class RenderRestoreRequest(BaseModel):
    brand: str = Field(default=DEFAULT_BRAND)
    template: str = Field(default=DEFAULT_TEMPLATE)
    archived_at: str = Field(..., min_length=16, max_length=20)


class RenderQualityReviewRequest(BaseModel):
    verdict: str = Field(..., min_length=1, max_length=32)
    reviewer: str = Field(..., min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=2000)
    can_publish: bool | None = None


class AbFeedbackRequest(BaseModel):
    verdict: str = Field(..., min_length=1, max_length=16)
    reviewer: str = Field(..., min_length=1, max_length=64)
    workflow_profile: str | None = Field(default=None, max_length=160)
    baseline_job_id: int | None = None
    candidate_job_id: int | None = None
    simulation_job_id: int | None = None
    hard_defect_tags: list[str] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=2000)
    source: str = Field(default="gray_rollout", max_length=64)


def _metrics_keep_unpublishable(metrics: dict[str, Any]) -> bool:
    for action in metrics.get("action_suggestions") or []:
        if not isinstance(action, dict):
            continue
        gate = action.get("publish_gate")
        if isinstance(gate, dict) and gate.get("can_publish_after_acceptance") is False:
            return True
    for alert in metrics.get("composition_alerts") or []:
        if isinstance(alert, dict) and str(alert.get("code") or "") == "front_source_crop_touches_frame":
            return True
    return False


def _quality_review_hard_blockers(qrow: Any, metrics: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if qrow is not None:
        if int(qrow["blocking_count"] or 0) > 0:
            blockers.append("render_quality.blocking_count")
        quality_status = str(qrow["quality_status"] or "")
        if quality_status in {"blocked", "failed"}:
            blockers.append(f"render_quality.quality_status:{quality_status}")
        manifest_status = str(qrow["manifest_status"] or "")
        if manifest_status in {"missing", "error", "failed"}:
            blockers.append(f"render_quality.manifest_status:{manifest_status}")
    if _metrics_keep_unpublishable(metrics):
        blockers.append("render_quality.metrics_publish_gate")
    return list(dict.fromkeys(blockers))


class LegacyRenderQuarantineRequest(BaseModel):
    cutoff: str = Field(default="2026-05-10T00:00:00+08:00")
    dry_run: bool = True
    reviewer: str = Field(default="system-legacy-quarantine", min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=2000)


ALLOWED_QUALITY_VERDICTS = {"approved", "needs_recheck", "rejected"}
ALLOWED_AB_FEEDBACK_VERDICTS = {"up", "down"}
ALLOWED_AB_HARD_DEFECT_TAGS = {
    "candidate_failed_or_blank",
    "halo_or_edge_artifact",
    "identity_drift_or_face_swap",
    "over_smoothing",
    "deformation",
    "mask_outside_delta",
    "candidate_fallback_used",
}
QUALITY_QUEUE_STATUSES = {
    "review_required",
    "all",
    "done",
    "done_with_issues",
    "blocked",
    "failed",
    "reviewed",
    "publishable",
    "not_publishable",
}
QUALITY_RENDER_MODES = {"all", "ai", "best-pair"}

_TERMINAL_RENDER_STATUSES_SQL = "'done', 'done_with_issues', 'blocked', 'failed'"
LEGACY_RENDER_DEFAULT_CUTOFF = "2026-05-10T00:00:00+08:00"


# `archived_at` must match _archive_existing_final_board's filename format
# exactly (`%Y%m%dT%H%M%SZ`). Strict match also blocks path traversal — the
# value is concatenated into a Path; without this guard `archived_at="../foo"`
# would escape the .history directory.
_ARCHIVED_AT_RE = re.compile(r"\d{8}T\d{6}Z")


def _validate_request(brand: str, semantic_judge: str) -> None:
    if brand not in ALLOWED_BRANDS:
        raise HTTPException(400, f"unsupported brand: {brand}")
    if semantic_judge not in ALLOWED_SEMANTIC:
        raise HTTPException(400, f"semantic_judge must be one of {sorted(ALLOWED_SEMANTIC)}")


def _effective_template_from_gate(requested: str | None, gate_result: dict[str, Any]) -> str:
    requested_text = str(requested or "").strip()
    if requested_text in {"", "auto", DEFAULT_TEMPLATE}:
        gate = gate_result.get("gate") if isinstance(gate_result, dict) else {}
        effective = str((gate or {}).get("effective_template") or "").strip()
        if effective:
            return effective
    return requested_text or DEFAULT_TEMPLATE


def _parse_legacy_cutoff(value: str | None) -> tuple[str, str]:
    raw = (value or LEGACY_RENDER_DEFAULT_CUTOFF).strip() or LEGACY_RENDER_DEFAULT_CUTOFF
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(400, "cutoff must be an ISO datetime")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return raw, dt.astimezone(timezone.utc).isoformat()


def _invalid_source_reason(profile: dict[str, Any]) -> str | None:
    source_kind = str(profile.get("source_kind") or "")
    if int(profile.get("missing_source_count") or 0) > 0 or source_kind == "missing_source_files":
        return "missing_source_files"
    if source_kind in {"manual_not_case_source_directory", "generated_output_collection", "empty"}:
        return "no_real_source_photos"
    if source_kind == "insufficient_source_photos":
        return "insufficient_source_photos"
    if source_kind == "missing_before_after_pair":
        return "missing_before_after_pair"
    return None


def _case_image_files(meta: dict[str, Any]) -> list[str]:
    return [str(item) for item in (meta.get("image_files") or []) if item] if isinstance(meta, dict) else []


def _binding_case_ids(meta: dict[str, Any]) -> list[int]:
    if not isinstance(meta, dict):
        return []
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


def _merged_profile(conn: sqlite3.Connection, row: sqlite3.Row, meta: dict[str, Any]) -> dict[str, Any]:
    binding_ids = _binding_case_ids(meta)
    if not binding_ids:
        return source_images.classify_existing_case_source_profile(row["abs_path"], _case_image_files(meta))
    placeholders = ",".join("?" * len(binding_ids))
    bound_rows = conn.execute(
        f"SELECT id, abs_path, meta_json FROM cases WHERE trashed_at IS NULL AND id IN ({placeholders})",
        binding_ids,
    ).fetchall()
    merged_files: list[str] = []
    missing_files: list[str] = []
    raw_meta_image_count = 0
    for source_row in [row, *bound_rows]:
        source_meta = _parse_meta_json(source_row["meta_json"])
        case_name = Path(str(source_row["abs_path"] or "")).name or f"case-{source_row['id']}"
        raw_files = _case_image_files(source_meta)
        raw_meta_image_count += len(raw_files)
        split = source_images.existing_source_image_files(source_row["abs_path"], raw_files)
        for filename in [str(item) for item in split["existing"]]:
            merged_files.append(str(Path(f"case{source_row['id']}-{case_name}") / filename))
        for filename in [str(item) for item in raw_files if not source_images.is_source_image_file(str(item))]:
            merged_files.append(str(Path(f"case{source_row['id']}-{case_name}") / filename))
        for filename in [str(item) for item in split["missing"]]:
            missing_files.append(str(Path(f"case{source_row['id']}-{case_name}") / filename))
    profile = source_images.classify_source_profile(merged_files)
    profile["raw_meta_image_count"] = raw_meta_image_count
    profile["missing_source_count"] = len(missing_files)
    profile["missing_source_samples"] = missing_files[:8]
    profile["file_integrity_status"] = "missing_source_files" if missing_files else "ok"
    if missing_files and not merged_files:
        profile["source_kind"] = "missing_source_files"
    if bound_rows:
        profile["bound_case_ids"] = [int(item["id"]) for item in bound_rows]
    return profile


def _parse_meta_json(raw: str | None) -> dict[str, Any]:
    try:
        meta = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _batch_preview_rows(case_ids: list[int]) -> tuple[list[int], list[dict[str, Any]]]:
    if not case_ids:
        return [], []
    with db.connect() as conn:
        placeholders = ",".join("?" * len(case_ids))
        rows = conn.execute(
            f"""
            SELECT id, abs_path, meta_json, tags_json, manual_blocking_issues_json
            FROM cases
            WHERE trashed_at IS NULL AND id IN ({placeholders})
            """,
            case_ids,
        ).fetchall()
    rows_by_id = {int(row["id"]): row for row in rows}
    valid_ids: list[int] = []
    invalid: list[dict[str, Any]] = []
    for cid in case_ids:
        row = rows_by_id.get(cid)
        if not row:
            invalid.append({"case_id": cid, "reason": "case_not_found"})
            continue
        meta = _parse_meta_json(row["meta_json"])
        with db.connect() as conn:
            profile = _merged_profile(conn, row, meta)
        try:
            tags = json.loads(row["tags_json"] or "[]")
        except (TypeError, ValueError):
            tags = []
        try:
            manual_issues = json.loads(row["manual_blocking_issues_json"] or "[]")
        except (TypeError, ValueError):
            manual_issues = []
        if source_images.case_marked_not_source(tags, manual_issues):
            profile = {
                **profile,
                "source_kind": "manual_not_case_source_directory",
                "manual_not_source": True,
            }
        reason = _invalid_source_reason(profile)
        if reason:
            invalid.append({"case_id": cid, "reason": reason, "source_profile": profile})
        else:
            valid_ids.append(cid)
    return valid_ids, invalid


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    meta_raw = row["meta_json"]
    meta = {}
    if meta_raw:
        try:
            meta = json.loads(meta_raw)
        except (TypeError, ValueError):
            meta = {}
    job = {
        "id": row["id"],
        "case_id": row["case_id"],
        "brand": row["brand"],
        "template": row["template"],
        "status": row["status"],
        "batch_id": row["batch_id"],
        "enqueued_at": row["enqueued_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "output_path": row["output_path"],
        "manifest_path": row["manifest_path"],
        "error_message": row["error_message"],
        "semantic_judge": row["semantic_judge"],
        "render_mode": row["render_mode"] if "render_mode" in row.keys() else "ai",
        "draft_preview": bool(row["draft_preview"]) if "draft_preview" in row.keys() else False,
        "best_pair_selection_id": row["best_pair_selection_id"] if "best_pair_selection_id" in row.keys() else None,
        "candidates_fingerprint_snapshot": (
            row["candidates_fingerprint_snapshot"] if "candidates_fingerprint_snapshot" in row.keys() else None
        ),
        "meta": meta,
    }
    with db.connect() as qconn:
        qrow = qconn.execute(
            "SELECT * FROM render_quality WHERE render_job_id = ?", (row["id"],)
        ).fetchone()
    job["quality"] = render_quality.quality_row_to_dict(qrow)
    job["delivery_audit"] = _delivery_audit_from_job_meta(meta, job["quality"])
    return job


def _delivery_audit_from_job_meta(meta: dict[str, Any], quality: dict[str, Any] | None) -> dict[str, Any]:
    audit = meta.get("render_selection_audit") if isinstance(meta.get("render_selection_audit"), dict) else {}
    applied = audit.get("applied_slots") if isinstance(audit.get("applied_slots"), list) else []
    selected_slots: list[str] = []
    for item in applied:
        if not isinstance(item, dict):
            continue
        slot = str(item.get("slot") or "").strip()
        if slot and slot not in selected_slots:
            selected_slots.append(slot)
    dropped = meta.get("render_selection_dropped_slots")
    if not isinstance(dropped, list):
        dropped = audit.get("dropped_slots") if isinstance(audit.get("dropped_slots"), list) else []
    stress_meta = meta.get("_stress") if isinstance(meta.get("_stress"), dict) else {}
    quality = quality if isinstance(quality, dict) else {}
    return {
        "run_id": meta.get("run_id") or stress_meta.get("run_id"),
        "code_version": meta.get("code_version") if isinstance(meta.get("code_version"), dict) else {},
        "source_manifest_hash": meta.get("source_manifest_hash"),
        "selected_slots": selected_slots,
        "dropped_slots": dropped,
        "source_provenance": meta.get("render_selection_source_provenance") if isinstance(meta.get("render_selection_source_provenance"), list) else [],
        "quality_summary": {
            "quality_status": quality.get("quality_status"),
            "quality_score": quality.get("quality_score"),
            "can_publish": bool(quality.get("can_publish")) if "can_publish" in quality else False,
            "actionable_warning_count": ((quality.get("metrics") or {}).get("actionable_warning_count") if isinstance(quality.get("metrics"), dict) else None),
        },
    }


def _read_manifest_blocking(manifest_path: str | None) -> dict[str, list[str]]:
    """Read manifest blocking issues plus display-grade warnings.

    Raw warnings can include stale pose text from pre-selection analysis and
    non-selected candidate noise. Those stay in manifest `warning_audit`; route
    consumers should only render `warning_display_layers.selected_actionable`.
    错误条件全部返回空列表 — manifest 缺失/破损不阻塞 job 详情。"""
    empty = {"blocking_issues": [], "warnings": []}
    if not manifest_path:
        return empty
    try:
        p = Path(manifest_path)
        if not p.is_file():
            return empty
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return empty
        display_layers = data.get("warning_display_layers") if isinstance(data.get("warning_display_layers"), dict) else None
        if display_layers and isinstance(display_layers.get("selected_actionable"), list):
            warnings = [str(x) for x in display_layers.get("selected_actionable") or []]
        else:
            layers = data.get("warning_layers") if isinstance(data.get("warning_layers"), dict) else None
            warnings = (
                [str(x) for x in layers.get("selected_actionable") or []]
                if layers and isinstance(layers.get("selected_actionable"), list)
                else [str(x) for x in (data.get("warnings") or [])]
            )
        return {
            "blocking_issues": [str(x) for x in (data.get("blocking_issues") or [])],
            "warnings": warnings,
        }
    except (OSError, ValueError, TypeError):
        return empty


def _quality_queue_condition(status: str, render_mode: str = "all") -> tuple[str, list[Any]]:
    base = [
        "c.trashed_at IS NULL",
        "j.status IN ('done', 'done_with_issues', 'blocked', 'failed')",
    ]
    params: list[Any] = []
    if render_mode not in QUALITY_RENDER_MODES:
        raise HTTPException(400, f"render_mode must be one of {sorted(QUALITY_RENDER_MODES)}")
    if render_mode != "all":
        base.append("COALESCE(j.render_mode, 'ai') = ?")
        params.append(render_mode)
    if status != "all":
        base.append(_current_latest_render_job_sql("j"))
    if status == "review_required":
        base.append(
            """
            (
              j.status IN ('done_with_issues', 'blocked', 'failed')
              OR rq.quality_status IN ('done_with_issues', 'blocked')
              OR (rq.id IS NOT NULL AND rq.can_publish = 0 AND COALESCE(rq.review_verdict, '') != 'approved')
            )
            """
        )
        base.append("(rq.review_verdict IS NULL OR rq.review_verdict = 'needs_recheck')")
        base.append(
            """
            NOT EXISTS (
              SELECT 1
              FROM render_jobs newer
              JOIN render_quality newer_rq ON newer_rq.render_job_id = newer.id
              WHERE newer.case_id = j.case_id
                AND newer.status IN ('done', 'done_with_issues')
                AND COALESCE(newer_rq.quality_status, newer.status) = 'done'
                AND newer_rq.can_publish = 1
                AND (
                  COALESCE(newer.finished_at, newer.enqueued_at, '') > COALESCE(j.finished_at, j.enqueued_at, '')
                  OR (
                    COALESCE(newer.finished_at, newer.enqueued_at, '') = COALESCE(j.finished_at, j.enqueued_at, '')
                    AND newer.id > j.id
                  )
                )
            )
            """
        )
    elif status in {"done", "done_with_issues", "blocked", "failed"}:
        base.append("j.status = ?")
        params.append(status)
    elif status == "reviewed":
        base.append("rq.review_verdict IS NOT NULL")
    elif status == "publishable":
        base.append("rq.can_publish = 1")
    elif status == "not_publishable":
        base.append("COALESCE(rq.can_publish, 0) = 0")
    elif status != "all":
        raise HTTPException(400, f"status must be one of {sorted(QUALITY_QUEUE_STATUSES)}")
    return " AND ".join(f"({item})" for item in base), params


def _render_job_recency_sql(alias: str) -> str:
    return f"COALESCE({alias}.finished_at, {alias}.enqueued_at, '')"


def _current_latest_render_job_sql(alias: str = "j") -> str:
    recency = _render_job_recency_sql(alias)
    newer_recency = _render_job_recency_sql("newer")
    return f"""
            NOT EXISTS (
              SELECT 1
              FROM render_jobs newer
              WHERE newer.case_id = {alias}.case_id
                AND newer.status IN ({_TERMINAL_RENDER_STATUSES_SQL})
                AND (
                  {newer_recency} > {recency}
                  OR ({newer_recency} = {recency} AND newer.id > {alias}.id)
                )
            )
    """


def _quality_queue_order_sql(status: str) -> str:
    if status == "all":
        return """
              COALESCE(j.finished_at, j.enqueued_at) DESC,
              j.id DESC
        """
    return """
              CASE
                WHEN j.status = 'failed' THEN 0
                WHEN j.status = 'blocked' THEN 1
                WHEN j.status = 'done_with_issues' THEN 2
                ELSE 3
              END,
              COALESCE(j.finished_at, j.enqueued_at) DESC,
              j.id DESC
    """


def _quality_queue_counts(conn: sqlite3.Connection) -> dict[str, int]:
    latest_sql = _current_latest_render_job_sql("j")
    count_rows = conn.execute(
        f"""
        SELECT COALESCE(rq.quality_status, j.status) AS status, COUNT(*) AS n
        FROM render_jobs j
        JOIN cases c ON c.id = j.case_id
        LEFT JOIN render_quality rq ON rq.render_job_id = j.id
        WHERE c.trashed_at IS NULL
          AND j.status IN ({_TERMINAL_RENDER_STATUSES_SQL})
          AND ({latest_sql})
        GROUP BY COALESCE(rq.quality_status, j.status)
        """
    ).fetchall()
    counts: dict[str, int] = {}
    for row in count_rows:
        counts[str(row["status"])] = int(row["n"])
    counts["reviewed"] = int(
        conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM render_quality rq
            JOIN render_jobs j ON j.id = rq.render_job_id
            JOIN cases c ON c.id = j.case_id
            WHERE c.trashed_at IS NULL
              AND j.status IN ({_TERMINAL_RENDER_STATUSES_SQL})
              AND rq.review_verdict IS NOT NULL
              AND ({latest_sql})
            """
        ).fetchone()["n"]
    )
    return counts


def _quality_queue_mode_counts(conn: sqlite3.Connection) -> dict[str, int]:
    latest_sql = _current_latest_render_job_sql("j")
    rows = conn.execute(
        f"""
        SELECT COALESCE(j.render_mode, 'ai') AS render_mode, COUNT(*) AS n
        FROM render_jobs j
        JOIN cases c ON c.id = j.case_id
        WHERE c.trashed_at IS NULL
          AND j.status IN ({_TERMINAL_RENDER_STATUSES_SQL})
          AND ({latest_sql})
        GROUP BY COALESCE(j.render_mode, 'ai')
        """
    ).fetchall()
    counts = {"ai": 0, "best-pair": 0}
    total = 0
    for row in rows:
        mode = str(row["render_mode"] or "ai")
        n = int(row["n"])
        counts[mode] = counts.get(mode, 0) + n
        total += n
    counts["all"] = total
    return counts


def _quality_queue_archive_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    latest_sql = _current_latest_render_job_sql("j")
    rows = conn.execute(
        f"""
        SELECT j.status, COALESCE(rq.quality_status, j.status) AS quality_status, COUNT(*) AS n
        FROM render_jobs j
        JOIN cases c ON c.id = j.case_id
        LEFT JOIN render_quality rq ON rq.render_job_id = j.id
        WHERE c.trashed_at IS NULL
          AND j.status IN ({_TERMINAL_RENDER_STATUSES_SQL})
          AND NOT ({latest_sql})
        GROUP BY j.status, COALESCE(rq.quality_status, j.status)
        """
    ).fetchall()
    by_status: dict[str, int] = {}
    by_quality_status: dict[str, int] = {}
    hidden = 0
    for row in rows:
        n = int(row["n"])
        hidden += n
        by_status[str(row["status"])] = by_status.get(str(row["status"]), 0) + n
        by_quality_status[str(row["quality_status"])] = by_quality_status.get(str(row["quality_status"]), 0) + n
    return {
        "scope": "historical_non_current_render_jobs",
        "hidden_by_current_latest": hidden,
        "by_status": by_status,
        "by_quality_status": by_quality_status,
    }


def _legacy_render_risk_rows(conn: sqlite3.Connection, cutoff_utc: str, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT j.*
        FROM render_jobs j
        JOIN cases c ON c.id = j.case_id
        JOIN render_quality rq ON rq.render_job_id = j.id
        WHERE c.trashed_at IS NULL
          AND j.status IN ({_TERMINAL_RENDER_STATUSES_SQL})
          AND j.enqueued_at < ?
          AND rq.can_publish = 1
        ORDER BY COALESCE(j.finished_at, j.enqueued_at) DESC, j.id DESC
        LIMIT ?
        """,
        (cutoff_utc, limit),
    ).fetchall()


def _legacy_render_risk_summary(conn: sqlite3.Connection, cutoff_utc: str, cutoff_label: str, limit: int) -> dict[str, Any]:
    publishable_count = int(
        conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM render_jobs j
            JOIN cases c ON c.id = j.case_id
            JOIN render_quality rq ON rq.render_job_id = j.id
            WHERE c.trashed_at IS NULL
              AND j.status IN ({_TERMINAL_RENDER_STATUSES_SQL})
              AND j.enqueued_at < ?
              AND rq.can_publish = 1
            """,
            (cutoff_utc,),
        ).fetchone()["n"]
    )
    quarantined_count = int(
        conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM render_jobs j
            JOIN cases c ON c.id = j.case_id
            JOIN render_quality rq ON rq.render_job_id = j.id
            WHERE c.trashed_at IS NULL
              AND j.status IN ({_TERMINAL_RENDER_STATUSES_SQL})
              AND j.enqueued_at < ?
              AND rq.can_publish = 0
              AND rq.review_verdict = 'needs_recheck'
              AND rq.metrics_json LIKE '%"legacy_quarantine"%'
            """,
            (cutoff_utc,),
        ).fetchone()["n"]
    )
    rows = _legacy_render_risk_rows(conn, cutoff_utc, limit)
    return {
        "cutoff": cutoff_label,
        "cutoff_utc": cutoff_utc,
        "publishable_count": publishable_count,
        "quarantined_count": quarantined_count,
        "total": len(rows),
        "items": [{"risk_status": "legacy_publishable", "job": _row_to_job(row)} for row in rows],
    }


def _quality_issue_summary(job: dict[str, Any]) -> tuple[list[str], list[str]]:
    quality = job.get("quality") or {}
    metrics = quality.get("metrics") if isinstance(quality, dict) else {}
    if not isinstance(metrics, dict):
        metrics = {}

    issues: list[str] = []
    warnings: list[str] = []
    for item in metrics.get("blocking_issues") or []:
        text = str(item).strip()
        if text:
            issues.append(text)
    if "display_warnings" in metrics:
        raw_warning_items = metrics.get("display_warnings") or []
    elif "warnings" in metrics:
        raw_warning_items = metrics.get("warnings") or []
    else:
        layers = metrics.get("warning_layers") if isinstance(metrics.get("warning_layers"), dict) else {}
        raw_warning_items = layers.get("selected_actionable") if isinstance(layers.get("selected_actionable"), list) else []
    for item in raw_warning_items:
        text = str(item).strip()
        if text:
            warnings.append(text)
    for alert in metrics.get("composition_alerts") or []:
        if not isinstance(alert, dict):
            continue
        message = str(alert.get("message") or "").strip()
        if not message:
            continue
        severity = str(alert.get("severity") or "warning")
        if severity == "block":
            issues.append(message)
        else:
            warnings.append(message)
    if job.get("error_message"):
        issues.append(str(job["error_message"]).strip())
    return issues[:6], warnings[:8]


def _quality_action_summary(job: dict[str, Any]) -> list[dict[str, Any]]:
    quality = job.get("quality") or {}
    metrics = quality.get("metrics") if isinstance(quality, dict) else {}
    if not isinstance(metrics, dict):
        return []
    actions = metrics.get("action_suggestions")
    if not isinstance(actions, list):
        return []
    out: list[dict[str, Any]] = []
    for item in actions:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        label = str(item.get("label") or "").strip()
        if code and label:
            normalized = {"code": code, "label": label, "source": str(item.get("source") or "")}
            for key in ("href", "view", "publish_gate"):
                value = item.get(key)
                if value:
                    normalized[key] = value
            out.append(normalized)
    return out[:8]


def _normalize_ab_feedback_tags(tags: list[str]) -> list[str]:
    normalized: list[str] = []
    invalid: list[str] = []
    for raw in tags:
        tag = str(raw or "").strip()
        if not tag:
            continue
        if tag not in ALLOWED_AB_HARD_DEFECT_TAGS:
            invalid.append(tag)
            continue
        if tag not in normalized:
            normalized.append(tag)
    if invalid:
        raise HTTPException(400, f"hard_defect_tags contains unsupported values: {invalid}")
    return normalized


def _feedback_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        hard_defect_tags = json.loads(row["hard_defect_tags_json"] or "[]")
    except (TypeError, ValueError):
        hard_defect_tags = []
    return {
        "id": int(row["id"]),
        "render_job_id": int(row["render_job_id"]),
        "case_id": int(row["case_id"]) if row["case_id"] is not None else None,
        "baseline_job_id": int(row["baseline_job_id"]) if row["baseline_job_id"] is not None else None,
        "candidate_job_id": int(row["candidate_job_id"]) if row["candidate_job_id"] is not None else None,
        "simulation_job_id": int(row["simulation_job_id"]) if row["simulation_job_id"] is not None else None,
        "workflow_profile": row["workflow_profile"],
        "verdict": row["verdict"],
        "hard_defect_tags": hard_defect_tags if isinstance(hard_defect_tags, list) else [],
        "reviewer": row["reviewer"],
        "note": row["note"],
        "source": row["source"],
        "created_at": row["created_at"],
    }


# ----------------------------------------------------------------------
# Single-case enqueue
# ----------------------------------------------------------------------


@router.post("/api/cases/{case_id}/pre-render-gate")
def run_pre_render_gate(case_id: int, payload: PreRenderGateRequest) -> dict[str, Any]:
    _validate_request(DEFAULT_BRAND, payload.semantic_judge)
    try:
        with db.connect() as conn:
            return pre_render_gate.evaluate_pre_render_gate(
                case_id,
                template=payload.template or DEFAULT_TEMPLATE,
                semantic_judge=payload.semantic_judge or DEFAULT_SEMANTIC,
                persist_tickets=payload.persist_tickets,
                conn=conn,
            )
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post("/api/cases/{case_id}/render")
def enqueue_single(case_id: int, payload: RenderRequest) -> dict:
    _validate_request(payload.brand, payload.semantic_judge)
    try:
        with db.connect() as conn:
            gate_result = pre_render_gate.evaluate_pre_render_gate(
                case_id,
                template=payload.template or DEFAULT_TEMPLATE,
                semantic_judge=payload.semantic_judge or DEFAULT_SEMANTIC,
                persist_tickets=True,
                conn=conn,
            )
    except ValueError as e:
        raise HTTPException(404, str(e))
    if not gate_result["gate"]["passed"] and not payload.force:
        raise HTTPException(
            409,
            {
                "reason": "pre_render_gate_blocked",
                "gate": gate_result["gate"],
                "tickets": gate_result["tickets"],
            },
        )
    effective_template = _effective_template_from_gate(payload.template, gate_result)
    try:
        job_id = RENDER_QUEUE.enqueue(
            case_id=case_id,
            brand=payload.brand,
            template=effective_template,
            semantic_judge=payload.semantic_judge or DEFAULT_SEMANTIC,
            draft_preview=payload.draft_preview,
            model=payload.model,
            system_prompt=payload.system_prompt,
            options=payload.options,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"job_id": job_id, "batch_id": None}


@router.post("/api/cases/render/batch")
def enqueue_batch(payload: RenderBatchRequest) -> dict:
    if not payload.case_ids:
        raise HTTPException(400, "case_ids cannot be empty")
    if len(payload.case_ids) > MAX_BATCH_SIZE:
        raise HTTPException(
            400,
            f"batch size {len(payload.case_ids)} exceeds maximum {MAX_BATCH_SIZE}; split into smaller batches",
        )
    _validate_request(payload.brand, payload.semantic_judge)
    seen: set[int] = set()
    deduped_ids: list[int] = []
    duplicate_count = 0
    for cid in payload.case_ids:
        if cid in seen:
            duplicate_count += 1
            continue
        seen.add(cid)
        deduped_ids.append(cid)
    valid_case_ids, invalid = _batch_preview_rows(deduped_ids)
    if not valid_case_ids:
        raise HTTPException(404, "no valid case ids in batch")
    gate_valid_jobs: list[tuple[int, str]] = []
    for cid in valid_case_ids:
        try:
            with db.connect() as conn:
                gate_result = pre_render_gate.evaluate_pre_render_gate(
                    cid,
                    template=payload.template or DEFAULT_TEMPLATE,
                    semantic_judge=payload.semantic_judge or DEFAULT_SEMANTIC,
                    persist_tickets=True,
                    conn=conn,
                )
        except ValueError:
            invalid.append({"case_id": cid, "reason": "case_not_found"})
            continue
        if gate_result["gate"]["passed"] or payload.force:
            gate_valid_jobs.append((cid, _effective_template_from_gate(payload.template, gate_result)))
        else:
            invalid.append(
                {
                    "case_id": cid,
                    "reason": "pre_render_gate_blocked",
                    "gate": gate_result["gate"],
                    "tickets": gate_result["tickets"],
                }
            )
    valid_case_ids = [case_id for case_id, _template in gate_valid_jobs]
    if not valid_case_ids:
        raise HTTPException(409, {"reason": "pre_render_gate_blocked", "invalid": invalid})
    templates = {template for _case_id, template in gate_valid_jobs}
    if len(templates) <= 1:
        batch_id, job_ids = RENDER_QUEUE.enqueue_batch(
            case_ids=valid_case_ids,
            brand=payload.brand,
            template=next(iter(templates)) if templates else (payload.template or DEFAULT_TEMPLATE),
            semantic_judge=payload.semantic_judge or DEFAULT_SEMANTIC,
            draft_preview=payload.draft_preview,
            model=payload.model,
            system_prompt=payload.system_prompt,
            options=payload.options,
        )
    else:
        batch_id = f"batch-{uuid.uuid4().hex[:12]}"
        job_ids = []
        for cid, template in gate_valid_jobs:
            try:
                job_ids.append(
                    RENDER_QUEUE.enqueue(
                        case_id=cid,
                        brand=payload.brand,
                        template=template,
                        semantic_judge=payload.semantic_judge or DEFAULT_SEMANTIC,
                        batch_id=batch_id,
                        draft_preview=payload.draft_preview,
                        model=payload.model,
                        system_prompt=payload.system_prompt,
                        options=payload.options,
                    )
                )
            except ValueError:
                invalid.append({"case_id": cid, "reason": "case_not_found"})
    if not job_ids:
        raise HTTPException(404, "no valid case ids in batch")
    return {
        "batch_id": batch_id,
        "job_ids": job_ids,
        "skipped_count": len(payload.case_ids) - len(job_ids),
        "invalid": invalid,
        "duplicate_count": duplicate_count,
    }


@router.post("/api/cases/render/batch/preview")
def preview_batch(payload: RenderBatchRequest) -> dict:
    """Dry-run validate a CSV-imported batch BEFORE enqueueing.

    Returns per-id status so the UI can show "N 条有效 / M 条无效 + 原因" and
    let the user fix the CSV before committing the enqueue.

    Validation checks:
    - case_ids non-empty + within MAX_BATCH_SIZE
    - brand / semantic_judge in allow-list
    - duplicate case_ids in same batch
    - case row exists in DB (not deleted / wrong id)
    """
    if not payload.case_ids:
        raise HTTPException(400, "case_ids cannot be empty")
    if len(payload.case_ids) > MAX_BATCH_SIZE:
        raise HTTPException(
            400,
            f"batch size {len(payload.case_ids)} exceeds maximum {MAX_BATCH_SIZE}; split into smaller batches",
        )
    _validate_request(payload.brand, payload.semantic_judge)

    seen: set[int] = set()
    duplicates: set[int] = set()
    deduped_ids: list[int] = []
    for cid in payload.case_ids:
        if cid in seen:
            duplicates.add(cid)
            continue
        seen.add(cid)
        deduped_ids.append(cid)

    valid_ids, invalid = _batch_preview_rows(deduped_ids)
    for cid in duplicates:
        invalid.append({"case_id": cid, "reason": "duplicate_in_batch"})

    return {
        "valid_count": len(valid_ids),
        "invalid_count": len(invalid),
        "valid_case_ids": valid_ids,
        "invalid": invalid,
        "brand": payload.brand,
        "template": payload.template or DEFAULT_TEMPLATE,
        "semantic_judge": payload.semantic_judge or DEFAULT_SEMANTIC,
    }


# ----------------------------------------------------------------------
# Per-case queries
# ----------------------------------------------------------------------


@router.get("/api/cases/{case_id}/render/jobs")
def list_case_jobs(case_id: int, limit: int = Query(20, le=200)) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM render_jobs
            WHERE case_id = ?
            ORDER BY enqueued_at DESC, id DESC
            LIMIT ?
            """,
            (case_id, limit),
        ).fetchall()
    return [_row_to_job(r) for r in rows]


@router.get("/api/cases/{case_id}/render/latest")
def latest_case_job(case_id: int) -> dict:
    """Current in-flight job, otherwise the latest job that can show an output.

    A failed retry must not blank the case detail preview when an older
    done/done_with_issues final-board still exists. The failed row remains in
    `/render/jobs`; this endpoint is optimized for the status card's visible
    artifact. If there is no visible output yet, fall back to the newest row so
    first-run failures are still explained.

    For jobs with an output, stat final-board.jpg and attach `output_mtime`
    (Unix seconds) so the frontend can cache-bust the <img src>.
    """
    with db.connect() as conn:
        latest_row = conn.execute(
            """
            SELECT * FROM render_jobs
            WHERE case_id = ?
            ORDER BY enqueued_at DESC, id DESC
            LIMIT 1
            """,
            (case_id,),
        ).fetchone()
        case_row = conn.execute(
            "SELECT abs_path FROM cases WHERE id = ? AND trashed_at IS NULL", (case_id,)
        ).fetchone()
        output_row = conn.execute(
            """
            SELECT * FROM render_jobs
            WHERE case_id = ?
              AND status IN ('done', 'done_with_issues')
            ORDER BY enqueued_at DESC, id DESC
            LIMIT 1
            """,
            (case_id,),
        ).fetchone()
    if not latest_row:
        return {"job": None}

    latest = _row_to_job(latest_row)
    if latest["status"] in {"queued", "running"}:
        job = latest
    elif output_row is not None:
        job = _row_to_job(output_row)
    else:
        job = latest

    if job.get("output_path") or (case_row and job["status"] in {"done", "done_with_issues"}):
        out_path = Path(job["output_path"]) if job.get("output_path") else (
            Path(case_row["abs_path"])
            / ".case-layout-output"
            / job["brand"]
            / job["template"]
            / "render"
            / "final-board.jpg"
        )
        try:
            job["output_mtime"] = out_path.stat().st_mtime
        except OSError:
            job["output_mtime"] = None
    # Stage A: 透传 manifest.final.json 的 blocking/warnings 列表
    detail = _read_manifest_blocking(job["manifest_path"])
    job["blocking_issues"] = detail["blocking_issues"]
    job["warnings"] = detail["warnings"]
    return {"job": job}


@router.get("/api/cases/{case_id}/render/history")
def list_render_history(case_id: int, brand: str = Query(DEFAULT_BRAND), template: str = Query(DEFAULT_TEMPLATE)) -> dict:
    """List archived final-board.jpg snapshots for a case (most recent first).

    Each render run archives the previous final-board.jpg into `.history/<ts>.jpg`
    before overwriting (see render_executor._archive_existing_final_board). This
    endpoint exposes those for visual comparison in the UI.

    Empty list if the case has never been rendered or .history/ doesn't exist.
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT abs_path FROM cases WHERE id = ? AND trashed_at IS NULL", (case_id,)
        ).fetchone()
        latest = conn.execute(
            """
            SELECT render_mode
            FROM render_jobs
            WHERE case_id = ?
              AND brand = ?
              AND template = ?
              AND status IN ('done', 'done_with_issues', 'blocked', 'failed')
            ORDER BY COALESCE(finished_at, enqueued_at, '') DESC, id DESC
            LIMIT 1
            """,
            (case_id, brand, template),
        ).fetchone()
    if not row:
        raise HTTPException(404, "case not found")
    latest_render_mode = (latest["render_mode"] if latest and latest["render_mode"] else "ai") if latest else None
    case_dir = Path(row["abs_path"])
    history_dir = case_dir / ".case-layout-output" / brand / template / "render" / ".history"
    if not history_dir.is_dir():
        return {
            "case_id": case_id,
            "brand": brand,
            "template": template,
            "latest_render_mode": latest_render_mode,
            "snapshots": [],
        }
    snapshots: list[dict[str, Any]] = []
    for p in sorted(history_dir.iterdir(), reverse=True):
        if not (p.is_file() and p.suffix == ".jpg"):
            continue
        snapshots.append({
            "filename": p.name,
            "archived_at": p.stem,  # ISO-ish timestamp from filename
            "size_bytes": p.stat().st_size,
        })
    return {
        "case_id": case_id,
        "brand": brand,
        "template": template,
        "latest_render_mode": latest_render_mode,
        "snapshots": snapshots,
    }


@router.post("/api/cases/{case_id}/render/restore")
def restore_render(case_id: int, payload: RenderRestoreRequest) -> dict:
    """Restore a previously archived `final-board.jpg` snapshot.

    Auto-archives the current final-board.jpg first (so the operation is
    reversible — re-restore the just-archived ts), then copies the requested
    snapshot over `final-board.jpg`. Records an audit revision with
    op="restore_render" so RevisionsDrawer can show the trail.

    Errors:
      400  unsupported brand / invalid archived_at format
      404  case not found / snapshot not found
      500  file-system IO error during copy
    """
    stress.assert_destructive_allowed("render restore")
    if payload.brand not in ALLOWED_BRANDS:
        raise HTTPException(400, f"unsupported brand: {payload.brand}")
    if not _ARCHIVED_AT_RE.fullmatch(payload.archived_at):
        raise HTTPException(400, "invalid archived_at format")
    template = payload.template or DEFAULT_TEMPLATE

    with db.connect() as conn:
        row = conn.execute(
            "SELECT abs_path FROM cases WHERE id = ? AND trashed_at IS NULL", (case_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "case not found")

    out_root = Path(row["abs_path"]) / ".case-layout-output" / payload.brand / template / "render"
    snapshot_path = out_root / ".history" / f"{payload.archived_at}.jpg"
    if not snapshot_path.is_file():
        raise HTTPException(404, "snapshot not found")

    try:
        result = render_executor.restore_archived_final_board(out_root, payload.archived_at)
    except FileNotFoundError as e:
        # Race: snapshot existed at our check above but is gone now.
        raise HTTPException(404, str(e))
    except OSError as e:
        raise HTTPException(500, f"restore IO error: {e}")

    with db.connect() as conn:
        revision_id = audit.record_revision(
            conn,
            case_id,
            op="restore_render",
            before={
                "render_output_path": str(out_root / "final-board.jpg"),
                "previous_archived_at": result["previous_archived_at"],
            },
            after={
                "render_output_path": result["output_path"],
                "restored_from": payload.archived_at,
                "brand": payload.brand,
                "template": template,
            },
            source_route=f"/api/cases/{case_id}/render/restore",
            actor="user",
        )

    return {
        "case_id": case_id,
        "brand": payload.brand,
        "template": template,
        "restored_from": payload.archived_at,
        "previous_archived_at": result["previous_archived_at"],
        "revision_id": revision_id,
        "output_path": result["output_path"],
    }


@router.post("/api/cases/{case_id}/render/undo")
def undo_render(case_id: int) -> dict:
    stress.assert_destructive_allowed("render undo")
    try:
        result = RENDER_QUEUE.undo_render(
            case_id, source_route=f"/api/cases/{case_id}/render/undo"
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    return result


# ----------------------------------------------------------------------
# Job / batch detail
# ----------------------------------------------------------------------


@router.get("/api/render/jobs/{job_id}/file")
def render_job_file(job_id: int, kind: str = Query("output")) -> FileResponse:
    if kind not in {"output", "final-board", "manifest"}:
        raise HTTPException(400, "kind must be output, final-board, or manifest")
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT j.output_path, j.manifest_path, c.abs_path AS case_abs_path
            FROM render_jobs j
            LEFT JOIN cases c ON c.id = j.case_id
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "render job not found")
    raw = row["manifest_path"] if kind == "manifest" else row["output_path"]
    if not raw:
        raise HTTPException(404, "render artifact path is empty")
    target = Path(raw).expanduser().resolve()
    case_dir = Path(row["case_abs_path"]).expanduser().resolve() if row["case_abs_path"] else None
    if not stress.is_path_allowed_artifact(target, case_dir=case_dir):
        raise HTTPException(403, "render artifact path is outside allowed roots")
    if not target.is_file():
        raise HTTPException(404, "render artifact not found")
    return FileResponse(target)


@router.get("/api/render/jobs/{job_id}")
def get_job(job_id: int) -> dict:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM render_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "job not found")
    job = _row_to_job(row)
    # Stage A: 透传 manifest.final.json 的逐条 blocking/warning 字符串
    detail = _read_manifest_blocking(job["manifest_path"])
    job["blocking_issues"] = detail["blocking_issues"]
    job["warnings"] = detail["warnings"]
    return job


@router.get("/api/render/legacy-risk")
def legacy_render_risk(
    cutoff: str = Query(default=LEGACY_RENDER_DEFAULT_CUTOFF),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    cutoff_label, cutoff_utc = _parse_legacy_cutoff(cutoff)
    with db.connect() as conn:
        return _legacy_render_risk_summary(conn, cutoff_utc, cutoff_label, limit)


@router.post("/api/render/legacy-risk/quarantine")
def quarantine_legacy_render_risk(payload: LegacyRenderQuarantineRequest) -> dict[str, Any]:
    cutoff_label, cutoff_utc = _parse_legacy_cutoff(payload.cutoff)
    reviewer = payload.reviewer.strip() or "system-legacy-quarantine"
    note = payload.note.strip() if payload.note else "2026-05-10 前旧正式出图质量不可信，隔离后重出"
    now = datetime_now_iso()
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
              j.id AS job_id,
              rq.metrics_json
            FROM render_jobs j
            JOIN cases c ON c.id = j.case_id
            JOIN render_quality rq ON rq.render_job_id = j.id
            WHERE c.trashed_at IS NULL
              AND j.status IN ({_TERMINAL_RENDER_STATUSES_SQL})
              AND j.enqueued_at < ?
              AND rq.can_publish = 1
            ORDER BY COALESCE(j.finished_at, j.enqueued_at) DESC, j.id DESC
            """,
            (cutoff_utc,),
        ).fetchall()
        affected_ids = [int(row["job_id"]) for row in rows]
        if not payload.dry_run:
            for row in rows:
                try:
                    metrics = json.loads(row["metrics_json"] or "{}")
                except (TypeError, ValueError):
                    metrics = {}
                if not isinstance(metrics, dict):
                    metrics = {}
                history = metrics.get("legacy_quarantine_history")
                if not isinstance(history, list):
                    history = []
                event = {
                    "cutoff": cutoff_label,
                    "cutoff_utc": cutoff_utc,
                    "reviewer": reviewer,
                    "note": note,
                    "applied_at": now,
                    "reason": "legacy_render_before_2026_05_10",
                }
                metrics["legacy_quarantine"] = event
                metrics["legacy_quarantine_history"] = [*history[-9:], event]
                conn.execute(
                    """
                    UPDATE render_quality
                    SET can_publish = 0,
                        review_verdict = 'needs_recheck',
                        reviewer = ?,
                        review_note = ?,
                        reviewed_at = ?,
                        updated_at = ?,
                        metrics_json = ?
                    WHERE render_job_id = ?
                    """,
                    (
                        reviewer,
                        note,
                        now,
                        now,
                        json.dumps(metrics, ensure_ascii=False),
                        int(row["job_id"]),
                    ),
                )
        summary = _legacy_render_risk_summary(conn, cutoff_utc, cutoff_label, 200)
    return {
        "cutoff": cutoff_label,
        "cutoff_utc": cutoff_utc,
        "dry_run": payload.dry_run,
        "affected_count": len(affected_ids),
        "affected_job_ids": affected_ids,
        "summary": summary,
    }


@router.get("/api/render/quality-queue")
def list_render_quality_queue(
    status: str = Query("review_required"),
    render_mode: str = Query("all"),
    limit: int = Query(100, ge=1, le=200),
) -> dict[str, Any]:
    """Central queue for final-render QA.

    Cases/listing pages answer "which case is this"; this endpoint answers
    "which render artifacts need a human quality decision" using real
    render_jobs/render_quality rows only.
    """
    status = status.strip() or "review_required"
    render_mode = render_mode.strip() or "all"
    where_sql, params = _quality_queue_condition(status, render_mode)
    order_sql = _quality_queue_order_sql(status)
    with db.connect() as conn:
        total = conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM render_jobs j
            JOIN cases c ON c.id = j.case_id
            LEFT JOIN render_quality rq ON rq.render_job_id = j.id
            WHERE {where_sql}
            """,
            params,
        ).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT
              j.*,
              c.abs_path AS case_abs_path,
              c.customer_raw AS case_customer_raw,
              cu.canonical_name AS case_customer_canonical
            FROM render_jobs j
            JOIN cases c ON c.id = j.case_id
            LEFT JOIN customers cu ON cu.id = c.customer_id
            LEFT JOIN render_quality rq ON rq.render_job_id = j.id
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()

        counts = _quality_queue_counts(conn)
        mode_counts = _quality_queue_mode_counts(conn)
        archive = _quality_queue_archive_summary(conn)

    items: list[dict[str, Any]] = []
    for row in rows:
        job = _row_to_job(row)
        detail = _read_manifest_blocking(job["manifest_path"])
        job["blocking_issues"] = detail["blocking_issues"]
        job["warnings"] = detail["warnings"]
        issues, warnings = _quality_issue_summary(job)
        actions = _quality_action_summary(job)
        items.append(
            {
                "job": job,
                "case": {
                    "id": row["case_id"],
                    "abs_path": row["case_abs_path"],
                    "customer_raw": row["case_customer_raw"],
                    "customer_canonical": row["case_customer_canonical"],
                },
                "reviewable": job["status"] in {"done", "done_with_issues", "blocked"},
                "issue_summary": issues,
                "warning_summary": warnings,
                "action_summary": actions,
            }
        )
    return {
        "items": items,
        "total": total,
        "counts": counts,
        "mode_counts": mode_counts,
        "archive": archive,
        "status": status,
        "render_mode": render_mode,
        "limit": limit,
    }


@router.post("/api/render-jobs/{job_id}/quality-review")
def review_render_quality(job_id: int, payload: RenderQualityReviewRequest) -> dict[str, Any]:
    verdict = payload.verdict.strip()
    reviewer = payload.reviewer.strip()
    if verdict not in ALLOWED_QUALITY_VERDICTS:
        raise HTTPException(400, f"verdict must be one of {sorted(ALLOWED_QUALITY_VERDICTS)}")
    if not reviewer:
        raise HTTPException(400, "reviewer cannot be blank")
    now = datetime_now_iso()
    with db.connect() as conn:
        job = conn.execute("SELECT id, status, draft_preview FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404, "job not found")
        if job["status"] not in {"done", "done_with_issues", "blocked"}:
            raise HTTPException(400, f"render job is not reviewable: {job['status']}")
        qrow = conn.execute(
            "SELECT * FROM render_quality WHERE render_job_id = ?", (job_id,)
        ).fetchone()
        if not qrow:
            conn.execute(
                """
                INSERT INTO render_quality
                  (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
                   manifest_status, blocking_count, warning_count, metrics_json, created_at, updated_at)
                VALUES (?, ?, 0, 0, 'real_layout', NULL, 0, 0, '{}', ?, ?)
                """,
                (job_id, job["status"], now, now),
            )
            qrow = conn.execute(
                "SELECT * FROM render_quality WHERE render_job_id = ?", (job_id,)
            ).fetchone()
        try:
            metrics = json.loads(qrow["metrics_json"] or "{}") if qrow else {}
        except (TypeError, ValueError):
            metrics = {}
        if not isinstance(metrics, dict):
            metrics = {}
        metrics = stress.tag_payload(metrics)
        hard_blockers = _quality_review_hard_blockers(qrow, metrics)
        can_publish = payload.can_publish
        if can_publish is None:
            can_publish = verdict == "approved" and job["status"] == "done"
        if can_publish and hard_blockers:
            can_publish = False
        if can_publish and bool(job["draft_preview"] if "draft_preview" in job.keys() else False):
            can_publish = False
            metrics["draft_preview"] = True
        conn.execute(
            """
            UPDATE render_quality
            SET review_verdict = ?,
                reviewer = ?,
                review_note = ?,
                can_publish = ?,
                metrics_json = ?,
                reviewed_at = ?,
                updated_at = ?
            WHERE render_job_id = ?
            """,
            (
                verdict,
                reviewer,
                payload.note.strip() if payload.note else None,
                1 if can_publish else 0,
                json.dumps(metrics, ensure_ascii=False),
                now,
                now,
                job_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM render_quality WHERE render_job_id = ?", (job_id,)
        ).fetchone()
    return render_quality.quality_row_to_dict(row) or {}


@router.post("/api/render-jobs/{job_id}/ab-feedback")
def record_ab_feedback(job_id: int, payload: AbFeedbackRequest) -> dict[str, Any]:
    verdict = payload.verdict.strip().lower()
    reviewer = payload.reviewer.strip()
    if verdict not in ALLOWED_AB_FEEDBACK_VERDICTS:
        raise HTTPException(400, f"verdict must be one of {sorted(ALLOWED_AB_FEEDBACK_VERDICTS)}")
    if not reviewer:
        raise HTTPException(400, "reviewer cannot be blank")
    hard_defect_tags = _normalize_ab_feedback_tags(payload.hard_defect_tags)
    now = datetime_now_iso()
    with db.connect() as conn:
        job = conn.execute("SELECT id, case_id FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404, "job not found")
        feedback_id = conn.execute(
            """
            INSERT INTO ab_feedback
              (render_job_id, case_id, baseline_job_id, candidate_job_id, simulation_job_id,
               workflow_profile, verdict, hard_defect_tags_json, reviewer, note, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                int(job["case_id"]) if job["case_id"] is not None else None,
                payload.baseline_job_id,
                payload.candidate_job_id,
                payload.simulation_job_id,
                (payload.workflow_profile or "").strip() or None,
                verdict,
                json.dumps(hard_defect_tags, ensure_ascii=False),
                reviewer,
                payload.note,
                (payload.source or "gray_rollout").strip() or "gray_rollout",
                now,
            ),
        ).lastrowid
        feedback = conn.execute("SELECT * FROM ab_feedback WHERE id = ?", (feedback_id,)).fetchone()
    return {
        "feedback": _feedback_row_to_dict(feedback),
        "policy": {
            "can_unlock_publish": False,
            "publish_gate": "feedback_only_human_signal; render_quality and DeliveryGate remain authoritative",
        },
    }


@router.get("/api/render/batches/{batch_id}")
def get_batch(batch_id: str) -> dict:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM render_jobs WHERE batch_id = ? ORDER BY id ASC",
            (batch_id,),
        ).fetchall()
    if not rows:
        raise HTTPException(404, "batch not found")
    jobs = [_row_to_job(r) for r in rows]
    counts: dict[str, int] = {}
    for j in jobs:
        counts[j["status"]] = counts.get(j["status"], 0) + 1
    return {
        "batch_id": batch_id,
        "total": len(jobs),
        "counts": counts,
        "jobs": jobs,
    }


@router.post("/api/render/jobs/{job_id}/cancel")
def cancel_job(job_id: int) -> dict:
    ok = RENDER_QUEUE.cancel(job_id)
    if not ok:
        raise HTTPException(409, "job not cancellable (already running or finished)")
    return {"cancelled": True, "job_id": job_id}


# ----------------------------------------------------------------------
# SSE stream
# ----------------------------------------------------------------------


@router.get("/api/render/stream")
async def render_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events stream of render job updates.

    Each event line is JSON:
        {"type": "job_update", "job_id": ..., "case_id": ..., "status": "running" | "done" | ..., ...}
    """

    async def event_source():
        # Initial comment to flush headers immediately.
        yield ":ok\n\n"
        async for event in RENDER_QUEUE.subscribe():
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# P0.1 Failure observability — simulation_jobs.audit_json.failure 聚合/单 job 查询
# ---------------------------------------------------------------------------

_FAILURE_GROUP_BY_VALID = {"stage", "error_class", "workflow"}
_FAILURE_GROUP_KEY_MAP = {
    "stage": "failure_stage",
    "error_class": "error_class",
    "workflow": "workflow_name",
}


@router.get("/api/render/jobs/failures/recent")
def list_recent_failures(
    days: int = Query(default=7, ge=1, le=90),
    group_by: str = Query(default="stage"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    """聚合最近 N 天 failed simulation_jobs，按 stage/error_class/workflow 分组。

    返回 `groups: [{key, count}]` + `total_failed`，oncall 可直接 SQL 归因失败模式。
    """
    if group_by not in _FAILURE_GROUP_BY_VALID:
        raise HTTPException(
            400,
            f"group_by must be one of {sorted(_FAILURE_GROUP_BY_VALID)}",
        )
    key_field = _FAILURE_GROUP_KEY_MAP[group_by]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, audit_json, created_at
            FROM simulation_jobs
            WHERE status = 'failed'
              AND created_at >= ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    counts: dict[str, int] = {}
    total = 0
    for row in rows:
        try:
            audit = json.loads(row["audit_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            audit = {}
        failure = audit.get("failure") if isinstance(audit, dict) else None
        if not isinstance(failure, dict):
            key = "unknown"
        else:
            key = str(failure.get(key_field) or "unknown")
        counts[key] = counts.get(key, 0) + 1
        total += 1
    groups = [{"key": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    return {
        "days": days,
        "group_by": group_by,
        "total_failed": total,
        "groups": groups,
    }


@router.get("/api/render/jobs/{job_id}/failure-trace")
def get_failure_trace(job_id: int) -> dict[str, Any]:
    """返回单 simulation_job 的结构化 failure 块 + 状态 + legacy error_message。

    成功 job 也能调用（failure 为 null），便于 UI 统一查询。
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, status, audit_json, error_message, created_at, updated_at "
            "FROM simulation_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "simulation job not found")
    try:
        audit = json.loads(row["audit_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        audit = {}
    failure = audit.get("failure") if isinstance(audit, dict) else None
    return {
        "simulation_job_id": int(row["id"]),
        "status": row["status"],
        "error_message": row["error_message"],
        "failure": failure if isinstance(failure, dict) else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------------------
# P0.4 ops status — VLM/ComfyUI/Gate 聚合面板（一个 endpoint 看全局）
# ---------------------------------------------------------------------------


def _ops_top_k(counter: dict[str, int], k: int = 10) -> list[dict[str, Any]]:
    return [
        {"key": key, "count": cnt}
        for key, cnt in sorted(counter.items(), key=lambda kv: -kv[1])[:k]
    ]


def _ops_vlm_section(conn, cutoff_iso: str) -> dict[str, Any]:
    from ..services import vlm_usage_metrics

    summary = vlm_usage_metrics.summarize_classifier_outputs(conn)
    rows = conn.execute(
        "SELECT status, created_at FROM vlm_usage_log WHERE created_at >= ?",
        (cutoff_iso,),
    ).fetchall()
    total = len(rows)
    failed = sum(1 for r in rows if r["status"] == "error")
    last_shadow = conn.execute(
        "SELECT MAX(created_at) FROM vlm_usage_log WHERE status IN ('live_no_apply', 'live-no-apply')"
    ).fetchone()
    return {
        "calibration_status": summary.get("calibration_status", "ok"),
        "calibration_recommendation": summary.get("calibration_recommendation"),
        "confidence_distribution": summary.get("confidence_buckets_calibrated", {}),
        "bias_alerts": summary.get("bias_alerts", []),
        "total_calls_7d": total,
        "fail_rate": round(failed / total, 4) if total else 0.0,
        "last_shadow_run": last_shadow[0] if last_shadow else None,
    }


def _ops_comfyui_section(conn, cutoff_iso: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT status, audit_json, model_plan_json, can_publish, created_at
        FROM simulation_jobs
        WHERE created_at >= ?
        """,
        (cutoff_iso,),
    ).fetchall()
    done = 0
    failed = 0
    by_workflow_counter: dict[str, int] = {}
    failure_stages_counter: dict[str, int] = {}
    candidate_only_pending = 0
    for row in rows:
        try:
            audit = json.loads(row["audit_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            audit = {}
        try:
            plan = json.loads(row["model_plan_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            plan = {}
        wf = (
            (audit.get("workflow_name") if isinstance(audit, dict) else None)
            or (audit.get("model_name") if isinstance(audit, dict) else None)
            or (plan.get("workflow_name") if isinstance(plan, dict) else None)
            or "unknown"
        )
        by_workflow_counter[str(wf)] = by_workflow_counter.get(str(wf), 0) + 1
        if row["status"] == "done":
            done += 1
            if not int(row["can_publish"] or 0):
                candidate_only_pending += 1
        elif row["status"] == "failed":
            failed += 1
            failure = audit.get("failure") if isinstance(audit, dict) else None
            if isinstance(failure, dict):
                stage = str(failure.get("failure_stage") or "unknown")
                failure_stages_counter[stage] = failure_stages_counter.get(stage, 0) + 1
    return {
        "simulation_jobs_7d": {
            "done": done,
            "failed": failed,
            "by_workflow": _ops_top_k(by_workflow_counter, 20),
        },
        "candidate_only_pending": candidate_only_pending,
        "failure_breakdown": _ops_top_k(failure_stages_counter, 10),
    }


def _ops_gate_section(conn) -> dict[str, Any]:
    pre_rows = conn.execute(
        """
        SELECT reason_code, COUNT(*) AS n
        FROM review_tickets
        WHERE status = 'open' AND blocks_render = 1 AND stage = 'pre_render_gate'
        GROUP BY reason_code
        ORDER BY n DESC
        LIMIT 10
        """
    ).fetchall()
    deliv_rows = conn.execute(
        """
        SELECT reason_code, COUNT(*) AS n
        FROM review_tickets
        WHERE status = 'open' AND blocks_publish = 1 AND stage = 'delivery_gate'
        GROUP BY reason_code
        ORDER BY n DESC
        LIMIT 10
        """
    ).fetchall()
    # accepted_warnings_pending = cases.meta_json.source_group_selection.accepted_warnings
    # has ≥1 entry。SQLite 不能直查嵌套 JSON 数组长度，扫表 + Python 数。
    case_rows = conn.execute(
        "SELECT meta_json FROM cases WHERE trashed_at IS NULL AND meta_json IS NOT NULL"
    ).fetchall()
    accepted_pending = 0
    for cr in case_rows:
        try:
            meta = json.loads(cr["meta_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        sgs = meta.get("source_group_selection") if isinstance(meta, dict) else None
        accepted = sgs.get("accepted_warnings") if isinstance(sgs, dict) else None
        if isinstance(accepted, list) and accepted:
            accepted_pending += 1
    return {
        "pre_render_blockers_top10": [{"key": r["reason_code"], "count": r["n"]} for r in pre_rows],
        "delivery_gate_blockers_top10": [{"key": r["reason_code"], "count": r["n"]} for r in deliv_rows],
        "accepted_warnings_pending": accepted_pending,
    }


@router.get("/api/render/ops/vlm-comfyui/status")
def ops_vlm_comfyui_status(days: int = Query(default=7, ge=1, le=90)) -> dict[str, Any]:
    """P0.4 oncall 聚合面板 — VLM 校准+调用量 / ComfyUI 任务分布 / Gate 阻断 top10。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        vlm = _ops_vlm_section(conn, cutoff)
        comfyui = _ops_comfyui_section(conn, cutoff)
        gate = _ops_gate_section(conn)
    return {
        "days": days,
        "vlm": vlm,
        "comfyui": comfyui,
        "gate": gate,
    }


# ---------------------------------------------------------------------------
# P1.1 ops API — 把"野蛮跑批脚本"收编为 audited job-queue API
#
# 设计原则：
#   1) 所有调用必落 ops_audit_log（含 dry_run）；reviewer 必填，reason 强烈推荐
#   2) request_id 取 X-Request-Id header；否则服务端生成 uuid
#   3) outcome ∈ {ok|partial|error|dry_run}；http_status 与返回一致
#   4) scope 受限——firing 路径触碰 owner WIP（ai_generation_adapter / VLM
#      runner）的子能力一律 record-only（dry_run / plan），actual fire 仍由
#      owner 现有路径触发；待 owner WIP 合 main 后再开放 fire
# ---------------------------------------------------------------------------


ALLOWED_OPS_RERUN_SCOPES = {"render", "simulation"}
ALLOWED_OPS_OUTCOMES = {"ok", "partial", "error", "dry_run"}
MAX_OPS_BATCH_CASE_IDS = 200
MAX_OPS_AB_SAMPLE_SIZE = 200
MAX_OPS_VLM_SHADOW_SAMPLE = 200
ALLOWED_OPS_AB_SOURCE_POOLS = {"recent_done", "candidate_layer", "case_ids"}


class OpsBatchRerunRequest(BaseModel):
    case_ids: list[int] = Field(..., min_length=1, max_length=MAX_OPS_BATCH_CASE_IDS)
    scope: str = Field(default="render")
    workflow_filter: str | None = Field(default=None, max_length=160)
    dry_run: bool = Field(default=False)
    reviewer: str = Field(..., min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=1000)
    brand: str = Field(default=DEFAULT_BRAND)
    template: str = Field(default=DEFAULT_TEMPLATE)
    semantic_judge: str = Field(default=DEFAULT_SEMANTIC)


def _gen_request_id(supplied: str | None) -> str:
    """Use client-supplied X-Request-Id if present (trimmed, ≤64), else uuid."""
    if supplied:
        text = supplied.strip()
        if text:
            return text[:64]
    return f"req-{uuid.uuid4().hex[:16]}"


def _write_ops_audit_log(
    *,
    request_id: str,
    endpoint: str,
    reviewer: str,
    reason: str | None,
    payload: Any,
    response: Any,
    outcome: str,
    http_status: int,
) -> int:
    """Insert one row into ops_audit_log. outcome ∈ ALLOWED_OPS_OUTCOMES."""
    if outcome not in ALLOWED_OPS_OUTCOMES:
        raise ValueError(f"invalid outcome {outcome!r}")
    with db.connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO ops_audit_log
              (request_id, endpoint, reviewer, reason,
               payload_json, response_json, outcome, http_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                endpoint,
                reviewer,
                reason,
                json.dumps(payload, ensure_ascii=False, default=str),
                json.dumps(response, ensure_ascii=False, default=str),
                outcome,
                int(http_status),
                datetime_now_iso(),
            ),
        )
    return int(cur.lastrowid or 0)


def _simulation_rerun_plan(
    case_ids: list[int],
    workflow_filter: str | None,
) -> list[dict[str, Any]]:
    """Build the simulation rerun plan: matching simulation_jobs filtered by workflow."""
    if not case_ids:
        return []
    with db.connect() as conn:
        placeholders = ",".join("?" * len(case_ids))
        rows = conn.execute(
            f"""
            SELECT id, case_id, status, model_plan_json, audit_json
            FROM simulation_jobs
            WHERE case_id IN ({placeholders})
            ORDER BY case_id, created_at DESC
            """,
            case_ids,
        ).fetchall()
    planned: list[dict[str, Any]] = []
    for row in rows:
        try:
            plan = json.loads(row["model_plan_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            plan = {}
        try:
            audit = json.loads(row["audit_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            audit = {}
        wf = (
            (plan.get("workflow_name") if isinstance(plan, dict) else None)
            or (audit.get("workflow_name") if isinstance(audit, dict) else None)
            or "unknown"
        )
        if workflow_filter and str(workflow_filter) != str(wf):
            continue
        planned.append(
            {
                "simulation_job_id": int(row["id"]),
                "case_id": int(row["case_id"]),
                "status": row["status"],
                "workflow": str(wf),
            }
        )
    return planned


@router.post("/api/render/ops/batch-rerun")
def ops_batch_rerun(
    payload: OpsBatchRerunRequest,
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
) -> dict[str, Any]:
    """P1.1 收编 force_render_*.py / formal_render_repair_execution.py 跑批脚本。

    scope=render 经 RENDER_QUEUE.enqueue_batch 真实入队；scope=simulation 因 owner
    `ai_generation_adapter` WIP，目前仅支持 dry_run（返回会被重跑的
    simulation_jobs 计划，由 operator 手动从 /api/cases/{id}/simulate-after 触发）。
    每次调用必落 `ops_audit_log`，含 dry_run。
    """
    request_id = _gen_request_id(x_request_id)
    endpoint = "POST /api/render/ops/batch-rerun"
    payload_dump = payload.model_dump()

    if payload.scope not in ALLOWED_OPS_RERUN_SCOPES:
        resp = {
            "request_id": request_id,
            "error": f"scope must be one of {sorted(ALLOWED_OPS_RERUN_SCOPES)}",
        }
        _write_ops_audit_log(
            request_id=request_id, endpoint=endpoint,
            reviewer=payload.reviewer, reason=payload.reason,
            payload=payload_dump, response=resp,
            outcome="error", http_status=400,
        )
        raise HTTPException(400, resp)

    seen: set[int] = set()
    deduped: list[int] = []
    duplicate_count = 0
    for cid in payload.case_ids:
        if cid in seen:
            duplicate_count += 1
            continue
        seen.add(cid)
        deduped.append(cid)
    valid_ids, invalid = _batch_preview_rows(deduped)

    if payload.scope == "simulation":
        if not payload.dry_run:
            resp = {
                "request_id": request_id,
                "error": (
                    "scope=simulation requires dry_run=true; real simulation rerun is "
                    "pending owner ai_generation_adapter integration. Use POST "
                    "/api/cases/{id}/simulate-after for manual fire."
                ),
            }
            _write_ops_audit_log(
                request_id=request_id, endpoint=endpoint,
                reviewer=payload.reviewer, reason=payload.reason,
                payload=payload_dump, response=resp,
                outcome="error", http_status=501,
            )
            raise HTTPException(501, resp)
        planned = _simulation_rerun_plan(valid_ids, payload.workflow_filter)
        resp = {
            "request_id": request_id,
            "scope": "simulation",
            "dry_run": True,
            "planned": planned,
            "planned_count": len(planned),
            "invalid": invalid,
            "duplicate_count": duplicate_count,
        }
        _write_ops_audit_log(
            request_id=request_id, endpoint=endpoint,
            reviewer=payload.reviewer, reason=payload.reason,
            payload=payload_dump, response=resp,
            outcome="dry_run", http_status=200,
        )
        return resp

    # scope == "render"
    _validate_request(payload.brand, payload.semantic_judge)
    if payload.dry_run:
        resp = {
            "request_id": request_id,
            "scope": "render",
            "dry_run": True,
            "would_enqueue_case_ids": valid_ids,
            "would_enqueue_count": len(valid_ids),
            "invalid": invalid,
            "duplicate_count": duplicate_count,
        }
        _write_ops_audit_log(
            request_id=request_id, endpoint=endpoint,
            reviewer=payload.reviewer, reason=payload.reason,
            payload=payload_dump, response=resp,
            outcome="dry_run", http_status=200,
        )
        return resp
    if not valid_ids:
        resp = {
            "request_id": request_id,
            "scope": "render",
            "error": "no valid case ids in batch",
            "invalid": invalid,
            "duplicate_count": duplicate_count,
        }
        _write_ops_audit_log(
            request_id=request_id, endpoint=endpoint,
            reviewer=payload.reviewer, reason=payload.reason,
            payload=payload_dump, response=resp,
            outcome="error", http_status=404,
        )
        raise HTTPException(404, resp)

    batch_id, job_ids = RENDER_QUEUE.enqueue_batch(
        case_ids=valid_ids,
        brand=payload.brand,
        template=payload.template,
        semantic_judge=payload.semantic_judge,
    )
    outcome = "partial" if (len(job_ids) < len(valid_ids) or invalid) else "ok"
    resp = {
        "request_id": request_id,
        "scope": "render",
        "dry_run": False,
        "batch_id": batch_id,
        "job_ids": job_ids,
        "enqueued_count": len(job_ids),
        "invalid": invalid,
        "duplicate_count": duplicate_count,
    }
    _write_ops_audit_log(
        request_id=request_id, endpoint=endpoint,
        reviewer=payload.reviewer, reason=payload.reason,
        payload=payload_dump, response=resp,
        outcome=outcome, http_status=200,
    )
    return resp


# ---------------------------------------------------------------------------
# P1.1.B /api/render/ops/ab-sample — 接管 comfyui_ab_runner.py
# ---------------------------------------------------------------------------


class OpsAbSampleRequest(BaseModel):
    workflow_a: str = Field(..., min_length=1, max_length=160)
    workflow_b: str = Field(..., min_length=1, max_length=160)
    sample_size: int = Field(..., ge=1, le=MAX_OPS_AB_SAMPLE_SIZE)
    source_pool: str = Field(default="recent_done")
    case_ids: list[int] | None = Field(default=None, max_length=MAX_OPS_AB_SAMPLE_SIZE)
    reviewer: str = Field(..., min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=1000)


def _pick_ab_sample_case_ids(source_pool: str, sample_size: int,
                             explicit_case_ids: list[int] | None) -> list[int]:
    """Pick up to `sample_size` distinct case_ids for A/B comparison.

    source_pool semantics:
      - case_ids: caller-supplied explicit list (truncated to sample_size)
      - recent_done: most recent simulation_jobs.status='done' case_ids
      - candidate_layer: simulation_jobs.status='done' AND can_publish=0
    """
    if source_pool == "case_ids":
        if not explicit_case_ids:
            return []
        seen: set[int] = set()
        out: list[int] = []
        for cid in explicit_case_ids:
            if cid not in seen:
                seen.add(cid)
                out.append(cid)
            if len(out) >= sample_size:
                break
        return out
    if source_pool not in {"recent_done", "candidate_layer"}:
        return []
    where_can_publish = " AND can_publish = 0" if source_pool == "candidate_layer" else ""
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT case_id
            FROM simulation_jobs
            WHERE status = 'done' AND case_id IS NOT NULL{where_can_publish}
            ORDER BY case_id DESC
            LIMIT ?
            """,
            (sample_size,),
        ).fetchall()
    return [int(r["case_id"]) for r in rows]


@router.post("/api/render/ops/ab-sample")
def ops_ab_sample(
    payload: OpsAbSampleRequest,
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
) -> dict[str, Any]:
    """P1.1 收编 comfyui_ab_runner.py。

    记录 A/B 抽样规格 + 候选 case_ids 计划入 ops_audit_log，返回
    `ab_sample_run_id` + `plan`。实际 ComfyUI 双 workflow 出图触发仍需 owner
    `ai_generation_adapter` 整合（candidate_lineage 同时由 owner 写入路径补全）。
    本 endpoint 是"规格 + 候选选定"的合约层，让前端/cron 可生成可审计计划。
    """
    request_id = _gen_request_id(x_request_id)
    endpoint = "POST /api/render/ops/ab-sample"
    payload_dump = payload.model_dump()

    if payload.source_pool not in ALLOWED_OPS_AB_SOURCE_POOLS:
        resp = {
            "request_id": request_id,
            "error": f"source_pool must be one of {sorted(ALLOWED_OPS_AB_SOURCE_POOLS)}",
        }
        _write_ops_audit_log(
            request_id=request_id, endpoint=endpoint,
            reviewer=payload.reviewer, reason=payload.reason,
            payload=payload_dump, response=resp,
            outcome="error", http_status=400,
        )
        raise HTTPException(400, resp)
    if payload.workflow_a == payload.workflow_b:
        resp = {
            "request_id": request_id,
            "error": "workflow_a and workflow_b must differ",
        }
        _write_ops_audit_log(
            request_id=request_id, endpoint=endpoint,
            reviewer=payload.reviewer, reason=payload.reason,
            payload=payload_dump, response=resp,
            outcome="error", http_status=400,
        )
        raise HTTPException(400, resp)

    picked = _pick_ab_sample_case_ids(
        payload.source_pool, payload.sample_size, payload.case_ids
    )
    ab_run_id = f"absamp-{uuid.uuid4().hex[:12]}"
    plan = {
        "ab_sample_run_id": ab_run_id,
        "workflow_a": payload.workflow_a,
        "workflow_b": payload.workflow_b,
        "sample_size_requested": payload.sample_size,
        "source_pool": payload.source_pool,
        "picked_case_ids": picked,
        "picked_count": len(picked),
    }
    resp = {
        "request_id": request_id,
        "status": "plan_recorded",
        "note": (
            "Sample plan recorded in ops_audit_log. Actual A/B fire is pending "
            "owner ai_generation_adapter integration; consumer scripts should "
            "read this plan and fire via POST /api/cases/{id}/simulate-after."
        ),
        **plan,
    }
    _write_ops_audit_log(
        request_id=request_id, endpoint=endpoint,
        reviewer=payload.reviewer, reason=payload.reason,
        payload=payload_dump, response=resp,
        outcome="ok", http_status=200,
    )
    return resp


# ---------------------------------------------------------------------------
# P1.1.C /api/render/ops/vlm-shadow — 收编 vlm_classify_batch.py --live-no-apply
# ---------------------------------------------------------------------------


class OpsVlmShadowRequest(BaseModel):
    all_low_confidence: bool = Field(default=False)
    case_ids: list[int] | None = Field(default=None, max_length=MAX_OPS_VLM_SHADOW_SAMPLE)
    max_items: int = Field(default=50, ge=1, le=MAX_OPS_VLM_SHADOW_SAMPLE)
    confidence_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    dry_run: bool = Field(default=True)
    reviewer: str = Field(..., min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=1000)


@router.post("/api/render/ops/vlm-shadow")
def ops_vlm_shadow(
    payload: OpsVlmShadowRequest,
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
) -> dict[str, Any]:
    """P1.4 每日 VLM shadow / `vlm_classify_batch.py --live-no-apply` 收编。

    dry_run=true（默认）：扫描候选 image_observations，返回 candidate_count + 截断
    candidates（按 max_items），不发任何 VLM 请求；mode='dry-run' 给 cron / UI 判
    断当日 shadow 工作量。dry_run=false：暂返 501，等 owner 把 VLMProvider 真
    classifier purpose 跑批路径合 main 后再开。

    target 来源（至少一个）：
      - all_low_confidence=true → 选 source='vlm_classifier' AND confidence
        < confidence_threshold（默认 0.85）
      - case_ids=[...] → 该 case 集合下所有 image_observations

    审计：所有结果（含 error / 501）落 ops_audit_log。
    """
    request_id = _gen_request_id(x_request_id)
    endpoint = "POST /api/render/ops/vlm-shadow"
    payload_dump = payload.model_dump()

    has_target = payload.all_low_confidence or bool(payload.case_ids)
    if not has_target:
        resp = {
            "request_id": request_id,
            "error": "must supply at least one of: all_low_confidence=true, case_ids=[...]",
        }
        _write_ops_audit_log(
            request_id=request_id, endpoint=endpoint,
            reviewer=payload.reviewer, reason=payload.reason,
            payload=payload_dump, response=resp,
            outcome="error", http_status=400,
        )
        raise HTTPException(400, resp)

    if not payload.dry_run:
        resp = {
            "request_id": request_id,
            "error": (
                "live VLM shadow fire is pending owner integration; use dry_run=true "
                "to get the candidate list and have cron drive vlm_classify_batch "
                "--live-no-apply for now."
            ),
        }
        _write_ops_audit_log(
            request_id=request_id, endpoint=endpoint,
            reviewer=payload.reviewer, reason=payload.reason,
            payload=payload_dump, response=resp,
            outcome="error", http_status=501,
        )
        raise HTTPException(501, resp)

    # dry-run path: count + sample
    conditions: list[str] = []
    args: list[Any] = []
    if payload.all_low_confidence:
        conditions.append("(source = ? AND confidence < ?)")
        args.extend(["vlm_classifier", payload.confidence_threshold])
    if payload.case_ids:
        placeholders = ",".join("?" * len(payload.case_ids))
        conditions.append(f"case_id IN ({placeholders})")
        args.extend(payload.case_ids)
    where_clause = " OR ".join(conditions) if conditions else "1=0"

    with db.connect() as conn:
        count_row = conn.execute(
            f"SELECT COUNT(*) AS n FROM image_observations WHERE {where_clause}",
            tuple(args),
        ).fetchone()
        candidate_count = int(count_row["n"] if count_row else 0)
        sample_rows = conn.execute(
            f"""
            SELECT id, group_id, case_id, image_path, source, confidence,
                   phase, view, body_part
            FROM image_observations
            WHERE {where_clause}
            ORDER BY confidence ASC, id ASC
            LIMIT ?
            """,
            (*args, payload.max_items),
        ).fetchall()
    candidates = [
        {
            "image_observation_id": int(r["id"]),
            "group_id": int(r["group_id"]) if r["group_id"] is not None else None,
            "case_id": int(r["case_id"]) if r["case_id"] is not None else None,
            "image_path": r["image_path"],
            "source": r["source"],
            "confidence": float(r["confidence"] or 0),
            "phase": r["phase"],
            "view": r["view"],
            "body_part": r["body_part"],
        }
        for r in sample_rows
    ]
    shadow_run_id = f"shadow-{uuid.uuid4().hex[:12]}"
    resp = {
        "request_id": request_id,
        "shadow_run_id": shadow_run_id,
        "mode": "dry-run",
        "dry_run": True,
        "candidate_count": candidate_count,
        "max_items": payload.max_items,
        "confidence_threshold": payload.confidence_threshold,
        "candidates": candidates,
        "note": (
            "Shadow candidates listed for dry-run; consumer can fire "
            "vlm_classify_batch.py --live-no-apply with this list."
        ),
    }
    _write_ops_audit_log(
        request_id=request_id, endpoint=endpoint,
        reviewer=payload.reviewer, reason=payload.reason,
        payload=payload_dump, response=resp,
        outcome="dry_run", http_status=200,
    )
    return resp


# ---------------------------------------------------------------------------
# P1.1.D /api/render/ops/repair-queue — identity / tone 修复批入口
# ---------------------------------------------------------------------------


ALLOWED_REPAIR_TYPES = {"identity", "tone", "both"}


class OpsRepairQueueRequest(BaseModel):
    case_ids: list[int] = Field(..., min_length=1, max_length=MAX_OPS_BATCH_CASE_IDS)
    repair_type: str = Field(..., min_length=1, max_length=32)
    dry_run: bool = Field(default=True)
    reviewer: str = Field(..., min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=1000)


@router.post("/api/render/ops/repair-queue")
def ops_repair_queue(
    payload: OpsRepairQueueRequest,
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
) -> dict[str, Any]:
    """P1.1.D identity / tone 修复批入口（dry_run 主能力 + audit-only fire stub）。

    repair_type:
      - identity: face identity drift 修复（VLM judge identity_match < threshold 触发）
      - tone:     skin tone / brightness 漂移修复
      - both:     依次跑 identity 再跑 tone

    dry_run=true（默认）：仅生成 planned_case_ids（去重 + DB-validate case 存在），
    invalid 列出未找到的 case_ids。0 入队，落审计。

    dry_run=false：当前返 501 — 真实 repair pipeline 走 owner T90 路径，
    待 ai_generation_adapter + render_executor repair 模式合 main 后开放。
    本 endpoint 提供 dry_run 让 oncall 提前确认 case 范围。
    """
    request_id = _gen_request_id(x_request_id)
    endpoint = "POST /api/render/ops/repair-queue"
    payload_dump = payload.model_dump()

    if payload.repair_type not in ALLOWED_REPAIR_TYPES:
        # Branch lets us audit-log the 422 attempt (Pydantic Literal would also
        # work but Pydantic 422 path doesn't run our route body, so we'd lose
        # the audit row).
        resp = {
            "request_id": request_id,
            "error": f"repair_type must be one of {sorted(ALLOWED_REPAIR_TYPES)}",
        }
        _write_ops_audit_log(
            request_id=request_id, endpoint=endpoint,
            reviewer=payload.reviewer, reason=payload.reason,
            payload=payload_dump, response=resp,
            outcome="error", http_status=422,
        )
        raise HTTPException(422, resp)

    seen: set[int] = set()
    deduped: list[int] = []
    duplicate_count = 0
    for cid in payload.case_ids:
        if cid in seen:
            duplicate_count += 1
            continue
        seen.add(cid)
        deduped.append(cid)
    valid_ids, invalid = _batch_preview_rows(deduped)

    if not payload.dry_run:
        resp = {
            "request_id": request_id,
            "error": (
                "real repair fire is pending owner render_executor + ai_generation_adapter "
                "T90 integration. dry_run=true is the only supported mode currently; "
                "use it to record planned cases, then fire via existing render endpoints "
                "or wait for owner WIP to merge to main."
            ),
        }
        _write_ops_audit_log(
            request_id=request_id, endpoint=endpoint,
            reviewer=payload.reviewer, reason=payload.reason,
            payload=payload_dump, response=resp,
            outcome="error", http_status=501,
        )
        raise HTTPException(501, resp)

    resp = {
        "request_id": request_id,
        "dry_run": True,
        "repair_type": payload.repair_type,
        "planned_case_ids": valid_ids,
        "enqueued_job_ids": [],
        "planned_count": len(valid_ids),
        "invalid": invalid,
        "duplicate_count": duplicate_count,
        "note": "dry_run plan recorded; no jobs enqueued",
    }
    _write_ops_audit_log(
        request_id=request_id, endpoint=endpoint,
        reviewer=payload.reviewer, reason=payload.reason,
        payload=payload_dump, response=resp,
        outcome="dry_run", http_status=200,
    )
    return resp
