"""effect_projection judge profile gates (anchored-simulation Phase 3.1).

Pure prompt-construction tests — no real VLM call. The effect judge is the
inverse of the保真 _single_image_fidelity_prompt: here image B (candidate) is
SUPPOSED to differ from image A (baseline), because B is an AI post-procedure
EFFECT projection. The prompt must:
  - switch framing on judge_profile == "effect_projection" (and leave the
    fidelity / board profiles untouched);
  - inject evidence-anchored do_right / avoid rows from
    procedure_region_mappings.effect_row (反臆造 — never invent expected effects);
  - tolerate botox on a static neutral face showing little/no change;
  - fail-closed (skip, never crash) on unregistered (project, region) pairs;
  - surface do_not_touch regions and the 4 effect criteria.
"""
from __future__ import annotations

from backend.scripts import comfyui_vlm_judge_runner as judge
from backend.services import procedure_region_mappings as prm

HA = prm.PROJECT_HA_FILLER
BOTOX = prm.PROJECT_BOTOX


def _effect_item(**overrides):
    item = {
        "ab_unit_id": "case45-front",
        "judge_profile": "effect_projection",
        "effect_pairs": [[HA, "唇"], [HA, "下巴"]],
        "criteria": ["effect projection quality", "identity preserved"],
    }
    item.update(overrides)
    return item


# --- profile switch ---------------------------------------------------------

def test_effect_profile_switches_framing():
    effect_prompt = judge._judge_prompt(_effect_item())
    fid_prompt = judge._judge_prompt(
        {"ab_unit_id": "x", "judge_profile": "single_image_fidelity",
         "focus_targets": ["泪沟"], "criteria": ["sharpness"]}
    )
    board_prompt = judge._judge_prompt({"ab_unit_id": "y", "criteria": ["overall quality"]})

    assert "EFFECT-PROJECTION judge" in effect_prompt
    # effect prompt is NOT the保真 fidelity framing, nor the default board framing.
    assert "FIDELITY judge" not in effect_prompt
    assert "delivery quality judge" not in effect_prompt
    # the other two profiles must be unaffected by the new branch.
    assert "FIDELITY judge" in fid_prompt
    assert "EFFECT-PROJECTION" not in fid_prompt
    assert "delivery quality judge" in board_prompt
    assert "EFFECT-PROJECTION" not in board_prompt


def test_effect_prompt_demands_candidate_differs():
    # Inverse of fidelity: a no-change projection is a FAILURE, not a win.
    prompt = judge._judge_prompt(_effect_item())
    assert "SUPPOSED to differ" in prompt
    # candidate (the projection) wins only when all four criteria hold.
    assert "winner_role=candidate ONLY if ALL" in prompt


# --- evidence injection (反臆造) --------------------------------------------

def test_filler_evidence_do_right_and_avoid_injected():
    prompt = judge._judge_prompt(_effect_item(effect_pairs=[[HA, "唇"]]))
    lip = prm.effect_row(HA, "唇")
    # do_right direction must be present verbatim from the evidence library.
    assert "唇珠形成" in prompt
    assert lip["do_right"] in prompt
    # at least one over-distortion red-line must be present.
    assert "香肠唇/鸭嘴" in prompt
    # quantified guardrail injected too.
    assert lip["guardrail"] in prompt


def test_multiple_pairs_all_regions_present():
    prompt = judge._judge_prompt(_effect_item(effect_pairs=[[HA, "唇"], [HA, "下巴"]]))
    assert "【唇】" in prompt
    assert "【下巴】" in prompt
    assert prm.effect_row(HA, "下巴")["do_right"] in prompt


def test_botox_region_marked_neutral_tolerant():
    prompt = judge._judge_prompt(_effect_item(effect_pairs=[[BOTOX, "额纹"], [BOTOX, "川字"]]))
    # botox on a static neutral face may show little/no change — must be flagged
    # as acceptable so the judge does not fail a correct no-visible-change region.
    assert "静止中性脸可不明显" in prompt
    assert prm.effect_row(BOTOX, "额纹")["do_right"] in prompt
    # ground-truth honesty note surfaces (no after-photo GT for botox).
    assert "循证预测" in prompt


