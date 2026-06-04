"""Best-pair candidate compute and render endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db
from ..services import best_pair_service
from ..workers.best_pair_compute_queue import COMPUTE_QUEUE

router = APIRouter(tags=["best-pair"])


class BestPairSelectPayload(BaseModel):
    view: str | None = Field(default=None, min_length=1, max_length=32)
    before: str = Field(..., min_length=1, max_length=500)
    after: str = Field(..., min_length=1, max_length=500)
    fingerprint: str = Field(..., min_length=1, max_length=160)
    reviewer: str | None = Field(default=None, max_length=120)
    reason: str | None = Field(default=None, max_length=500)


class BestPairBatchPayload(BaseModel):
    case_ids: list[int] = Field(..., min_length=1, max_length=500)


def _assert_case_exists(case_id: int) -> None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM cases WHERE id = ? AND trashed_at IS NULL",
            (case_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "case not found")


@router.post("/api/cases/best-pair/batch-compute", status_code=202)
def batch_compute(payload: BestPairBatchPayload) -> dict[str, Any]:
    case_ids = [int(case_id) for case_id in payload.case_ids]
    batch_id = COMPUTE_QUEUE.submit_batch(case_ids)
    return {"batch_id": batch_id, "queued": len(case_ids)}


@router.get("/api/cases/best-pair/batch-compute/{batch_id}")
def batch_compute_status(batch_id: str) -> dict[str, Any]:
    status = COMPUTE_QUEUE.status(batch_id)
    if status is None:
        raise HTTPException(404, "batch not found")
    return status


@router.get("/api/cases/{case_id}/best-pair")
def get_best_pair(case_id: int) -> dict[str, Any]:
    _assert_case_exists(case_id)
    return best_pair_service.list_best_pair(case_id)


@router.post("/api/cases/{case_id}/best-pair/compute")
def compute_best_pair(case_id: int) -> dict[str, Any]:
    _assert_case_exists(case_id)
    return best_pair_service.compute_best_pair(case_id)


@router.post("/api/cases/{case_id}/best-pair/select")
def select_best_pair(case_id: int, payload: BestPairSelectPayload) -> dict[str, int]:
    _assert_case_exists(case_id)
    selection_id = best_pair_service.select_best_pair_for_case(
        case_id,
        payload.before,
        payload.after,
        payload.fingerprint,
        view=payload.view,
        reviewer=payload.reviewer,
        reason=payload.reason,
    )
    return {"selection_id": selection_id}


@router.post("/api/cases/{case_id}/best-pair/render")
def render_best_pair(
    case_id: int,
    brand: str = Query(default="fumei"),
    template: str = Query(default="tri-compare"),
) -> dict[str, int]:
    _assert_case_exists(case_id)
    job_id = best_pair_service.trigger_best_pair_render(case_id, brand=brand, template=template)
    return {"job_id": job_id}
