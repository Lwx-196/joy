"""T90 ComfyUI production-candidate quality gate."""
from __future__ import annotations

from pathlib import Path


def _record(unit: str, workflow: str, role: str, **overrides) -> dict:
    variant = "ps_model_router@default" if role == "baseline" else f"comfyui_local:{workflow}"
    base = {
        "ab_unit_id": unit,
        "case_id": 100,
        "workflow": workflow,
        "variant_role": role,
        "variant": variant,
        "status": "done",
        "ok": True,
        "dry_run": False,
        "qa_scores": {
            "dimension_match": True,
            "halo_score": 1.0,
            "mask_outside_delta": 1.0,
            "subject_scale_delta": 0.0,
            "slot_center_delta": 0.0,
        },
    }
    base.update(overrides)
    return base


def _decision(unit: str, workflow: str, winner_role: str) -> dict:
    return {
        "ab_unit_id": unit,
        "workflow": workflow,
        "winner_role": winner_role,
        "winner_variant": (
            f"comfyui_local:{workflow}"
            if winner_role == "candidate"
            else "ps_model_router@default"
        ),
        "reviewer": "real-reviewer",
    }


def test_gate_only_allows_local_region_for_repair_and_retest() -> None:
    from backend.scripts import comfyui_production_quality_gate

    records = [
        _record("local-1", "local_region_enhance_v1@conservative", "baseline"),
        _record("local-1", "local_region_enhance_v1@conservative", "candidate"),
        _record("background-1", "background_cleanup_v1@conservative", "baseline"),
        _record("background-1", "background_cleanup_v1@conservative", "candidate"),
        _record("portrait-1", "portrait_front_compare_v1@conservative", "baseline"),
        _record(
            "portrait-1",
            "portrait_front_compare_v1@conservative",
            "candidate",
            status="failed",
            ok=False,
            error_message="blank gray output",
            output_refs=[],
        ),
    ]
    decisions = [
        _decision("local-1", "local_region_enhance_v1@conservative", "candidate"),
        _decision("background-1", "background_cleanup_v1@conservative", "baseline"),
        _decision("portrait-1", "portrait_front_compare_v1@conservative", "baseline"),
    ]

    gate = comfyui_production_quality_gate.build_gate_report(records, decisions, vlm_guardrail={})

    assert gate["production_gate"]["production_ready"] is False
    assert gate["formal_chain_policy"]["integration_mode"] == "candidate_layer_only_with_baseline_fallback"
    assert gate["next_retest_plan"]["allowed_workflows"] == ["local_region_enhance_v1@conservative"]
    by_workflow = {item["workflow"]: item for item in gate["workflow_summary"]}
    assert by_workflow["local_region_enhance_v1@conservative"]["recommended_action"] == "repair_then_retest"
    assert by_workflow["background_cleanup_v1@conservative"]["recommended_action"] == "disable_candidate_keep_baseline"
    assert by_workflow["portrait_front_compare_v1@conservative"]["recommended_action"] == "disable_candidate_hard_defects"
    assert "candidate_failed_or_blank" in by_workflow["portrait_front_compare_v1@conservative"]["hard_defect_codes"]


def test_gate_blocks_promotion_on_vlm_false_candidate_promotion_even_with_candidate_wins() -> None:
    from backend.scripts import comfyui_production_quality_gate

    workflow = "local_region_enhance_v1@conservative"
    records: list[dict] = []
    decisions: list[dict] = []
    for index in range(20):
        unit = f"unit-{index}"
        records.append(_record(unit, workflow, "baseline"))
        records.append(_record(unit, workflow, "candidate"))
        decisions.append(_decision(unit, workflow, "candidate"))

    gate = comfyui_production_quality_gate.build_gate_report(
        records,
        decisions,
        vlm_guardrail={
            "calibration_status": "not_calibrated_fail_closed",
            "false_candidate_promotion_count": 1,
            "candidate_promotion_guardrail": {
                "guardrail_status": "hard_veto",
                "items": [
                    {
                        "ab_unit_id": "unit-0",
                        "workflow": workflow,
                        "action": "hard_veto",
                        "risk_flags": ["candidate image contains AI SIMULATION watermark", "gray border"],
                    }
                ],
            },
        },
    )

    assert gate["production_gate"]["production_ready"] is False
    assert gate["production_gate"]["reason_code"] == "vlm_guardrail_fail_closed"
    assert gate["vlm_guardrail"]["false_candidate_promotion_count"] == 1
    by_workflow = {item["workflow"]: item for item in gate["workflow_summary"]}
    assert "vlm_false_candidate_promotion" in by_workflow[workflow]["hard_defect_codes"]
    assert "watermark_or_gray_border" in by_workflow[workflow]["hard_defect_codes"]


