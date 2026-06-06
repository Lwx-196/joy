"""Phase 0 entrypoint: consensus-aware re-calibration.

Wraps the existing single-judge metrics with new consensus + hard-veto fields.
Does NOT replace `comfyui_vlm_judge_calibration.py`; this script reads the
combined pro+flash judgment file and emits the augmented report.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from backend.services.vlm_consensus_judge import (
    ConsensusMode,
    JudgeVerdict,
    decide_consensus,
)


def _verdict_from_payload(payload: dict[str, Any], role: str) -> JudgeVerdict:
    return JudgeVerdict(
        judge_role=role,
        winner_role=str(payload.get("winner_role") or "manual_review"),
        confidence=float(payload.get("confidence") or 0.0),
        hard_veto_reason=payload.get("hard_veto_reason") or None,
        risk_flags=list(payload.get("risk_flags") or []),
    )


def calibrate_with_consensus(
    judgments: Iterable[dict[str, Any]],
    *,
    confidence_cutoff: float = 0.85,
    output_path: Path | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    consensus_disagreement = 0
    hard_veto = 0
    auto_promote = 0
    fast_review = 0
    deep_review = 0
    false_candidate_under_consensus = 0

    for item in judgments:
        pro = _verdict_from_payload(item.get("pro") or {}, "gemini-2.5-pro")
        flash = _verdict_from_payload(item.get("flash") or {}, "gemini-2.5-flash")
        decision = decide_consensus(pro, flash, confidence_cutoff=confidence_cutoff)
        human_winner = str(item.get("human_winner") or "").strip()

        if decision.mode is ConsensusMode.MANUAL_REVIEW_DEEP:
            consensus_disagreement += 1
            deep_review += 1
        elif decision.mode is ConsensusMode.MANUAL_REVIEW_FAST:
            fast_review += 1
        elif decision.mode is ConsensusMode.HARD_VETO:
            hard_veto += 1
        elif decision.mode is ConsensusMode.AUTO_PROMOTE:
            auto_promote += 1
            if human_winner == "baseline":
                false_candidate_under_consensus += 1

        rows.append(
            {
                "ab_unit_id": item.get("ab_unit_id"),
                "human_winner": human_winner,
                "consensus_mode": decision.mode.value,
                "consensus_winner": decision.winner_role,
                "consensus_reason": decision.reason,
            }
        )

    report = {
        "accepted_judgments": len(rows),
        "confidence_cutoff": confidence_cutoff,
        "auto_promote_count": auto_promote,
        "manual_review_fast_count": fast_review,
        "manual_review_deep_count": deep_review,
        "hard_veto_count": hard_veto,
        "consensus_disagreement_count": consensus_disagreement,
        "false_candidate_promotion_count_under_consensus": false_candidate_under_consensus,
        "rows": rows,
    }

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 0 consensus-aware calibration")
    parser.add_argument("--judgments", required=True, type=Path, help="JSON file: list of pro+flash+human triplets")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--confidence-cutoff", type=float, default=0.85)
    args = parser.parse_args()

    judgments = json.loads(args.judgments.read_text(encoding="utf-8"))
    if isinstance(judgments, dict):
        judgments = judgments.get("judgments") or judgments.get("rows") or []
    calibrate_with_consensus(
        judgments=judgments,
        confidence_cutoff=args.confidence_cutoff,
        output_path=args.output,
    )


if __name__ == "__main__":
    main()
