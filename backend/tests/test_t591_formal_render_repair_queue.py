"""T59.1 formal render blocker repair queue tests."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _record(
    *,
    job_id: int,
    case_id: int,
    status: str,
    blocking_issues: list[str] | None = None,
    warnings: list[str] | None = None,
    error_message: str | None = None,
    artifact_status: str = "missing_final_board_and_manifest",
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
        "error_message": error_message,
        "artifact_integrity": {"status": artifact_status},
        "quality": {
            "quality_status": status,
            "can_publish": False,
            "metrics": {
                "blocking_issues": blocking_issues or [],
                "warnings": warnings or [],
                "display_warnings": warnings or [],
                "action_suggestions": [],
            },
        },
        "delivery_envelope": {"class": "experimental_blocked", "can_deliver": False},
    }


def test_build_repair_queue_groups_classification_slot_and_quality_actions() -> None:
    from backend.scripts import formal_render_repair_queue as repair

    matrix_report = {
        "run_status": "completed_real_render_matrix",
        "used_mock_data": False,
        "records": [
            _record(
                job_id=205,
                case_id=4,
                status="blocked",
                error_message="正式出图已阻断：还有未闭环的照片分类任务（低置信 1 张）。",
                blocking_issues=["未闭环图片：side-before.jpg"],
            ),
            _record(
                job_id=209,
                case_id=21,
                status="blocked",
                error_message="正式出图已阻断：三联正式出图槽位未配齐。",
                blocking_issues=["缺槽位：45° 术前,术后", "缺槽位：侧面 术前,术后"],
            ),
            _record(
                job_id=207,
                case_id=18,
                status="done_with_issues",
                warnings=["视觉补判仅供参考(confidence=low)：provider quota failed"],
                artifact_status="ok",
            ),
        ],
    }
    matrix_report["records"][1]["hard_blockers"] = ["blocking_issue:缺槽位：45° 术前,术后"]

    queue = repair.build_repair_queue(matrix_report)

    assert queue["run_status"] == "completed_real_repair_queue"
    assert queue["used_mock_data"] is False
    assert queue["summary"]["source_record_count"] == 3
    assert queue["summary"]["action_item_count"] == 3
    assert queue["summary"]["by_category"] == {
        "classification_closure": 1,
        "slot_completeness": 1,
        "quality_review": 1,
    }
    action_by_job = {item["job_id"]: item for item in queue["action_items"]}
    assert action_by_job[205]["category"] == "classification_closure"
    assert action_by_job[205]["files_to_review"] == ["side-before.jpg"]
    assert action_by_job[209]["missing_slots"] == [
        {"view": "45°", "phases": ["术前", "术后"]},
        {"view": "侧面", "phases": ["术前", "术后"]},
    ]
    assert action_by_job[207]["category"] == "quality_review"
    assert action_by_job[207]["requires_render_rerun_after_fix"] is False


def test_manual_override_trace_action_uses_live_override_rows(tmp_path: Path) -> None:
    from backend.scripts import formal_render_repair_queue as repair

    db_path = tmp_path / "case-workbench.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE case_image_overrides (
          case_id INTEGER NOT NULL,
          filename TEXT NOT NULL,
          manual_phase TEXT,
          manual_view TEXT,
          updated_at TEXT,
          reason_json TEXT,
          reviewer TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO case_image_overrides
        (case_id, filename, manual_phase, manual_view, updated_at, reason_json, reviewer)
        VALUES (66, '术前3.JPG', 'before', 'front', '2026-05-13T00:00:00Z', NULL, NULL)
        """
    )
    conn.commit()
    conn.close()

    matrix_report = {
        "run_status": "completed_real_render_matrix",
        "used_mock_data": False,
        "records": [
            _record(
                job_id=219,
                case_id=66,
                status="blocked",
                error_message="正式出图已阻断：还有未闭环的照片分类任务（人工覆盖缺少原因 1 张）。",
                blocking_issues=["未闭环图片：术前3.JPG"],
            )
        ],
    }

    queue = repair.build_repair_queue(matrix_report, db_path=db_path)

    assert queue["summary"]["by_category"] == {"manual_override_trace": 1}
    item = queue["action_items"][0]
    assert item["category"] == "manual_override_trace"
    assert item["override_trace_gaps"] == [
        {
            "filename": "术前3.JPG",
            "manual_phase": "before",
            "manual_view": "front",
            "missing": ["reason_json", "reviewer"],
        }
    ]


def test_blocked_report_when_t59_report_missing_does_not_fabricate_actions() -> None:
    from backend.scripts import formal_render_repair_queue as repair

    report = repair.blocked_report("missing T59 matrix")

    assert report["run_status"] == "blocked_missing_real_t59_matrix"
    assert report["used_mock_data"] is False
    assert report["summary"]["source_record_count"] == 0
    assert report["action_items"] == []
    assert "未验证/无法获取" in report["decision"]