def test_anti_fabrication_instruction_present():
    prompt = judge._judge_prompt(_effect_item())
    # judge must be told to evaluate ONLY against injected evidence rows.
    assert "do NOT invent" in prompt or "反臆造" in prompt


def test_unregistered_pair_is_skipped_not_crashed():
    # (HA_filler, 耳) is not in EFFECT_ROWS → must be skipped, registered pair kept.
    assert prm.effect_row(HA, "耳") is None
    prompt = judge._judge_prompt(_effect_item(effect_pairs=[[HA, "唇"], [HA, "耳"]]))
    assert "【唇】" in prompt
    assert "【耳】" not in prompt


def test_no_effect_pairs_emits_explicit_marker_no_crash():
    prompt = judge._judge_prompt(_effect_item(effect_pairs=[]))
    # must not silently look like effects were evaluated; explicit no-evidence marker.
    assert "未注入循证效果行" in prompt
    assert "EFFECT-PROJECTION judge" in prompt


def test_malformed_pairs_are_ignored():
    # defensive: bad shapes must not crash prompt construction.
    prompt = judge._judge_prompt(
        _effect_item(effect_pairs=[[HA, "唇"], ["only-one"], "nope", [HA, "下巴", "extra"]])
    )
    assert "【唇】" in prompt
    # 3-tuple and scalar/short entries are ignored.
    assert "【下巴】" not in prompt


# --- do_not_touch + criteria -----------------------------------------------

def test_do_not_touch_regions_surface():
    prompt = judge._judge_prompt(_effect_item(do_not_touch=["眼", "鼻", "苹果肌"]))
    assert "眼" in prompt
    assert "鼻" in prompt
    assert "苹果肌" in prompt


def test_four_effect_criteria_keywords_present():
    prompt = judge._judge_prompt(_effect_item())
    # ① effect direction ② identity ③ only treated regions ④ natural / not overdone
    assert "effect_direction" in prompt
    assert "identity_preserved" in prompt
    assert "only_treated_regions" in prompt
    assert "natural_not_overdone" in prompt


# --- §4 owner-aesthetic calibration (Phase 6) -------------------------------

def test_natural_criterion_rewards_real_skin_texture():
    # §4: a CREDIBLE post-op look KEEPS real skin texture — pores / a slight healthy
    # flush are GOOD signs of a faithful edit, NOT defects. (Why the judge tied the
    # owner-preferred gpt-edit raw before: it failed to reward natural restraint.)
    prompt = judge._judge_prompt(_effect_item())
    assert "微微红润健康气色" in prompt
    assert "毛孔" in prompt or "real skin texture" in prompt
    assert "真实可信术后感" in prompt


def test_plastic_oversmoothed_is_a_natural_defect():
    # §4 inverse: a plastic / over-smoothed / whitened look (磨皮塑料感) is ITSELF an
    # over-distortion defect that LOWERS the candidate's natural score even when the
    # effect direction is correct — not a "clean" win.
    prompt = judge._judge_prompt(_effect_item())
    assert "磨皮塑料感" in prompt


def test_aesthetic_calibration_only_in_effect_profile():
    # the §4 aesthetic language must not leak into the fidelity / board profiles.
    fid = judge._judge_prompt(
        {"ab_unit_id": "x", "judge_profile": "single_image_fidelity",
         "focus_targets": ["泪沟"], "criteria": ["sharpness"]}
    )
    board = judge._judge_prompt({"ab_unit_id": "y", "criteria": ["overall quality"]})
    assert "磨皮塑料感" not in fid and "微微红润健康气色" not in fid
    assert "磨皮塑料感" not in board and "微微红润健康气色" not in board


def test_required_json_keys_match_runner_parser():
    # the parser (_judgment_from_parsed) reads these keys — keep contract aligned.
    prompt = judge._judge_prompt(_effect_item())
    for key in ("ab_unit_id", "winner_role", "confidence", "criterion_scores",
                "visual_evidence_summary", "rationale", "risk_flags", "hard_veto_reason"):
        assert key in prompt
    assert "winner_role may be baseline, candidate, tie, or manual_review" in prompt
