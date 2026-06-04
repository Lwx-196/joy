"""T59.4 hard quality blocker feedback tests."""
from __future__ import annotations


def test_render_feedback_penalizes_real_pose_blocking_issue_from_manifest() -> None:
    from backend import source_selection

    payload = {
        "render_selection_audit": {
            "applied_slots": [
                {
                    "slot": "front",
                    "before": "术前1.jpg",
                    "after": "术后1.jpg",
                }
            ]
        },
        "render_selection_source_provenance": [
            {"case_id": 21, "filename": "术前1.jpg", "render_filename": "术前1.jpg"},
            {"case_id": 21, "filename": "术后1.jpg", "render_filename": "术后1.jpg"},
        ],
        "blocking_issues": [
            "2026.1.25玻尿酸卧蚕 唇填充：正面 术前术后姿态差过大(yaw=0.32, pitch=7.57, roll=3.02, weighted=9.40)，该角度已从本次出图中排除"
        ],
    }

    feedback = source_selection.render_feedback_from_payload(249, payload)

    assert feedback["pair_penalties"] == [
        {
            "view": "front",
            "before_render_filename": "术前1.jpg",
            "after_render_filename": "术后1.jpg",
            "before_source_key": "21:术前1.jpg",
            "after_source_key": "21:术后1.jpg",
            "penalty": 24,
            "codes": ["selected_pose_delta_large"],
            "reasons": ["上一轮正式出图硬阻断：该配对姿态差需重选"],
            "source_job_id": 249,
        }
    ]


def test_render_feedback_penalizes_real_sharpness_blocking_issue_from_manifest() -> None:
    from backend import source_selection

    payload = {
        "render_selection_audit": {
            "applied_slots": [
                {
                    "slot": "front",
                    "before": "术前2.jpg",
                    "after": "术后1.jpg",
                }
            ]
        },
        "render_selection_source_provenance": [
            {"case_id": 69, "filename": "术前2.jpg", "render_filename": "术前2.jpg"},
            {"case_id": 69, "filename": "术后1.jpg", "render_filename": "术后1.jpg"},
        ],
        "blocking_issues": [
            "2026.3.3柯芮琦泪沟注射：正面 前后清晰度差过大(before=48.25, after=15.81)，已废弃该角度"
        ],
    }

    feedback = source_selection.render_feedback_from_payload(261, payload)

    assert feedback["pair_penalties"][0]["view"] == "front"
    assert feedback["pair_penalties"][0]["codes"] == ["selected_pair_sharpness_mismatch"]
    assert feedback["pair_penalties"][0]["penalty"] == 18


def test_hard_quality_report_keeps_done_with_issues_unpublishable(tmp_path) -> None:
    import json

    from backend.scripts.formal_render_hard_quality_repair_report import build_report

    queue = {
        "action_items": [
            {"case_id": 21, "primary_category": "hard_blocker_in_done_with_issues"},
            {"case_id": 54, "primary_category": "hard_blocker_in_done_with_issues"},
            {"case_id": 69, "primary_category": "hard_blocker_in_done_with_issues"},
        ]
    }
    matrix = {
        "records": [
            {
                "case_id": 21,
                "job_id": 271,
                "status": "done_with_issues",
                "artifact_integrity": {
                    "status": "ok",
                    "final_board": {"exists": True},
                    "manifest": {"exists": True},
                },
                "quality": {
                    "quality_status": "done_with_issues",
                    "manifest_status": "ok",
                    "blocking_count": 0,
                    "metrics": {
                        "warnings": ["视觉补判仅供参考(confidence=low)：GPT-5.4 API 403: insufficient_user_quota"],
                        "blocking_issues": [],
                    },
                },
                "hard_blockers": ["render_quality.quality_status:done_with_issues"],
                "delivery_envelope": {"class": "experimental_blocked"},
            },
            {
                "case_id": 54,
                "job_id": 272,
                "status": "done_with_issues",
                "artifact_integrity": {
                    "status": "ok",
                    "final_board": {"exists": True},
                    "manifest": {"exists": True},
                },
                "quality": {"quality_status": "done_with_issues", "manifest_status": "ok", "blocking_count": 0, "metrics": {}},
                "hard_blockers": ["render_quality.quality_status:done_with_issues"],
                "delivery_envelope": {"class": "experimental_blocked"},
            },
            {
                "case_id": 69,
                "job_id": 273,
                "status": "done_with_issues",
                "artifact_integrity": {
                    "status": "ok",
                    "final_board": {"exists": True},
                    "manifest": {"exists": True},
                },
                "quality": {"quality_status": "done_with_issues", "manifest_status": "ok", "blocking_count": 0, "metrics": {}},
                "hard_blockers": ["render_quality.quality_status:done_with_issues"],
                "delivery_envelope": {"class": "experimental_blocked"},
            },
        ]
    }
    queue_path = tmp_path / "queue.json"
    matrix_path = tmp_path / "matrix.json"
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    report = build_report(queue_path=queue_path, matrix_path=matrix_path)

    assert report["summary"]["after_concrete_hard_blocker_count"] == 0
    assert report["summary"]["formal_ready_count"] == 0
    assert report["summary"]["semantic_judge_unavailable_count"] == 1
    assert "不能交付" in report["decision"]


def test_semantic_judge_readiness_fail_closed_without_verified_probe(tmp_path, monkeypatch) -> None:
    import json

    from backend.scripts.formal_render_semantic_judge_readiness import build_report

    monkeypatch.setenv("GEMINI_FLASH_API_KEY", "secret-should-not-leak")
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "case_id": 21,
                        "job_id": 271,
                        "quality": {
                            "metrics": {
                                "warnings": [
                                    "视觉补判仅供参考(confidence=low)：GPT-5.4 API 403: insufficient_user_quota"
                                ]
                            }
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = build_report(
        matrix_path=matrix_path,
        provider="flashapi",
        model="gemini-3.1-flash-image-preview",
        probe_status="blocked",
        probe_detail="quota blocked",
    )

    encoded = json.dumps(report, ensure_ascii=False)
    assert report["run_status"] == "blocked_semantic_judge_unverified"
    assert report["summary"]["ready_for_publish_gate"] is False
    assert "未验证/无法获取" in report["decision"]
    assert "secret-should-not-leak" not in encoded


def test_semantic_judge_readiness_verified_probe_requires_matrix_rerun(tmp_path) -> None:
    import json

    from backend.scripts.formal_render_semantic_judge_readiness import build_report

    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "case_id": 21,
                        "job_id": 271,
                        "quality": {
                            "metrics": {
                                "warnings": [
                                    "视觉补判仅供参考(confidence=low)：GPT-5.4 API 403: insufficient_user_quota"
                                ]
                            }
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = build_report(
        matrix_path=matrix_path,
        provider="flashapi",
        model="gemini-3.1-flash-image-preview",
        probe_status="verified",
        probe_detail="one real image probe passed",
    )

    assert report["run_status"] == "backup_judge_verified_pending_matrix_rerun"
    assert report["summary"]["backup_judge_ready_for_rerun"] is True
    assert report["summary"]["ready_for_publish_gate"] is False
