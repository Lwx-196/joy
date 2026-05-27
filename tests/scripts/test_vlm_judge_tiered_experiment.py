"""Phase 2 research-only: parse_tiered_response unit tests.

Anchors:
- Tier 1 hard veto fires -> winner_role forced to baseline
- Tier 3 - Tier 2 positive -> winner_role candidate
- Malformed JSON -> manual_review fail-soft (never auto-promote)
"""
from __future__ import annotations

from backend.scripts.vlm_judge_tiered_experiment import parse_tiered_response


def test_tier1_fires_forces_baseline() -> None:
    raw = (
        '{"winner_role": "baseline", "confidence": 0.95, '
        '"tier1_flags": ["structural_change"], "tier2_flags": [], '
        '"tier3_flags": [], "hard_veto_reason": "structural_change: clothing differs", '
        '"rationale": "candidate changed black drape to white collar shirt"}'
    )
    v = parse_tiered_response(raw)
    assert v.winner_role == "baseline"
    assert v.hard_veto_reason and "structural_change" in v.hard_veto_reason
    assert v.tier1_flags == ["structural_change"]


def test_tier3_minus_tier2_positive_picks_candidate() -> None:
    raw = (
        '{"winner_role": "candidate", "confidence": 0.88, "tier1_flags": [], '
        '"tier2_flags": ["over_smoothing"], "tier3_flags": ["skin_improvement", '
        '"light_balance"], "hard_veto_reason": null, "rationale": ""}'
    )
    v = parse_tiered_response(raw)
    assert v.winner_role == "candidate"
    assert v.hard_veto_reason is None
    assert v.tier3_flags == ["skin_improvement", "light_balance"]
    assert v.tier2_flags == ["over_smoothing"]


def test_malformed_json_returns_manual_review() -> None:
    v = parse_tiered_response("not json")
    assert v.winner_role == "manual_review"
    assert v.confidence == 0.0
    assert v.tier1_flags == []
    assert v.tier2_flags == []
    assert v.tier3_flags == []
    assert v.hard_veto_reason is None
