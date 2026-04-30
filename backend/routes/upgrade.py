"""V3 upgrade queue endpoints — async batch sibling to the sync /upgrade.

Endpoints:
    POST   /api/cases/upgrade/batch                   enqueue batch
    GET    /api/jobs/upgrade/batches/{batch_id}       batch summary
    POST   /api/jobs/upgrade/batches/{id}/undo        undo whole batch (apply_undo per case)
    GET    /api/jobs/upgrade/{job_id}                 single job
    POST   /api/jobs/upgrade/{job_id}/cancel          cancel queued job
    POST   /api/jobs/upgrade/{job_id}/retry           re-enqueue failed/cancelled job

The single-case sync path /api/cases/{id}/upgrade is unchanged and lives in
routes/cases.py. The shared core is `_upgrade_executor.execute_upgrade`.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..upgrade_queue import UPGRADE_QUEUE

router = APIRouter(tags=["upgrade"])

ALLOWED_BRANDS = {"fumei", "shimei", "芙美", "莳美"}
DEFAULT_BRAND = "fumei"
MAX_BATCH_SIZE = 50


class UpgradeBatchRequest(BaseModel):
    case_ids: list[int]
    brand: str = Field(default=DEFAULT_BRAND)


def _validate_brand(brand: str) -> None:
    if brand not in ALLOWED_BRANDS:
        raise HTTPException(400, f"unsupported brand: {brand}")


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    meta = {}
    if row["meta_json"]:
        try:
            meta = json.loads(row["meta_json"])
        except (TypeError, ValueError):
            meta = {}
    return {
        "id": row["id"],
        "case_id": row["case_id"],
        "brand": row["brand"],
        "status": row["status"],
        "batch_id": row["batch_id"],
        "enqueued_at": row["enqueued_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error_message": row["error_message"],
        "meta": meta,
    }


# ----------------------------------------------------------------------
# Batch enqueue
# ----------------------------------------------------------------------


@router.post("/api/cases/upgrade/batch")
def enqueue_upgrade_batch(payload: UpgradeBatchRequest) -> dict:
    if not payload.case_ids:
        raise HTTPException(400, "case_ids cannot be empty")
    if len(payload.case_ids) > MAX_BATCH_SIZE:
        raise HTTPException(
            400,
            f"batch size {len(payload.case_ids)} exceeds maximum {MAX_BATCH_SIZE}; "
            f"split into smaller batches",
        )
    _validate_brand(payload.brand)
    batch_id, job_ids = UPGRADE_QUEUE.enqueue_batch(payload.case_ids, payload.brand)
    if not job_ids:
        raise HTTPException(404, "no valid case ids in batch")
    return {
        "batch_id": batch_id,
        "job_ids": job_ids,
        "skipped_count": len(payload.case_ids) - len(job_ids),
    }


# ----------------------------------------------------------------------
# Batch detail / undo
# ----------------------------------------------------------------------


@router.get("/api/jobs/upgrade/batches/{batch_id}")
def get_upgrade_batch(batch_id: str) -> dict:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM upgrade_jobs WHERE batch_id = ? ORDER BY id ASC",
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


@router.post("/api/jobs/upgrade/batches/{batch_id}/undo")
def undo_upgrade_batch(batch_id: str) -> dict:
    result = UPGRADE_QUEUE.undo_batch(batch_id)
    return result


# ----------------------------------------------------------------------
# Single job
# ----------------------------------------------------------------------


@router.get("/api/jobs/upgrade/{job_id}")
def get_upgrade_job(job_id: int) -> dict:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM upgrade_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "job not found")
    return _row_to_job(row)


@router.post("/api/jobs/upgrade/{job_id}/cancel")
def cancel_upgrade_job(job_id: int) -> dict:
    ok = UPGRADE_QUEUE.cancel(job_id)
    if not ok:
        raise HTTPException(409, "job not cancellable (already running or finished)")
    return {"cancelled": True, "job_id": job_id}


@router.post("/api/jobs/upgrade/{job_id}/retry")
def retry_upgrade_job(job_id: int) -> dict:
    try:
        new_id = UPGRADE_QUEUE.retry(job_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"retried": True, "old_job_id": job_id, "new_job_id": new_id}
