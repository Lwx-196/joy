"""Tests for render quality status and review API."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from PIL import Image

from backend import render_quality as rq
from backend.render_quality import evaluate_render_result


def test_render_quality_metrics_include_evaluation_version(tmp_path):
    output = tmp_path / "final-board.jpg"
    Image.new("RGB", (64, 64), (24, 24, 24)).save(output, "JPEG")

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "ai_usage": {},
        }
    )

    assert quality["metrics"]["quality_evaluation_version"] == rq.QUALITY_EVALUATION_VERSION


def test_backfill_recomputes_stale_quality_version(temp_db, seed_case, tmp_path, monkeypatch):
    from backend import db

    case_id = seed_case(abs_path="/tmp/case-stale-quality-version", customer_raw="小质")
    now = datetime.now(timezone.utc).isoformat()
    final_board = tmp_path / "final-board.jpg"
    Image.new("RGB", (64, 64), (24, 24, 24)).save(final_board, "JPEG")
    with db.connect() as conn:
        job_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, output_path,
               semantic_judge, meta_json)
            VALUES (?, 'fumei', 'tri-compare', 'done', ?, ?, ?, 'off', '{}')
            """,
            (case_id, now, now, str(final_board)),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO render_quality
              (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
               manifest_status, blocking_count, warning_count, metrics_json, created_at, updated_at)
            VALUES (?, 'done', 100, 1, 'real_layout', 'done', 0, 0, ?, ?, ?)
            """,
            (job_id, json.dumps({"quality_evaluation_version": 0}, ensure_ascii=False), now, now),
        )

    calls: list[dict] = []

    def _blocked_quality(result: dict) -> dict:
        calls.append(result)
        return {
            "quality_status": "blocked",
            "quality_score": 85.0,
            "can_publish": False,
            "artifact_mode": "real_layout",
            "manifest_status": "done",
            "blocking_count": 1,
            "warning_count": 0,
            "metrics": {
                "quality_evaluation_version": rq.QUALITY_EVALUATION_VERSION,
                "policy_blockers": ["new gate"],
                "pixel_metrics": {"flags": ["cutout_artifact"], "cv_penalty": 15.0},
            },
        }

    monkeypatch.setattr(rq, "evaluate_render_result", _blocked_quality)

    with db.connect() as conn:
        assert rq.backfill_existing_render_quality(conn) == 1
        qrow = conn.execute(
            "SELECT * FROM render_quality WHERE render_job_id = ?", (job_id,)
        ).fetchone()
        jrow = conn.execute("SELECT status FROM render_jobs WHERE id = ?", (job_id,)).fetchone()

    assert calls and calls[0]["output_path"] == str(final_board)
    assert qrow["quality_status"] == "blocked"
    assert qrow["can_publish"] == 0
    assert json.loads(qrow["metrics_json"])["quality_evaluation_version"] == rq.QUALITY_EVALUATION_VERSION
    assert jrow["status"] == "blocked"


def test_backfill_skips_current_quality_version(temp_db, seed_case, tmp_path, monkeypatch):
    from backend import db

    case_id = seed_case(abs_path="/tmp/case-current-quality-version", customer_raw="小稳")
    now = datetime.now(timezone.utc).isoformat()
    final_board = tmp_path / "final-board.jpg"
    Image.new("RGB", (64, 64), (24, 24, 24)).save(final_board, "JPEG")
    with db.connect() as conn:
        job_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, output_path,
               semantic_judge, meta_json)
            VALUES (?, 'fumei', 'tri-compare', 'done', ?, ?, ?, 'off', '{}')
            """,
            (case_id, now, now, str(final_board)),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO render_quality
              (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
               manifest_status, blocking_count, warning_count, metrics_json, created_at, updated_at)
            VALUES (?, 'done', 100, 1, 'real_layout', 'done', 0, 0, ?, ?, ?)
            """,
            (
                job_id,
                json.dumps(
                    {"quality_evaluation_version": rq.QUALITY_EVALUATION_VERSION},
                    ensure_ascii=False,
                ),
                now,
                now,
            ),
        )

    def _unexpected_recompute(result: dict) -> dict:
        raise AssertionError(f"current quality row should not be recomputed: {result}")

    monkeypatch.setattr(rq, "evaluate_render_result", _unexpected_recompute)

    with db.connect() as conn:
        assert rq.backfill_existing_render_quality(conn) == 0
        qrow = conn.execute(
            "SELECT quality_status, can_publish FROM render_quality WHERE render_job_id = ?",
            (job_id,),
        ).fetchone()

    assert qrow["quality_status"] == "done"
    assert qrow["can_publish"] == 1


def test_backfill_recomputes_existing_cancelled_quality_without_status_change(
    temp_db, seed_case, tmp_path, monkeypatch
):
    from backend import db

    case_id = seed_case(abs_path="/tmp/case-cancelled-quality-version", customer_raw="小停")
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        job_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, output_path,
               semantic_judge, meta_json)
            VALUES (?, 'fumei', 'tri-compare', 'cancelled', ?, ?, NULL, 'off', '{}')
            """,
            (case_id, now, now),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO render_quality
              (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
               manifest_status, blocking_count, warning_count, metrics_json, created_at, updated_at)
            VALUES (?, 'blocked', 5, 0, 'real_layout', 'error', 1, 0, ?, ?, ?)
            """,
            (job_id, json.dumps({"quality_evaluation_version": 0}, ensure_ascii=False), now, now),
        )

    def _blocked_quality(result: dict) -> dict:
        return {
            "quality_status": "blocked",
            "quality_score": 5.0,
            "can_publish": False,
            "artifact_mode": "real_layout",
            "manifest_status": "error",
            "blocking_count": 1,
            "warning_count": 0,
            "metrics": {"quality_evaluation_version": rq.QUALITY_EVALUATION_VERSION},
        }

    monkeypatch.setattr(rq, "evaluate_render_result", _blocked_quality)

    with db.connect() as conn:
        assert rq.backfill_existing_render_quality(conn) == 1
        row = conn.execute(
            """
            SELECT j.status, q.metrics_json
            FROM render_jobs j JOIN render_quality q ON q.render_job_id = j.id
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()

    assert row["status"] == "cancelled"
    assert json.loads(row["metrics_json"])["quality_evaluation_version"] == rq.QUALITY_EVALUATION_VERSION


