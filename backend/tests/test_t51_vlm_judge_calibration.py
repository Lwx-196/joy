"""T51 independent VLM judge calibration gates."""
from __future__ import annotations


def _manifest() -> dict:
    return {
        "scope": "t46_comfyui_human_review_packet_manifest_v1",
        "review_units": [
            {
                "ab_unit_id": "unit-1",
                "case_id": 101,
                "view": "front",
                "workflow": "background_cleanup_v1@conservative",
                "packet_assets": [
                    {
                        "role": "baseline",
                        "variant": "ps_model_router@default",
                        "packet_relative_path": "assets/unit-1/baseline.jpeg",
                        "source_path": "/tmp/unit-1/baseline.jpeg",
                        "status": "done",
                    },
                    {
                        "role": "candidate",
                        "variant": "comfyui_local:background_cleanup_v1@conservative",
                        "packet_relative_path": "assets/unit-1/candidate.png",
                        "source_path": "/tmp/unit-1/candidate.png",
                        "status": "done_with_issues",
                    },
                ],
                "ready_for_review": True,
            },
            {
                "ab_unit_id": "unit-2",
                "case_id": 102,
                "view": "front",
                "workflow": "local_region_enhance_v1@conservative",
                "packet_assets": [
                    {
                        "role": "baseline",
                        "variant": "ps_model_router@default",
                        "packet_relative_path": "assets/unit-2/baseline.jpeg",
                        "source_path": "/tmp/unit-2/baseline.jpeg",
                    },
                    {
                        "role": "candidate",
                        "variant": "comfyui_local:local_region_enhance_v1@conservative",
                        "packet_relative_path": "assets/unit-2/candidate.png",
                        "source_path": "/tmp/unit-2/candidate.png",
                    },
                ],
                "ready_for_review": True,
            },
            {
                "ab_unit_id": "unit-3",
                "case_id": 103,
                "view": "oblique",
                "workflow": "portrait_45_compare_v1@conservative",
                "packet_assets": [
                    {
                        "role": "baseline",
                        "variant": "ps_model_router@default",
                        "packet_relative_path": "assets/unit-3/baseline.jpeg",
                        "source_path": "/tmp/unit-3/baseline.jpeg",
                    },
                    {
                        "role": "candidate",
                        "variant": "comfyui_local:portrait_45_compare_v1@conservative",
                        "packet_relative_path": "assets/unit-3/candidate.png",
                        "source_path": "/tmp/unit-3/candidate.png",
                    },
                ],
                "ready_for_review": True,
            },
        ],
    }


def _human_decisions() -> dict:
    return {
        "scope": "t47_sanitized_comfyui_review_decisions_v1",
        "decisions": [
            {
                "ab_unit_id": "unit-1",
                "winner_role": "baseline",
                "winner_variant": "ps_model_router@default",
                "reviewer": "human-reviewer",
            },
            {
                "ab_unit_id": "unit-2",
                "winner_role": "baseline",
                "winner_variant": "ps_model_router@default",
                "reviewer": "human-reviewer",
            },
            {
                "ab_unit_id": "unit-3",
                "winner_role": "candidate",
                "winner_variant": "comfyui_local:portrait_45_compare_v1@conservative",
                "reviewer": "human-reviewer",
            },
        ],
    }


def test_vlm_judge_packet_is_blind_and_keeps_human_labels_separate() -> None:
    from backend.scripts import comfyui_vlm_judge_calibration

    packet = comfyui_vlm_judge_calibration.build_vlm_judge_packet(_manifest(), _human_decisions())

    assert packet["scope"] == "t51_independent_vlm_judge_packet_v1"
    assert len(packet["judge_items"]) == 3
    assert len(packet["human_labels"]) == 3

    item = packet["judge_items"][0]
    assert item["baseline"]["variant"] == "ps_model_router@default"
    assert item["candidate"]["variant"].startswith("comfyui_local:")
    assert "winner_role" not in item
    assert "winner_variant" not in item
    assert "human_winner_role" not in item
    assert "human_labels" not in item
    assert packet["human_labels"][0]["winner_role"] == "baseline"


def test_missing_real_vlm_results_blocks_calibration() -> None:
    from backend.scripts import comfyui_vlm_judge_calibration

    packet = comfyui_vlm_judge_calibration.build_vlm_judge_packet(_manifest(), _human_decisions())
    report = comfyui_vlm_judge_calibration.evaluate_vlm_calibration(packet, judge_results=None)

    assert report["calibration_status"] == "blocked_missing_real_vlm_judgments"
    assert report["accepted_judgment_count"] == 0
    assert report["production_gate"]["production_ready"] is False
    assert "未验证/无法获取" in report["decision"]


def test_vlm_results_compute_agreement_and_fail_closed_for_false_candidate_promotion() -> None:
    from backend.scripts import comfyui_vlm_judge_calibration

    packet = comfyui_vlm_judge_calibration.build_vlm_judge_packet(_manifest(), _human_decisions())
    judge_results = {
        "scope": "external_independent_vlm_judge_results_v1",
        "judgments": [
            {
                "ab_unit_id": "unit-1",
                "winner_role": "candidate",
                "judge_provider": "independent-vlm",
                "judge_model": "vision-quality-judge",
                "confidence": 0.83,
                "rationale": "Candidate looks cleaner.",
            },
            {
                "ab_unit_id": "unit-2",
                "winner_role": "baseline",
                "judge_provider": "independent-vlm",
                "judge_model": "vision-quality-judge",
                "confidence": 0.77,
                "rationale": "Baseline preserves detail.",
            },
            {
                "ab_unit_id": "unit-3",
                "winner_role": "candidate",
                "judge_provider": "independent-vlm",
                "judge_model": "vision-quality-judge",
                "confidence": 0.81,
                "rationale": "Candidate is preferred.",
            },
        ],
    }

    report = comfyui_vlm_judge_calibration.evaluate_vlm_calibration(
        packet,
        judge_results=judge_results,
        min_judgments=3,
        min_agreement=0.8,
    )

    assert report["accepted_judgment_count"] == 3
    assert report["agreement_count"] == 2
    assert report["agreement_rate"] == 0.6667
    assert report["false_candidate_promotion_count"] == 1
    assert report["calibration_status"] == "not_calibrated_fail_closed"
    assert report["production_gate"]["production_ready"] is False
    assert report["production_gate"]["promote_to_default"] is False


