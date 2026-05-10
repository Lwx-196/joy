from __future__ import annotations

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
    auto_before["manual"] = False; auto_before["view_source"] = "pose"
    auto_after = _candidate(
        "术后即刻3.jpeg", score=76, phase="after",
        pose={"yaw": 1.96, "pitch": 8.0, "roll": -0.6},
    )
    auto_after["manual"] = False; auto_after["view_source"] = "pose"

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