def test_render_quality_marks_composition_alerts_as_review(tmp_path):
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "ai_usage": {},
            "composition_alerts": [
                {
                    "slot": "side",
                    "slot_label": "侧面",
                    "code": "before_body_scope_larger_than_after",
                    "severity": "warning",
                    "message": "侧面 术前主体高度比术后多 42px",
                    "recommended_action": "manual_reselect_or_edge_repair",
                    "metrics": {"height_delta": 42},
                }
            ],
        }
    )

    assert quality["quality_status"] == "done_with_issues"
    assert quality["can_publish"] is False
    assert quality["metrics"]["composition"] == "review"
    assert quality["metrics"]["composition_alerts"][0]["slot_label"] == "侧面"


def test_render_quality_downgrades_unselected_candidate_warnings(tmp_path):
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")
    manifest = tmp_path / "manifest.final.json"
    manifest.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "selected_slots": {
                            "front": {
                                "before": {"name": "术前-正面.jpg"},
                                "after": {"name": "术后-正面.jpg"},
                            }
                        }
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "manifest_path": str(manifest),
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 2,
            "ai_usage": {},
            "warnings": [
                "候选图 mystery.jpg - 面部检测失败: 未检测到面部",
                "术后 正面 存在多个姿态推断候选，已按最佳分数择优",
            ],
        }
    )

    assert quality["quality_status"] == "done"
    assert quality["can_publish"] is True
    assert quality["metrics"]["warning_buckets"]["candidate_noise"] == 1
    assert quality["metrics"]["warning_buckets"]["pose_candidates"] == 1
    assert quality["metrics"]["actionable_warning_count"] == 0


def test_render_quality_uses_warning_layers_for_actionable_count(tmp_path):
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 3,
            "ai_usage": {},
            "warnings": [
                "未入选.jpg - 面部检测失败",
                "入选侧面.jpg - 正脸检测失败，已使用侧脸检测兜底",
                "侧面 术前术后姿态差过大(yaw=90)",
            ],
            "warning_layers": {
                "selected_actionable": [],
                "selected_expected_profile": ["入选侧面.jpg - 正脸检测失败，已使用侧脸检测兜底"],
                "candidate_noise": ["未入选.jpg - 面部检测失败"],
                "stale_pose_noise": ["侧面 术前术后姿态差过大(yaw=90)"],
            },
        }
    )

    assert quality["quality_status"] == "done"
    assert quality["can_publish"] is True
    assert quality["metrics"]["actionable_warning_count"] == 0
    assert quality["metrics"]["noise_warning_count"] == 2
    assert quality["metrics"]["audit_warning_count"] == 3
    assert quality["metrics"]["warnings"] == []
    assert quality["metrics"]["audit_warnings"] == [
        "未入选.jpg - 面部检测失败",
        "入选侧面.jpg - 正脸检测失败，已使用侧脸检测兜底",
        "侧面 术前术后姿态差过大(yaw=90)",
    ]
    assert quality["metrics"]["warning_buckets"]["stale_pose_noise"] == 1


def test_ai_board_done_status_is_clean_when_only_light_pixel_signal(tmp_path, monkeypatch):
    """AI-enhanced board CLI returns status='done', not manifest status='ok'.

    A light CV signal such as cutout_artifact should remain visible and lower
    the score, but it must not be misread as a manifest error or hard blocker.
    """
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")
    monkeypatch.setattr(
        rq,
        "compute_pixel_metrics",
        lambda _path: {
            "available": True,
            "flags": ["cutout_artifact"],
            "cv_penalty": 12.5,
        },
    )

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "done",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "ai_usage": {"used_after_enhancement": False, "used_ai_padfill": False},
        }
    )

    assert quality["quality_score"] == 87.5
    assert quality["quality_status"] == "done"
    assert quality["can_publish"] is True
    assert quality["manifest_status"] == "done"
    assert quality["metrics"]["cv_flags"] == ["cutout_artifact"]


def test_render_quality_blocks_workbench_staging_title(tmp_path, monkeypatch):
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")
    monkeypatch.setattr(
        rq,
        "compute_pixel_metrics",
        lambda _path: {"available": True, "flags": [], "cv_penalty": 0.0},
    )

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "done",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "board_title": ".case-workbench-bound-render job-1006",
            "ai_usage": {
                "formal_ai_enhancement_run": True,
                "used_after_enhancement": True,
                "generated_artifact_count": 2,
                "external_call_count": 2,
                "cache_hit_count": 0,
                "fresh_ai_call": True,
            },
        }
    )

    assert quality["quality_status"] == "blocked"
    assert quality["can_publish"] is False
    assert quality["metrics"]["title_integrity"] == "blocked"
    assert any("staging/job" in item for item in quality["metrics"]["policy_blockers"])


