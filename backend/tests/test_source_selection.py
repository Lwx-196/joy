from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from backend import source_selection


def _candidate(
    filename: str,
    *,
    score: int,
    phase: str,
    view: str = "front",
    case_id: int = 126,
    render_filename: str | None = None,
    pose: dict[str, float] | None = None,
    direction: str | None = None,
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "source_role": "primary",
        "filename": filename,
        "render_filename": render_filename or filename,
        "phase": phase,
        "view": view,
        "view_source": "manual",
        "selection_score": score,
        "risk_level": "ok",
        "angle_confidence": 0.96,
        "quality_warnings": [],
        "pose": pose or {"yaw": 0.0, "pitch": 0.0, "roll": 0.0},
        "direction": direction,
    }


def _write_side_source(path: Path, *, scale: float) -> Path:
    image = Image.new("RGB", (720, 480), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    face_w = int(170 * scale)
    face_h = int(230 * scale)
    x0 = 190
    y0 = 70
    x1 = x0 + face_w
    y1 = y0 + face_h
    draw.ellipse((x0, y0, x1, y1), fill=(178, 136, 112))
    draw.rectangle((x0 + 18, y0 + face_h // 2, x1 - 10, min(440, y1 + 95)), fill=(178, 136, 112))
    draw.rectangle((x0 - 46, min(455, y1 + 80), min(680, x1 + 70), 480), fill=(50, 56, 62))
    image.save(path)
    return path


def test_select_best_pair_prefers_pose_aligned_pair_over_single_image_score():
    before = _candidate(
        "术前-正面.jpg",
        score=87,
        phase="before",
        pose={"yaw": 0.0, "pitch": 0.0, "roll": 0.0},
    )
    high_score_bad_pose = _candidate(
        "术后-正面-高单张分但姿态差.jpg",
        score=90,
        phase="after",
        pose={"yaw": 0.0, "pitch": 12.0, "roll": 0.0},
    )
    lower_score_aligned = _candidate(
        "术后-正面-姿态接近.jpg",
        score=82,
        phase="after",
        pose={"yaw": 1.0, "pitch": 2.0, "roll": 1.0},
    )

    selected_before, selected_after, pair_quality = source_selection.select_best_pair(
        "front",
        [before],
        [high_score_bad_pose, lower_score_aligned],
    )

    assert selected_before == before
    assert selected_after == lower_score_aligned
    assert pair_quality is not None
    assert pair_quality["severity"] == "ok"
    assert pair_quality["metrics"]["pose_delta"]["weighted"] == 3.5


def test_profile_pose_delta_normalizes_same_direction_yaw_sign():
    before = _candidate(
        "术前-右侧.jpg",
        score=88,
        phase="before",
        view="side",
        pose={"yaw": 45.0, "pitch": 0.0, "roll": 0.0},
        direction="right",
    )
    after = _candidate(
        "术后-右侧.jpg",
        score=88,
        phase="after",
        view="side",
        pose={"yaw": -45.0, "pitch": 0.0, "roll": 0.0},
        direction="right",
    )

    delta = source_selection.pose_delta("side", before, after)

    assert delta is not None
    assert delta["weighted"] == 0.0
    assert delta["raw"]["yaw"] == 90.0
    assert delta["normalization"] == "profile_abs_yaw_same_direction"


def test_oblique_opposite_direction_keeps_direction_warning_without_pose_penalty():
    before = _candidate(
        "术前-左45.jpg",
        score=88,
        phase="before",
        view="oblique",
        pose={"yaw": -45.0, "pitch": 0.0, "roll": 0.0},
        direction="left",
    )
    after = _candidate(
        "术后-右45.jpg",
        score=88,
        phase="after",
        view="oblique",
        pose={"yaw": 45.0, "pitch": 0.0, "roll": 0.0},
        direction="right",
    )

    quality = source_selection.slot_pair_quality("oblique", before, after)

    assert quality is not None
    assert quality["metrics"]["pose_delta"]["weighted"] == 0.0
    assert quality["metrics"]["pose_delta"]["normalization"] == "profile_abs_yaw_same_direction"
    codes = [item["code"] for item in quality["warnings"]]
    assert "direction_mismatch" in codes
    assert "pose_delta_large" not in codes


def test_locked_side_pair_with_large_pose_delta_is_dropped_from_formal_render():
    before = _candidate(
        "术前-侧面-锁定.jpg",
        score=92,
        phase="before",
        view="side",
        case_id=114,
        pose={"yaw": 20.0, "pitch": 0.0, "roll": 0.0},
        direction="right",
    )
    after = _candidate(
        "术后-侧面-锁定.jpg",
        score=92,
        phase="after",
        view="side",
        case_id=116,
        pose={"yaw": 82.0, "pitch": 8.0, "roll": 8.0},
        direction="right",
    )

    selected_before, selected_after, pair_quality = source_selection.select_best_pair(
        "side",
        [before],
        [after],
        lock={
            "before": {"case_id": 114, "filename": "术前-侧面-锁定.jpg"},
            "after": {"case_id": 116, "filename": "术后-侧面-锁定.jpg"},
            "reviewer": "operator",
            "reason": "旧锁片复核",
        },
    )

    assert selected_before is None
    assert selected_after is None
    assert pair_quality is not None
    assert pair_quality["render_slot_status"] == "dropped"
    assert pair_quality["drop_reason"]["code"] == "low_comparison_value"
    assert pair_quality["metrics"]["pose_delta"]["weighted"] > source_selection.POSE_THRESHOLDS["side"]["weighted"]


def test_side_slot_prefers_true_profile_over_weak_yaw_candidate():
    """#33: side 槽位不能把 yaw≈35 的 45°候选当成严格侧面首选。"""

    def scored(filename: str, *, phase: str, yaw: float, direction: str, issues: list[str] | None = None) -> dict[str, object]:
        candidate = _candidate(
            filename,
            score=0,
            phase=phase,
            view="side",
            case_id=33,
            pose={"yaw": yaw, "pitch": 0.0, "roll": 0.0},
            direction=direction,
        )
        candidate["manual"] = False
        candidate["phase_source"] = "filename"
        candidate["view_source"] = "semantic_screen"
        candidate["angle_confidence"] = 0.88
        candidate["issues"] = issues or []
        candidate.update(source_selection.candidate_quality(candidate, "primary"))
        return candidate

    before_left = scored(
        "术前3.JPG",
        phase="before",
        yaw=-45.0,
        direction="left",
        issues=["正脸检测失败，已使用侧脸检测兜底: 未检测到面部"],
    )
    before_right = scored(
        "术前5.JPG",
        phase="before",
        yaw=45.0,
        direction="right",
        issues=["正脸检测失败，已使用侧脸检测兜底: 未检测到面部"],
    )
    weak_after_left = scored("术后2.JPG", phase="after", yaw=-35.42, direction="left")
    true_after_right = scored(
        "术后5.JPG",
        phase="after",
        yaw=45.0,
        direction="right",
        issues=["正脸检测失败，已使用侧脸检测兜底: 未检测到面部"],
    )

    assert weak_after_left["selection_score"] < true_after_right["selection_score"]
    assert "side_profile_yaw_weak" in [
        str(item.get("code")) for item in (weak_after_left.get("quality_warnings") or [])
    ]

    selected_before, selected_after, pair_quality = source_selection.select_best_pair(
        "side",
        [before_left, before_right],
        [weak_after_left, true_after_right],
    )

    assert selected_before == before_right
    assert selected_after == true_after_right
    assert pair_quality is not None
    assert pair_quality["metrics"]["pose_delta"]["weighted"] == 0.0


def test_side_pair_quality_penalizes_source_scale_mismatch(tmp_path: Path):
    before_path = _write_side_source(tmp_path / "before-side.jpg", scale=1.0)
    after_bad_path = _write_side_source(tmp_path / "after-side-bad.jpg", scale=1.34)
    after_good_path = _write_side_source(tmp_path / "after-side-good.jpg", scale=1.0)
    before = _candidate(
        "术前-side.jpg",
        score=88,
        phase="before",
        view="side",
        pose={"yaw": 45.0, "pitch": 0.0, "roll": 0.0},
        direction="right",
    )
    after_bad = _candidate(
        "术后-side-bad.jpg",
        score=96,
        phase="after",
        view="side",
        pose={"yaw": 45.0, "pitch": 0.0, "roll": 0.0},
        direction="right",
    )
    after_good = _candidate(
        "术后-side-good.jpg",
        score=86,
        phase="after",
        view="side",
        pose={"yaw": 45.0, "pitch": 0.0, "roll": 0.0},
        direction="right",
    )
    before["path"] = str(before_path)
    after_bad["path"] = str(after_bad_path)
    after_good["path"] = str(after_good_path)

    bad_quality = source_selection.slot_pair_quality("side", before, after_bad)
    selected_before, selected_after, selected_quality = source_selection.select_best_pair(
        "side",
        [before],
        [after_bad, after_good],
    )

    assert bad_quality is not None
    assert bad_quality["metrics"]["source_scale"]["status"] == "review"
    assert any(item["code"] == "side_source_scale_mismatch" for item in bad_quality["warnings"])
    assert selected_before == before
    assert selected_after == after_good
    assert selected_quality is not None
    assert selected_quality["metrics"]["source_scale"]["status"] == "ok"


def test_side_source_scale_mismatch_drops_side_slot_even_above_score_floor():
    quality = {
        "score": source_selection.LOW_COMPARISON_VALUE_SCORE + 8,
        "label": "review",
        "severity": "review",
        "warnings": [
            {
                "code": "side_source_scale_mismatch",
                "severity": "review",
                "message": "侧面源图人物尺度不一致",
            }
        ],
    }

    drop = source_selection.render_slot_drop_reason("side", quality)

    assert drop is not None
    assert drop["code"] == "low_comparison_value"
    assert "side_source_scale_mismatch" in drop["trigger_codes"]


def test_render_feedback_penalizes_selected_quality_risk_without_penalizing_cross_case_review():
    feedback = source_selection.render_feedback_from_payload(
        119,
        {
            "render_selection_source_provenance": [
                {
                    "case_id": 114,
                    "filename": "IMG_2604.JPG",
                    "render_filename": "case114-术前-IMG_2604.JPG",
                    "view": "side",
                },
                {
                    "case_id": 116,
                    "filename": "IMG_2685.JPG",
                    "render_filename": "case116-术后-IMG_2685.JPG",
                    "view": "side",
                },
            ],
            "selection_quality": [
                {
                    "slot": "side",
                    "before": {"name": "case114-术前-IMG_2604.JPG", "sharpness_score": 0.0},
                    "after": {"name": "case116-术后-IMG_2685.JPG", "sharpness_score": 0.0},
                    "actions": ["before:侧脸兜底", "after:侧脸兜底"],
                }
            ],
            "warning_layers": {
                "selected_actionable": [
                    "job-119：case114-术前-IMG_2604.JPG - 面部检测失败: 未检测到面部",
                    "job-119：case116-术后-IMG_2685.JPG - 面部检测失败: 未检测到面部",
                    "job-119：侧面 人工配对方向不一致，已按人工选择保留该角度，需人工复核",
                ]
            },
            "composition_alerts": [
                {
                    "slot": "side",
                    "code": "side_face_alignment_fallback",
                    "message": "侧面人脸检测失败，已使用整图等比留白对齐兜底",
                }
            ],
            "render_selection_audit": {
                "applied_slots": [
                    {
                        "slot": "side",
                        "before": "case114-术前-IMG_2604.JPG",
                        "after": "case116-术后-IMG_2685.JPG",
                        "pair_quality": {
                            "warnings": [
                                {
                                    "code": "cross_case_pair",
                                    "severity": "review",
                                    "message": "术前术后来自不同 case，需确认同次治疗",
                                }
                            ]
                        },
                    }
                ]
            },
        },
    )
    bad_before = _candidate(
        "IMG_2604.JPG",
        case_id=114,
        render_filename="case114-术前-IMG_2604.JPG",
        score=88,
        phase="before",
        view="side",
    )
    bad_after = _candidate(
        "IMG_2685.JPG",
        case_id=116,
        render_filename="case116-术后-IMG_2685.JPG",
        score=88,
        phase="after",
        view="side",
    )
    clean_before = _candidate("IMG_2646.JPG", case_id=114, score=82, phase="before", view="side")
    clean_after = _candidate("IMG_2696.JPG", case_id=116, score=82, phase="after", view="side")

    for item in [bad_before, bad_after, clean_before, clean_after]:
        source_selection.apply_render_feedback(item, feedback)

    selected_before, selected_after, pair_quality = source_selection.select_best_pair(
        "side",
        [bad_before, clean_before],
        [bad_after, clean_after],
    )

    assert selected_before == clean_before
    assert selected_after == clean_after
    assert pair_quality is not None
    assert bad_before["render_feedback"]["penalty"] >= 30
    assert bad_after["render_feedback"]["penalty"] >= 30
    assert "cross_case_pair" not in bad_before["render_feedback"]["codes"]


def test_render_feedback_ignores_cross_case_pair_when_it_is_the_only_review_warning():
    feedback = source_selection.render_feedback_from_payload(
        120,
        {
            "render_selection_source_provenance": [
                {
                    "case_id": 114,
                    "filename": "IMG_2601.JPG",
                    "render_filename": "case114-术前-IMG_2601.JPG",
                    "view": "front",
                }
            ],
            "render_selection_audit": {
                "applied_slots": [
                    {
                        "slot": "front",
                        "before": "case114-术前-IMG_2601.JPG",
                        "after": "case116-术后-IMG_2673.JPG",
                        "pair_quality": {
                            "warnings": [
                                {
                                    "code": "cross_case_pair",
                                    "severity": "review",
                                    "message": "术前术后来自不同 case，需确认同次治疗",
                                }
                            ]
                        },
                    }
                ]
            },
            "warning_layers": {"selected_actionable": []},
        },
    )
    candidate = _candidate(
        "IMG_2601.JPG",
        case_id=114,
        render_filename="case114-术前-IMG_2601.JPG",
        score=88,
        phase="before",
    )

    source_selection.apply_render_feedback(candidate, feedback)

    assert candidate["selection_score"] == 88
    assert "render_feedback" not in candidate


def test_merge_render_feedbacks_keeps_penalties_from_multiple_recent_jobs():
    older = source_selection.render_feedback_from_payload(
        119,
        {
            "render_selection_source_provenance": [
                {
                    "case_id": 114,
                    "filename": "IMG_2604.JPG",
                    "render_filename": "case114-术前-IMG_2604.JPG",
                    "view": "side",
                }
            ],
            "warning_layers": {
                "selected_actionable": [
                    "job-119：case114-术前-IMG_2604.JPG - 面部检测失败: 未检测到面部"
                ]
            },
        },
    )
    latest = source_selection.render_feedback_from_payload(
        120,
        {
            "render_selection_source_provenance": [
                {
                    "case_id": 114,
                    "filename": "IMG_2605.JPG",
                    "render_filename": "case114-术前-IMG_2605.JPG",
                    "view": "side",
                }
            ],
            "warning_layers": {
                "selected_actionable": [
                    "job-120：case114-术前-IMG_2605.JPG - 面部检测失败: 未检测到面部"
                ]
            },
        },
    )

    merged = source_selection.merge_render_feedbacks([latest, older])
    first_bad = _candidate(
        "IMG_2604.JPG",
        case_id=114,
        render_filename="case114-术前-IMG_2604.JPG",
        score=88,
        phase="before",
        view="side",
    )
    second_bad = _candidate(
        "IMG_2605.JPG",
        case_id=114,
        render_filename="case114-术前-IMG_2605.JPG",
        score=88,
        phase="before",
        view="side",
    )

    source_selection.apply_render_feedback(first_bad, merged)
    source_selection.apply_render_feedback(second_bad, merged)

    assert merged["source_job_id"] == 120
    assert merged["source_job_ids"] == [120, 119]
    assert first_bad["render_feedback"]["source_job_ids"] == [120, 119]
    assert first_bad["render_feedback"]["penalty"] >= 30
    assert second_bad["render_feedback"]["penalty"] >= 30


def _manual_candidate(
    filename: str,
    *,
    score: int,
    phase: str,
    view: str = "front",
    case_id: int = 126,
    pose: dict[str, float] | None = None,
    direction: str | None = None,
) -> dict[str, object]:
    cand = _candidate(
        filename,
        score=score,
        phase=phase,
        view=view,
        case_id=case_id,
        pose=pose,
        direction=direction,
    )
    cand["manual"] = True
    cand["phase_source"] = "manual"
    cand["view_source"] = "manual"
    return cand


def test_manual_pair_beats_pose_aligned_auto_pair_for_front_slot():
    """Manual+manual front pair 即使姿态略差（pose_delta_review 区间）应胜过姿态接近的 auto pair。

    回归 case 126 / 2026-05-08：术前-正面-手动-898978 + 术后-正面-手动-925641 (yaw delta ~7) 应当
    被选中，而不是 auto 候选 术前1 + 术后即刻3 (yaw delta ~3.8) — 后者在修复前因为 +12 pose-aligned
    bonus + ok severity 把 manual pair 打到第二位。
    """
    manual_before = _manual_candidate(
        "术前-正面-手动-925641.jpeg", score=87, phase="before",
        pose={"yaw": -8.0, "pitch": 4.5, "roll": 1.0},
    )
    manual_after = _manual_candidate(
        "术后-正面-手动-925641.jpeg", score=87, phase="after",
        pose={"yaw": 1.0, "pitch": 1.0, "roll": 1.0},
    )
    auto_before = _candidate(
        "术前1.jpeg", score=76, phase="before",
        pose={"yaw": -1.87, "pitch": 6.0, "roll": -0.5},
    )
    auto_before["manual"] = False
    auto_before["view_source"] = "pose"
    auto_after = _candidate(
        "术后即刻3.jpeg", score=76, phase="after",
        pose={"yaw": 1.96, "pitch": 8.0, "roll": -0.6},
    )
    auto_after["manual"] = False
    auto_after["view_source"] = "pose"

    before, after, quality = source_selection.select_best_pair(
        "front",
        [manual_before, auto_before],
        [manual_after, auto_after],
    )
    assert before is manual_before
    assert after is manual_after
    assert quality["metrics"].get("manual_pair") is True
    assert "术前术后均为人工精选 pair" in (quality.get("reasons") or [])


def test_manual_pair_pose_review_does_not_emit_review_warning():
    """Manual pair 在 weighted<=14 front 区间不应再触发 pose_delta_review，避免被 severity 压制。"""
    before = _manual_candidate(
        "术前-正面-手动-A.jpeg", score=87, phase="before",
        pose={"yaw": -7.0, "pitch": 3.0, "roll": 0.0},
    )
    after = _manual_candidate(
        "术后-正面-手动-A.jpeg", score=87, phase="after",
        pose={"yaw": 2.0, "pitch": 1.0, "roll": 0.0},
    )
    quality = source_selection.slot_pair_quality("front", before, after)
    codes = [str(w.get("code")) for w in (quality.get("warnings") or [])]
    assert "pose_delta_review" not in codes


def test_lip_pair_near_duplicate_real_case_blocks_target_effect():
    """#65/job1048: 丰唇 pair 不能因姿态接近而误判 strong/render。"""
    case_dir = Path("/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/刘柏玲/25.6.4娇兰丰唇")
    before_path = case_dir / "06041654_01.jpg"
    after_path = case_dir / "06041654_02.jpg"
    assert before_path.is_file()
    assert after_path.is_file()
    before = _candidate(
        "06041654_01.jpg",
        score=63,
        phase="before",
        case_id=65,
        pose={"yaw": -1.2, "pitch": 3.38, "roll": 2.42},
    )
    after = _candidate(
        "06041654_02.jpg",
        score=63,
        phase="after",
        case_id=65,
        pose={"yaw": -1.14, "pitch": 3.02, "roll": 2.58},
    )
    before["source_path"] = str(before_path)
    after["source_path"] = str(after_path)

    quality = source_selection.slot_pair_quality("front", before, after, treatment_type="lip")

    assert quality is not None
    codes = [str(w.get("code")) for w in (quality.get("warnings") or [])]
    assert "target_effect_near_duplicate_lip" in codes
    assert quality["severity"] == "block"
    target_effect = quality["metrics"]["target_effect"]
    assert target_effect["status"] == "block"
    assert target_effect["dhash_distance"] <= 2


def test_multi_treatment_lip_pair_near_duplicate_blocks_target_effect():
    """多项目 case 主项目不是 lip 时，含 lip 仍要跑唇部目标效果门禁。"""
    case_dir = Path("/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/刘柏玲/25.6.4娇兰丰唇")
    before_path = case_dir / "06041654_01.jpg"
    after_path = case_dir / "06041654_02.jpg"
    assert before_path.is_file()
    assert after_path.is_file()
    before = _candidate(
        "06041654_01.jpg",
        score=63,
        phase="before",
        case_id=65,
        pose={"yaw": -1.2, "pitch": 3.38, "roll": 2.42},
    )
    after = _candidate(
        "06041654_02.jpg",
        score=63,
        phase="after",
        case_id=65,
        pose={"yaw": -1.14, "pitch": 3.02, "roll": 2.58},
    )
    before["source_path"] = str(before_path)
    after["source_path"] = str(after_path)

    quality = source_selection.slot_pair_quality(
        "front",
        before,
        after,
        treatment_type="tear_trough",
        treatment_types=["tear_trough", "lip"],
    )

    assert quality is not None
    target_effect = quality["metrics"]["target_effect"]
    assert target_effect["treatment_type"] == "lip"
    assert target_effect["treatment_types"] == ["tear_trough", "lip"]
    assert target_effect["status"] == "block"
    assert "target_effect_near_duplicate_lip" in [
        str(w.get("code")) for w in (quality.get("warnings") or [])
    ]


def test_front_pair_mouth_expression_mismatch_blocks_bad_pair_from_metadata():
    before = _candidate(
        "术前-嘟嘴.jpg",
        score=90,
        phase="before",
        pose={"yaw": 0.0, "pitch": 0.0, "roll": 0.0},
    )
    after = _candidate(
        "术后-中性.jpg",
        score=90,
        phase="after",
        pose={"yaw": 0.0, "pitch": 0.0, "roll": 0.0},
    )
    before["mouth_expression"] = {"status": "evaluated", "mouthPucker": 0.99}
    after["mouth_expression"] = {"status": "evaluated", "mouthPucker": 0.12}

    quality = source_selection.slot_pair_quality("front", before, after)

    assert quality is not None
    assert quality["severity"] == "block"
    codes = [str(w.get("code")) for w in (quality.get("warnings") or [])]
    assert "mouth_expression_mismatch" in codes
    assert quality["metrics"]["mouth_expression"]["mouth_pucker_delta"] == 0.87


def test_real_case396_front_selection_avoids_pucker_before_source():
    pytest.importorskip("mediapipe")
    root = Path("/Users/a1234/Desktop/案例生成器/incoming/无创案例库/无创注射案例库/林方如/林方如2026.4.1盈致1支下巴，颏肌释放术前")
    before_pucker_path = root / "术前7.jpg"
    before_neutral_path = root / "术前1.jpg"
    after_neutral_path = root / "术后1.jpg"
    for path in (before_pucker_path, before_neutral_path, after_neutral_path):
        assert path.is_file()
    before_pucker = _candidate(
        "术前7.jpg",
        score=76,
        phase="before",
        case_id=396,
        pose={"pitch": 1.4, "yaw": -1.55, "roll": 3.49},
    )
    before_pucker["source_path"] = str(before_pucker_path)
    before_neutral = _candidate(
        "术前1.jpg",
        score=63,
        phase="before",
        case_id=396,
        pose={"pitch": 5.46, "yaw": -0.04, "roll": -0.27},
    )
    before_neutral["source_path"] = str(before_neutral_path)
    after_neutral = _candidate(
        "术后1.jpg",
        score=63,
        phase="after",
        case_id=396,
        pose={"pitch": 6.33, "yaw": -0.39, "roll": 1.55},
    )
    after_neutral["source_path"] = str(after_neutral_path)

    blocked_quality = source_selection.slot_pair_quality("front", before_pucker, after_neutral)
    selected_before, selected_after, selected_quality = source_selection.select_best_pair(
        "front",
        [before_pucker, before_neutral],
        [after_neutral],
    )

    assert blocked_quality is not None
    assert blocked_quality["severity"] == "block"
    assert [
        str(w.get("code")) for w in (blocked_quality.get("warnings") or [])
    ].count("mouth_expression_mismatch") == 1
    assert selected_before == before_neutral
    assert selected_after == after_neutral
    assert selected_quality is not None
    assert selected_quality["severity"] != "block"


def test_direction_filename_fallback_right_for_side():
    """oblique/side 候选 direction=None 时，文件名含「右侧/右45/右脸」→ direction='right'。"""
    cand = {
        "filename": "术前-右侧面-手动-20260502-052256-441132.jpeg",
        "phase": "before",
        "view": "side",
        "view_source": "manual",
        "phase_source": "manual",
        "direction": None,
        "angle_confidence": 0.96,
    }
    enriched = source_selection.candidate_quality(cand, "primary")
    assert cand["direction"] == "right"
    assert cand["direction_source"] == "filename_fallback"
    # 加分逻辑仍正常（manual + primary + confidence_high ≈ 50+18+5+x）
    assert enriched["selection_score"] >= 60


def test_direction_filename_fallback_left_for_oblique():
    cand = {
        "filename": "术后-左45-自动.jpeg",
        "phase": "after",
        "view": "oblique",
        "view_source": "pose",
        "direction": None,
        "angle_confidence": 0.92,
    }
    source_selection.candidate_quality(cand, "primary")
    assert cand["direction"] == "left"
    assert cand["direction_source"] == "filename_fallback"


def test_direction_fallback_skips_front_view():
    """front 槽位不应被 fallback 改到 left/right，front 应保持 center/None。"""
    cand = {
        "filename": "术前-正面-右侧背景-手动.jpeg",  # 文件名歧义包含「右侧」
        "phase": "before",
        "view": "front",
        "view_source": "manual",
        "direction": None,
        "angle_confidence": 0.95,
    }
    source_selection.candidate_quality(cand, "primary")
    assert cand.get("direction") is None
    assert "direction_source" not in cand
