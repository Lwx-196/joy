"""T59.3 quality warning repair queue tests."""
from __future__ import annotations

from pathlib import Path


def _record(
    *,
    job_id: int,
    case_id: int,
    status: str = "done_with_issues",
    warnings: list[str] | None = None,
    blocking_issues: list[str] | None = None,
    composition_alerts: list[dict] | None = None,
    action_suggestions: list[dict] | None = None,
    hard_blockers: list[str] | None = None,
    can_deliver: bool = False,
) -> dict:
    return {
        "job_id": job_id,
        "case_id": case_id,
        "case": {
            "case_id": case_id,
            "abs_path": f"/real/cases/{case_id}",
            "customer_raw": f"case-{case_id}",
        },
        "status": status,
        "template": "tri-compare",
        "artifact_integrity": {
            "status": "ok",
            "final_board": {"path": f"/real/cases/{case_id}/final-board.jpg", "exists": True},
            "manifest": {"path": f"/real/cases/{case_id}/manifest.final.json", "exists": True},
        },
        "quality": {
            "quality_status": status,
            "quality_score": 80,
            "can_publish": False,
            "metrics": {
                "blocking_issues": blocking_issues or [],
                "warnings": warnings or [],
                "display_warnings": warnings or [],
                "composition_alerts": composition_alerts or [],
                "action_suggestions": action_suggestions or [],
            },
        },
        "hard_blockers": hard_blockers or [],
        "delivery_envelope": {
            "class": "experimental_blocked",
            "can_deliver": can_deliver,
            "reasons": ["render_quality.quality_status:done_with_issues"],
        },
    }


def test_blocked_report_when_t59_matrix_missing_does_not_fabricate_actions() -> None:
    from backend.scripts import formal_render_quality_warning_queue as queue

    report = queue.blocked_report("T59 matrix report not found")

    assert report["run_status"] == "blocked_missing_real_t59_matrix"
    assert report["used_mock_data"] is False
    assert report["summary"]["source_done_with_issues_count"] == 0
    assert report["action_items"] == []
    assert "未验证/无法获取" in report["decision"]


def test_warning_queue_classifies_done_with_issues_and_never_marks_publishable() -> None:
    from backend.scripts import formal_render_quality_warning_queue as queue

    report = queue.build_warning_queue(
        {
            "run_status": "completed_real_render_matrix",
            "used_mock_data": False,
            "records": [
                _record(
                    job_id=226,
                    case_id=14,
                    warnings=[
                        "术前1.jpg - 视觉补判仅供参考(confidence=low)：GPT-5.4 API 403 insufficient_user_quota",
                        "自动降级已排除 45°侧：缺少术后 45°侧",
                    ],
                    action_suggestions=[
                        {"code": "manual_quality_review", "label": "人工复核 warning 后决定通过、复检或拒绝"}
                    ],
                ),
                _record(
                    job_id=229,
                    case_id=21,
                    blocking_issues=["正面 术前术后姿态差过大，已废弃该角度"],
                    warnings=["术前1.jpg - 视觉补判仅供参考(confidence=low)：API 403"],
                    action_suggestions=[
                        {
                            "code": "add_front_pitch_material_pair",
                            "source": "material_loop",
                            "publish_gate": {"can_publish_after_acceptance": False},
                        }
                    ],
                ),
                _record(
                    job_id=244,
                    case_id=78,
                    warnings=["术前1.jpg - 视觉补判仅供参考(confidence=low)：API 403"],
                    composition_alerts=[
                        {
                            "code": "side_face_alignment_fallback",
                            "severity": "warning",
                            "message": "侧面人脸检测失败，已使用整图等比留白对齐兜底",
                        }
                    ],
                    action_suggestions=[{"code": "review_composition", "source": "composition"}],
                ),
                _record(job_id=250, case_id=99, status="done", can_deliver=True),
            ],
        },
        source_report_path=Path("/real/tasks/t59_formal_render_quality_matrix.json"),
    )

    assert report["run_status"] == "completed_real_warning_repair_queue"
    assert report["summary"]["source_done_with_issues_count"] == 3
    assert report["summary"]["action_item_count"] == 3
    assert report["summary"]["by_category"]["semantic_judge_unavailable"] == 3
    assert report["summary"]["by_category"]["template_downgrade_review"] == 1
    assert report["summary"]["by_category"]["hard_blocker_in_done_with_issues"] == 1
    assert report["summary"]["by_category"]["material_loop_required"] == 1
    assert report["summary"]["by_category"]["composition_review"] == 1
    assert all(item["publishable_after_warning_review"] is False for item in report["action_items"])
    assert all(item["delivery_envelope_class"] == "experimental_blocked" for item in report["action_items"])


def test_warning_queue_blocks_publish_when_done_with_issues_contains_hard_quality_gate() -> None:
    from backend.scripts import formal_render_quality_warning_queue as queue

    report = queue.build_warning_queue(
        {
            "run_status": "completed_real_render_matrix",
            "used_mock_data": False,
            "records": [
                _record(
                    job_id=241,
                    case_id=69,
                    blocking_issues=["正面 前后清晰度差过大，已废弃该角度"],
                    action_suggestions=[{"code": "manual_quality_review", "source": "quality"}],
                )
            ],
        }
    )

    item = report["action_items"][0]
    assert item["primary_category"] == "hard_blocker_in_done_with_issues"
    assert item["blocks_publish"] is True
    assert item["requires_new_material_or_reselect"] is True
    assert report["summary"]["blocks_publish_count"] == 1


def test_done_with_issues_status_gate_alone_is_not_classified_as_hard_blocker() -> None:
    from backend.scripts import formal_render_quality_warning_queue as queue

    report = queue.build_warning_queue(
        {
            "run_status": "completed_real_render_matrix",
            "used_mock_data": False,
            "records": [
                _record(
                    job_id=226,
                    case_id=14,
                    warnings=["术前1.jpg - 视觉补判仅供参考(confidence=low)：API 403"],
                    hard_blockers=["render_quality.quality_status:done_with_issues"],
                )
            ],
        }
    )

    item = report["action_items"][0]
    assert item["primary_category"] == "semantic_judge_unavailable"
    assert "hard_blocker_in_done_with_issues" not in item["categories"]
    assert item["blocks_publish"] is True