def test_render_quality_blocks_bound_staging_without_title_context(tmp_path, monkeypatch):
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")
    monkeypatch.setattr(
        rq,
        "compute_pixel_metrics",
        lambda _path: {"available": True, "flags": [], "cv_penalty": 0.0},
    )

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "done",
            "case_mode": "ai_enhanced_board",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "ai_usage": {
                "formal_ai_enhancement_run": True,
                "used_after_enhancement": True,
                "generated_artifact_count": 2,
                "external_call_count": 2,
                "cache_hit_count": 0,
                "ai_enhance_command": {
                    "args": [
                        "python",
                        "render_ai_enhanced_boards.py",
                        "--case-dir",
                        "/x/.case-workbench-bound-render/job-1006",
                    ]
                },
            },
        }
    )

    assert quality["quality_status"] == "blocked"
    assert quality["can_publish"] is False
    assert quality["metrics"]["title_integrity"] == "blocked"
    assert any("缺少真实标题证据" in item for item in quality["metrics"]["policy_blockers"])


def test_render_quality_blocks_stage_token_in_project_title(tmp_path, monkeypatch):
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")
    monkeypatch.setattr(
        rq,
        "compute_pixel_metrics",
        lambda _path: {"available": True, "flags": [], "cv_penalty": 0.0},
    )

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "done",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "board_title": "骆萍 2026.3.31 塑公主2支注射下巴术前，衡力150u注射下颌缘颈阔肌咬肌",
            "ai_usage": {
                "formal_ai_enhancement_run": True,
                "used_after_enhancement": True,
                "generated_artifact_count": 3,
                "external_call_count": 3,
                "cache_hit_count": 0,
                "fresh_ai_call": True,
                "board_title_context": {
                    "customer_name": "骆萍",
                    "date": "2026.3.31",
                    "project": "塑公主2支注射下巴术前，衡力150u注射下颌缘颈阔肌咬肌",
                },
            },
        }
    )

    assert quality["quality_status"] == "blocked"
    assert quality["quality_score"] == 60.0
    assert quality["can_publish"] is False
    assert quality["metrics"]["title_integrity"] == "blocked"
    assert any("阶段词" in item for item in quality["metrics"]["policy_blockers"])


def test_render_quality_allows_source_treatment_stage_token_when_board_title_clean(tmp_path, monkeypatch):
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")
    monkeypatch.setattr(
        rq,
        "compute_pixel_metrics",
        lambda _path: {"available": True, "flags": [], "cv_penalty": 0.0},
    )

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "done",
            "template": "tri-compare",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "board_title": "林惠贞 2026.3.31 盈致2支注射面颊，娇兰注射唇2026.3.31塑公主4支注射耳基地",
            "ai_usage": {
                "formal_ai_enhancement_run": True,
                "used_after_enhancement": True,
                "generated_artifact_count": 3,
                "external_call_count": 3,
                "cache_hit_count": 0,
                "fresh_ai_call": True,
                "enhancement_evidence": {
                    "board_title": "林惠贞 2026.3.31 盈致2支注射面颊，娇兰注射唇2026.3.31塑公主4支注射耳基地",
                    "treatment": "2026.3.31盈致2支注射面颊，娇兰注射唇2026.3.31塑公主4支注射耳基地术前",
                    "board_title_context": {
                        "customer_name": "林惠贞",
                        "date": "",
                        "project": "",
                    },
                },
            },
        }
    )

    assert quality["quality_status"] == "done"
    assert quality["can_publish"] is True
    assert quality["metrics"]["title_integrity"] == "ok"
    assert not any("阶段词" in item for item in quality["metrics"]["policy_blockers"])


def test_render_quality_blocks_postop_cyan_cast_pixel_flag(tmp_path, monkeypatch):
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")
    monkeypatch.setattr(
        rq,
        "compute_pixel_metrics",
        lambda _path: {
            "available": True,
            "flags": ["postop_cyan_cast"],
            "cv_penalty": 10.0,
            "postop_skin_cast": {
                "evaluated": True,
                "flagged": True,
                "before": {"r_minus_g": 21.0, "r_minus_b": 36.0},
                "after": {"r_minus_g": 12.4, "r_minus_b": 28.8},
            },
        },
    )

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "done",
            "template": "single-compare",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "board_title": "黄靖榕 2026.03.31 弗缦1支注射泪沟，薇旖美1支注射眼下",
            "ai_usage": {
                "formal_ai_enhancement_run": True,
                "used_after_enhancement": True,
                "generated_artifact_count": 1,
                "external_call_count": 1,
                "cache_hit_count": 0,
                "source_profile": {"before_count": 1, "after_count": 1},
            },
        }
    )

    assert quality["quality_status"] == "blocked"
    assert quality["can_publish"] is False
    assert any("术后肤色偏青" in item for item in quality["metrics"]["policy_blockers"])