def test_gate_blocks_missing_real_vlm_judgments_even_with_candidate_wins() -> None:
    from backend.scripts import comfyui_production_quality_gate

    workflow = "local_region_enhance_v1@conservative"
    records: list[dict] = []
    decisions: list[dict] = []
    for index in range(30):
        unit = f"unit-{index}"
        records.append(_record(unit, workflow, "baseline"))
        records.append(_record(unit, workflow, "candidate"))
        decisions.append(_decision(unit, workflow, "candidate"))

    gate = comfyui_production_quality_gate.build_gate_report(
        records,
        decisions,
        vlm_guardrail={
            "calibration_status": "blocked_missing_real_vlm_judgments",
            "false_candidate_promotion_count": 0,
            "candidate_promotion_guardrail": {"guardrail_status": "no_candidate_wins", "items": []},
        },
    )

    assert gate["production_gate"]["production_ready"] is False
    assert gate["production_gate"]["promote_to_default"] is False
    assert gate["production_gate"]["reason_code"] == "vlm_guardrail_fail_closed"
    assert gate["production_gate"]["candidate_win_count"] == 30


def test_gate_surfaces_vlm_false_baseline_rejection_as_candidate_repair_defects() -> None:
    from backend.scripts import comfyui_production_quality_gate

    workflow = "local_region_enhance_v1@conservative"
    records = [
        _record("unit-fbr", workflow, "baseline"),
        _record("unit-fbr", workflow, "candidate"),
    ]
    decisions = [_decision("unit-fbr", workflow, "candidate")]

    gate = comfyui_production_quality_gate.build_gate_report(
        records,
        decisions,
        vlm_guardrail={
            "calibration_status": "not_calibrated_fail_closed",
            "false_candidate_promotion_count": 0,
            "false_baseline_rejection_count": 1,
            "accepted_judgments": [
                {
                    "ab_unit_id": "unit-fbr",
                    "workflow": workflow,
                    "false_baseline_rejection": True,
                    "risk_flags": ["color_shift", "facial_feature_drift"],
                    "visual_evidence_summary": "candidate has red cast and lip alteration",
                }
            ],
        },
    )

    by_workflow = {item["workflow"]: item for item in gate["workflow_summary"]}
    codes = set(by_workflow[workflow]["hard_defect_codes"])
    assert "vlm_false_baseline_rejection" in codes
    assert "tone_color_shift" in codes
    assert "identity_or_feature_drift" in codes


def test_gate_blocks_candidate_records_with_weak_visible_improvement() -> None:
    from backend.scripts import comfyui_production_quality_gate

    workflow = "local_region_enhance_v1@conservative"
    records = [
        _record("weak-unit", workflow, "baseline"),
        _record(
            "weak-unit",
            workflow,
            "candidate",
            difference_analysis={"target_region_change_score": 0.42, "non_target_change_score": 0.01},
        ),
    ]
    decisions = [_decision("weak-unit", workflow, "candidate")]

    gate = comfyui_production_quality_gate.build_gate_report(records, decisions, vlm_guardrail={})
    by_workflow = {item["workflow"]: item for item in gate["workflow_summary"]}

    assert "candidate_weak_visible_improvement" in by_workflow[workflow]["hard_defect_codes"]
    assert gate["production_gate"]["reason_code"] == "hard_defects_present"


