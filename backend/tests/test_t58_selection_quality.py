"""T58: source classification and best-pair quality gates."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend import db, render_queue


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _touch_files(case_dir: Path, names: list[str]) -> None:
    for name in names:
        target = case_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"real-source-bytes")


def _mk_case(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    abs_path: str,
    image_files: list[str],
    skill_meta: list[dict[str, Any]] | None = None,
) -> None:
    now = _now()
    conn.execute(
        """INSERT OR IGNORE INTO scans (id, started_at, completed_at, root_paths, case_count, mode)
           VALUES (1, ?, ?, '/tmp', 1, 'test')""",
        (now, now),
    )
    conn.execute(
        """INSERT INTO cases
             (id, scan_id, abs_path, customer_raw, category, template_tier,
              blocking_issues_json, last_modified, indexed_at, meta_json, skill_image_metadata_json)
           VALUES (?, 1, ?, 'tester', 'A', 'standard', '[]', ?, ?, ?, ?)""",
        (
            case_id,
            abs_path,
            now,
            now,
            json.dumps({"image_files": image_files}, ensure_ascii=False),
            json.dumps(skill_meta or [], ensure_ascii=False),
        ),
    )


def _seed_best_pair(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    candidates: list[dict[str, Any]],
    fingerprint: str = "fp-v1",
) -> None:
    now = _now()
    conn.execute(
        """INSERT INTO case_best_pairs
             (case_id, status, candidates_json, candidates_fingerprint,
              source_version, scanned_at, updated_at)
           VALUES (?, 'ready', ?, ?, 0, ?, ?)""",
        (case_id, json.dumps(candidates, ensure_ascii=False), fingerprint, now, now),
    )


def test_render_preflight_blocks_low_confidence_manual_override_without_traceability() -> None:
    image_files = ["front-before.jpg", "front-after.jpg"]
    skill_meta = [
        {
            "filename": "front-before.jpg",
            "relative_path": "front-before.jpg",
            "phase": "before",
            "view": "front",
            "angle_confidence": 0.31,
        },
        {
            "filename": "front-after.jpg",
            "relative_path": "front-after.jpg",
            "phase": "after",
            "view": "front",
            "angle_confidence": 0.34,
        },
    ]

    result = render_queue._classification_blocking_preflight(
        case_meta_json=json.dumps({"image_files": image_files}, ensure_ascii=False),
        skill_image_metadata_json=json.dumps(skill_meta, ensure_ascii=False),
        image_files=image_files,
        manual_overrides={
            "front-before.jpg": {"phase": "before", "view": "front"},
            "front-after.jpg": {"phase": "after", "view": "front"},
        },
        semantic_judge="auto",
    )

    assert result is not None
    assert result["ai_usage"]["classification_untraced_manual_override_count"] == 2
    assert "人工覆盖缺少原因 2 张" in result["render_error"]


def test_render_preflight_accepts_low_confidence_override_with_reviewer_and_reason() -> None:
    image_files = ["front-before.jpg", "front-after.jpg"]
    skill_meta = [
        {
            "filename": "front-before.jpg",
            "relative_path": "front-before.jpg",
            "phase": "before",
            "view": "front",
            "angle_confidence": 0.31,
        },
        {
            "filename": "front-after.jpg",
            "relative_path": "front-after.jpg",
            "phase": "after",
            "view": "front",
            "angle_confidence": 0.34,
        },
    ]

    result = render_queue._classification_blocking_preflight(
        case_meta_json=json.dumps({"image_files": image_files}, ensure_ascii=False),
        skill_image_metadata_json=json.dumps(skill_meta, ensure_ascii=False),
        image_files=image_files,
        manual_overrides={
            "front-before.jpg": {
                "phase": "before",
                "view": "front",
                "reviewer": "qa",
                "reason": "低置信正面已人工确认",
            },
            "front-after.jpg": {
                "phase": "after",
                "view": "front",
                "reviewer": "qa",
                "reason": "低置信正面已人工确认",
            },
        },
        semantic_judge="auto",
    )

    assert result is None


def test_best_pair_ranks_sharp_complete_pair_before_blurry_lower_pose_delta(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc

    case_dir = tmp_path / "case-best-pair-quality"
    files = ["术前/front-b.jpg", "术后/front-blurry.jpg", "术后/front-good.jpg"]
    _touch_files(case_dir, files)
    skill_meta = [
        {
            "filename": "front-b.jpg",
            "relative_path": "术前/front-b.jpg",
            "phase": "before",
            "view": "front",
            "pose": {"yaw": 0, "pitch": 0, "roll": 0},
            "angle_confidence": 0.96,
            "sharpness_score": 88,
            "face_detected": True,
        },
        {
            "filename": "front-blurry.jpg",
            "relative_path": "术后/front-blurry.jpg",
            "phase": "after",
            "view": "front",
            "pose": {"yaw": 0.1, "pitch": 0.1, "roll": 0},
            "angle_confidence": 0.38,
            "sharpness_score": 6,
            "rejection_reason": "face_detection_failure",
            "issues": ["面部检测失败"],
        },
        {
            "filename": "front-good.jpg",
            "relative_path": "术后/front-good.jpg",
            "phase": "after",
            "view": "front",
            "pose": {"yaw": 1.8, "pitch": 0.8, "roll": 0.2},
            "angle_confidence": 0.93,
            "sharpness_score": 84,
            "face_detected": True,
        },
    ]
    with db.connect() as conn:
        _mk_case(conn, case_id=1, abs_path=str(case_dir), image_files=files, skill_meta=skill_meta)

    result = svc.compute_best_pair(1)

    front = result["candidates_by_slot"]["front"]
    assert front[0]["after"] == "术后/front-good.jpg"
    breakdown = front[0]["score_breakdown"]
    assert breakdown["pose"]["score"] > 0
    assert breakdown["sharpness"]["score"] > 0
    assert breakdown["face_completeness"]["score"] > 0
    assert breakdown["comparability"]["score"] > 0
    assert front[0]["rank_audit"]["quality_label"] == "strong"


def test_best_pair_select_persists_reviewer_and_reason(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc

    case_dir = tmp_path / "case-select-reason"
    files = ["front-b.jpg", "front-a.jpg"]
    _touch_files(case_dir, files)
    with db.connect() as conn:
        _mk_case(conn, case_id=1, abs_path=str(case_dir), image_files=files)
        _seed_best_pair(
            conn,
            case_id=1,
            candidates=[{"view": "front", "before": "front-b.jpg", "after": "front-a.jpg", "delta_deg": 0.5}],
            fingerprint="fp-reason",
        )

    selection_id = svc.select_best_pair_for_case(
        1,
        "front-b.jpg",
        "front-a.jpg",
        "fp-reason",
        view="front",
        reviewer="reviewer-a",
        reason="正面可比性最好，保留为正式出图输入",
    )

    with db.connect() as conn:
        selection = conn.execute(
            "SELECT selected_by, selection_reason FROM case_best_pair_selections WHERE id = ?",
            (selection_id,),
        ).fetchone()
        overrides = conn.execute(
            """SELECT filename, reviewer, reason_json
               FROM case_image_overrides
               WHERE case_id = 1
               ORDER BY filename""",
        ).fetchall()
        meta_row = conn.execute("SELECT meta_json FROM cases WHERE id = 1").fetchone()

    assert selection["selected_by"] == "reviewer-a"
    assert selection["selection_reason"] == "正面可比性最好，保留为正式出图输入"
    assert {row["reviewer"] for row in overrides} == {"reviewer-a"}
    assert all("正面可比性最好" in (row["reason_json"] or "") for row in overrides)
    lock = json.loads(meta_row["meta_json"])["source_group_selection"]["locked_slots"]["front"]
    assert lock["reviewer"] == "reviewer-a"
    assert lock["reason"] == "正面可比性最好，保留为正式出图输入"
