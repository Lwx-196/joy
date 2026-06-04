"""T65 formal render review packet and decision gate tests."""
from __future__ import annotations

import json
from pathlib import Path


def _item(tmp_path: Path, *, case_id: int = 4, job_id: int = 320) -> dict:
    case_dir = tmp_path / f"case-{case_id}"
    render_dir = case_dir / ".case-layout-output" / "fumei" / "single-compare" / "render"
    render_dir.mkdir(parents=True)
    final_board = render_dir / "final-board.jpg"
    manifest = render_dir / "manifest.final.json"
    final_board.write_bytes(b"real-jpeg")
    manifest.write_text(json.dumps({"status": "done_with_issues"}, ensure_ascii=False), encoding="utf-8")
    return {
        "primary_category": "template_downgrade_review",
        "categories": ["template_downgrade_review", "manual_quality_review"],
        "job_id": job_id,
        "case_id": case_id,
        "case_path": str(case_dir),
        "customer_raw": f"case-{case_id}",
        "job_status": "done_with_issues",
        "quality_status": "done_with_issues",
        "quality_score": 96.5,
        "template": "single-compare",
        "artifact_integrity": "ok",
        "final_board_path": str(final_board),
        "manifest_path": str(manifest),
        "warning_count": 1,
        "blocking_issue_count": 0,
        "warning_samples": ["自动降级已排除 侧面：缺少术前 侧面"],
        "blocking_issue_samples": [],
        "recommended_next_step": "复核自动降级是否符合交付模板预期；不满足则补齐槽位。",
        "requires_new_material_or_reselect": False,
        "blocks_publish": True,
        "publishable_after_warning_review": False,
        "delivery_envelope_class": "experimental_blocked",
        "delivery_can_deliver": False,
        "delivery_reasons": ["render_quality.quality_status:done_with_issues"],
    }


def test_review_packet_copies_only_existing_real_final_boards_and_manifest(tmp_path: Path) -> None:
    from backend.scripts import formal_render_review_packet as packet

    source = {
        "run_status": "completed_real_consolidated_warning_repair_queue",
        "used_mock_data": False,
        "summary": {"action_item_count": 1},
        "action_items": [_item(tmp_path)],
    }
    out = tmp_path / "packet"

    summary = packet.build_review_packet(source, out)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    draft = json.loads((out / "review_decisions_draft.json").read_text(encoding="utf-8"))

    assert summary["ready_for_review"] is True
    assert manifest["review_unit_count"] == 1
    assert manifest["missing_asset_count"] == 0
    unit = manifest["review_units"][0]
    assert Path(unit["packet_final_board_path"]).is_file()
    assert Path(unit["packet_manifest_path"]).is_file()
    assert unit["source_final_board_path"].endswith("final-board.jpg")
    assert draft["decisions"][0]["reviewer"] is None
    assert draft["decisions"][0]["decision"] is None
    assert (out / "index.html").is_file()


