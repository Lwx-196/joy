"""Enhanced classification routes — multi-tier fusion pipeline."""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..services.enhanced_classifier import fetch_case_observations, run_enhanced_classification
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
