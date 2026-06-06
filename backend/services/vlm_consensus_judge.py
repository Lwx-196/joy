"""Phase 0 fail-closed consensus judge.

Combines pro + flash verdicts into a single decision under four modes:
- AUTO_PROMOTE: both judges agree on candidate AND min confidence >= cutoff AND no hard veto
- MANUAL_REVIEW_FAST: judges agree on candidate BUT min confidence < cutoff
- MANUAL_REVIEW_DEEP: judges disagree
- HARD_VETO: any judge raises hard_veto_reason -> baseline wins
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ConsensusMode(str, Enum):
    AUTO_PROMOTE = "auto_promote"
    MANUAL_REVIEW_FAST = "manual_review_fast"
    MANUAL_REVIEW_DEEP = "manual_review_deep"
    HARD_VETO = "hard_veto"


@dataclass(frozen=True)
class JudgeVerdict:
    judge_role: str
    winner_role: str  # "candidate" | "baseline" | "tie" | "manual_review"
    confidence: float
    hard_veto_reason: str | None = None
    risk_flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ConsensusDecision:
    mode: ConsensusMode
    winner_role: str  # "candidate" | "baseline" | "manual_review"
    reason: str


def decide_consensus(
    pro: JudgeVerdict,
    flash: JudgeVerdict,
    *,
    confidence_cutoff: float = 0.85,
) -> ConsensusDecision:
    if pro.hard_veto_reason:
        return ConsensusDecision(
            mode=ConsensusMode.HARD_VETO,
            winner_role="baseline",
            reason=f"pro hard_veto: {pro.hard_veto_reason}",
        )
    if flash.hard_veto_reason:
        return ConsensusDecision(
            mode=ConsensusMode.HARD_VETO,
            winner_role="baseline",
            reason=f"flash hard_veto: {flash.hard_veto_reason}",
        )

    if pro.winner_role != flash.winner_role:
        return ConsensusDecision(
            mode=ConsensusMode.MANUAL_REVIEW_DEEP,
            winner_role="manual_review",
            reason=(
                f"disagreement: pro={pro.winner_role}@{pro.confidence:.2f} "
                f"vs flash={flash.winner_role}@{flash.confidence:.2f}"
            ),
        )

    min_conf = min(pro.confidence, flash.confidence)
    if min_conf < confidence_cutoff:
        return ConsensusDecision(
            mode=ConsensusMode.MANUAL_REVIEW_FAST,
            winner_role="manual_review",
            reason=(
                f"both agree {pro.winner_role} but min_confidence={min_conf:.2f} "
                f"below cutoff={confidence_cutoff}"
            ),
        )

    return ConsensusDecision(
        mode=ConsensusMode.AUTO_PROMOTE if pro.winner_role == "candidate" else ConsensusMode.HARD_VETO,
        winner_role=pro.winner_role,
        reason=(
            f"both judges agree {pro.winner_role}, "
            f"min_confidence={min_conf:.2f} >= cutoff={confidence_cutoff}"
        ),
    )
