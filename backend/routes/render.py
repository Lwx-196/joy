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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import audit, db, render_executor, render_quality, source_images, stress
from ..render_queue import RENDER_QUEUE

router = APIRouter(tags=["render"])

ALLOWED_BRANDS = {"fumei", "shimei", "芙美", "莳美"}
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


class RenderBatchRequest(BaseModel):
    case_ids: list[int]
    brand: str = Field(default=DEFAULT_BRAND)
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


ALLOWED_QUALITY_VERDICTS = {"approved", "needs_recheck", "rejected"}
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

_TERMINAL_RENDER_STATUSES_SQL = "'done', 'done_with_issues', 'blocked', 'failed'"


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


def _quality_queue_condition(status: str) -> tuple[str, list[Any]]:
    base = [
        "c.trashed_at IS NULL",
        "j.status IN ('done', 'done_with_issues', 'blocked', 'failed')",
    ]
    params: list[Any] = []
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
            out.append({"code": code, "label": label, "source": str(item.get("source") or "")})
    return out[:8]


# ----------------------------------------------------------------------
# Single-case enqueue
# ----------------------------------------------------------------------


@router.post("/api/cases/{case_id}/render")
def enqueue_single(case_id: int, payload: RenderRequest) -> dict:
    _validate_request(payload.brand, payload.semantic_judge)
    try:
        job_id = RENDER_QUEUE.enqueue(
            case_id=case_id,
            brand=payload.brand,
            template=payload.template or DEFAULT_TEMPLATE,
            semantic_judge=payload.semantic_judge or DEFAULT_SEMANTIC,
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
    batch_id, job_ids = RENDER_QUEUE.enqueue_batch(
        case_ids=valid_case_ids,
        brand=payload.brand,
        template=payload.template or DEFAULT_TEMPLATE,
        semantic_judge=payload.semantic_judge or DEFAULT_SEMANTIC,
    )
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
    if not row:
        raise HTTPException(404, "case not found")
    case_dir = Path(row["abs_path"])
    history_dir = case_dir / ".case-layout-output" / brand / template / "render" / ".history"
    if not history_dir.is_dir():
        return {"case_id": case_id, "brand": brand, "template": template, "snapshots": []}
    snapshots: list[dict[str, Any]] = []
    for p in sorted(history_dir.iterdir(), reverse=True):
        if not (p.is_file() and p.suffix == ".jpg"):
            continue
        snapshots.append({
            "filename": p.name,
            "archived_at": p.stem,  # ISO-ish timestamp from filename
            "size_bytes": p.stat().st_size,
        })
    return {"case_id": case_id, "brand": brand, "template": template, "snapshots": snapshots}


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


@router.get("/api/render/quality-queue")
def list_render_quality_queue(
    status: str = Query("review_required"),
    limit: int = Query(100, ge=1, le=200),
) -> dict[str, Any]:
    """Central queue for final-render QA.

    Cases/listing pages answer "which case is this"; this endpoint answers
    "which render artifacts need a human quality decision" using real
    render_jobs/render_quality rows only.
    """
    status = status.strip() or "review_required"
    where_sql, params = _quality_queue_condition(status)
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
        "archive": archive,
        "status": status,
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
        job = conn.execute("SELECT id, status FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
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
        can_publish = payload.can_publish
        if can_publish is None:
            can_publish = verdict == "approved" and job["status"] == "done"
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