def test_vlm_results_can_calibrate_judge_without_promoting_candidate() -> None:
    from backend.scripts import comfyui_vlm_judge_calibration

    packet = comfyui_vlm_judge_calibration.build_vlm_judge_packet(_manifest(), _human_decisions())
    judge_results = {
        "judgments": [
            {
                "ab_unit_id": unit_id,
                "winner_role": winner,
                "judge_provider": "independent-vlm",
                "judge_model": "vision-quality-judge",
                "confidence": 0.9,
                "rationale": "Matches human review.",
            }
            for unit_id, winner in [("unit-1", "baseline"), ("unit-2", "baseline"), ("unit-3", "candidate")]
        ],
    }

    report = comfyui_vlm_judge_calibration.evaluate_vlm_calibration(
        packet,
        judge_results=judge_results,
        min_judgments=3,
        min_agreement=0.8,
    )

    assert report["calibration_status"] == "calibrated_for_fail_closed_review"
    assert report["judge_calibrated"] is True
    assert report["production_gate"]["production_ready"] is False
    assert report["production_gate"]["promote_to_default"] is False


def test_candidate_promotion_guardrail_requires_human_review_for_weak_subjective_evidence() -> None:
    from backend.scripts import comfyui_vlm_judge_calibration

    packet = comfyui_vlm_judge_calibration.build_vlm_judge_packet(_manifest(), _human_decisions())
    judge_results = {
        "judgments": [
            {
                "ab_unit_id": "unit-1",
                "winner_role": "baseline",
                "judge_provider": "independent-vlm",
                "judge_model": "vision-quality-judge",
                "confidence": 0.9,
                "rationale": "Baseline avoids masking artifacts.",
            },
            {
                "ab_unit_id": "unit-2",
                "winner_role": "baseline",
                "judge_provider": "independent-vlm",
                "judge_model": "vision-quality-judge",
                "confidence": 0.9,
                "rationale": "Baseline preserves natural texture.",
            },
            {
                "ab_unit_id": "unit-3",
                "winner_role": "candidate",
                "judge_provider": "independent-vlm",
                "judge_model": "vision-quality-judge",
                "confidence": 0.9,
                "rationale": (
                    "The candidate is slightly more refined and smoother. "
                    "All other aspects are equally good."
                ),
                "risk_flags": [],
            },
        ],
    }

    report = comfyui_vlm_judge_calibration.evaluate_vlm_calibration(
        packet,
        judge_results=judge_results,
        min_judgments=3,
        min_agreement=0.8,
    )

    guardrail = report["candidate_promotion_guardrail"]
    assert guardrail["guardrail_status"] == "manual_review_required"
    assert guardrail["manual_review_required_count"] == 1
    assert guardrail["candidate_quality_clearance_count"] == 0
    assert guardrail["items"][0]["action"] == "requires_human_review"
    assert guardrail["items"][0]["reason_code"] == "weak_subjective_candidate_evidence"
    assert report["production_gate"]["production_ready"] is False
    assert report["production_gate"]["promote_to_default"] is False


def test_candidate_promotion_guardrail_hard_vetoes_delivery_defects() -> None:
    from backend.scripts import comfyui_vlm_judge_calibration

    packet = comfyui_vlm_judge_calibration.build_vlm_judge_packet(_manifest(), _human_decisions())
    judge_results = {
        "judgments": [
            {
                "ab_unit_id": "unit-1",
                "winner_role": "baseline",
                "judge_provider": "independent-vlm",
                "judge_model": "vision-quality-judge",
                "confidence": 0.9,
                "rationale": "Baseline avoids masking artifacts.",
            },
            {
                "ab_unit_id": "unit-2",
                "winner_role": "baseline",
                "judge_provider": "independent-vlm",
                "judge_model": "vision-quality-judge",
                "confidence": 0.9,
                "rationale": "Baseline preserves natural texture.",
            },
            {
                "ab_unit_id": "unit-3",
                "winner_role": "candidate",
                "judge_provider": "independent-vlm",
                "judge_model": "vision-quality-judge",
                "confidence": 0.9,
                "rationale": "Candidate is correctly oriented but contains AI SIMULATION watermark and grey border.",
                "risk_flags": ["candidate image contains AI SIMULATION watermark", "grey border"],
            },
        ],
    }

    report = comfyui_vlm_judge_calibration.evaluate_vlm_calibration(
        packet,
        judge_results=judge_results,
        min_judgments=3,
        min_agreement=0.8,
    )

    guardrail = report["candidate_promotion_guardrail"]
    assert guardrail["guardrail_status"] == "hard_veto"
    assert guardrail["hard_veto_count"] == 1
    assert guardrail["candidate_quality_clearance_count"] == 0
    assert guardrail["items"][0]["action"] == "hard_veto"
    assert guardrail["items"][0]["reason_code"] == "candidate_delivery_defect"
    assert report["production_gate"]["production_ready"] is False
    assert report["production_gate"]["promote_to_default"] is False
