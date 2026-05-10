"""Case group diagnosis endpoints.

These endpoints expose the new classification pipeline:
case boundary → image observations → pair candidates → template diagnosis.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import case_grouping, db
from ..render_queue import RENDER_QUEUE
from .render import ALLOWED_BRANDS, ALLOWED_SEMANTIC, DEFAULT_BRAND, DEFAULT_SEMANTIC, DEFAULT_TEMPLATE

router = APIRouter(tags=["case-groups"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConfirmClassificationPayload(BaseModel):
    status: str = Field(default="confirmed")
    category: str | None = None
    template_tier: str | None = None
    note: str | None = Field(default=None, max_length=2000)


class GroupRenderPayload(BaseModel):
    brand: str = Field(default=DEFAULT_BRAND)
    template: str = Field(default=DEFAULT_TEMPLATE)
    semantic_judge: str = Field(default=DEFAULT_SEMANTIC)


class SimulateAfterPayload(BaseModel):
    focus_targets: list[str] = Field(default_factory=list)
    ai_generation_authorized: bool = False
    provider: str | None = None
    model_name: str | None = None
    note: str | None = Field(default=None, max_length=2000)


@router.post("/api/cases/rescan-groups")
def rescan_groups() -> dict[str, Any]:
    with db.connect() as conn:
        summary = case_grouping.rebuild_case_groups(conn)
    return summary


@router.get("/api/case-groups")
def list_groups(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    with db.connect() as conn:
        items = case_grouping.list_case_groups(conn, status=status, limit=limit)
    return {"items": items, "total": len(items)}


@router.get("/api/case-groups/{group_id}/diagnosis")
def group_diagnosis(group_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        result = case_grouping.get_case_group_diagnosis(conn, group_id)
    if result is None:
        raise HTTPException(404, "case group not found")
    return result


@router.post("/api/case-groups/{group_id}/confirm-classification")
def confirm_classification(group_id: int, payload: ConfirmClassificationPayload) -> dict[str, Any]:
    if payload.status not in {"confirmed", "needs_review", "auto"}:
        raise HTTPException(400, "status must be confirmed, needs_review, or auto")
    with db.connect() as conn:
        result = case_grouping.update_group_confirmation(
            conn,
            group_id,
            status=payload.status,
            category=payload.category,
            template_tier=payload.template_tier,
            note=payload.note,
        )
    if result is None:
        raise HTTPException(404, "case group not found")
    return result


@router.post("/api/case-groups/{group_id}/render")
def render_group(group_id: int, payload: GroupRenderPayload) -> dict[str, Any]:
    if payload.brand not in ALLOWED_BRANDS:
        raise HTTPException(400, f"unsupported brand: {payload.brand}")
    if payload.semantic_judge not in ALLOWED_SEMANTIC:
        raise HTTPException(400, f"semantic_judge must be one of {sorted(ALLOWED_SEMANTIC)}")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT primary_case_id FROM case_groups WHERE id = ?", (group_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "case group not found")
    if not row["primary_case_id"]:
        raise HTTPException(400, "case group has no primary case")
    try:
        job_id = RENDER_QUEUE.enqueue(
            case_id=row["primary_case_id"],
            brand=payload.brand,
            template=payload.template or DEFAULT_TEMPLATE,
            semantic_judge=payload.semantic_judge or DEFAULT_SEMANTIC,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"job_id": job_id, "case_id": row["primary_case_id"], "group_id": group_id}


@router.post("/api/case-groups/{group_id}/simulate-after")
def simulate_after(group_id: int, payload: SimulateAfterPayload) -> dict[str, Any]:
    focus_targets = [x.strip() for x in payload.focus_targets if x.strip()]
    if not focus_targets:
        raise HTTPException(400, "focus_targets is required for after-image simulation")
    if not payload.ai_generation_authorized:
        raise HTTPException(400, "ai_generation_authorized must be true")

    generation_enabled = os.environ.get("CASE_WORKBENCH_ENABLE_AI_GENERATION") == "1"
    now = _now_iso()
    with db.connect() as conn:
        group = conn.execute(
            "SELECT id, primary_case_id FROM case_groups WHERE id = ?", (group_id,)
        ).fetchone()
        if not group:
            raise HTTPException(404, "case group not found")
        status = "queued" if generation_enabled else "blocked"
        error_message = None if generation_enabled else "AI generation backend is disabled by default"
        policy = {
            "artifact_mode": "ai_after_simulation",
            "focus_scope": "focus-scoped-light",
            "non_target_policy": "light-unify-only",
            "watermark_required": True,
            "mix_with_real_case": False,
        }
        model_plan = {
            "provider": payload.provider or "not_selected",
            "model_name": payload.model_name or "not_selected",
            "enabled_by_env": generation_enabled,
        }
        cur = conn.execute(
            """
            INSERT INTO simulation_jobs
              (group_id, case_id, status, focus_targets_json, policy_json,
               model_plan_json, watermarked, audit_json, error_message, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                group_id,
                group["primary_case_id"],
                status,
                json.dumps(focus_targets, ensure_ascii=False),
                json.dumps(policy, ensure_ascii=False),
                json.dumps(model_plan, ensure_ascii=False),
                json.dumps({"note": payload.note, "created_via": "/api/case-groups/{id}/simulate-after"}, ensure_ascii=False),
                error_message,
                now,
                now,
            ),
        )
        job_id = cur.lastrowid or 0
        conn.execute(
            """
            INSERT INTO ai_runs
              (subject_kind, subject_id, model_role, provider, model_name,
               input_summary_json, output_json, status, error_message, started_at, finished_at)
            VALUES ('simulation_job', ?, 'image_generation', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                payload.provider,
                payload.model_name,
                json.dumps({"focus_targets": focus_targets, "group_id": group_id}, ensure_ascii=False),
                json.dumps({"policy": policy, "model_plan": model_plan}, ensure_ascii=False),
                "planned" if generation_enabled else "blocked",
                error_message,
                now,
                now,
            ),
        )
    return {
        "simulation_job_id": job_id,
        "group_id": group_id,
        "case_id": group["primary_case_id"],
        "status": status,
        "focus_targets": focus_targets,
        "policy": policy,
        "error_message": error_message,
    }