def test_review_packet_missing_real_artifact_is_not_review_ready(tmp_path: Path) -> None:
    from backend.scripts import formal_render_review_packet as packet

    item = _item(tmp_path)
    Path(item["final_board_path"]).unlink()
    report = packet.build_review_packet({"action_items": [item], "used_mock_data": False}, tmp_path / "packet")

    assert report["ready_for_review"] is False
    assert report["missing_asset_count"] == 1
    manifest = json.loads(Path(report["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["review_units"][0]["ready_for_review"] is False


def test_empty_or_missing_reviewer_decisions_fail_closed(tmp_path: Path) -> None:
    from backend.scripts import formal_render_review_decision_gate as gate
    from backend.scripts import formal_render_review_packet as packet

    manifest = packet.build_review_packet({"action_items": [_item(tmp_path)], "used_mock_data": False}, tmp_path / "packet")
    manifest_payload = json.loads(Path(manifest["manifest_path"]).read_text(encoding="utf-8"))
    draft = json.loads(Path(manifest["decision_draft_path"]).read_text(encoding="utf-8"))

    validation = gate.validate_review_decisions(draft, manifest_payload)

    assert validation["validation_status"] == "unverified_missing_human_decisions"
    assert validation["accepted_decision_count"] == 0
    assert validation["ready_for_repair_queue"] is False
    assert "未验证/无法获取" in validation["decision"]


def test_valid_decisions_generate_repair_queue_without_auto_publish(tmp_path: Path) -> None:
    from backend.scripts import formal_render_review_decision_gate as gate
    from backend.scripts import formal_render_review_packet as packet

    manifest = packet.build_review_packet({"action_items": [_item(tmp_path)], "used_mock_data": False}, tmp_path / "packet")
    manifest_payload = json.loads(Path(manifest["manifest_path"]).read_text(encoding="utf-8"))
    unit_id = manifest_payload["review_units"][0]["unit_id"]
    decisions = {
        "decisions": [
            {
                "unit_id": unit_id,
                "case_id": 4,
                "job_id": 320,
                "reviewer": "human-reviewer",
                "decision": "needs_slot_fill",
                "review_note": "侧面缺术前，需补槽位后复跑",
            }
        ]
    }

    validation = gate.validate_review_decisions(decisions, manifest_payload)
    repair = gate.build_repair_queue(validation, manifest_payload)

    assert validation["validation_status"] == "ready_for_repair_queue"
    assert validation["accepted_decision_count"] == 1
    assert repair["summary"]["accepted_decision_count"] == 1
    assert repair["summary"]["accept_template_downgrade_count"] == 0
    assert repair["summary"]["requires_rerun_count"] == 1
    assert repair["repair_items"][0]["recommended_action"] == "fill_missing_slots"
    assert repair["repair_items"][0]["can_publish_now"] is False


def test_invalid_decision_value_is_rejected(tmp_path: Path) -> None:
    from backend.scripts import formal_render_review_decision_gate as gate
    from backend.scripts import formal_render_review_packet as packet

    manifest = packet.build_review_packet({"action_items": [_item(tmp_path)], "used_mock_data": False}, tmp_path / "packet")
    manifest_payload = json.loads(Path(manifest["manifest_path"]).read_text(encoding="utf-8"))
    unit_id = manifest_payload["review_units"][0]["unit_id"]
    validation = gate.validate_review_decisions(
        {"decisions": [{"unit_id": unit_id, "reviewer": "qa", "decision": "publish"}]},
        manifest_payload,
    )

    assert validation["accepted_decision_count"] == 0
    assert validation["rejected_decision_count"] == 1
    assert validation["rejected_decisions"][0]["reason_code"] == "invalid_decision"


def test_click_review_server_saves_formal_decision(tmp_path: Path) -> None:
    from backend.scripts import formal_render_review_packet as packet
    from backend.scripts import formal_render_review_server as server

    summary = packet.build_review_packet({"action_items": [_item(tmp_path)], "used_mock_data": False}, tmp_path / "packet")
    manifest_path = Path(summary["manifest_path"])
    draft_path = Path(summary["decision_draft_path"])
    state = server.build_review_state(manifest_path, draft_path)
    unit_id = state["units"][0]["unit_id"]

    result = server.save_review_decision(
        manifest_path,
        draft_path,
        {
            "unit_id": unit_id,
            "reviewer": "human-reviewer",
            "decision": "accept_template_downgrade",
            "review_note": "降级单图可接受",
        },
    )

    assert result["ok"] is True
    saved = json.loads(draft_path.read_text(encoding="utf-8"))["decisions"][0]
    assert saved["reviewer"] == "human-reviewer"
    assert saved["decision"] == "accept_template_downgrade"
    assert saved["reviewed_at"]


def test_import_review_decisions_marks_only_accepted_template_downgrade_publishable(tmp_path: Path) -> None:
    from backend.scripts import formal_render_review_import as importer

    db_path = tmp_path / "case-workbench.db"
    conn = importer.sqlite3.connect(db_path)
    conn.row_factory = importer.sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE render_jobs (
          id INTEGER PRIMARY KEY,
          status TEXT
        );
        CREATE TABLE render_quality (
          render_job_id INTEGER PRIMARY KEY,
          quality_status TEXT,
          quality_score REAL,
          can_publish INTEGER,
          artifact_mode TEXT,
          manifest_status TEXT,
          blocking_count INTEGER,
          warning_count INTEGER,
          metrics_json TEXT,
          review_verdict TEXT,
          reviewer TEXT,
          review_note TEXT,
          reviewed_at TEXT,
          updated_at TEXT
        );
        INSERT INTO render_jobs (id, status) VALUES (322, 'done_with_issues');
        INSERT INTO render_jobs (id, status) VALUES (330, 'done_with_issues');
        INSERT INTO render_quality
          (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
           manifest_status, blocking_count, warning_count, metrics_json)
        VALUES
          (322, 'done_with_issues', 90, 0, 'real_layout', 'ok', 0, 5, '{}'),
          (330, 'done_with_issues', 92, 0, 'real_layout', 'ok', 0, 5, '{}');
        """
    )
    conn.commit()
    conn.close()

    report = importer.import_review_decisions(
        {
            "accepted_decisions": [
                {
                    "job_id": 322,
                    "case_id": 22,
                    "reviewer": "joy",
                    "decision": "accept_template_downgrade",
                    "review_note": "模板降级可接受",
                },
                {
                    "job_id": 330,
                    "case_id": 27,
                    "reviewer": "joy",
                    "decision": "needs_rerender",
                    "review_note": "需要重跑",
                },
            ]
        },
        db_path=db_path,
    )

    assert report["summary"]["publishable_import_count"] == 1
    assert report["summary"]["needs_recheck_import_count"] == 1
    conn = importer.sqlite3.connect(db_path)
    conn.row_factory = importer.sqlite3.Row
    accepted = conn.execute("SELECT * FROM render_quality WHERE render_job_id = 322").fetchone()
    rerender = conn.execute("SELECT * FROM render_quality WHERE render_job_id = 330").fetchone()
    conn.close()
    assert accepted["can_publish"] == 1
    assert accepted["review_verdict"] == "approved"
    assert rerender["can_publish"] == 0
    assert rerender["review_verdict"] == "needs_recheck"


def test_import_review_decisions_refuses_publish_when_hard_blocker_exists(tmp_path: Path) -> None:
    from backend.scripts import formal_render_review_import as importer

    db_path = tmp_path / "case-workbench.db"
    conn = importer.sqlite3.connect(db_path)
    conn.row_factory = importer.sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE render_jobs (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE render_quality (
          render_job_id INTEGER PRIMARY KEY,
          quality_status TEXT,
          quality_score REAL,
          can_publish INTEGER,
          artifact_mode TEXT,
          manifest_status TEXT,
          blocking_count INTEGER,
          warning_count INTEGER,
          metrics_json TEXT,
          review_verdict TEXT,
          reviewer TEXT,
          review_note TEXT,
          reviewed_at TEXT,
          updated_at TEXT
        );
        INSERT INTO render_jobs (id, status) VALUES (322, 'done_with_issues');
        INSERT INTO render_quality
          (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
           manifest_status, blocking_count, warning_count, metrics_json)
        VALUES
          (322, 'done_with_issues', 90, 0, 'real_layout', 'ok', 2, 5, '{}');
        """
    )
    conn.commit()
    conn.close()

    report = importer.import_review_decisions(
        {
            "accepted_decisions": [
                {
                    "job_id": 322,
                    "case_id": 22,
                    "reviewer": "joy",
                    "decision": "accept_template_downgrade",
                    "review_note": "模板降级可接受",
                }
            ]
        },
        db_path=db_path,
    )

    assert report["summary"]["publishable_import_count"] == 0
    assert report["imported_items"][0]["can_publish"] is False
    assert "render_quality.blocking_count" in report["imported_items"][0]["hard_blockers"]