def test_gate_does_not_turn_positive_vlm_rationale_into_hard_defect() -> None:
    from backend.scripts import comfyui_production_quality_gate

    workflow = "local_region_enhance_v1@conservative"
    records = [
        _record("unit-0", workflow, "baseline"),
        _record("unit-0", workflow, "candidate"),
    ]
    decisions = [_decision("unit-0", workflow, "candidate")]

    gate = comfyui_production_quality_gate.build_gate_report(
        records,
        decisions,
        vlm_guardrail={
            "calibration_status": "calibrated_for_fail_closed_review",
            "false_candidate_promotion_count": 0,
            "candidate_promotion_guardrail": {
                "guardrail_status": "candidate_quality_evidence_supported",
                "items": [
                    {
                        "ab_unit_id": "unit-0",
                        "workflow": workflow,
                        "action": "candidate_quality_evidence_supported",
                        "rationale": "The candidate is free of major editing artifacts and does not look waxy.",
                        "risk_flags": [],
                    }
                ],
            },
        },
    )

    by_workflow = {item["workflow"]: item for item in gate["workflow_summary"]}

    assert by_workflow[workflow]["hard_defect_codes"] == []


def test_gate_reaches_promotion_approval_required_only_after_calibrated_guardrail_and_candidate_wins() -> None:
    from backend.scripts import comfyui_production_quality_gate

    workflow = "local_region_enhance_v1@conservative"
    records: list[dict] = []
    decisions: list[dict] = []
    guardrail_items: list[dict] = []
    for index in range(20):
        unit = f"unit-{index}"
        records.append(_record(unit, workflow, "baseline"))
        records.append(_record(unit, workflow, "candidate"))
        decisions.append(_decision(unit, workflow, "candidate"))
        guardrail_items.append(
            {
                "ab_unit_id": unit,
                "workflow": workflow,
                "judge_winner_role": "candidate",
                "action": "candidate_quality_evidence_supported",
                "rationale": "Candidate reduces visible redness and blemishes without artifacts.",
                "risk_flags": [],
            }
        )

    gate = comfyui_production_quality_gate.build_gate_report(
        records,
        decisions,
        vlm_guardrail={
            "calibration_status": "calibrated_for_fail_closed_review",
            "false_candidate_promotion_count": 0,
            "false_baseline_rejection_count": 0,
            "candidate_promotion_guardrail": {
                "guardrail_status": "candidate_quality_evidence_supported",
                "items": guardrail_items,
            },
        },
    )

    assert gate["production_gate"]["production_ready"] is False
    assert gate["production_gate"]["promote_to_default"] is False
    assert gate["production_gate"]["reason_code"] == "promotion_approval_required"
    assert gate["production_gate"]["candidate_win_count"] == 20


def test_gate_marks_missing_blank_and_size_mismatch_candidate_outputs_as_hard_defects(tmp_path: Path) -> None:
    from PIL import Image

    from backend.scripts import comfyui_production_quality_gate

    workflow = "local_region_enhance_v1@conservative"
    blank = tmp_path / "blank.png"
    Image.new("RGB", (16, 16), color=(0, 0, 0)).save(blank)
    missing = tmp_path / "missing.png"
    records = [
        _record("blank-unit", workflow, "baseline"),
        _record(
            "blank-unit",
            workflow,
            "candidate",
            output_refs=[{"kind": "generated_raw", "path": str(blank)}],
        ),
        _record("missing-unit", workflow, "baseline"),
        _record(
            "missing-unit",
            workflow,
            "candidate",
            output_refs=[{"kind": "generated_raw", "path": str(missing)}],
        ),
        _record("size-unit", workflow, "baseline"),
        _record(
            "size-unit",
            workflow,
            "candidate",
            output_refs=[],
            qa_scores={
                "dimension_match": True,
                "output_width": 32,
                "output_height": 16,
                "canvas_width": 16,
                "canvas_height": 16,
                "halo_score": 1,
                "mask_outside_delta": 1,
            },
        ),
    ]
    decisions = [
        _decision("blank-unit", workflow, "candidate"),
        _decision("missing-unit", workflow, "candidate"),
        _decision("size-unit", workflow, "candidate"),
    ]

    gate = comfyui_production_quality_gate.build_gate_report(records, decisions, vlm_guardrail={})
    by_workflow = {item["workflow"]: item for item in gate["workflow_summary"]}

    assert "candidate_failed_or_blank" in by_workflow[workflow]["hard_defect_codes"]
    assert "dimension_mismatch" in by_workflow[workflow]["hard_defect_codes"]
    assert gate["production_gate"]["reason_code"] == "hard_defects_present"


