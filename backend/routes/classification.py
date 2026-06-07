"""Enhanced classification routes — multi-tier fusion pipeline."""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..services.enhanced_classifier import fetch_case_observations, run_enhanced_classification
from ..services import vlm_source_classifier
from ..services.vlm_provider import VLMProvider

router = APIRouter(prefix="/api/classification", tags=["classification"])


class EnhancedClassifyRequest(BaseModel):
    mode: str = Field(default="dry-run", description="dry-run / live-no-apply / apply")
    tiers: list[str] | None = Field(
        default=None,
        description="Tiers to run: path_rules, exif, vlm_single, vlm_pair. Default: all.",
    )
    concurrency: int = Field(default=2, ge=1, le=10)
    timeout_seconds: float = Field(default=45.0, ge=1.0, le=300.0)


class BatchClassifyRequest(BaseModel):
    case_ids: list[int] | None = Field(
        default=None,
        description="Case IDs to classify. If null, all_low_confidence must be true.",
    )
    all_low_confidence: bool = Field(
        default=False,
        description="Classify all low-confidence observations across all cases.",
    )
    mode: str = Field(default="dry-run", description="dry-run / live-no-apply / apply")
    max_items_per_case: int = Field(default=50, ge=1, le=500)
    concurrency: int = Field(default=3, ge=1, le=10)
    timeout_seconds: float = Field(default=45.0, ge=1.0, le=300.0)
    max_retries: int = Field(default=2, ge=0, le=5, description="Per-item retry rounds for timeout/transient errors")


@router.post("/{case_id}/enhanced")
def classify_enhanced(case_id: int, payload: EnhancedClassifyRequest) -> dict[str, Any]:
    if payload.mode not in {"dry-run", "live-no-apply", "apply"}:
        raise HTTPException(400, f"invalid mode: {payload.mode!r}")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM cases WHERE id = ? AND trashed_at IS NULL",
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        provider = None if payload.mode == "dry-run" else VLMProvider(env=dict(os.environ))
        return run_enhanced_classification(
            conn,
            case_id,
            tiers=payload.tiers,
            mode=payload.mode,
            provider=provider,
            concurrency=payload.concurrency,
            timeout=payload.timeout_seconds,
        )


@router.get("/{case_id}/signals")
def get_classification_signals(case_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM cases WHERE id = ? AND trashed_at IS NULL",
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        observations = fetch_case_observations(conn, case_id)
        return {
            "case_id": case_id,
            "image_count": len(observations),
            "observations": [
                {
                    "observation_id": obs.observation_id,
                    "image_path": obs.image_path,
                    "phase": obs.phase,
                    "confidence": obs.confidence,
                    "source": obs.source,
                }
                for obs in observations
            ],
        }


@router.post("/batch")
def classify_batch(payload: BatchClassifyRequest) -> dict[str, Any]:
    if not payload.case_ids and not payload.all_low_confidence:
        raise HTTPException(400, "case_ids or all_low_confidence=true required")
    if payload.mode not in {"dry-run", "live-no-apply", "apply"}:
        raise HTTPException(400, f"invalid mode: {payload.mode!r}")
    provider = None if payload.mode == "dry-run" else VLMProvider(env=dict(os.environ))
    case_results: list[dict[str, Any]] = []
    totals = {"candidate_count": 0, "classified_count": 0, "skipped_count": 0, "error_count": 0}
    with db.connect() as conn:
        if payload.all_low_confidence:
            result = vlm_source_classifier.run_classification(
                conn,
                provider=provider,
                all_low_confidence=True,
                max_items=payload.max_items_per_case,
                mode=payload.mode,
                concurrency=payload.concurrency,
                timeout=payload.timeout_seconds,
                max_retries=payload.max_retries,
            )
            case_results.append(result)
            for key in totals:
                totals[key] += result.get(key, 0)
        else:
            valid_case_ids = []
            for cid in payload.case_ids:
                row = conn.execute(
                    "SELECT id FROM cases WHERE id = ? AND trashed_at IS NULL", (cid,),
                ).fetchone()
                if row:
                    valid_case_ids.append(cid)
                else:
                    case_results.append({"case_id": cid, "run_status": "case_not_found"})
            for cid in valid_case_ids:
                result = vlm_source_classifier.run_classification(
                    conn,
                    provider=provider,
                    case_id=cid,
                    max_items=payload.max_items_per_case,
                    mode=payload.mode,
                    concurrency=payload.concurrency,
                    timeout=payload.timeout_seconds,
                    max_retries=payload.max_retries,
                )
                case_results.append(result)
                for key in totals:
                    totals[key] += result.get(key, 0)
    return {
        "batch_status": "completed",
        "mode": payload.mode,
        "case_count": len(case_results),
        **totals,
        "results": case_results,
    }