def test_render_quality_blocks_side_source_scale_mismatch_from_inferred_manifest(tmp_path, monkeypatch):
    render_dir = tmp_path / "render"
    render_dir.mkdir()
    output = render_dir / "final-board.jpg"
    output.write_bytes(b"jpeg")
    before = tmp_path / "before-side.jpg"
    after = tmp_path / "after-side.jpg"
    Image.new("RGB", (1000, 1000), (20, 20, 20)).save(before)
    Image.new("RGB", (1000, 1000), (20, 20, 20)).save(after)
    (render_dir / "manifest.final.json").write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "selected_slots": {
                            "side": {
                                "before": {
                                    "name": "before-side.jpg",
                                    "path": str(before),
                                    "crop_box": {"x1": 150, "y1": 120, "x2": 780, "y2": 780},
                                },
                                "after": {
                                    "name": "after-side.jpg",
                                    "path": str(after),
                                    "crop_box": {"x1": 260, "y1": 220, "x2": 760, "y2": 700},
                                },
                            }
                        }
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        rq,
        "compute_pixel_metrics",
        lambda _path: {"available": True, "flags": [], "cv_penalty": 0.0},
    )

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "done",
            "template": "bi-compare",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "ai_usage": {},
        }
    )

    assert quality["quality_status"] == "blocked"
    assert quality["can_publish"] is False
    assert quality["metrics"]["source_scale_policy"]["status"] == "blocked"
    assert any("侧面对比人物尺度不一致" in item for item in quality["metrics"]["policy_blockers"])


def test_bi_compare_blocks_cutout_penalty_at_policy_ceiling(tmp_path, monkeypatch):
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")
    monkeypatch.setattr(
        rq,
        "compute_pixel_metrics",
        lambda _path: {
            "available": True,
            "flags": ["cutout_artifact"],
            "cv_penalty": 15.0,
        },
    )

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "done",
            "template": "tri-compare",
            "effective_templates": ["bi-compare"],
            "blocking_issue_count": 0,
            "warning_count": 0,
            "ai_usage": {
                "used_after_enhancement": True,
                "external_call_count": 2,
                "cache_hit_count": 0,
                "generated_artifact_count": 2,
            },
        }
    )

    assert quality["quality_status"] == "blocked"
    assert quality["can_publish"] is False
    assert quality["metrics"]["edge_integrity"] == "blocked"
    assert quality["metrics"]["quality_template_tier"] == "bi"
    assert quality["metrics"]["template_quality_policy"]["max_cv_penalty"] == 15.0
    assert "达到或超过" in quality["metrics"]["policy_blockers"][0]


def test_bi_compare_blocks_side_scale_mismatch_cv_flag(tmp_path, monkeypatch):
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")
    monkeypatch.setattr(
        rq,
        "compute_pixel_metrics",
        lambda _path: {
            "available": True,
            "flags": ["side_scale_mismatch"],
            "cv_penalty": 12.0,
            "side_scale_mismatch": {
                "flagged": True,
                "ratios": {"skin_height": 1.26, "skin_area": 1.47},
            },
        },
    )

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "done",
            "template": "bi-compare",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "ai_usage": {
                "used_after_enhancement": True,
                "external_call_count": 2,
                "cache_hit_count": 0,
                "generated_artifact_count": 2,
            },
        }
    )

    assert quality["quality_status"] == "blocked"
    assert quality["can_publish"] is False
    assert quality["quality_score"] == 70.0
    assert quality["metrics"]["angle_match"] == "review"
    assert "侧面对比人物尺度不一致" in quality["metrics"]["policy_blockers"][0]


def test_ai_board_blocks_combined_cutout_and_blank_region(tmp_path, monkeypatch):
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")
    monkeypatch.setattr(
        rq,
        "compute_pixel_metrics",
        lambda _path: {
            "available": True,
            "flags": ["cutout_artifact", "blank_region"],
            "cv_penalty": 14.8,
        },
    )

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "done",
            "template": "tri-compare",
            "effective_templates": ["bi-compare"],
            "blocking_issue_count": 0,
            "warning_count": 0,
            "ai_usage": {
                "used_after_enhancement": True,
                "used_ai_padfill": False,
                "external_call_count": 1,
                "cache_hit_count": 0,
                "generated_artifact_count": 1,
            },
        }
    )

    assert quality["quality_status"] == "blocked"
    assert quality["can_publish"] is False
    assert quality["metrics"]["quality_template_tier"] == "bi"
    assert quality["metrics"]["cv_flags"] == ["cutout_artifact", "blank_region"]
    assert "cutout_artifact + blank_region" in quality["metrics"]["policy_blockers"][0]


def test_bi_compare_can_publish_under_template_policy(tmp_path, monkeypatch):
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")
    monkeypatch.setattr(
        rq,
        "compute_pixel_metrics",
        lambda _path: {"available": True, "flags": [], "cv_penalty": 5.0},
    )

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "ok",
            "template": "tri-compare",
            "effective_templates": ["bi-compare"],
            "blocking_issue_count": 0,
            "warning_count": 0,
            "ai_usage": {
                "source_profile": {"before_count": 1, "after_count": 1},
                "render_selection_slot_count": 2,
            },
        }
    )

    assert quality["quality_status"] == "done"
    assert quality["can_publish"] is True
    assert quality["metrics"]["quality_template_tier"] == "bi"
    assert quality["metrics"]["template_quality_policy"]["label"] == "bi_publishable"


def test_single_compare_blocks_without_info_or_copy(tmp_path, monkeypatch):
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")
    monkeypatch.setattr(
        rq,
        "compute_pixel_metrics",
        lambda _path: {"available": True, "flags": [], "cv_penalty": 3.0},
    )

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "ok",
            "template": "single-compare",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "ai_usage": {"source_profile": {"before_count": 1, "after_count": 0}},
        }
    )

    assert quality["quality_status"] == "blocked"
    assert quality["can_publish"] is False
    assert quality["blocking_count"] == 2
    assert quality["metrics"]["quality_template_tier"] == "single"
    assert quality["metrics"]["single_info_complete"] is False
    assert quality["metrics"]["single_explanation_present"] is False


