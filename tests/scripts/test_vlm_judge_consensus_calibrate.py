"""Phase 0: consensus-aware calibration metrics."""
from __future__ import annotations

import json
from pathlib import Path

from backend.scripts.vlm_judge_consensus_calibrate import calibrate_with_consensus


def test_calibrate_with_consensus_emits_new_metrics(tmp_path: Path) -> None:
    judgments = [
        {
            "ab_unit_id": "case126:side",
            "human_winner": "baseline",
            "pro": {"winner_role": "candidate", "confidence": 1.0, "hard_veto_reason": None},
            "flash": {"winner_role": "baseline", "confidence": 0.88, "hard_veto_reason": None},
        },
        {
            "ab_unit_id": "case54:side",
            "human_winner": "baseline",
            "pro": {
                "winner_role": "baseline",
                "confidence": 0.95,
                "hard_veto_reason": "structural_change: clothing differs",
            },
            "flash": {"winner_role": "baseline", "confidence": 0.92, "hard_veto_reason": None},
        },
        {
            "ab_unit_id": "case10:front",
            "human_winner": "candidate",
            "pro": {"winner_role": "candidate", "confidence": 0.95, "hard_veto_reason": None},
            "flash": {"winner_role": "candidate", "confidence": 0.92, "hard_veto_reason": None},
        },
    ]
    out = tmp_path / "report.json"
    report = calibrate_with_consensus(
        judgments=judgments,
        confidence_cutoff=0.85,
        output_path=out,
    )

    assert report["consensus_disagreement_count"] == 1
    assert report["hard_veto_count"] == 1
    assert report["auto_promote_count"] == 1
    assert report["false_candidate_promotion_count_under_consensus"] == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["false_candidate_promotion_count_under_consensus"] == 0