def test_gate_uses_latest_record_when_retry_supersedes_failed_candidate(tmp_path: Path) -> None:
    from PIL import Image

    from backend.scripts import comfyui_production_quality_gate

    workflow = "local_region_enhance_v1@conservative"
    candidate = tmp_path / "candidate.png"
    image = Image.new("RGB", (16, 16), color=(1, 2, 3))
    image.putpixel((0, 0), (9, 8, 7))
    image.save(candidate)
    records = [
        _record("retry-unit", workflow, "baseline"),
        _record(
            "retry-unit",
            workflow,
            "candidate",
            status="failed",
            ok=False,
            error_message="VAEEncodeForInpaint failed",
            output_refs=[],
        ),
        _record(
            "retry-unit",
            workflow,
            "candidate",
            status="done",
            ok=True,
            error_message=None,
            output_refs=[{"kind": "generated_raw", "path": str(candidate)}],
            qa_scores={
                "output_width": 16,
                "output_height": 16,
                "canvas_width": 16,
                "canvas_height": 16,
                "halo_score": 0.2,
                "mask_outside_delta": 0.1,
            },
        ),
    ]
    decisions = [_decision("retry-unit", workflow, "candidate")]

    gate = comfyui_production_quality_gate.build_gate_report(records, decisions, vlm_guardrail={})
    by_workflow = {item["workflow"]: item for item in gate["workflow_summary"]}

    assert gate["record_policy"]["superseded_record_count"] == 1
    assert by_workflow[workflow]["hard_defect_codes"] == []
    assert by_workflow[workflow]["comparable_unit_count"] == 1


def test_gate_blocks_candidate_darkening_and_texture_loss() -> None:
    from backend.scripts import comfyui_production_quality_gate

    workflow = "local_region_enhance_v1@conservative"
    records = [
        _record("tone-texture-unit", workflow, "baseline"),
        _record(
            "tone-texture-unit",
            workflow,
            "candidate",
            qa_scores={
                "output_width": 16,
                "output_height": 16,
                "canvas_width": 16,
                "canvas_height": 16,
                "halo_score": 0.2,
                "mask_outside_delta": 0.1,
                "masked_luma_delta": -9.0,
                "texture_detail_delta": -7.0,
            },
        ),
    ]
    decisions = [_decision("tone-texture-unit", workflow, "candidate")]

    gate = comfyui_production_quality_gate.build_gate_report(records, decisions, vlm_guardrail={})
    by_workflow = {item["workflow"]: item for item in gate["workflow_summary"]}

    codes = set(by_workflow[workflow]["hard_defect_codes"])
    assert "tone_color_shift" in codes
    assert "over_smoothing" in codes
    assert gate["production_gate"]["reason_code"] == "hard_defects_present"


def test_gate_blocks_over_contoured_lower_face_shadow() -> None:
    from backend.scripts import comfyui_production_quality_gate

    workflow = "local_region_enhance_v1@conservative"
    records = [
        _record("shadow-unit", workflow, "baseline"),
        _record(
            "shadow-unit",
            workflow,
            "candidate",
            qa_scores={
                "output_width": 16,
                "output_height": 16,
                "canvas_width": 16,
                "canvas_height": 16,
                "halo_score": 0.2,
                "mask_outside_delta": 0.1,
                "masked_luma_delta": 2.0,
                "texture_detail_delta": 2.0,
                "masked_shadow_contrast_delta": 18.0,
                "masked_shadow_p10_delta": -1.5,
            },
        ),
    ]
    decisions = [_decision("shadow-unit", workflow, "candidate")]

    gate = comfyui_production_quality_gate.build_gate_report(records, decisions, vlm_guardrail={})
    by_workflow = {item["workflow"]: item for item in gate["workflow_summary"]}

    assert "over_contoured_shadow" in set(by_workflow[workflow]["hard_defect_codes"])
    assert gate["production_gate"]["reason_code"] == "hard_defects_present"