def test_single_compare_can_publish_with_strict_policy(tmp_path, monkeypatch):
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")
    monkeypatch.setattr(
        rq,
        "compute_pixel_metrics",
        lambda _path: {"available": True, "flags": [], "cv_penalty": 6.0},
    )

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "ok",
            "template": "single-compare",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "explanatory_copy": "术前术后单角度对比，项目信息完整。",
            "ai_usage": {
                "source_profile": {"before_count": 1, "after_count": 1},
                "render_selection_slot_count": 1,
            },
        }
    )

    assert quality["quality_status"] == "done"
    assert quality["can_publish"] is True
    assert quality["metrics"]["single_info_complete"] is True
    assert quality["metrics"]["single_explanation_present"] is True


def test_formal_ai_enhancement_requires_real_evidence(tmp_path, monkeypatch):
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")
    monkeypatch.setattr(
        rq,
        "compute_pixel_metrics",
        lambda _path: {"available": True, "flags": [], "cv_penalty": 4.0},
    )

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "done",
            "case_mode": "ai_enhanced_board",
            "enhance": {"direction": "heal", "model": "gemini-3-pro-image"},
            "blocking_issue_count": 0,
            "warning_count": 0,
            "ai_usage": {"formal_ai_enhancement_run": True},
        }
    )

    assert quality["quality_status"] == "blocked"
    assert quality["can_publish"] is False
    assert quality["metrics"]["formal_ai_enhancement_required"] is True
    assert quality["metrics"]["formal_ai_enhancement_verified"] is False
    assert quality["metrics"]["formal_ai_enhancement_gate"] == "missing_evidence"


def test_formal_ai_enhancement_with_cache_evidence_can_publish(tmp_path, monkeypatch):
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")
    monkeypatch.setattr(
        rq,
        "compute_pixel_metrics",
        lambda _path: {"available": True, "flags": [], "cv_penalty": 4.0},
    )

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "done",
            "case_mode": "ai_enhanced_board",
            "enhance": {"direction": "heal", "model": "gemini-3-pro-image"},
            "blocking_issue_count": 0,
            "warning_count": 0,
            "ai_usage": {
                "formal_ai_enhancement_run": True,
                "used_after_enhancement": True,
                "generated_artifact_count": 3,
                "cache_hit_count": 3,
                "enhancement_evidence": {
                    "generated_count": 3,
                    "cache_hit_count": 3,
                    "external_call_count": 0,
                    "provider_counts": {"cache": 3},
                },
            },
        }
    )

    assert quality["quality_status"] == "done"
    assert quality["can_publish"] is True
    assert quality["artifact_mode"] == "ai_after_simulation"
    assert quality["metrics"]["formal_ai_enhancement_gate"] == "verified"
    assert quality["metrics"]["fresh_ai_enhancement_verified"] is False


def test_fresh_ai_enhancement_blocks_when_only_cache_evidence(tmp_path, monkeypatch):
    output = tmp_path / "final-board.jpg"
    output.write_bytes(b"jpeg")
    monkeypatch.setattr(
        rq,
        "compute_pixel_metrics",
        lambda _path: {"available": True, "flags": [], "cv_penalty": 4.0},
    )

    quality = evaluate_render_result(
        {
            "output_path": str(output),
            "status": "done",
            "case_mode": "ai_enhanced_board",
            "enhance": {"direction": "heal", "model": "gemini-3-pro-image"},
            "require_fresh_ai_enhancement": True,
            "blocking_issue_count": 0,
            "warning_count": 0,
            "ai_usage": {
                "formal_ai_enhancement_run": True,
                "used_after_enhancement": True,
                "generated_artifact_count": 2,
                "cache_hit_count": 2,
                "enhancement_evidence": {
                    "generated_count": 2,
                    "cache_hit_count": 2,
                    "external_call_count": 0,
                    "provider_counts": {"cache": 2},
                },
            },
        }
    )

    assert quality["quality_status"] == "blocked"
    assert quality["can_publish"] is False
    assert quality["metrics"]["formal_ai_enhancement_verified"] is True
    assert quality["metrics"]["fresh_ai_enhancement_required"] is True
    assert quality["metrics"]["fresh_ai_enhancement_verified"] is False
    assert quality["metrics"]["formal_ai_enhancement_gate"] == "missing_external_call_evidence"


