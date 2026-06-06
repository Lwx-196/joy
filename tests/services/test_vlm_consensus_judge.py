"""Phase 0: pro+flash consensus + Tier 1 hard fail + Mode C fail-closed."""
from __future__ import annotations

import pytest

from backend.services.vlm_consensus_judge import (
    ConsensusDecision,
    ConsensusMode,
    JudgeVerdict,
    decide_consensus,
)


def _verdict(
    role: str,
    winner: str,
    confidence: float,
    hard_veto_reason: str | None = None,
    risk_flags: list[str] | None = None,
) -> JudgeVerdict:
    return JudgeVerdict(
        judge_role=role,
        winner_role=winner,
        confidence=confidence,
        hard_veto_reason=hard_veto_reason,
        risk_flags=risk_flags or [],
    )


def test_both_agree_candidate_high_confidence_auto_promotes() -> None:
    pro = _verdict("gemini-2.5-pro", "candidate", 0.95)
    flash = _verdict("gemini-2.5-flash", "candidate", 0.92)
    result = decide_consensus(pro, flash, confidence_cutoff=0.85)
    assert result == ConsensusDecision(
        mode=ConsensusMode.AUTO_PROMOTE,
        winner_role="candidate",
        reason="both judges agree candidate, min_confidence=0.92 >= cutoff=0.85",
    )


def test_disagreement_forces_mode_c_human_review() -> None:
    pro = _verdict("gemini-2.5-pro", "candidate", 0.99)
    flash = _verdict("gemini-2.5-flash", "baseline", 0.88)
    result = decide_consensus(pro, flash, confidence_cutoff=0.85)
    assert result.mode is ConsensusMode.MANUAL_REVIEW_DEEP
    assert result.winner_role == "manual_review"
    assert "disagreement" in result.reason


def test_hard_veto_from_any_judge_forces_baseline_win() -> None:
    pro = _verdict(
        "gemini-2.5-pro",
        "candidate",
        1.0,
        hard_veto_reason="structural_change: clothing differs",
    )
    flash = _verdict("gemini-2.5-flash", "candidate", 0.91)
    result = decide_consensus(pro, flash, confidence_cutoff=0.85)
    assert result.mode is ConsensusMode.HARD_VETO
    assert result.winner_role == "baseline"
    assert "structural_change" in result.reason


def test_confidence_below_cutoff_blocks_auto_promote() -> None:
    pro = _verdict("gemini-2.5-pro", "candidate", 0.82)
    flash = _verdict("gemini-2.5-flash", "candidate", 0.80)
    result = decide_consensus(pro, flash, confidence_cutoff=0.85)
    assert result.mode is ConsensusMode.MANUAL_REVIEW_FAST
    assert result.winner_role == "manual_review"
    assert "below cutoff" in result.reason


def test_case126_side_fail_closed_regression() -> None:
    """Anchor: 2026-05-19 校准里唯一 false_candidate_promotion 案例。"""
    pro = _verdict(
        "gemini-2.5-pro",
        "candidate",
        1.0,
        risk_flags=["subtle_skin_retouching_misread_as_value"],
    )
    flash = _verdict("gemini-2.5-flash", "baseline", 0.88)
    result = decide_consensus(pro, flash, confidence_cutoff=0.85)
    assert result.mode is ConsensusMode.MANUAL_REVIEW_DEEP
    assert result.winner_role != "candidate"
