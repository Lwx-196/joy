"""Phase 2 research-only experiment runner (Tier 1/2/3 prompt v2 parser).

Schema and decision rule live in docs/vlm_judge_prompt_v2.md. This module
only parses the VLM response; it does NOT call any VLM API and does NOT
integrate with the production runner (backend/scripts/comfyui_vlm_judge_runner.py).

Fail-soft contract: malformed JSON returns TieredVerdict(winner_role="manual_review",
confidence=0.0). A parse failure must never auto-promote.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TieredVerdict:
    winner_role: str
    confidence: float
    tier1_flags: list[str] = field(default_factory=list)
    tier2_flags: list[str] = field(default_factory=list)
    tier3_flags: list[str] = field(default_factory=list)
    hard_veto_reason: str | None = None
    rationale: str = ""


def parse_tiered_response(raw: str) -> TieredVerdict:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return TieredVerdict(winner_role="manual_review", confidence=0.0)
    if not isinstance(payload, dict):
        return TieredVerdict(winner_role="manual_review", confidence=0.0)
    return TieredVerdict(
        winner_role=str(payload.get("winner_role") or "manual_review"),
        confidence=float(payload.get("confidence") or 0.0),
        tier1_flags=list(payload.get("tier1_flags") or []),
        tier2_flags=list(payload.get("tier2_flags") or []),
        tier3_flags=list(payload.get("tier3_flags") or []),
        hard_veto_reason=payload.get("hard_veto_reason"),
        rationale=str(payload.get("rationale") or ""),
    )