def test_render_quality_review_updates_row(client, seed_case):
    case_id = seed_case(abs_path="/tmp/case-quality")
    from backend import db

    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        job_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'done_with_issues', ?, ?, 'off', 'standard')
            """,
            (case_id, now, now),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO render_quality
              (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
               manifest_status, blocking_count, warning_count, metrics_json, created_at, updated_at)
            VALUES (?, 'done_with_issues', 52, 0, 'real_layout', 'error', 2, 4, '{}', ?, ?)
            """,
            (job_id, now, now),
        )

    resp = client.post(
        f"/api/render-jobs/{job_id}/quality-review",
        json={"verdict": "needs_recheck", "reviewer": "qa", "note": "边缘破碎"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["review_verdict"] == "needs_recheck"
    assert body["reviewer"] == "qa"
    assert body["can_publish"] is False


def test_render_done_with_issues_is_pending_evaluation(client, seed_case):
    case_id = seed_case(abs_path="/tmp/case-quality-pending")
    from backend import db

    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'done_with_issues', ?, ?, 'off', 'standard')
            """,
            (case_id, now, now),
        )

    resp = client.get("/api/evaluations/pending", params={"subject_kind": "render"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["case_id"] == case_id


def test_render_quality_queue_lists_real_jobs_and_review_state(client, seed_case):
    issue_case_id = seed_case(abs_path="/tmp/case-quality-queue", customer_raw="小绿")
    failed_case_id = seed_case(abs_path="/tmp/case-quality-queue-failed", customer_raw="小红")
    from backend import db

    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        issue_job_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, output_path,
               error_message, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'done_with_issues', ?, ?, '/tmp/final-board.jpg',
                    NULL, 'auto', 'standard')
            """,
            (issue_case_id, now, now),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO render_quality
              (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
               manifest_status, blocking_count, warning_count, metrics_json, created_at, updated_at)
            VALUES (?, 'done_with_issues', 71, 0, 'real_layout', 'ok', 0, 3, ?, ?, ?)
            """,
            (
                issue_job_id,
                json.dumps({"warnings": ["侧面对齐需要复核"]}, ensure_ascii=False),
                now,
                now,
            ),
        )
        failed_job_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, error_message, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'failed', ?, ?, 'subprocess exit 1', 'auto', 'standard')
            """,
            (failed_case_id, now, now),
        ).lastrowid

    resp = client.get("/api/render/quality-queue")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = [item["job"]["id"] for item in body["items"]]
    assert failed_job_id in ids
    assert issue_job_id in ids
    issue_item = next(item for item in body["items"] if item["job"]["id"] == issue_job_id)
    assert issue_item["case"]["customer_raw"] == "小绿"
    assert issue_item["warning_summary"] == ["侧面对齐需要复核"]
    failed_item = next(item for item in body["items"] if item["job"]["id"] == failed_job_id)
    assert failed_item["reviewable"] is False
    assert failed_item["issue_summary"] == ["subprocess exit 1"]

    review_resp = client.post(
        f"/api/render-jobs/{issue_job_id}/quality-review",
        json={"verdict": "approved", "reviewer": "qa", "can_publish": True},
    )
    assert review_resp.status_code == 200, review_resp.text

    reviewed = client.get("/api/render/quality-queue", params={"status": "reviewed"})
    assert reviewed.status_code == 200
    reviewed_ids = [item["job"]["id"] for item in reviewed.json()["items"]]
    assert issue_job_id in reviewed_ids

    pending = client.get("/api/render/quality-queue")
    pending_ids = [item["job"]["id"] for item in pending.json()["items"]]
    assert issue_job_id not in pending_ids


def test_render_quality_queue_hides_old_failures_after_publishable_latest(client, seed_case):
    case_id = seed_case(abs_path="/tmp/case-quality-current-latest", customer_raw="小新")
    from backend import db

    older = datetime(2026, 5, 1, tzinfo=timezone.utc).isoformat()
    newer = datetime(2026, 5, 2, tzinfo=timezone.utc).isoformat()
    with db.connect() as conn:
        failed_job_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, error_message, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'failed', ?, ?, 'old renderer crash', 'off', 'standard')
            """,
            (case_id, older, older),
        ).lastrowid
        latest_job_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, output_path, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'done', ?, ?, '/tmp/current-final-board.jpg', 'off', 'standard')
            """,
            (case_id, newer, newer),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO render_quality
              (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
               manifest_status, blocking_count, warning_count, metrics_json, created_at, updated_at)
            VALUES (?, 'done', 96, 1, 'real_layout', 'ok', 0, 0, ?, ?, ?)
            """,
            (
                latest_job_id,
                json.dumps({"warnings": [], "warning_buckets": {"actionable_count": 0}}, ensure_ascii=False),
                newer,
                newer,
            ),
        )

    pending = client.get("/api/render/quality-queue")
    assert pending.status_code == 200, pending.text
    pending_ids = [item["job"]["id"] for item in pending.json()["items"]]
    assert failed_job_id not in pending_ids
    assert latest_job_id not in pending_ids

    all_items = client.get("/api/render/quality-queue", params={"status": "all"})
    assert all_items.status_code == 200, all_items.text
    ids = [item["job"]["id"] for item in all_items.json()["items"]]
    assert latest_job_id in ids
    assert failed_job_id in ids
    assert ids.index(latest_job_id) < ids.index(failed_job_id)


def test_render_quality_queue_defaults_to_latest_problem_per_case(client, seed_case):
    case_id = seed_case(abs_path="/tmp/case-quality-current-problem", customer_raw="小旧")
    from backend import db

    first = datetime(2026, 5, 1, 8, tzinfo=timezone.utc).isoformat()
    second = datetime(2026, 5, 1, 9, tzinfo=timezone.utc).isoformat()
    latest = datetime(2026, 5, 1, 10, tzinfo=timezone.utc).isoformat()
    with db.connect() as conn:
        old_failed_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, error_message, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'failed', ?, ?, 'old renderer crash', 'off', 'standard')
            """,
            (case_id, first, first),
        ).lastrowid
        old_blocked_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, error_message, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'blocked', ?, ?, 'old source blocker', 'off', 'standard')
            """,
            (case_id, second, second),
        ).lastrowid
        latest_failed_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, error_message, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'failed', ?, ?, 'current timeout', 'off', 'standard')
            """,
            (case_id, latest, latest),
        ).lastrowid

    pending = client.get("/api/render/quality-queue")
    assert pending.status_code == 200, pending.text
    body = pending.json()
    pending_ids = [item["job"]["id"] for item in body["items"]]
    assert pending_ids == [latest_failed_id]
    assert body["total"] == 1
    assert body["counts"]["failed"] == 1
    assert body["counts"].get("blocked", 0) == 0
    assert body["archive"]["hidden_by_current_latest"] == 2
    assert body["archive"]["by_status"] == {"failed": 1, "blocked": 1}

    all_items = client.get("/api/render/quality-queue", params={"status": "all"})
    assert all_items.status_code == 200, all_items.text
    all_ids = [item["job"]["id"] for item in all_items.json()["items"]]
    assert latest_failed_id in all_ids
    assert old_failed_id in all_ids
    assert old_blocked_id in all_ids


