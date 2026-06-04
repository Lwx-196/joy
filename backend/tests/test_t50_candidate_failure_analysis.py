"""T50 ComfyUI candidate failure analysis from real human-review decisions."""
from __future__ import annotations


def _records() -> list[dict]:
    records: list[dict] = []
    for index in range(24):
        workflow = "local_region_enhance_v1@conservative" if index < 8 else "background_cleanup_v1@conservative"
        unit_id = f"unit-{index}"
        records.append(
            {
                "ab_unit_id": unit_id,
                "variant_role": "baseline",
                "variant": "ps_model_router@default",
                "workflow": workflow,
                "status": "done",
                "ok": True,
                "dry_run": False,
            }
        )
        records.append(
            {
                "ab_unit_id": unit_id,
                "variant_role": "candidate",
                "variant": f"comfyui_local:{workflow}",
                "workflow": workflow,
                "status": "done_with_issues",
                "ok": True,
                "dry_run": False,
                "qa_scores": {"dimension_match": True, "halo_score": 9.1 if index == 0 else 2.0},
            }
        )
    return records


def _decisions() -> list[dict]:
    decisions: list[dict] = []
    for index in range(24):
        workflow = "local_region_enhance_v1@conservative" if index < 8 else "background_cleanup_v1@conservative"
        role = "candidate" if index < 4 else "baseline"
        decisions.append(
            {
                "ab_unit_id": f"unit-{index}",
                "workflow": workflow,
                "winner_role": role,
                "winner_variant": f"comfyui_local:{workflow}" if role == "candidate" else "ps_model_router@default",
                "reviewer": "real-reviewer",
                "review_note": None,
            }
        )
    return decisions


def test_failure_analysis_blocks_delivery_when_candidate_wins_are_below_threshold() -> None:
    from backend.scripts import comfyui_candidate_failure_analysis

    analysis = comfyui_candidate_failure_analysis.analyze_candidate_failures(
        _records(),
        _decisions(),
        min_candidate_wins=20,
    )

    assert analysis["delivery_gate"]["production_ready"] is False
    assert analysis["delivery_gate"]["reason_code"] == "candidate_wins_below_threshold"
    assert analysis["candidate_win_count"] == 4
    assert analysis["baseline_win_count"] == 20
    by_workflow = {item["workflow"]: item for item in analysis["workflow_summary"]}
    assert by_workflow["local_region_enhance_v1@conservative"]["candidate_win_count"] == 4
    assert by_workflow["background_cleanup_v1@conservative"]["candidate_win_count"] == 0


def test_failure_analysis_does_not_fabricate_visual_reasons_without_review_notes() -> None:
    from backend.scripts import comfyui_candidate_failure_analysis

    decisions = _decisions()
    decisions[0]["review_note"] = "j"

    analysis = comfyui_candidate_failure_analysis.analyze_candidate_failures(_records(), decisions)

    assert analysis["visual_failure_reason_status"] == "未验证/无法获取"
    assert analysis["human_review_note_count"] == 0
    assert all(
        item["visual_failure_reason_status"] == "未验证/无法获取"
        for item in analysis["workflow_summary"]
    )


def test_failure_analysis_recommends_baseline_fallback_tuning_and_vlm_judge() -> None:
    from backend.scripts import comfyui_candidate_failure_analysis

    analysis = comfyui_candidate_failure_analysis.analyze_candidate_failures(_records(), _decisions())
    by_workflow = {item["workflow"]: item for item in analysis["workflow_summary"]}

    assert analysis["strategy"]["default_policy"] == "fallback_to_baseline"
    assert analysis["strategy"]["vlm_judge"] == "required_before_future_promotion"
    assert by_workflow["background_cleanup_v1@conservative"]["recommended_action"] == "fallback_baseline_disable_candidate"
    assert by_workflow["local_region_enhance_v1@conservative"]["recommended_action"] == "tune_workflow_then_retest"
    assert by_workflow["local_region_enhance_v1@conservative"]["candidate_status_counts"]["done_with_issues"] == 8
    assert by_workflow["local_region_enhance_v1@conservative"]["qa_warning_counts"]["halo_score"] == 1
