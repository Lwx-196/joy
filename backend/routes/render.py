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
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import audit, db, render_executor
from ..render_queue import RENDER_QUEUE

router = APIRouter(tags=["render"])

ALLOWED_BRANDS = {"fumei", "shimei", "芙美", "莳美"}
DEFAULT_BRAND = "fumei"
DEFAULT_TEMPLATE = "tri-compare"
DEFAULT_SEMANTIC = "off"
ALLOWED_SEMANTIC = {"off", "auto"}
MAX_BATCH_SIZE = 50


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


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    meta_raw = row["meta_json"]
    meta = {}
    if meta_raw:
        try:
            meta = json.loads(meta_raw)
        except (TypeError, ValueError):
            meta = {}
    return {
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


def _read_manifest_blocking(manifest_path: str | None) -> dict[str, list[str]]:
    """Stage A: 读 manifest.final.json 透传 blocking_issues + warnings 字符串列表。

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
        return {
            "blocking_issues": [str(x) for x in (data.get("blocking_issues") or [])],
            "warnings": [str(x) for x in (data.get("warnings") or [])],
        }
    except (OSError, ValueError, TypeError):
        return empty


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
    batch_id, job_ids = RENDER_QUEUE.enqueue_batch(
        case_ids=payload.case_ids,
        brand=payload.brand,
        template=payload.template or DEFAULT_TEMPLATE,
        semantic_judge=payload.semantic_judge or DEFAULT_SEMANTIC,
    )
    if not job_ids:
        raise HTTPException(404, "no valid case ids in batch")
    return {"batch_id": batch_id, "job_ids": job_ids, "skipped_count": len(payload.case_ids) - len(job_ids)}


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

    with db.connect() as conn:
        placeholders = ",".join("?" * len(deduped_ids))
        rows = conn.execute(
            f"SELECT id FROM cases WHERE id IN ({placeholders})",
            deduped_ids,
        ).fetchall()
    existing_ids = {r["id"] for r in rows}

    valid_ids: list[int] = []
    invalid: list[dict[str, Any]] = []
    for cid in deduped_ids:
        if cid in existing_ids:
            valid_ids.append(cid)
        else:
            invalid.append({"case_id": cid, "reason": "case_not_found"})
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
    """Most recent job for this case (any status).

    For done jobs, also stat final-board.jpg and attach `output_mtime`
    (Unix seconds) so the frontend can cache-bust the <img src> after
    `restore_render` (which rewrites the file but doesn't create a new job row).
    """
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM render_jobs
            WHERE case_id = ?
            ORDER BY enqueued_at DESC, id DESC
            LIMIT 1
            """,
            (case_id,),
        ).fetchone()
        case_row = conn.execute(
            "SELECT abs_path FROM cases WHERE id = ?", (case_id,)
        ).fetchone()
    if not row:
        return {"job": None}
    job = _row_to_job(row)
    if job["status"] == "done" and case_row:
        out_path = (
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
        row = conn.execute("SELECT abs_path FROM cases WHERE id = ?", (case_id,)).fetchone()
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
    if payload.brand not in ALLOWED_BRANDS:
        raise HTTPException(400, f"unsupported brand: {payload.brand}")
    if not _ARCHIVED_AT_RE.fullmatch(payload.archived_at):
        raise HTTPException(400, "invalid archived_at format")
    template = payload.template or DEFAULT_TEMPLATE

    with db.connect() as conn:
        row = conn.execute("SELECT abs_path FROM cases WHERE id = ?", (case_id,)).fetchone()
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