def test_quality_report_current_baseline_uses_latest_job_per_case(client, seed_case, tmp_path):
    publishable_case_id = seed_case(abs_path="/tmp/case-quality-report-publishable", customer_raw="小新")
    failed_case_id = seed_case(abs_path="/tmp/case-quality-report-current-failed", customer_raw="小旧")
    from backend import db

    old_time = datetime(2026, 5, 1, tzinfo=timezone.utc).isoformat()
    new_time = datetime(2026, 5, 2, tzinfo=timezone.utc).isoformat()
    final_board = tmp_path / "current-final-board.jpg"
    final_board.write_bytes(b"jpeg")
    manifest = tmp_path / "manifest.final.json"
    manifest.write_text("{}", encoding="utf-8")

    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, error_message, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'failed', ?, ?, 'old crash', 'off', 'standard')
            """,
            (publishable_case_id, old_time, old_time),
        )
        latest_done_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, output_path,
               manifest_path, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'done', ?, ?, ?, ?, 'off', 'standard')
            """,
            (publishable_case_id, new_time, new_time, str(final_board), str(manifest)),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO render_quality
              (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
               manifest_status, blocking_count, warning_count, metrics_json, created_at, updated_at)
            VALUES (?, 'done', 98, 1, 'real_layout', 'ok', 0, 0, ?, ?, ?)
            """,
            (
                latest_done_id,
                json.dumps({"warning_buckets": {"actionable_count": 0}}, ensure_ascii=False),
                new_time,
                new_time,
            ),
        )
        conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, output_path,
               manifest_path, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'done', ?, ?, ?, ?, 'off', 'standard')
            """,
            (failed_case_id, old_time, old_time, str(final_board), str(manifest)),
        )
        conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, error_message, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'failed', ?, ?, 'current timeout', 'off', 'standard')
            """,
            (failed_case_id, new_time, new_time),
        )

    resp = client.get("/api/cases/quality-report")
    assert resp.status_code == 200, resp.text
    baseline = resp.json()["render"]["current_version_baseline"]
    assert baseline["scope"] == "current_latest_per_case_recent_30"
    assert baseline["sample_size"] == 2
    assert baseline["by_status"] == {"failed": 1, "done": 1}
    assert baseline["historical_archived_count"] == 2
    assert baseline["renderer_success_rate_excluding_blocked"] == 0.5
    assert baseline["publishable_rate"] == 0.5
    assert baseline["artifact_visibility"]["output_artifact_count"] == 1
    assert baseline["artifact_visibility"]["final_board_visible_count"] == 1


def test_quality_report_exposes_delivery_baseline_without_counting_blocked_as_renderer_failure(
    client, seed_case, tmp_path
):
    publishable_case_id = seed_case(abs_path="/tmp/case-delivery-publishable", customer_raw="小新")
    failed_case_id = seed_case(abs_path="/tmp/case-delivery-failed", customer_raw="小旧")
    blocked_case_id = seed_case(abs_path="/tmp/case-delivery-blocked", customer_raw="小挡")
    issue_case_id = seed_case(abs_path="/tmp/case-delivery-issue", customer_raw="小黄")
    from backend import db

    now = datetime(2026, 5, 6, tzinfo=timezone.utc).isoformat()
    final_board = tmp_path / "final-board.jpg"
    final_board.write_bytes(b"jpeg")
    manifest = tmp_path / "manifest.final.json"
    manifest.write_text("{}", encoding="utf-8")
    with db.connect() as conn:
        done_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, output_path,
               manifest_path, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'done', ?, ?, ?, ?, 'off', 'standard')
            """,
            (publishable_case_id, now, now, str(final_board), str(manifest)),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO render_quality
              (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
               manifest_status, blocking_count, warning_count, metrics_json, created_at, updated_at)
            VALUES (?, 'done', 96, 1, 'real_layout', 'ok', 0, 0, ?, ?, ?)
            """,
            (done_id, json.dumps({"warning_buckets": {"actionable_count": 0}}, ensure_ascii=False), now, now),
        )
        conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, error_message, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'failed', ?, ?, 'renderer timeout', 'off', 'standard')
            """,
            (failed_case_id, now, now),
        )
        conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, error_message, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'blocked', ?, ?, '分类未闭环', 'off', 'standard')
            """,
            (blocked_case_id, now, now),
        )
        issue_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, output_path,
               manifest_path, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'done_with_issues', ?, ?, ?, ?, 'off', 'standard')
            """,
            (issue_case_id, now, now, str(final_board), str(manifest)),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO render_quality
              (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
               manifest_status, blocking_count, warning_count, metrics_json, created_at, updated_at)
            VALUES (?, 'done_with_issues', 72, 0, 'real_layout', 'ok', 0, 1, ?, ?, ?)
            """,
            (
                issue_id,
                json.dumps(
                    {
                        "warning_buckets": {"actionable_count": 1},
                        "action_suggestions": [
                            {"code": "reselect_pair", "label": "回到源组候选重选姿态更接近的术前术后配对"}
                        ],
                    },
                    ensure_ascii=False,
                ),
                now,
                now,
            ),
        )

    resp = client.get("/api/cases/quality-report")
    assert resp.status_code == 200, resp.text
    delivery = resp.json()["delivery_baseline"]
    assert delivery["scope"] == "current_latest_per_case_delivery_v1"
    assert delivery["sample_size"] == 4
    assert delivery["renderer"]["terminal_count"] == 4
    assert delivery["renderer"]["blocked_guardrail_count"] == 1
    assert delivery["renderer"]["failed_count"] == 1
    assert delivery["renderer"]["failed_rate_excluding_blocked"] == 0.3333
    assert delivery["renderer"]["blocked_is_guardrail"] is True
    assert delivery["publishability"]["publishable_count"] == 1
    assert delivery["publishability"]["publishable_rate"] == 0.25
    assert delivery["publishability"]["final_board_visible_rate"] == 1.0
    assert delivery["quality"]["done_with_issues_rate"] == 0.5
    assert delivery["root_causes"]["top_causes"][0]["code"] in {"classification_open", "renderer_failed", "pair_quality"}


def test_render_job_detail_exposes_delivery_audit_from_meta_and_quality(client, seed_case, tmp_path):
    case_id = seed_case(abs_path="/tmp/case-delivery-job-detail", customer_raw="小审")
    from backend import db

    now = datetime.now(timezone.utc).isoformat()
    final_board = tmp_path / "final-board.jpg"
    final_board.write_bytes(b"jpeg")
    meta = {
        "run_id": "run-123",
        "code_version": {"commit": "abc123", "dirty": False},
        "source_manifest_hash": "sha256:manifest",
        "render_selection_audit": {
            "applied_slots": [{"slot": "front"}, {"slot": "oblique"}],
            "dropped_slots": [{"view": "side", "reason": {"code": "low_comparison_value"}}],
        },
        "render_selection_source_provenance": [
            {"slot": "front", "role": "before", "case_id": case_id, "filename": "before.jpg"}
        ],
    }
    with db.connect() as conn:
        job_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, output_path,
               semantic_judge, meta_json)
            VALUES (?, 'fumei', 'tri-compare', 'done', ?, ?, ?, 'off', ?)
            """,
            (case_id, now, now, str(final_board), json.dumps(meta, ensure_ascii=False)),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO render_quality
              (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
               manifest_status, blocking_count, warning_count, metrics_json, created_at, updated_at)
            VALUES (?, 'done', 96, 1, 'real_layout', 'ok', 0, 0, ?, ?, ?)
            """,
            (job_id, json.dumps({"actionable_warning_count": 0}, ensure_ascii=False), now, now),
        )

    resp = client.get(f"/api/render/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    audit = resp.json()["delivery_audit"]
    assert audit["run_id"] == "run-123"
    assert audit["code_version"]["commit"] == "abc123"
    assert audit["source_manifest_hash"] == "sha256:manifest"
    assert audit["selected_slots"] == ["front", "oblique"]
    assert audit["dropped_slots"][0]["view"] == "side"
    assert audit["quality_summary"]["can_publish"] is True
    assert audit["quality_summary"]["quality_score"] == 96


def test_render_quality_queue_hides_stale_pose_noise_from_summary(client, seed_case):
    case_id = seed_case(abs_path="/tmp/case-quality-stale-noise", customer_raw="小蓝")
    from backend import db

    now = datetime.now(timezone.utc).isoformat()
    raw_warnings = [
        "未入选.jpg - 面部检测失败",
        "侧面 术前术后姿态差过大(yaw=90)",
    ]
    with db.connect() as conn:
        job_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, output_path,
               semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'done_with_issues', ?, ?, '/tmp/final-board.jpg',
                    'off', 'standard')
            """,
            (case_id, now, now),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO render_quality
              (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
               manifest_status, blocking_count, warning_count, metrics_json, created_at, updated_at)
            VALUES (?, 'done', 96, 1, 'real_layout', 'ok', 0, 2, ?, ?, ?)
            """,
            (
                job_id,
                json.dumps(
                    {
                        "warnings": [],
                        "display_warnings": [],
                        "audit_warnings": raw_warnings,
                        "warning_layers": {
                            "selected_actionable": [],
                            "candidate_noise": [raw_warnings[0]],
                            "stale_pose_noise": [raw_warnings[1]],
                        },
                        "warning_buckets": {
                            "candidate_noise": 1,
                            "stale_pose_noise": 1,
                            "noise_count": 1,
                            "audit_noise_count": 2,
                            "actionable_count": 0,
                        },
                    },
                    ensure_ascii=False,
                ),
                now,
                now,
            ),
        )

    resp = client.get("/api/render/quality-queue", params={"status": "all"})
    assert resp.status_code == 200, resp.text
    item = next(row for row in resp.json()["items"] if row["job"]["id"] == job_id)
    assert item["warning_summary"] == []
