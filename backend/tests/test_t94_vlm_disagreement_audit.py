"""T94 VLM disagreement review packet audit."""
from __future__ import annotations

from pathlib import Path


def _write_real_png(path: Path) -> None:
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def test_disagreement_audit_packet_uses_real_assets_and_does_not_prefill_winner(tmp_path: Path) -> None:
    from backend.scripts import comfyui_vlm_disagreement_audit

    baseline = tmp_path / "baseline.png"
    candidate = tmp_path / "candidate.png"
    _write_real_png(baseline)
    _write_real_png(candidate)
    packet = {
        "judge_items": [
            {
                "ab_unit_id": "case126:side:local_region_enhance_v1@conservative",
                "case_id": 126,
                "view": "side",
                "workflow": "local_region_enhance_v1@conservative",
                "baseline": {"packet_path": str(baseline), "variant": "ps_model_router@default"},
                "candidate": {
                    "packet_path": str(candidate),
                    "variant": "comfyui_local:local_region_enhance_v1@conservative",
                },
            }
        ]
    }
    calibration = {
        "accepted_judgments": [
            {
                "ab_unit_id": "case126:side:local_region_enhance_v1@conservative",
                "case_id": 126,
                "view": "side",
                "workflow": "local_region_enhance_v1@conservative",
                "human_winner_role": "baseline",
                "judge_winner_role": "candidate",
                "rationale": "Candidate is slightly smoother.",
                "risk_flags": [],
            }
        ],
        "candidate_promotion_guardrail": {
            "items": [
                {
                    "ab_unit_id": "case126:side:local_region_enhance_v1@conservative",
                    "action": "requires_human_review",
                    "reason_code": "weak_subjective_candidate_evidence",
                }
            ]
        },
    }

    review = comfyui_vlm_disagreement_audit.build_disagreement_review_packet(
        calibration,
        packet,
        packet_root=tmp_path,
        max_items=10,
    )

    assert review["review_unit_count"] == 1
    unit = review["review_units"][0]
    assert unit["ab_unit_id"] == "case126:side:local_region_enhance_v1@conservative"
    assert unit["ready_for_review"] is True
    assert "winner_role" not in unit
    assert unit["baseline"]["sha256"].startswith("sha256:")
    assert unit["candidate"]["sha256"].startswith("sha256:")
    assert unit["disagreement_type"] == "false_candidate_promotion"


def test_disagreement_audit_dry_run_import_requires_real_reviewer_and_winner() -> None:
    from backend.scripts import comfyui_vlm_disagreement_audit

    review = {
        "review_units": [
            {"ab_unit_id": "unit-1"},
            {"ab_unit_id": "unit-2"},
        ]
    }
    decisions = {
        "decisions": [
            {"ab_unit_id": "unit-1", "winner_role": "candidate", "reviewer": "joy"},
            {"ab_unit_id": "unit-2", "winner_role": "candidate", "reviewer": ""},
            {"ab_unit_id": "unit-3", "winner_role": "baseline", "reviewer": "joy"},
        ]
    }

    report = comfyui_vlm_disagreement_audit.validate_review_decisions_dry_run(review, decisions)

    assert report["validation_status"] == "blocked"
    assert report["accepted_decision_count"] == 1
    assert report["rejected_decision_count"] == 2
    assert {item["reason_code"] for item in report["rejected_decisions"]} == {
        "missing_reviewer",
        "unknown_review_unit",
    }


def test_disagreement_audit_dry_run_accepts_manual_review_without_prefilling_winner() -> None:
    from backend.scripts import comfyui_vlm_disagreement_audit

    review = {"review_units": [{"ab_unit_id": "case87:oblique:local_region_enhance_v1@conservative"}]}
    decisions = {
        "decisions": [
            {
                "ab_unit_id": "case87:oblique:local_region_enhance_v1@conservative",
                "winner_role": "manual_review",
                "reviewer": "lead-reviewer",
                "review_note": "二次复核仍无法安全归入 baseline/candidate。",
            }
        ]
    }

    report = comfyui_vlm_disagreement_audit.validate_review_decisions_dry_run(review, decisions)

    assert report["validation_status"] == "ready_for_report"
    assert report["accepted_decision_count"] == 1
    assert report["manual_review_count"] == 1
    assert report["candidate_win_count"] == 0
    assert report["baseline_win_count"] == 0
    assert report["accepted_decisions"][0]["winner_role"] == "manual_review"