def test_gate_blocks_waxy_highlight_and_specular_overprocessing() -> None:
    from backend.scripts import comfyui_production_quality_gate

    workflow = "local_region_enhance_v1@conservative"
    records = [
        _record("waxy-highlight-unit", workflow, "baseline"),
        _record(
            "waxy-highlight-unit",
            workflow,
            "candidate",
            qa_scores={
                "output_width": 16,
                "output_height": 16,
                "canvas_width": 16,
                "canvas_height": 16,
                "halo_score": 0.2,
                "mask_outside_delta": 0.1,
                "masked_luma_delta": 4.0,
                "texture_detail_delta": -1.5,
                "masked_highlight_p95_delta": 14.0,
                "masked_specular_ratio_delta": 0.05,
            },
        ),
    ]
    decisions = [_decision("waxy-highlight-unit", workflow, "candidate")]

    gate = comfyui_production_quality_gate.build_gate_report(records, decisions, vlm_guardrail={})
    by_workflow = {item["workflow"]: item for item in gate["workflow_summary"]}

    assert "over_waxy_highlight" in set(by_workflow[workflow]["hard_defect_codes"])
    assert gate["production_gate"]["reason_code"] == "hard_defects_present"


def test_gate_blocks_local_highlight_smudge_artifact() -> None:
    from backend.scripts import comfyui_production_quality_gate

    workflow = "local_region_enhance_v1@conservative"
    records = [
        _record("small-highlight-unit", workflow, "baseline"),
        _record(
            "small-highlight-unit",
            workflow,
            "candidate",
            qa_scores={
                "output_width": 16,
                "output_height": 16,
                "canvas_width": 16,
                "canvas_height": 16,
                "halo_score": 0.2,
                "mask_outside_delta": 0.1,
                "masked_luma_delta": 1.5,
                "texture_detail_delta": -0.5,
                "masked_highlight_p95_delta": 2.0,
                "masked_highlight_p99_delta": 22.0,
                "masked_specular_ratio_delta": 0.005,
            },
        ),
    ]
    decisions = [_decision("small-highlight-unit", workflow, "candidate")]

    gate = comfyui_production_quality_gate.build_gate_report(records, decisions, vlm_guardrail={})
    by_workflow = {item["workflow"]: item for item in gate["workflow_summary"]}

    assert "local_highlight_artifact" in set(by_workflow[workflow]["hard_defect_codes"])
    assert gate["production_gate"]["reason_code"] == "hard_defects_present"


def test_gate_blocks_face_luma_contrast_insufficient_candidate() -> None:
    from backend.scripts import comfyui_production_quality_gate

    workflow = "local_region_enhance_v1@conservative"
    records = [
        _record("muted-face-unit", workflow, "baseline"),
        _record(
            "muted-face-unit",
            workflow,
            "candidate",
            qa_scores={
                "output_width": 16,
                "output_height": 16,
                "canvas_width": 16,
                "canvas_height": 16,
                "halo_score": 0.2,
                "mask_outside_delta": 0.1,
                "masked_luma_delta": 0.0,
                "texture_detail_delta": 0.0,
                "face_luma_delta": -6.0,
                "face_background_contrast_delta": -5.0,
            },
        ),
    ]
    decisions = [_decision("muted-face-unit", workflow, "candidate")]

    gate = comfyui_production_quality_gate.build_gate_report(records, decisions, vlm_guardrail={})
    by_workflow = {item["workflow"]: item for item in gate["workflow_summary"]}

    assert "face_luma_contrast_insufficient" in set(by_workflow[workflow]["hard_defect_codes"])
    assert gate["production_gate"]["reason_code"] == "hard_defects_present"


def test_retest_plan_keeps_only_allowed_workflow_units() -> None:
    from backend.scripts import comfyui_production_quality_gate

    plan = {
        "scope": "t43_comfyui_real_ab_plan_v1",
        "units": [
            {
                "ab_unit_id": "local-1",
                "workflow": "local_region_enhance_v1@conservative",
                "runs": [{"role": "baseline"}, {"role": "candidate"}],
            },
            {
                "ab_unit_id": "background-1",
                "workflow": "background_cleanup_v1@conservative",
                "runs": [{"role": "baseline"}, {"role": "candidate"}],
            },
        ],
    }

    filtered = comfyui_production_quality_gate.build_retest_plan(
        plan,
        ["local_region_enhance_v1@conservative"],
    )

    assert filtered["scope"] == "t90_comfyui_local_region_retest_plan_v1"
    assert filtered["planned_pair_count"] == 1
    assert [unit["ab_unit_id"] for unit in filtered["units"]] == ["local-1"]
    assert [run["role"] for run in filtered["units"][0]["runs"]] == ["baseline", "candidate"]
