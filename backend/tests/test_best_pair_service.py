"""Best-pair service: compute, select, queue, and render handoff."""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from backend import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mk_case(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    abs_path: str,
    image_files: list[str] | None = None,
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
            json.dumps({"image_files": image_files or []}, ensure_ascii=False),
            json.dumps(skill_meta, ensure_ascii=False) if skill_meta is not None else None,
        ),
    )


def _touch_files(case_dir: Path, names: list[str]) -> None:
    for name in names:
        target = case_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"real-source-bytes")


def _seed_best_pair(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    candidates: list[dict[str, Any]],
    fingerprint: str = "fp-v1",
    status: str = "ready",
    source_version: int = 0,
) -> None:
    now = _now()
    conn.execute(
        """INSERT INTO case_best_pairs
             (case_id, status, candidates_json, candidates_fingerprint,
              source_version, scanned_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (case_id, status, json.dumps(candidates, ensure_ascii=False), fingerprint, source_version, now, now),
    )


def test_compute_uses_real_source_files_and_skill_pose_top5(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc

    case_dir = tmp_path / "case-real"
    files = ["术前/b1.jpg", "术前/b2.jpg", "术后/a1.jpg", "术后/a2.jpg", "术后/a3.jpg"]
    _touch_files(case_dir, files)
    skill_meta = [
        {"filename": "b1.jpg", "relative_path": "术前/b1.jpg", "phase": "before", "pose": {"yaw": 5, "pitch": 2, "roll": 1}},
        {"filename": "b2.jpg", "relative_path": "术前/b2.jpg", "phase": "before", "pose": {"yaw": 13, "pitch": 3, "roll": 0}},
        {"filename": "a1.jpg", "relative_path": "术后/a1.jpg", "phase": "after", "pose": {"yaw": 5.1, "pitch": 2.2, "roll": 1.2}},
        {"filename": "a2.jpg", "relative_path": "术后/a2.jpg", "phase": "after", "pose": {"yaw": 7, "pitch": 5, "roll": 3}},
        {"filename": "a3.jpg", "relative_path": "术后/a3.jpg", "phase": "after", "pose": {"yaw": 12.8, "pitch": 3.2, "roll": 0.1}},
    ]
    with db.connect() as conn:
        _mk_case(conn, case_id=1, abs_path=str(case_dir), image_files=files, skill_meta=skill_meta)

    with patch("backend.services.best_pair_service._analyze_faces", side_effect=AssertionError("skill pose should be reused")):
        result = svc.compute_best_pair(1)

    assert result["status"] == "ready"
    assert result["fingerprint"]
    assert len(result["candidates"]) == 5
    assert result["candidates"][0]["before"] == "术前/b1.jpg"
    assert result["candidates"][0]["after"] == "术后/a1.jpg"
    assert result["candidates"][0]["delta_deg"] <= result["candidates"][-1]["delta_deg"]
    with db.connect() as conn:
        row = conn.execute("SELECT status, candidates_json FROM case_best_pairs WHERE case_id = 1").fetchone()
    assert row["status"] == "ready"
    assert json.loads(row["candidates_json"])[0]["after"] == "术后/a1.jpg"


def test_compute_returns_slot_grouped_candidates_from_view_metadata(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc

    case_dir = tmp_path / "case-slots"
    files = ["术前/front-b.jpg", "术后/front-a.jpg", "术前/side-b.jpg", "术后/side-a.jpg"]
    _touch_files(case_dir, files)
    skill_meta = [
        {
            "filename": "front-b.jpg",
            "relative_path": "术前/front-b.jpg",
            "phase": "before",
            "view": "front",
            "pose": {"yaw": 1, "pitch": 0, "roll": 0},
        },
        {
            "filename": "front-a.jpg",
            "relative_path": "术后/front-a.jpg",
            "phase": "after",
            "view": "front",
            "pose": {"yaw": 1.2, "pitch": 0.1, "roll": 0},
        },
        {
            "filename": "side-b.jpg",
            "relative_path": "术前/side-b.jpg",
            "phase": "before",
            "view": "side",
            "pose": {"yaw": 76, "pitch": 1, "roll": 0},
        },
        {
            "filename": "side-a.jpg",
            "relative_path": "术后/side-a.jpg",
            "phase": "after",
            "view": "side",
            "pose": {"yaw": 77, "pitch": 1.5, "roll": 0.2},
        },
    ]
    with db.connect() as conn:
        _mk_case(conn, case_id=1, abs_path=str(case_dir), image_files=files, skill_meta=skill_meta)

    result = svc.compute_best_pair(1)

    assert result["status"] == "ready"
    assert result["candidates_by_slot"]["front"][0]["view"] == "front"
    assert result["candidates_by_slot"]["front"][0]["before"] == "术前/front-b.jpg"
    assert result["candidates_by_slot"]["front"][0]["after"] == "术后/front-a.jpg"
    assert result["candidates_by_slot"]["side"][0]["view"] == "side"
    assert result["candidates_by_slot"]["side"][0]["before"] == "术前/side-b.jpg"
    assert result["candidates_by_slot"]["side"][0]["after"] == "术后/side-a.jpg"
    listed = svc.list_best_pair(1)
    assert listed["candidates_by_slot"]["front"][0]["before"] == "术前/front-b.jpg"
    assert listed["candidates_by_slot"]["side"][0]["after"] == "术后/side-a.jpg"


def test_front_slot_ranks_pitch_safe_pair_before_lower_delta_pitch_risk(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc

    case_dir = tmp_path / "case-front-pitch"
    files = ["术前/front-b.jpg", "术后/front-risk.jpg", "术后/front-safe.jpg"]
    _touch_files(case_dir, files)
    skill_meta = [
        {
            "filename": "front-b.jpg",
            "relative_path": "术前/front-b.jpg",
            "phase": "before",
            "view": "front",
            "pose": {"yaw": 0, "pitch": 0, "roll": 0},
        },
        {
            "filename": "front-risk.jpg",
            "relative_path": "术后/front-risk.jpg",
            "phase": "after",
            "view": "front",
            "pose": {"yaw": 0, "pitch": 7.3, "roll": 0},
        },
        {
            "filename": "front-safe.jpg",
            "relative_path": "术后/front-safe.jpg",
            "phase": "after",
            "view": "front",
            "pose": {"yaw": 4.4, "pitch": 6.0, "roll": 0},
        },
    ]
    with db.connect() as conn:
        _mk_case(conn, case_id=1, abs_path=str(case_dir), image_files=files, skill_meta=skill_meta)

    result = svc.compute_best_pair(1)

    front = result["candidates_by_slot"]["front"]
    assert front[0]["after"] == "术后/front-safe.jpg"
    assert front[0]["delta_pitch"] == 6.0
    assert "front_pitch_within_threshold" in front[0]["rank_audit"]["reasons"]
    assert front[1]["after"] == "术后/front-risk.jpg"
    assert front[1]["delta_pitch"] == 7.3
    assert "front_pitch_over_threshold" in front[1]["rank_audit"]["warnings"]


def test_front_slot_all_pitch_risky_returns_material_loop_task(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc

    case_dir = tmp_path / "case-front-pitch-gap"
    files = ["术前/front-b.jpg", "术后/front-risk-1.jpg", "术后/front-risk-2.jpg"]
    _touch_files(case_dir, files)
    skill_meta = [
        {
            "filename": "front-b.jpg",
            "relative_path": "术前/front-b.jpg",
            "phase": "before",
            "view": "front",
            "pose": {"yaw": 0, "pitch": 0, "roll": 0},
        },
        {
            "filename": "front-risk-1.jpg",
            "relative_path": "术后/front-risk-1.jpg",
            "phase": "after",
            "view": "front",
            "pose": {"yaw": 1, "pitch": 7.8, "roll": 0},
        },
        {
            "filename": "front-risk-2.jpg",
            "relative_path": "术后/front-risk-2.jpg",
            "phase": "after",
            "view": "front",
            "pose": {"yaw": 0.5, "pitch": 8.4, "roll": 0},
        },
    ]
    with db.connect() as conn:
        _mk_case(conn, case_id=1, abs_path=str(case_dir), image_files=files, skill_meta=skill_meta)

    result = svc.compute_best_pair(1)

    task = result["material_tasks"][0]
    assert task["code"] == "front_pitch_material_gap"
    assert task["view"] == "front"
    assert task["severity"] == "block_publish"
    assert task["publish_gate"]["can_publish_after_acceptance"] is False
    assert "补一组 pitch 更接近的正面术前/术后" in task["recommended_actions"][0]["label"]
    assert task["recommended_actions"][0]["href"].startswith("/cases/1?manual_seed_source=front_pitch_blocker")
    assert "manual_seed_view=front" in task["recommended_actions"][0]["href"]
    assert task["recommended_actions"][0]["href"].endswith("#manual-render")
    assert "人工接受" in task["recommended_actions"][1]["label"]
    assert task["candidate_count"] == 2
    assert task["best_candidate"]["after"] == "术后/front-risk-1.jpg"

    listed = svc.list_best_pair(1)
    assert listed["material_tasks"][0]["code"] == "front_pitch_material_gap"


def test_compute_persists_each_slot_even_when_global_top5_is_one_slot(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc

    case_dir = tmp_path / "case-slot-persist"
    files = [
        "术前/front-b.jpg",
        "术后/front-a.jpg",
        *[f"术前/oblique-b{i}.jpg" for i in range(3)],
        *[f"术后/oblique-a{i}.jpg" for i in range(3)],
    ]
    _touch_files(case_dir, files)
    skill_meta = [
        {
            "filename": "front-b.jpg",
            "relative_path": "术前/front-b.jpg",
            "phase": "before",
            "view": "front",
            "pose": {"yaw": 0, "pitch": 0, "roll": 0},
        },
        {
            "filename": "front-a.jpg",
            "relative_path": "术后/front-a.jpg",
            "phase": "after",
            "view": "front",
            "pose": {"yaw": 20, "pitch": 0, "roll": 0},
        },
    ]
    for index in range(3):
        skill_meta.extend(
            [
                {
                    "filename": f"oblique-b{index}.jpg",
                    "relative_path": f"术前/oblique-b{index}.jpg",
                    "phase": "before",
                    "view": "oblique",
                    "pose": {"yaw": 30 + index * 0.1, "pitch": 2, "roll": 0},
                },
                {
                    "filename": f"oblique-a{index}.jpg",
                    "relative_path": f"术后/oblique-a{index}.jpg",
                    "phase": "after",
                    "view": "oblique",
                    "pose": {"yaw": 30 + index * 0.1, "pitch": 2.2, "roll": 0},
                },
            ]
        )
    with db.connect() as conn:
        _mk_case(conn, case_id=1, abs_path=str(case_dir), image_files=files, skill_meta=skill_meta)

    svc.compute_best_pair(1)
    listed = svc.list_best_pair(1)

    assert listed["candidates_by_slot"]["oblique"]
    assert listed["candidates_by_slot"]["front"][0]["before"] == "术前/front-b.jpg"


def test_compute_source_version_race_keeps_dirty(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc

    case_dir = tmp_path / "case-race"
    files = ["术前/before.jpg", "术后/after.jpg"]
    _touch_files(case_dir, files)
    with db.connect() as conn:
        _mk_case(conn, case_id=1, abs_path=str(case_dir), image_files=files)
        _seed_best_pair(conn, case_id=1, candidates=[], status="pending", source_version=0)

    def _fake_analyze(_: Path, filenames: list[str]) -> dict[str, dict[str, float]]:
        with db.connect() as conn:
            conn.execute("UPDATE case_best_pairs SET source_version = source_version + 1, status = 'dirty' WHERE case_id = 1")
        assert set(filenames) == set(files)
        return {
            "术前/before.jpg": {"yaw": 1.0, "pitch": 2.0, "roll": 0.5},
            "术后/after.jpg": {"yaw": 1.2, "pitch": 2.1, "roll": 0.4},
        }

    with patch("backend.services.best_pair_service._analyze_faces", side_effect=_fake_analyze):
        result = svc.compute_best_pair(1)

    assert result["status"] == "dirty"
    with db.connect() as conn:
        row = conn.execute("SELECT status, source_version, candidates_json FROM case_best_pairs WHERE case_id = 1").fetchone()
    assert row["status"] == "dirty"
    assert row["source_version"] == 1
    assert row["candidates_json"] == "[]"


def test_compute_skips_when_phase_pair_missing(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc

    case_dir = tmp_path / "case-missing-phase"
    files = ["术前/b1.jpg", "术前/b2.jpg"]
    _touch_files(case_dir, files)
    with db.connect() as conn:
        _mk_case(conn, case_id=1, abs_path=str(case_dir), image_files=files)

    result = svc.compute_best_pair(1)

    assert result["status"] == "skipped"
    assert result["skipped_reason"] == "no_phase_labels"


def test_compute_tolerates_missing_local_pose_backend_when_skill_pose_sufficient(
    temp_db: Path,
    tmp_path: Path,
) -> None:
    from backend.services import best_pair_service as svc

    case_dir = tmp_path / "case-partial-skill"
    files = ["术前/b1.jpg", "术后/a1.jpg", "术后/a2.jpg"]
    _touch_files(case_dir, files)
    skill_meta = [
        {"filename": "b1.jpg", "relative_path": "术前/b1.jpg", "phase": "before", "pose": {"yaw": 2, "pitch": 1, "roll": 0}},
        {"filename": "a1.jpg", "relative_path": "术后/a1.jpg", "phase": "after", "pose": {"yaw": 2.2, "pitch": 1.1, "roll": 0.1}},
    ]
    with db.connect() as conn:
        _mk_case(conn, case_id=1, abs_path=str(case_dir), image_files=files, skill_meta=skill_meta)

    with patch("backend.services.best_pair_service._analyze_faces", side_effect=ModuleNotFoundError("cv2")):
        result = svc.compute_best_pair(1)

    assert result["status"] == "ready"
    assert result["candidates"][0]["before"] == "术前/b1.jpg"
    assert result["candidates"][0]["after"] == "术后/a1.jpg"


def test_select_writes_selection_snapshots_and_overrides_without_dirty(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc

    case_dir = tmp_path / "case-select"
    files = ["术前/b.jpg", "术后/a.jpg"]
    _touch_files(case_dir, files)
    candidates = [{"before": "术前/b.jpg", "after": "术后/a.jpg", "delta_deg": 1.25}]
    with db.connect() as conn:
        _mk_case(conn, case_id=1, abs_path=str(case_dir), image_files=files)
        _seed_best_pair(conn, case_id=1, candidates=candidates, fingerprint="fp-v1", source_version=4)
        conn.execute(
            """INSERT INTO case_image_overrides
                 (case_id, filename, manual_phase, manual_view, manual_transform_json, updated_at)
               VALUES (1, '术前/b.jpg', 'after', 'side', '{"scale":1.1}', ?)""",
            (_now(),),
        )

    selection_id = svc.select_best_pair_for_case(1, "术前/b.jpg", "术后/a.jpg", "fp-v1")

    assert selection_id > 0
    with db.connect() as conn:
        selection = conn.execute("SELECT * FROM case_best_pair_selections WHERE id = ?", (selection_id,)).fetchone()
        overrides = conn.execute(
            "SELECT filename, manual_phase, manual_view FROM case_image_overrides WHERE case_id = 1 ORDER BY filename"
        ).fetchall()
        cache = conn.execute("SELECT status, source_version, candidates_fingerprint FROM case_best_pairs WHERE case_id = 1").fetchone()
    assert selection["before_filename"] == "术前/b.jpg"
    assert selection["after_filename"] == "术后/a.jpg"
    assert selection["candidates_fingerprint"] == "fp-v1"
    assert selection["candidates_fingerprint_snapshot"] == "fp-v1"
    assert json.loads(selection["before_override_before_json"])["manual_phase"] == "after"
    assert selection["after_override_before_json"] is None
    assert [(row["filename"], row["manual_phase"]) for row in overrides] == [
        ("术前/b.jpg", "before"),
        ("术后/a.jpg", "after"),
    ]
    assert cache["status"] == "ready"
    assert cache["source_version"] == 5
    assert cache["candidates_fingerprint"] == "fp-v1"


def test_select_slot_writes_source_group_lock_and_selection_view(temp_db: Path, tmp_path: Path) -> None:
    from backend import source_selection
    from backend.services import best_pair_service as svc

    case_dir = tmp_path / "case-select-slot"
    files = ["术前/front-b.jpg", "术后/front-a.jpg", "术前/side-b.jpg", "术后/side-a.jpg"]
    _touch_files(case_dir, files)
    candidates = [
        {"view": "front", "before": "术前/front-b.jpg", "after": "术后/front-a.jpg", "delta_deg": 0.6},
        {"view": "side", "before": "术前/side-b.jpg", "after": "术后/side-a.jpg", "delta_deg": 1.1},
    ]
    with db.connect() as conn:
        _mk_case(conn, case_id=1, abs_path=str(case_dir), image_files=files)
        _seed_best_pair(conn, case_id=1, candidates=candidates, fingerprint="fp-slot", source_version=3)

    selection_id = svc.select_best_pair_for_case(1, "术前/front-b.jpg", "术后/front-a.jpg", "fp-slot", view="front")

    with db.connect() as conn:
        selection = conn.execute("SELECT view FROM case_best_pair_selections WHERE id = ?", (selection_id,)).fetchone()
        case_row = conn.execute("SELECT meta_json FROM cases WHERE id = 1").fetchone()
    meta = json.loads(case_row["meta_json"])
    lock = meta[source_selection.SOURCE_GROUP_SELECTION_META_KEY]["locked_slots"]["front"]
    assert selection["view"] == "front"
    assert lock["before"] == {"case_id": 1, "filename": "术前/front-b.jpg"}
    assert lock["after"] == {"case_id": 1, "filename": "术后/front-a.jpg"}
    assert lock["reviewer"] == "best-pair"
    assert "best-pair" in lock["reason"]


def test_select_rejects_stale_fingerprint(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc
    from fastapi import HTTPException

    case_dir = tmp_path / "case-stale"
    files = ["b.jpg", "a.jpg"]
    _touch_files(case_dir, files)
    with db.connect() as conn:
        _mk_case(conn, case_id=1, abs_path=str(case_dir), image_files=files)
        _seed_best_pair(conn, case_id=1, candidates=[{"before": "b.jpg", "after": "a.jpg", "delta_deg": 1}], fingerprint="real")

    with pytest.raises(HTTPException) as exc_info:
        svc.select_best_pair_for_case(1, "b.jpg", "a.jpg", "old")

    assert exc_info.value.status_code == 409


def test_select_rejects_path_outside_case(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc
    from fastapi import HTTPException

    case_dir = tmp_path / "case-path"
    files = ["b.jpg", "a.jpg"]
    _touch_files(case_dir, files)
    with db.connect() as conn:
        _mk_case(conn, case_id=1, abs_path=str(case_dir), image_files=files)
        _seed_best_pair(
            conn,
            case_id=1,
            candidates=[{"before": "b.jpg", "after": "../a.jpg", "delta_deg": 1}],
            fingerprint="fp",
        )

    with pytest.raises(HTTPException) as exc_info:
        svc.select_best_pair_for_case(1, "b.jpg", "../a.jpg", "fp")

    assert exc_info.value.status_code == 400


def test_trigger_best_pair_render_enqueues_mode_and_selection_snapshot(temp_db: Path, tmp_path: Path, no_job_pool) -> None:
    from backend.services import best_pair_service as svc

    case_dir = tmp_path / "case-render"
    _touch_files(case_dir, ["b.jpg", "a.jpg"])
    with db.connect() as conn:
        _mk_case(conn, case_id=1, abs_path=str(case_dir), image_files=["b.jpg", "a.jpg"])
        _seed_best_pair(conn, case_id=1, candidates=[{"before": "b.jpg", "after": "a.jpg", "delta_deg": 1}], fingerprint="fp-render")
    selection_id = svc.select_best_pair_for_case(1, "b.jpg", "a.jpg", "fp-render")

    job_id = svc.trigger_best_pair_render(1, brand="fumei", template="tri-compare")

    with db.connect() as conn:
        row = conn.execute(
            "SELECT render_mode, best_pair_selection_id, candidates_fingerprint_snapshot FROM render_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert row["render_mode"] == "best-pair"
    assert row["best_pair_selection_id"] == selection_id
    assert row["candidates_fingerprint_snapshot"] == "fp-render"


def test_compute_queue_runs_batch_serially(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.workers.best_pair_compute_queue import BestPairComputeQueue

    calls: list[int] = []

    def _submit(fn, *args, **kwargs):
        fut: Future = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except Exception as exc:  # pragma: no cover - defensive for failure output.
            fut.set_exception(exc)
        return fut

    monkeypatch.setattr("backend.workers.best_pair_compute_queue._job_pool.submit", _submit)
    monkeypatch.setattr(
        "backend.workers.best_pair_compute_queue.best_pair_service.compute_best_pair",
        lambda case_id: calls.append(case_id) or {"status": "ready"},
    )
    queue = BestPairComputeQueue()

    batch_id = queue.submit_batch([3, 1, 2])
    status = queue.status(batch_id)

    assert calls == [3, 1, 2]
    assert status is not None
    assert status["status"] == "done"
    assert status["done"] == 3
    assert status["failed"] == 0
