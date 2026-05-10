"""Stage B: case_image_overrides table + PATCH endpoint + render injection.

Coverage:
- Schema bootstrap: case_image_overrides table exists after init_schema
- PATCH happy paths: phase only / view only / both
- PATCH validation: bad enum → 400 / traversal → 400 / no fields → 400
- Clear semantics: empty string clears one dim; both cleared deletes row
- Override merge in GET /api/cases/{id}: manual phase wins over skill phase
- Render injection: render_queue feeds overrides into render_executor.run_render
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pytest


def _write_case_files(abs_path: str, filenames: list[str]) -> None:
    root = Path(abs_path)
    root.mkdir(parents=True, exist_ok=True)
    for filename in filenames:
        target = root / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"real image bytes")


def _override_count(client) -> int:
    from backend import db

    with db.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM case_image_overrides").fetchone()
    return int(row["n"])


def test_schema_creates_overrides_table(temp_db):
    from backend import db

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='case_image_overrides'"
        ).fetchall()
    assert len(rows) == 1


def test_patch_phase_only_creates_row(client, seed_case):
    case_id = seed_case()
    resp = client.patch(
        f"/api/cases/{case_id}/images/术前1.jpeg",
        json={"manual_phase": "before"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["case_id"] == case_id
    assert data["filename"] == "术前1.jpeg"
    assert data["manual_phase"] == "before"
    assert data["manual_view"] is None
    assert _override_count(client) == 1


def test_patch_view_only_creates_row(client, seed_case):
    case_id = seed_case()
    resp = client.patch(
        f"/api/cases/{case_id}/images/术后1.jpeg",
        json={"manual_view": "front"},
    )
    assert resp.status_code == 200
    assert resp.json()["manual_view"] == "front"


def test_patch_both_fields(client, seed_case):
    case_id = seed_case()
    resp = client.patch(
        f"/api/cases/{case_id}/images/sample.jpg",
        json={"manual_phase": "after", "manual_view": "side"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["manual_phase"] == "after"
    assert body["manual_view"] == "side"


def test_patch_subsequent_partial_update_preserves_other(client, seed_case):
    case_id = seed_case()
    client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_phase": "before", "manual_view": "front"},
    )
    # Update only view, phase should remain
    resp = client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_view": "oblique"},
    )
    body = resp.json()
    assert body["manual_phase"] == "before"
    assert body["manual_view"] == "oblique"


def test_patch_clear_one_dim_keeps_row(client, seed_case):
    case_id = seed_case()
    client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_phase": "before", "manual_view": "front"},
    )
    resp = client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_phase": ""},  # clear phase
    )
    body = resp.json()
    assert body["manual_phase"] is None
    assert body["manual_view"] == "front"
    assert _override_count(client) == 1


def test_patch_clear_both_deletes_row(client, seed_case):
    case_id = seed_case()
    client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_phase": "before", "manual_view": "front"},
    )
    resp = client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_phase": "", "manual_view": ""},
    )
    body = resp.json()
    assert body["manual_phase"] is None
    assert body["manual_view"] is None
    assert _override_count(client) == 0


def test_patch_invalid_phase_returns_400(client, seed_case):
    case_id = seed_case()
    resp = client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_phase": "operative"},  # not in allowed set
    )
    assert resp.status_code == 400
    assert "manual_phase" in resp.json()["detail"]


def test_patch_invalid_view_returns_400(client, seed_case):
    case_id = seed_case()
    resp = client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_view": "back"},
    )
    assert resp.status_code == 400


def test_patch_path_traversal_returns_400(client, seed_case):
    """Inputs that survive httpx URL normalization and reach our handler must
    be rejected with 400. Note: httpx auto-resolves bare '..' / '.' segments
    so those cases get caught at the routing layer, not in our handler — for
    those we accept any 4xx as proof of refusal."""
    case_id = seed_case()
    # Case-relative subdirectories are allowed; traversal and backslash are not.
    ok = client.patch(
        f"/api/cases/{case_id}/images/subdir/x.jpg",
        json={"manual_phase": "before"},
    )
    assert ok.status_code == 200, ok.text
    for bad in ["..%2Fx.jpg", r"foo%5Cbar.jpg"]:
        resp = client.patch(
            f"/api/cases/{case_id}/images/{bad}",
            json={"manual_phase": "before"},
        )
        assert resp.status_code in (400, 404), f"expected reject for {bad!r}, got {resp.status_code}"
    assert _override_count(client) == 1


def test_patch_no_fields_returns_400(client, seed_case):
    case_id = seed_case()
    resp = client.patch(f"/api/cases/{case_id}/images/x.jpg", json={})
    assert resp.status_code == 400
    assert "no fields" in resp.json()["detail"]


def test_patch_unknown_case_returns_404(client):
    resp = client.patch(
        "/api/cases/9999/images/x.jpg", json={"manual_phase": "before"}
    )
    assert resp.status_code == 404


def test_get_case_merges_override_phase_over_skill(client, seed_case):
    """When skill_image_metadata_json says phase=None, manual override 'before'
    should appear in skill_image_metadata for that filename, with
    phase_override_source='manual'."""
    from backend import db

    case_id = seed_case()
    skill_meta = [
        {
            "filename": "术中1.jpeg",
            "phase": None,  # skill failed to label
            "view_bucket": "front",
            "angle": "front",
        }
    ]
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET skill_image_metadata_json = ? WHERE id = ?",
            (json.dumps(skill_meta, ensure_ascii=False), case_id),
        )
    # Apply manual override
    client.patch(
        f"/api/cases/{case_id}/images/术中1.jpeg",
        json={"manual_phase": "after"},
    )
    resp = client.get(f"/api/cases/{case_id}")
    assert resp.status_code == 200
    items = resp.json()["skill_image_metadata"]
    assert len(items) == 1
    assert items[0]["phase"] == "after"
    assert items[0]["phase_override_source"] == "manual"
    assert items[0]["view_override_source"] is None  # view not overridden


def test_get_case_view_override_replaces_bucket_and_angle(client, seed_case):
    from backend import db

    case_id = seed_case()
    skill_meta = [
        {
            "filename": "x.jpg",
            "phase": "before",
            "view_bucket": "side",
            "angle": "side",
        }
    ]
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET skill_image_metadata_json = ? WHERE id = ?",
            (json.dumps(skill_meta, ensure_ascii=False), case_id),
        )
    client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_view": "front"},
    )
    items = client.get(f"/api/cases/{case_id}").json()["skill_image_metadata"]
    assert items[0]["view_bucket"] == "front"
    assert items[0]["angle"] == "front"
    assert items[0]["view_override_source"] == "manual"
    assert items[0]["phase_override_source"] is None


def test_get_case_returns_classification_preflight_queue(client, seed_case):
    from backend import db

    case_id = seed_case()
    image_files = ["术前1.jpg", "术后1.jpg", "mystery.jpg", "low-side.jpg"]
    skill_meta = [
        {
            "filename": "术前1.jpg",
            "phase": "before",
            "view_bucket": "front",
            "angle": "front",
            "angle_confidence": 0.97,
        },
        {
            "filename": "术后1.jpg",
            "phase": "after",
            "view_bucket": "front",
            "angle": "front",
            "angle_confidence": 0.94,
        },
        {
            "filename": "mystery.jpg",
            "phase": None,
            "view_bucket": None,
            "angle": None,
        },
        {
            "filename": "low-side.jpg",
            "phase": "before",
            "view_bucket": "side",
            "angle": "side",
            "angle_confidence": 0.31,
        },
    ]
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE cases
            SET meta_json = ?, skill_image_metadata_json = ?
            WHERE id = ?
            """,
            (
                json.dumps({"image_files": image_files}, ensure_ascii=False),
                json.dumps(skill_meta, ensure_ascii=False),
                case_id,
            ),
        )

    resp = client.get(f"/api/cases/{case_id}")
    assert resp.status_code == 200
    preflight = resp.json()["classification_preflight"]

    assert preflight["classification"]["source_count"] == 4
    assert preflight["classification"]["classified_count"] == 3
    assert preflight["classification"]["needs_manual_count"] == 1
    assert preflight["classification"]["low_confidence_count"] == 1
    by_file = {item["filename"]: item for item in preflight["classification"]["review_items"]}
    assert by_file["mystery.jpg"]["severity"] == "block"
    assert by_file["mystery.jpg"]["layer"] == "classification"
    assert set(by_file["mystery.jpg"]["reasons"]) == {"missing_phase", "missing_view"}
    assert by_file["low-side.jpg"]["reasons"] == ["low_view_confidence"]
    assert by_file["low-side.jpg"]["layer"] == "confidence"
    layer_counts = {item["key"]: item["count"] for item in preflight["classification"]["review_layers"]}
    assert layer_counts["classification"] == 1
    assert layer_counts["confidence"] == 1
    front = next(slot for slot in preflight["render"]["slots"] if slot["view"] == "front")
    side = next(slot for slot in preflight["render"]["slots"] if slot["view"] == "side")
    assert front["ready"] is True
    assert side["ready"] is False
    assert preflight["render"]["status"] == "blocked"


def test_get_case_preflight_summarizes_latest_render_review(client, seed_case, tmp_path):
    from backend import db

    case_id = seed_case()
    image_files = [
        "术前正面.jpg",
        "术后正面.jpg",
        "术前45.jpg",
        "术后45.jpg",
        "术前侧面.jpg",
        "术后侧面.jpg",
    ]
    skill_meta = [
        {"filename": "术前正面.jpg", "phase": "before", "view_bucket": "front", "angle": "front"},
        {"filename": "术后正面.jpg", "phase": "after", "view_bucket": "front", "angle": "front"},
        {"filename": "术前45.jpg", "phase": "before", "view_bucket": "oblique", "angle": "oblique"},
        {"filename": "术后45.jpg", "phase": "after", "view_bucket": "oblique", "angle": "oblique"},
        {"filename": "术前侧面.jpg", "phase": "before", "view_bucket": "side", "angle": "side"},
        {"filename": "术后侧面.jpg", "phase": "after", "view_bucket": "side", "angle": "side"},
    ]
    now = datetime.now(timezone.utc).isoformat()
    manifest_path = tmp_path / "manifest.final.json"
    manifest_path.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "selected_slots": {
                            "side": {
                                "pose_delta": {"weighted": 12.0},
                                "before": {"name": "术前侧面.jpg", "group_relative_path": "术前侧面.jpg"},
                                "after": {"name": "术后侧面.jpg", "group_relative_path": "术后侧面.jpg"},
                            }
                        }
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, skill_image_metadata_json = ? WHERE id = ?",
            (
                json.dumps({"image_files": image_files}, ensure_ascii=False),
                json.dumps(skill_meta, ensure_ascii=False),
                case_id,
            ),
        )
        job_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, semantic_judge, manifest_path, meta_json)
            VALUES (?, 'fumei', 'single-compare', 'done_with_issues', ?, 'off', ?, ?)
            """,
            (
                case_id,
                now,
                str(manifest_path),
                json.dumps(
                    {
                        "ai_usage": {
                            "used_after_enhancement": False,
                            "used_ai_padfill": False,
                            "semantic_judge_requested": "auto",
                            "semantic_judge_effective": "off",
                        }
                    },
                    ensure_ascii=False,
                ),
            ),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO render_quality
              (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
               manifest_status, blocking_count, warning_count, metrics_json, created_at, updated_at)
            VALUES (?, 'done_with_issues', 70, 0, 'real_layout', 'ok', 0, 4, ?, ?, ?)
            """,
            (
                job_id,
                json.dumps(
                    {
                        "warnings": [
                            "术前侧面.jpg - 正脸检测失败，已使用侧脸检测兜底",
                            "术后侧面.jpg - 面部检测失败",
                            "侧面 术前术后姿态差过大",
                            "多个姿态推断候选",
                        ],
                        "ai_after_enhancement": False,
                        "ai_edge_padfill": False,
                    },
                    ensure_ascii=False,
                ),
                now,
                now,
            ),
        )

    preflight = client.get(f"/api/cases/{case_id}").json()["classification_preflight"]
    latest = preflight["latest_render"]
    assert latest["job_id"] == job_id
    assert latest["quality_status"] == "done_with_issues"
    assert latest["can_publish"] is False
    assert latest["warning_buckets"]["face_detection"] == 1
    assert latest["warning_buckets"]["profile_expected"] == 1
    assert latest["warning_buckets"]["profile_quality"] == 1
    assert latest["warning_buckets"]["pose_delta"] == 1
    assert latest["warning_buckets"]["pose_candidates"] == 1
    assert latest["warning_buckets"]["noise_count"] == 2
    assert latest["warning_buckets"]["actionable_count"] == 2
    latest_layers = {item["key"]: item["count"] for item in latest["warning_layers"]}
    assert latest_layers["profile_expected"] == 1
    assert latest_layers["profile_quality"] == 1
    assert latest_layers["render_pose"] == 2
    render_pose = next(item for item in latest["warning_layers"] if item["key"] == "render_pose")
    assert render_pose["filenames"] == [
        "术前侧面.jpg",
        "术后侧面.jpg",
    ]
    assert render_pose["slots"][0]["key"] == "side"
    assert render_pose["slots"][0]["before"] == "术前侧面.jpg"
    assert render_pose["slots"][0]["after"] == "术后侧面.jpg"
    assert latest["ai_usage"]["used_after_enhancement"] is False
    assert preflight["render"]["status"] == "review"


def test_get_case_preflight_hides_stale_pose_noise_from_latest_layers(client, seed_case, tmp_path):
    from backend import db

    case_id = seed_case()
    image_files = [
        "术前正面.jpg",
        "术后正面.jpg",
        "术前45-新.jpg",
        "术后45-新.jpg",
        "术前45-旧.jpg",
        "术后45-旧.jpg",
        "术前侧面.jpg",
        "术后侧面.jpg",
    ]
    skill_meta = [
        {"filename": "术前正面.jpg", "phase": "before", "view_bucket": "front", "angle": "front"},
        {"filename": "术后正面.jpg", "phase": "after", "view_bucket": "front", "angle": "front"},
        {"filename": "术前45-新.jpg", "phase": "before", "view_bucket": "oblique", "angle": "oblique"},
        {"filename": "术后45-新.jpg", "phase": "after", "view_bucket": "oblique", "angle": "oblique"},
        {"filename": "术前45-旧.jpg", "phase": "before", "view_bucket": "oblique", "angle": "oblique"},
        {"filename": "术后45-旧.jpg", "phase": "after", "view_bucket": "oblique", "angle": "oblique"},
        {"filename": "术前侧面.jpg", "phase": "before", "view_bucket": "side", "angle": "side"},
        {"filename": "术后侧面.jpg", "phase": "after", "view_bucket": "side", "angle": "side"},
    ]
    manifest_path = tmp_path / "manifest.final.json"
    manifest_path.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "selected_slots": {
                            "oblique": {
                                "pose_delta": {"weighted": 8.67},
                                "before": {"name": "术前45-新.jpg", "group_relative_path": "术前45-新.jpg"},
                                "after": {"name": "术后45-新.jpg", "group_relative_path": "术后45-新.jpg"},
                            }
                        }
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    stale_warning = "45°侧 术前术后姿态差过大(术前45-旧.jpg / 术后45-旧.jpg, weighted=14.78)"
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, skill_image_metadata_json = ? WHERE id = ?",
            (
                json.dumps({"image_files": image_files}, ensure_ascii=False),
                json.dumps(skill_meta, ensure_ascii=False),
                case_id,
            ),
        )
        job_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, semantic_judge,
               manifest_path, meta_json)
            VALUES (?, 'fumei', 'tri-compare', 'done', ?, ?, 'off', ?, ?)
            """,
            (
                case_id,
                now,
                now,
                str(manifest_path),
                json.dumps({"warnings": [stale_warning]}, ensure_ascii=False),
            ),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO render_quality
              (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
               manifest_status, blocking_count, warning_count, metrics_json, created_at, updated_at)
            VALUES (?, 'done', 96, 1, 'real_layout', 'ok', 0, 1, ?, ?, ?)
            """,
            (
                job_id,
                json.dumps(
                    {
                        "warnings": [],
                        "display_warnings": [],
                        "audit_warnings": [stale_warning],
                        "warning_layers": {
                            "selected_actionable": [],
                            "candidate_noise": [],
                            "selected_expected_profile": [],
                            "stale_pose_noise": [stale_warning],
                        },
                        "warning_buckets": {
                            "candidate_noise": 0,
                            "profile_expected": 0,
                            "profile_quality": 0,
                            "pose_delta": 0,
                            "pose_candidates": 0,
                            "stale_pose_noise": 1,
                            "other": 0,
                            "noise_count": 0,
                            "audit_noise_count": 1,
                            "actionable_count": 0,
                        },
                    },
                    ensure_ascii=False,
                ),
                now,
                now,
            ),
        )

    preflight = client.get(f"/api/cases/{case_id}").json()["classification_preflight"]
    latest = preflight["latest_render"]
    assert latest["job_id"] == job_id
    assert latest["quality_status"] == "done"
    assert latest["can_publish"] is True
    assert latest["warning_buckets"]["actionable_count"] == 0
    assert latest["warning_buckets"]["stale_pose_noise"] == 1
    assert latest["blocking_warning_count"] == 0
    assert latest["acceptable_warning_count"] == 0
    assert [item["key"] for item in latest["warning_layers"]] == []
    assert preflight["render"]["status"] == "ready"


def test_image_review_usable_resolves_actionable_side_review(client, seed_case, tmp_path):
    from backend import db

    case_dir = tmp_path / "case-review-usable"
    case_dir.mkdir()
    (case_dir / "术前侧面.jpg").write_bytes(b"fake")
    (case_dir / "术后侧面.jpg").write_bytes(b"fake")
    case_id = seed_case(abs_path=str(case_dir))
    image_files = ["术前侧面.jpg", "术后侧面.jpg"]
    skill_meta = [
        {
            "filename": "术前侧面.jpg",
            "phase": "before",
            "view_bucket": "side",
            "angle": "side",
            "issues": ["面部检测失败: 未检测到面部"],
            "rejection_reason": "face_detection_failure",
        },
        {"filename": "术后侧面.jpg", "phase": "after", "view_bucket": "side", "angle": "side"},
    ]
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, skill_image_metadata_json = ? WHERE id = ?",
            (
                json.dumps({"image_files": image_files}, ensure_ascii=False),
                json.dumps(skill_meta, ensure_ascii=False),
                case_id,
            ),
        )

    before = client.get(f"/api/cases/{case_id}").json()["classification_preflight"]
    assert before["classification"]["actionable_review_count"] == 1
    resp = client.post(
        f"/api/cases/{case_id}/image-review/{quote('术前侧面.jpg', safe='')}",
        json={"verdict": "usable", "reviewer": "tester", "layer": "profile_quality"},
    )
    assert resp.status_code == 200, resp.text
    detail = resp.json()["detail"]
    preflight = detail["classification_preflight"]
    assert preflight["classification"]["actionable_review_count"] == 0
    assert preflight["classification"]["reviewed_count"] == 1
    reviewed = next(item for item in detail["skill_image_metadata"] if item["filename"] == "术前侧面.jpg")
    assert reviewed["review_state"]["verdict"] == "usable"


def test_image_review_excluded_drops_render_pair_slot(client, seed_case, tmp_path):
    from backend import db

    case_dir = tmp_path / "case-review-excluded"
    case_dir.mkdir()
    (case_dir / "术前侧面.jpg").write_bytes(b"fake")
    (case_dir / "术后侧面.jpg").write_bytes(b"fake")
    case_id = seed_case(abs_path=str(case_dir))
    image_files = ["术前侧面.jpg", "术后侧面.jpg"]
    skill_meta = [
        {"filename": "术前侧面.jpg", "phase": "before", "view_bucket": "side", "angle": "side"},
        {"filename": "术后侧面.jpg", "phase": "after", "view_bucket": "side", "angle": "side"},
    ]
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, skill_image_metadata_json = ? WHERE id = ?",
            (
                json.dumps({"image_files": image_files}, ensure_ascii=False),
                json.dumps(skill_meta, ensure_ascii=False),
                case_id,
            ),
        )

    resp = client.post(
        f"/api/cases/{case_id}/image-review/{quote('术后侧面.jpg', safe='')}",
        json={"verdict": "excluded", "reviewer": "tester", "note": "too blurry"},
    )
    assert resp.status_code == 200, resp.text
    preflight = resp.json()["detail"]["classification_preflight"]
    assert preflight["classification"]["render_excluded_count"] == 1
    side = next(slot for slot in preflight["render"]["slots"] if slot["view"] == "side")
    assert side["before_count"] == 1
    assert side["after_count"] == 0
    assert side["ready"] is False
    layers = {item["key"]: item for item in preflight["classification"]["review_layers"]}
    assert layers["render_excluded"]["count"] == 1


def test_image_review_reopen_clears_state(client, seed_case, tmp_path):
    from backend import db

    case_dir = tmp_path / "case-review-reopen"
    case_dir.mkdir()
    (case_dir / "术前侧面.jpg").write_bytes(b"fake")
    case_id = seed_case(abs_path=str(case_dir))
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["术前侧面.jpg"]}, ensure_ascii=False), case_id),
        )
    client.post(
        f"/api/cases/{case_id}/image-review/{quote('术前侧面.jpg', safe='')}",
        json={"verdict": "deferred", "reviewer": "tester"},
    )
    resp = client.post(
        f"/api/cases/{case_id}/image-review/{quote('术前侧面.jpg', safe='')}",
        json={"verdict": "reopen", "reviewer": "tester"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["review_state"] is None
    assert "image_review_states" not in resp.json()["detail"]["meta"]


def test_patch_image_override_accepts_case_relative_path(client, seed_case, tmp_path):
    from backend import db

    case_dir = tmp_path / "case-rel"
    (case_dir / "术前").mkdir(parents=True)
    case_id = seed_case(abs_path=str(case_dir), category="non_labeled", template_tier=None)

    rel = "术前/a.jpg"
    r = client.patch(
        f"/api/cases/{case_id}/images/{quote(rel, safe='/')}",
        json={"manual_phase": "before", "manual_view": "front"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["filename"] == rel
    with db.connect() as conn:
        row = conn.execute(
            "SELECT manual_phase, manual_view FROM case_image_overrides WHERE case_id=? AND filename=?",
            (case_id, rel),
        ).fetchone()
    assert row["manual_phase"] == "before"
    assert row["manual_view"] == "front"


def test_get_case_surfaces_override_when_skill_metadata_missing(client, seed_case):
    """Older real cases may have only image_files + case_image_overrides."""
    from backend import db

    case_id = seed_case()
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, skill_image_metadata_json = NULL WHERE id = ?",
            (json.dumps({"image_files": ["unlabeled.jpg"]}, ensure_ascii=False), case_id),
        )
    client.patch(
        f"/api/cases/{case_id}/images/unlabeled.jpg",
        json={"manual_phase": "before", "manual_view": "oblique"},
    )

    items = client.get(f"/api/cases/{case_id}").json()["skill_image_metadata"]
    assert len(items) == 1
    assert items[0]["filename"] == "unlabeled.jpg"
    assert items[0]["phase"] == "before"
    assert items[0]["view_bucket"] == "oblique"
    assert items[0]["phase_override_source"] == "manual"
    assert items[0]["view_override_source"] == "manual"


def test_get_case_filters_stale_skill_metadata_not_in_image_files(client, seed_case):
    """Historical manifest entries should not inflate current source counts."""
    from backend import db

    case_id = seed_case()
    skill_meta = [
        {"filename": "active.jpg", "phase": "before", "view_bucket": "front", "angle": "front"},
        {"filename": "preview.jpg", "phase": None, "view_bucket": None, "angle": None},
    ]
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, skill_image_metadata_json = ? WHERE id = ?",
            (
                json.dumps({"image_files": ["active.jpg"]}, ensure_ascii=False),
                json.dumps(skill_meta, ensure_ascii=False),
                case_id,
            ),
        )

    items = client.get(f"/api/cases/{case_id}").json()["skill_image_metadata"]
    assert [item["filename"] for item in items] == ["active.jpg"]


def test_prepare_manual_render_sources_materializes_standard_pair(client, seed_case, tmp_path):
    case_dir = tmp_path / "case-manual-render"
    case_dir.mkdir()
    (case_dir / "07041738_00.jpg").write_bytes(b"real before bytes")
    (case_dir / "07041738_01.jpg").write_bytes(b"real after bytes")
    case_id = seed_case(abs_path=str(case_dir), category="non_labeled", template_tier=None)

    payload = {
        "before": {"kind": "existing", "filename": "07041738_00.jpg"},
        "after": {
            "kind": "upload",
            "upload_name": "after-upload.jpg",
            "data_url": "data:image/jpeg;base64,"
            + base64.b64encode(b"real uploaded after bytes").decode("ascii"),
        },
        "view": "front",
        "before_transform": {"offset_x_pct": 0.03, "offset_y_pct": -0.04, "scale": 0.96},
    }
    r = client.post(f"/api/cases/{case_id}/manual-render-sources", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    created = body["created_files"]
    assert len(created) == 2
    assert created[0].startswith("术前-正面-手动-")
    assert created[1].startswith("术后-正面-手动-")
    assert all((case_dir / name).is_file() for name in created)

    detail = body["detail"]
    assert detail["category"] == "standard_face"
    assert detail["template_tier"] == "single"
    assert detail["source_count"] == 4
    assert detail["labeled_count"] == 2
    assert detail["meta"]["image_files"][:2] == created
    assert {o["manual_phase"] for o in body["manual_overrides"]} == {"before", "after"}
    assert {o["manual_view"] for o in body["manual_overrides"]} == {"front"}
    before_override = next(o for o in body["manual_overrides"] if o["manual_phase"] == "before")
    after_override = next(o for o in body["manual_overrides"] if o["manual_phase"] == "after")
    assert before_override["manual_transform"] == {
        "enabled": True,
        "offset_x_pct": 0.03,
        "offset_y_pct": -0.04,
        "scale": 0.96,
    }
    assert after_override["manual_transform"] is None


def test_manual_render_preview_uses_temporary_directory(client, seed_case, monkeypatch, tmp_path):
    from backend import render_executor

    case_dir = tmp_path / "case-manual-preview"
    case_dir.mkdir()
    (case_dir / "before.jpg").write_bytes(b"real before bytes")
    (case_dir / "after.jpg").write_bytes(b"real after bytes")
    case_id = seed_case(abs_path=str(case_dir), category="standard_face", template_tier="single")

    captured = {}

    def fake_preview(**kwargs):
        captured.update(kwargs)
        preview_dir = kwargs["preview_dir"]
        output = preview_dir / "preview.jpg"
        manifest = preview_dir / "manifest.preview.json"
        output.write_bytes(b"real preview bytes")
        manifest.write_text("{}", encoding="utf-8")
        return {
            "output_path": str(output),
            "manifest_path": str(manifest),
            "render_plan": {"slots": [{"slot": kwargs["view"]}]},
            "warnings": [],
        }

    monkeypatch.setattr(render_executor, "run_manual_render_preview", fake_preview)

    r = client.post(
        f"/api/cases/{case_id}/manual-render-preview",
        json={
            "before": {"kind": "existing", "filename": "before.jpg"},
            "after": {"kind": "existing", "filename": "after.jpg"},
            "view": "front",
            "brand": "fumei",
            "before_transform": {"offset_x_pct": 0.02, "offset_y_pct": 0.01, "scale": 0.98},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["view"] == "front"
    assert body["preview_id"]
    assert ".case-workbench-preview" in body["output_path"]
    assert captured["before_path"] == case_dir / "before.jpg"
    assert captured["after_path"] == case_dir / "after.jpg"
    assert captured["before_transform"] == {
        "enabled": True,
        "offset_x_pct": 0.02,
        "offset_y_pct": 0.01,
        "scale": 0.98,
    }
    assert not (case_dir / "术前-正面-手动-preview.jpg").exists()

    file_r = client.get(f"/api/cases/{case_id}/manual-render-preview/{body['preview_id']}/file")
    assert file_r.status_code == 200
    assert file_r.content == b"real preview bytes"


def test_trash_and_restore_source_image_rescans_case(client, seed_case, tmp_path):
    case_dir = tmp_path / "case-trash"
    case_dir.mkdir()
    (case_dir / "术前-正面.jpg").write_bytes(b"real before bytes")
    (case_dir / "术后-正面.jpg").write_bytes(b"real after bytes")
    case_id = seed_case(abs_path=str(case_dir), category="standard_face", template_tier="single")

    trash = client.post(
        f"/api/cases/{case_id}/images/trash",
        json={"filename": "术后-正面.jpg"},
    )
    assert trash.status_code == 200, trash.text
    body = trash.json()
    assert body["original_filename"] == "术后-正面.jpg"
    assert not (case_dir / "术后-正面.jpg").exists()
    assert (case_dir / ".case-workbench-trash" / body["trash_path"]).is_file()
    assert "术后-正面.jpg" not in body["detail"]["meta"]["image_files"]

    restore = client.post(
        f"/api/cases/{case_id}/images/restore",
        json={"trash_path": body["trash_path"]},
    )
    assert restore.status_code == 200, restore.text
    restored = restore.json()
    assert restored["restored_filename"] == "术后-正面.jpg"
    assert (case_dir / "术后-正面.jpg").is_file()
    assert "术后-正面.jpg" in restored["detail"]["meta"]["image_files"]


def test_image_workbench_transfer_copies_to_target_and_inherits_metadata(client, seed_case, tmp_path):
    from backend import db

    source_dir = tmp_path / "case-transfer-source"
    target_dir = tmp_path / "case-transfer-target"
    source_dir.mkdir()
    target_dir.mkdir()
    source_filename = "术后-正面.jpeg"
    (source_dir / source_filename).write_bytes(b"real source image bytes")
    (target_dir / source_filename).write_bytes(b"existing target bytes")

    source_id = seed_case(abs_path=str(source_dir), category="standard_face", template_tier="single")
    target_id = seed_case(abs_path=str(target_dir), category="non_labeled", template_tier=None)
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "image_files": [source_filename],
                        "image_review_states": {
                            source_filename: {
                                "verdict": "usable",
                                "label": "已确认可用",
                                "body_part": "face",
                                "treatment_area": "下颌线",
                                "render_excluded": False,
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                source_id,
            ),
        )
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": [source_filename]}, ensure_ascii=False), target_id),
        )
        conn.execute(
            """
            INSERT INTO case_image_overrides
              (case_id, filename, manual_phase, manual_view, updated_at)
            VALUES (?, ?, 'after', 'front', ?)
            """,
            (source_id, source_filename, now),
        )

    resp = client.post(
        "/api/image-workbench/transfer",
        json={
            "items": [{"case_id": source_id, "filename": source_filename}],
            "target_case_id": target_id,
            "mode": "copy",
            "reviewer": "tester",
            "note": "跨案例补图",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    copied_filename = f"术后-正面-来自case{source_id}.jpeg"
    assert body["mode"] == "copy"
    assert body["copied"] == 1
    assert body["items"][0]["target_filename"] == copied_filename
    assert body["skipped"] == []
    assert (source_dir / source_filename).is_file()
    assert (target_dir / copied_filename).read_bytes() == b"real source image bytes"
    assert (target_dir / source_filename).read_bytes() == b"existing target bytes"

    with db.connect() as conn:
        override = conn.execute(
            """
            SELECT manual_phase, manual_view
            FROM case_image_overrides
            WHERE case_id = ? AND filename = ?
            """,
            (target_id, copied_filename),
        ).fetchone()
        assert override["manual_phase"] == "after"
        assert override["manual_view"] == "front"

        meta = json.loads(conn.execute("SELECT meta_json FROM cases WHERE id = ?", (target_id,)).fetchone()["meta_json"])
        assert copied_filename in meta["image_files"]
        state = meta["image_review_states"][copied_filename]
        assert state["verdict"] == "usable"
        assert state["body_part"] == "face"
        assert state["treatment_area"] == "下颌线"
        assert state["copied_from_case_id"] == source_id
        assert state["copied_from_filename"] == source_filename
        assert state["reviewer"] == "tester"
        assert state["note"] == "跨案例补图"

        revision = conn.execute(
            """
            SELECT op, actor, source_route
            FROM case_revisions
            WHERE case_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (target_id,),
        ).fetchone()
        assert revision["op"] == "image_workbench_transfer"
        assert revision["actor"] == "tester"
        assert revision["source_route"] == "/api/image-workbench/transfer"


def test_image_workbench_transfer_rejects_trashed_source_path(client, seed_case, tmp_path):
    source_dir = tmp_path / "case-transfer-trash-source"
    target_dir = tmp_path / "case-transfer-trash-target"
    trash_dir = source_dir / ".case-workbench-trash"
    trash_dir.mkdir(parents=True)
    target_dir.mkdir()
    (trash_dir / "hidden.jpeg").write_bytes(b"hidden trash bytes")
    source_id = seed_case(abs_path=str(source_dir), category="standard_face", template_tier="single")
    target_id = seed_case(abs_path=str(target_dir), category="non_labeled", template_tier=None)

    resp = client.post(
        "/api/image-workbench/transfer",
        json={
            "items": [{"case_id": source_id, "filename": ".case-workbench-trash/hidden.jpeg"}],
            "target_case_id": target_id,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["copied"] == 0
    assert body["items"] == []
    assert body["skipped"][0]["reason"] == "trashed images cannot be transferred"
    assert not any(target_dir.iterdir())


def test_supplement_candidates_find_safe_cross_case_image_and_copy_requires_review(client, seed_case, tmp_path):
    from backend import db

    target_dir = tmp_path / "case-supplement-target"
    source_dir = tmp_path / "case-supplement-source"
    target_dir.mkdir()
    source_dir.mkdir()
    (target_dir / "术前-正面.jpeg").write_bytes(b"target before bytes")
    (source_dir / "术后-正面.jpeg").write_bytes(b"source after bytes")

    target_id = seed_case(abs_path=str(target_dir), category="standard_face", template_tier="tri", customer_raw="同客")
    source_id = seed_case(abs_path=str(source_dir), category="standard_face", template_tier="tri", customer_raw="同客")
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["术前-正面.jpeg"]}, ensure_ascii=False), target_id),
        )
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "image_files": ["术后-正面.jpeg"],
                        "image_review_states": {
                            "术后-正面.jpeg": {
                                "verdict": "usable",
                                "label": "已确认可用",
                                "body_part": "face",
                                "treatment_area": "下颌线",
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                source_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO case_image_overrides
              (case_id, filename, manual_phase, manual_view, updated_at)
            VALUES (?, '术前-正面.jpeg', 'before', 'front', ?)
            """,
            (target_id, now),
        )
        conn.execute(
            """
            INSERT INTO case_image_overrides
              (case_id, filename, manual_phase, manual_view, updated_at)
            VALUES (?, '术后-正面.jpeg', 'after', 'front', ?)
            """,
            (source_id, now),
        )

    candidates = client.get(
        "/api/image-workbench/supplement-candidates",
        params={"target_case_id": target_id, "limit_per_gap": 5},
    )
    assert candidates.status_code == 200, candidates.text
    front_after = next(gap for gap in candidates.json()["gaps"] if gap["key"] == "front-after")
    assert front_after["candidate_count"] >= 1
    candidate = next(item for item in front_after["candidates"] if item["case_id"] == source_id)
    assert candidate["filename"] == "术后-正面.jpeg"
    assert "同客户名" in candidate["match_reasons"]
    assert "已确认可用" in candidate["match_reasons"]

    copied = client.post(
        "/api/image-workbench/transfer",
        json={
            "target_case_id": target_id,
            "require_target_review": True,
            "reviewer": "tester",
            "note": "补齐正面术后",
            "items": [{"case_id": source_id, "filename": "术后-正面.jpeg"}],
        },
    )
    assert copied.status_code == 200, copied.text
    copied_filename = copied.json()["items"][0]["target_filename"]

    detail = client.get(f"/api/cases/{target_id}").json()
    copied_meta = next(item for item in detail["skill_image_metadata"] if item["filename"] == copied_filename)
    state = copied_meta["review_state"]
    assert state["copied_requires_review"] is True
    assert state["inherited_verdict"] == "usable"
    assert state["label"] == "补图待确认"
    assert "verdict" not in state
    assert detail["classification_preflight"]["render"]["status"] == "blocked"
    copied_review = next(
        item for item in detail["classification_preflight"]["classification"]["review_items"]
        if item["filename"] == copied_filename
    )
    assert copied_review["layer"] == "copied_review"
    assert copied_review["severity"] == "block"

    from backend import render_queue

    with db.connect() as conn:
        meta_json = conn.execute("SELECT meta_json FROM cases WHERE id = ?", (target_id,)).fetchone()["meta_json"]
        override_rows = conn.execute(
            "SELECT filename, manual_phase, manual_view FROM case_image_overrides WHERE case_id = ?",
            (target_id,),
        ).fetchall()
    manual_overrides = {
        row["filename"]: {"phase": row["manual_phase"], "view": row["manual_view"]}
        for row in override_rows
    }
    render_preflight = render_queue._classification_blocking_preflight(
        case_meta_json=meta_json,
        skill_image_metadata_json=None,
        image_files=detail["meta"]["image_files"],
        manual_overrides=manual_overrides,
        semantic_judge="auto",
    )
    assert render_preflight is not None
    assert render_preflight["ai_usage"]["classification_copied_review_count"] == 1
    assert "补图待确认 1 张" in render_preflight["render_error"]


def test_simulate_after_authorization_required_focus_regions_optional(client, seed_case, monkeypatch, tmp_path):
    from backend import ai_generation_adapter

    case_dir = tmp_path / "case-sim-required"
    case_dir.mkdir()
    (case_dir / "术后-正面.jpg").write_bytes(b"real after bytes")
    case_id = seed_case(abs_path=str(case_dir), category="standard_face", template_tier="single")

    no_auth = client.post(
        f"/api/cases/{case_id}/simulate-after",
        json={
            "after_image_path": "术后-正面.jpg",
            "focus_targets": ["下颌线"],
            "focus_regions": [{"x": 0.2, "y": 0.25, "width": 0.3, "height": 0.2}],
            "ai_generation_authorized": False,
            "provider": "ps_model_router",
        },
    )
    assert no_auth.status_code == 400
    assert "authorized" in no_auth.json()["detail"]

    captured: list[dict] = []

    def fake_run(**kwargs):
        captured.append(kwargs)
        return {
            "status": "done",
            "output_refs": [],
            "audit": {"provider": "ps_model_router", "focus_targets": kwargs["focus_targets"]},
            "watermarked": True,
            "error_message": None,
        }

    monkeypatch.setattr(ai_generation_adapter, "run_ps_model_router_after_simulation", fake_run)

    no_target_no_region = client.post(
        f"/api/cases/{case_id}/simulate-after",
        json={
            "after_image_path": "术后-正面.jpg",
            "focus_targets": [],
            "focus_regions": [],
            "ai_generation_authorized": True,
            "provider": "ps_model_router",
        },
    )
    assert no_target_no_region.status_code == 200, no_target_no_region.text
    assert no_target_no_region.json()["focus_targets"] == []
    assert no_target_no_region.json()["focus_regions"] == []
    assert captured[-1]["focus_regions"] == []

    text_only = client.post(
        f"/api/cases/{case_id}/simulate-after",
        json={
            "after_image_path": "术后-正面.jpg",
            "focus_targets": ["下颌线"],
            "focus_regions": [],
            "ai_generation_authorized": True,
            "provider": "ps_model_router",
        },
    )
    assert text_only.status_code == 200, text_only.text
    assert text_only.json()["focus_targets"] == ["下颌线"]
    assert text_only.json()["focus_regions"] == []

    region_only = client.post(
        f"/api/cases/{case_id}/simulate-after",
        json={
            "after_image_path": "术后-正面.jpg",
            "focus_targets": [],
            "focus_regions": [{"x": 0.2, "y": 0.25, "width": 0.3, "height": 0.2}],
            "ai_generation_authorized": True,
            "provider": "ps_model_router",
        },
    )
    assert region_only.status_code == 200, region_only.text
    assert region_only.json()["focus_targets"] == []
    assert region_only.json()["focus_regions"][0]["x"] == 0.2
    assert captured[-1]["focus_targets"] == []
    assert captured[-1]["focus_regions"][0]["width"] == 0.3


def test_ps_image_model_options_are_explicit(client):
    resp = client.get("/api/cases/ps-image-model-options")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "ps_model_router"
    models = [item["value"] for item in body["options"]]
    assert len(models) >= 3
    assert "gpt-image-2-vip" in models
    assert all("自动链路" not in item["label"] for item in body["options"])


def test_ai_enhancement_prompt_supports_whole_image_when_no_regions():
    from backend import ai_generation_adapter

    prompt_with_regions = ai_generation_adapter.build_after_enhancement_prompt(
        ["下颌线"],
        [{"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2, "label": None}],
    )
    assert "框选区域" in prompt_with_regions
    assert "增强必须限制在框选区域内" in prompt_with_regions

    prompt_whole = ai_generation_adapter.build_after_enhancement_prompt(["下颌线"], [])
    assert "整张" in prompt_whole or "整体" in prompt_whole
    assert "区域1" not in prompt_whole

    prompt_empty = ai_generation_adapter.build_after_enhancement_prompt([], None)
    assert "整张" in prompt_empty or "整体" in prompt_empty

    prompt = ai_generation_adapter.build_after_enhancement_prompt(
        ["下颌线"],
        [{"x": 0.2, "y": 0.3, "width": 0.4, "height": 0.2, "label": "下颌线"}],
    )
    assert "增强必须限制在框选区域内" in prompt
    assert "不得调亮" in prompt
    assert "不得统一肤色" in prompt
    assert "非目标区域只能做亮度" not in prompt


def test_ai_adapter_difference_heatmap_scores_regions(tmp_path):
    from PIL import Image, ImageDraw
    from backend import ai_generation_adapter

    original = tmp_path / "original.jpg"
    generated = tmp_path / "generated.jpg"
    heatmap = tmp_path / "heatmap.png"
    Image.new("RGB", (80, 80), (120, 120, 120)).save(original)
    enhanced = Image.new("RGB", (80, 80), (120, 120, 120))
    draw = ImageDraw.Draw(enhanced)
    draw.rectangle((20, 20, 55, 55), fill=(170, 170, 170))
    enhanced.save(generated)

    metrics = ai_generation_adapter._create_difference_heatmap(
        original,
        generated,
        heatmap,
        [{"x": 0.2, "y": 0.2, "width": 0.55, "height": 0.55, "label": "target"}],
    )
    assert heatmap.is_file()
    assert metrics["full_frame_change_score"] > 0
    assert metrics["target_region_change_score"] > metrics["non_target_change_score"]
    assert metrics["heatmap_kind"] == "difference_heatmap"


def test_simulate_after_accepts_uploaded_after_without_before_reference(client, seed_case, monkeypatch, tmp_path):
    from backend import ai_generation_adapter, db

    case_dir = tmp_path / "case-sim-upload"
    case_dir.mkdir()
    case_id = seed_case(abs_path=str(case_dir), category="standard_face", template_tier="single")
    uploaded = "data:image/jpeg;base64," + base64.b64encode(b"uploaded after bytes").decode("ascii")
    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {
            "status": "done",
            "output_refs": [],
            "audit": {"provider": "ps_model_router", "prompt": "fake prompt"},
            "watermarked": True,
            "error_message": None,
        }

    monkeypatch.setattr(ai_generation_adapter, "run_ps_model_router_after_simulation", fake_run)

    resp = client.post(
        f"/api/cases/{case_id}/simulate-after",
        json={
            "after_image": {"kind": "upload", "upload_name": "after.jpg", "data_url": uploaded},
            "focus_targets": ["下颌线"],
            "focus_regions": [{"x": 0.2, "y": 0.25, "width": 0.3, "height": 0.2}],
            "ai_generation_authorized": True,
            "provider": "ps_model_router",
            "model_name": "gpt-image-2-vip",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "done"
    assert body["input_refs"][0]["role"] == "after_source"
    assert ".case-workbench-simulation-inputs" in body["input_refs"][0]["case_relative_path"]
    assert captured["before_image_path"] is None
    assert ".case-workbench-simulation-inputs" in str(captured["after_image_path"])
    assert Path(captured["after_image_path"]).read_bytes() == b"uploaded after bytes"

    with db.connect() as conn:
        sim = conn.execute("SELECT input_refs_json, audit_json FROM simulation_jobs").fetchone()
    assert ".case-workbench-simulation-inputs" in json.loads(sim["input_refs_json"])[0]["case_relative_path"]
    assert json.loads(sim["audit_json"])["input_refs"][0]["role"] == "after_source"


def test_simulate_after_uses_ps_adapter_and_audits(client, seed_case, monkeypatch, tmp_path):
    from backend import ai_generation_adapter, db

    case_dir = tmp_path / "case-sim"
    case_dir.mkdir()
    (case_dir / "术前-正面.jpg").write_bytes(b"real before bytes")
    (case_dir / "术后-正面.jpg").write_bytes(b"real after bytes")
    case_id = seed_case(abs_path=str(case_dir), category="standard_face", template_tier="single")

    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        output_dir = tmp_path / "sim-output"
        output_dir.mkdir()
        out = output_dir / "after-ai-enhanced.jpg"
        out.write_bytes(b"real ai output bytes")
        return {
            "status": "done",
            "output_refs": [{"kind": "ai_after_simulation", "path": str(out), "watermarked": True}],
            "audit": {"provider": "ps_model_router", "focus_targets": kwargs["focus_targets"], "watermark_applied": True},
            "watermarked": True,
            "error_message": None,
        }

    monkeypatch.setattr(ai_generation_adapter, "run_ps_model_router_after_simulation", fake_run)

    resp = client.post(
        f"/api/cases/{case_id}/simulate-after",
        json={
            "after_image_path": "术后-正面.jpg",
            "before_image_path": "术前-正面.jpg",
            "focus_targets": ["下颌线:更清晰"],
            "focus_regions": [{"x": 0.2, "y": 0.3, "width": 0.4, "height": 0.25, "label": "下颌线"}],
            "ai_generation_authorized": True,
            "provider": "ps_model_router",
            "model_name": "gpt-image-2-vip",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "done"
    assert body["provider"] == "ps_model_router"
    assert body["focus_regions"][0]["label"] == "下颌线"
    assert body["output_refs"][0]["kind"] == "ai_after_simulation"
    assert captured["model_name"] == "gpt-image-2-vip"
    assert captured["focus_regions"][0]["x"] == 0.2
    assert captured["after_image_path"] == case_dir / "术后-正面.jpg"
    assert captured["before_image_path"] == case_dir / "术前-正面.jpg"

    with db.connect() as conn:
        sim = conn.execute("SELECT * FROM simulation_jobs").fetchone()
        ai = conn.execute("SELECT * FROM ai_runs").fetchone()
    assert sim["status"] == "done"
    assert json.loads(sim["model_plan_json"])["focus_regions"][0]["label"] == "下颌线"
    assert json.loads(sim["output_refs_json"])[0]["kind"] == "ai_after_simulation"
    assert ai["provider"] == "ps_model_router"
    assert ai["status"] == "done"
    assert json.loads(ai["input_summary_json"])["focus_regions"][0]["width"] == 0.4


def test_simulate_after_records_failed_ps_adapter(client, seed_case, monkeypatch, tmp_path):
    from backend import ai_generation_adapter, db

    case_dir = tmp_path / "case-sim-fail"
    case_dir.mkdir()
    (case_dir / "术后-正面.jpg").write_bytes(b"real after bytes")
    case_id = seed_case(abs_path=str(case_dir), category="standard_face", template_tier="single")

    def fake_run(**kwargs):
        raise RuntimeError("ps router exploded")

    monkeypatch.setattr(ai_generation_adapter, "run_ps_model_router_after_simulation", fake_run)

    resp = client.post(
        f"/api/cases/{case_id}/simulate-after",
        json={
            "after_image_path": "术后-正面.jpg",
            "focus_targets": ["下颌线"],
            "focus_regions": [{"x": 0.2, "y": 0.25, "width": 0.3, "height": 0.2}],
            "ai_generation_authorized": True,
            "provider": "ps_model_router",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"
    assert "ps router exploded" in resp.json()["error_message"]

    with db.connect() as conn:
        sim = conn.execute("SELECT status, error_message, output_refs_json FROM simulation_jobs").fetchone()
        ai = conn.execute("SELECT status, error_message FROM ai_runs").fetchone()
    assert sim["status"] == "failed"
    assert sim["output_refs_json"] == "[]"
    assert "ps router exploded" in sim["error_message"]
    assert ai["status"] == "failed"


def test_list_preview_and_review_simulation_job(client, seed_case, monkeypatch, tmp_path):
    from backend import db

    case_dir = tmp_path / "case-sim-review"
    case_dir.mkdir()
    case_id = seed_case(abs_path=str(case_dir), category="standard_face", template_tier="single")
    sim_root = tmp_path / "simulation_jobs" / "1"
    sim_root.mkdir(parents=True)
    output = sim_root / "after-ai-enhanced.jpg"
    output.write_bytes(b"real ai bytes")

    with db.connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO simulation_jobs
              (case_id, status, focus_targets_json, policy_json, model_plan_json,
               input_refs_json, output_refs_json, watermarked, audit_json,
               error_message, created_at, updated_at)
            VALUES (?, 'done', ?, '{}', ?, '[]', ?, 1, '{}', NULL, ?, ?)
            """,
            (
                case_id,
                json.dumps(["下颌线"], ensure_ascii=False),
                json.dumps(
                    {
                        "provider": "ps_model_router",
                        "focus_regions": [{"x": 0.2, "y": 0.3, "width": 0.4, "height": 0.2}],
                    },
                    ensure_ascii=False,
                ),
                json.dumps([{"kind": "ai_after_simulation", "path": str(output), "watermarked": True}], ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        job_id = cur.lastrowid

    from backend import ai_generation_adapter

    # Keep the file-serving guard pointed at this test's fake simulation root.
    monkeypatch.setattr(ai_generation_adapter, "SIMULATION_ROOT", tmp_path / "simulation_jobs")

    listing = client.get(f"/api/cases/{case_id}/simulation-jobs")
    assert listing.status_code == 200, listing.text
    jobs = listing.json()
    assert jobs[0]["id"] == job_id
    assert jobs[0]["review_status"] is None
    assert jobs[0]["can_publish"] is False
    assert jobs[0]["review_decision"]["can_approve"] is True
    assert jobs[0]["review_decision"]["recommended_verdict"] == "needs_recheck"
    assert any(file["kind"] == "ai_after_simulation" for file in jobs[0]["available_files"])

    file_resp = client.get(f"/api/cases/{case_id}/simulation-jobs/{job_id}/file")
    assert file_resp.status_code == 200
    assert file_resp.content == b"real ai bytes"

    review = client.post(
        f"/api/cases/{case_id}/simulation-jobs/{job_id}/review",
        json={"verdict": "approved", "reviewer": "doctor", "note": "可作为AI增强示意"},
    )
    assert review.status_code == 200, review.text
    body = review.json()
    assert body["review_status"] == "approved"
    assert body["reviewer"] == "doctor"
    assert body["can_publish"] is True
    assert body["audit"]["review_history"][-1]["reviewer"] == "doctor"
    assert body["audit"]["review_history"][-1]["decision_snapshot"]["can_approve"] is True


def test_simulation_quality_queue_lists_and_reviews_ai_jobs(client, seed_case, monkeypatch, tmp_path):
    from backend import ai_generation_adapter, db

    case_dir = tmp_path / "case-sim-queue"
    case_dir.mkdir()
    case_id = seed_case(abs_path=str(case_dir), category="standard_face", template_tier="single", customer_raw="小绿")
    monkeypatch.setattr(ai_generation_adapter, "SIMULATION_ROOT", tmp_path / "simulation_jobs")

    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        done_id = conn.execute(
            """
            INSERT INTO simulation_jobs
              (case_id, status, focus_targets_json, policy_json, model_plan_json,
               input_refs_json, output_refs_json, watermarked, audit_json,
               error_message, created_at, updated_at)
            VALUES (?, 'done', ?, '{}', ?, '[]', '[]', 1, '{}', NULL, ?, ?)
            """,
            (
                case_id,
                json.dumps(["下颌线"], ensure_ascii=False),
                json.dumps(
                    {
                        "provider": "ps_model_router",
                        "model_name": "gpt-image-2-vip",
                        "focus_regions": [{"x": 0.2, "y": 0.3, "width": 0.4, "height": 0.2}],
                    },
                    ensure_ascii=False,
                ),
                now,
                now,
            ),
        ).lastrowid
        sim_root = tmp_path / "simulation_jobs" / str(done_id)
        sim_root.mkdir(parents=True)
        output = sim_root / "after-ai-enhanced.jpg"
        output.write_bytes(b"real ai queue bytes")
        comparison = sim_root / "controlled-policy-five-way-comparison.png"
        comparison.write_bytes(b"real comparison bytes")
        conn.execute(
            "UPDATE simulation_jobs SET output_refs_json = ? WHERE id = ?",
            (
                json.dumps([{"kind": "ai_after_simulation", "path": str(output), "watermarked": True}], ensure_ascii=False),
                done_id,
            ),
        )
        failed_id = conn.execute(
            """
            INSERT INTO simulation_jobs
              (case_id, status, focus_targets_json, policy_json, model_plan_json,
               input_refs_json, output_refs_json, watermarked, audit_json,
               error_message, created_at, updated_at)
            VALUES (?, 'failed', ?, '{}', '{}', '[]', '[]', 0, '{}', 'model timeout', ?, ?)
            """,
            (case_id, json.dumps(["口角"], ensure_ascii=False), now, now),
        ).lastrowid

    queue = client.get("/api/cases/simulation-jobs/quality-queue")
    assert queue.status_code == 200, queue.text
    body = queue.json()
    ids = [item["job"]["id"] for item in body["items"]]
    assert done_id in ids
    assert failed_id in ids
    done_item = next(item for item in body["items"] if item["job"]["id"] == done_id)
    assert done_item["case"]["customer_raw"] == "小绿"
    assert done_item["reviewable"] is True
    assert done_item["job"]["review_decision"]["can_approve"] is True
    assert {file["kind"] for file in done_item["job"]["available_files"]} >= {
        "ai_after_simulation",
        "controlled_policy_comparison",
    }
    failed_item = next(item for item in body["items"] if item["job"]["id"] == failed_id)
    assert failed_item["reviewable"] is False
    assert failed_item["issue_summary"][0] == "model timeout"
    assert any("没有可审核" in item for item in failed_item["issue_summary"])

    file_resp = client.get(f"/api/cases/simulation-jobs/{done_id}/file")
    assert file_resp.status_code == 200
    assert file_resp.content == b"real ai queue bytes"
    comparison_resp = client.get(f"/api/cases/simulation-jobs/{done_id}/file", params={"kind": "comparison"})
    assert comparison_resp.status_code == 200
    assert comparison_resp.content == b"real comparison bytes"

    review = client.post(
        f"/api/cases/simulation-jobs/{done_id}/review",
        json={"verdict": "approved", "reviewer": "qa", "note": "AI示意可用"},
    )
    assert review.status_code == 200, review.text
    assert review.json()["can_publish"] is True

    reviewed = client.get("/api/cases/simulation-jobs/quality-queue", params={"status": "reviewed"})
    assert reviewed.status_code == 200
    reviewed_ids = [item["job"]["id"] for item in reviewed.json()["items"]]
    assert done_id in reviewed_ids

    pending = client.get("/api/cases/simulation-jobs/quality-queue")
    pending_ids = [item["job"]["id"] for item in pending.json()["items"]]
    assert done_id not in pending_ids


def test_simulation_review_blocks_approval_without_controlled_inputs(client, seed_case, monkeypatch, tmp_path):
    from backend import ai_generation_adapter, db

    case_id = seed_case(abs_path=str(tmp_path / "case-sim-hard-block"), category="standard_face", template_tier="single")
    monkeypatch.setattr(ai_generation_adapter, "SIMULATION_ROOT", tmp_path / "simulation_jobs")
    now = datetime.now(timezone.utc).isoformat()
    sim_root = tmp_path / "simulation_jobs" / "1"
    sim_root.mkdir(parents=True)
    output = sim_root / "after-ai-enhanced.jpg"
    output.write_bytes(b"real ai bytes")
    with db.connect() as conn:
        job_id = conn.execute(
            """
            INSERT INTO simulation_jobs
              (case_id, status, focus_targets_json, policy_json, model_plan_json,
               input_refs_json, output_refs_json, watermarked, audit_json,
               error_message, created_at, updated_at)
            VALUES (?, 'done', ?, '{}', '{}', '[]', ?, 1, '{}', NULL, ?, ?)
            """,
            (
                case_id,
                json.dumps(["下颌线"], ensure_ascii=False),
                json.dumps([{"kind": "ai_after_simulation", "path": str(output), "watermarked": True}], ensure_ascii=False),
                now,
                now,
            ),
        ).lastrowid

    queue = client.get("/api/cases/simulation-jobs/quality-queue")
    item = next(item for item in queue.json()["items"] if item["job"]["id"] == job_id)
    assert item["job"]["review_decision"]["recommended_verdict"] == "rejected"
    assert item["job"]["review_decision"]["can_approve"] is False

    review = client.post(
        f"/api/cases/simulation-jobs/{job_id}/review",
        json={"verdict": "approved", "reviewer": "qa"},
    )
    assert review.status_code == 400
    assert "cannot be approved" in review.json()["detail"]


def test_ai_review_policy_is_configurable_and_quality_report_counts(client, seed_case, monkeypatch, tmp_path):
    from backend import ai_generation_adapter, db, simulation_quality

    case_id = seed_case(abs_path=str(tmp_path / "case-policy-report"), category="standard_face", template_tier="single")
    monkeypatch.setattr(ai_generation_adapter, "SIMULATION_ROOT", tmp_path / "simulation_jobs")
    monkeypatch.setattr(simulation_quality, "POLICY_PATH", tmp_path / "ai-review-policy.json")
    now = datetime.now(timezone.utc).isoformat()
    sim_root = tmp_path / "simulation_jobs" / "1"
    sim_root.mkdir(parents=True)
    output = sim_root / "after-ai-enhanced.jpg"
    output.write_bytes(b"real ai bytes")
    audit_json = {
        "difference_analysis": {
            "full_frame_change_score": 2.0,
            "target_region_change_score": 4.2,
            "non_target_change_score": 1.7,
            "p95_change_score": 5.4,
            "changed_pixel_ratio_8pct": 0.016,
        }
    }

    with db.connect() as conn:
        render_id = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, finished_at, output_path, semantic_judge)
               VALUES (?, 'fumei', 'tri-compare', 'done_with_issues', ?, ?, '/tmp/final.jpg', 'auto')""",
            (case_id, now, now),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO render_quality
              (render_job_id, quality_status, quality_score, can_publish, artifact_mode,
               manifest_status, blocking_count, warning_count, metrics_json, review_verdict,
               created_at, updated_at)
            VALUES (?, 'done_with_issues', 70, 0, 'real_layout', 'ok', 0, 2, '{}', 'needs_recheck', ?, ?)
            """,
            (render_id, now, now),
        )
        job_id = conn.execute(
            """
            INSERT INTO simulation_jobs
              (case_id, status, focus_targets_json, policy_json, model_plan_json,
               input_refs_json, output_refs_json, watermarked, audit_json,
               error_message, created_at, updated_at)
            VALUES (?, 'done', ?, '{}', ?, '[]', ?, 1, ?, NULL, ?, ?)
            """,
            (
                case_id,
                json.dumps(["下颌线"], ensure_ascii=False),
                json.dumps(
                    {
                        "provider": "ps_model_router",
                        "focus_regions": [{"x": 0.2, "y": 0.3, "width": 0.4, "height": 0.2}],
                    },
                    ensure_ascii=False,
                ),
                json.dumps([{"kind": "ai_after_simulation", "path": str(output), "watermarked": True}], ensure_ascii=False),
                json.dumps(audit_json, ensure_ascii=False),
                now,
                now,
            ),
        ).lastrowid

    policy = client.get("/api/cases/simulation-jobs/review-policy")
    assert policy.status_code == 200
    assert policy.json()["thresholds"]["approve_non_target_max"] == 3.0

    queue = client.get("/api/cases/simulation-jobs/quality-queue")
    item = next(item for item in queue.json()["items"] if item["job"]["id"] == job_id)
    assert item["job"]["review_decision"]["recommended_verdict"] == "approved"

    preview = client.post(
        "/api/cases/simulation-jobs/review-policy/preview?limit=50",
        json={"thresholds": {"approve_non_target_max": 1.0}, "name": "strict-preview"},
    )
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert preview_body["preview_policy"]["name"] == "strict-preview"
    assert preview_body["summary"]["changed_count"] >= 1
    preview_item = next(item for item in preview_body["items"] if item["id"] == job_id)
    assert preview_item["changed"] is True
    assert preview_item["current"]["recommended_verdict"] == "approved"
    assert preview_item["preview"]["recommended_verdict"] == "needs_recheck"
    assert client.get("/api/cases/simulation-jobs/review-policy").json()["name"] == "controlled_region_diff_v1"

    updated = client.put(
        "/api/cases/simulation-jobs/review-policy",
        json={"thresholds": {"approve_non_target_max": 1.0}, "name": "strict-test"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "strict-test"
    assert updated.json()["version"] == 2

    queue = client.get("/api/cases/simulation-jobs/quality-queue")
    item = next(item for item in queue.json()["items"] if item["job"]["id"] == job_id)
    assert item["job"]["review_decision"]["recommended_verdict"] == "needs_recheck"
    assert item["job"]["review_decision"]["policy_name"] == "strict-test"
    assert "框外变化 1.7 偏高" in "；".join(item["job"]["review_decision"]["warning_reasons"])

    rec_queue = client.get("/api/cases/simulation-jobs/quality-queue?status=all&recommendation=needs_recheck")
    assert rec_queue.status_code == 200, rec_queue.text
    assert any(row["job"]["id"] == job_id for row in rec_queue.json()["items"])

    report = client.get("/api/cases/quality-report")
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["policy"]["name"] == "strict-test"
    assert body["render"]["total"] == 1
    assert body["render"]["reviewed"] == 1
    assert body["simulation"]["by_system_recommendation"]["needs_recheck"] >= 1
    assert body["totals"]["artifacts"] >= 2


def test_simulation_file_rejects_path_outside_audit_dir(client, seed_case, monkeypatch, tmp_path):
    from backend import ai_generation_adapter, db

    case_id = seed_case(abs_path=str(tmp_path / "case-sim-unsafe"), category="standard_face", template_tier="single")
    unsafe = tmp_path / "outside.jpg"
    unsafe.write_bytes(b"unsafe")
    monkeypatch.setattr(ai_generation_adapter, "SIMULATION_ROOT", tmp_path / "simulation_jobs")

    with db.connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO simulation_jobs
              (case_id, status, focus_targets_json, policy_json, model_plan_json,
               input_refs_json, output_refs_json, watermarked, audit_json,
               error_message, created_at, updated_at)
            VALUES (?, 'done', '[]', '{}', '{}', '[]', ?, 1, '{}', NULL, ?, ?)
            """,
            (
                case_id,
                json.dumps([{"kind": "ai_after_simulation", "path": str(unsafe)}], ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        job_id = cur.lastrowid

    resp = client.get(f"/api/cases/{case_id}/simulation-jobs/{job_id}/file")
    assert resp.status_code == 403


def test_render_queue_pulls_overrides_into_run_render(client, seed_case, monkeypatch, tmp_path):
    """Stage B: render_queue._execute_render reads case_image_overrides and
    passes them as kwarg to render_executor.run_render."""
    from backend import db, render_executor, render_queue

    case_id = seed_case(abs_path="/tmp/case-render-ov")
    # Create one override
    client.patch(
        f"/api/cases/{case_id}/images/term.jpg",
        json={"manual_phase": "before", "manual_view": "front"},
    )
    # Insert a queued render job for this case
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, ?, ?, 'queued', ?, 'off')""",
            (case_id, "fumei", "single-compare", datetime.now(timezone.utc).isoformat()),
        )
        job_id = cur.lastrowid

    captured: dict[str, object] = {}
    output = tmp_path / "out.jpg"
    output.write_bytes(b"jpeg")

    def fake_run(case_dir, brand, template, semantic_judge, manual_overrides=None, **kw):
        captured["case_dir"] = case_dir
        captured["manual_overrides"] = manual_overrides
        return {
            "output_path": str(output),
            "manifest_path": "/tmp/m.json",
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "case_mode": "tri-compare",
            "effective_templates": [],
            "manual_overrides_applied": list((manual_overrides or {}).keys()),
        }

    monkeypatch.setattr(render_executor, "run_render", fake_run)

    queue = render_queue.RenderQueue()
    queue._execute_render(job_id)

    assert captured["manual_overrides"] == {
        "term.jpg": {"phase": "before", "view": "front"},
    }
    # Verify the job moved to done
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "done"


def test_render_queue_no_overrides_passes_empty_dict(client, seed_case, monkeypatch, tmp_path):
    from backend import db, render_executor, render_queue

    case_id = seed_case(abs_path="/tmp/case-render-no-ov")
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, ?, ?, 'queued', ?, 'off')""",
            (case_id, "fumei", "single-compare", datetime.now(timezone.utc).isoformat()),
        )
        job_id = cur.lastrowid

    captured: dict[str, object] = {}
    output = tmp_path / "out.jpg"
    output.write_bytes(b"jpeg")

    def fake_run(case_dir, brand, template, semantic_judge, manual_overrides=None, **kw):
        captured["manual_overrides"] = manual_overrides
        return {
            "output_path": str(output),
            "manifest_path": "/tmp/m.json",
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "case_mode": "tri-compare",
            "effective_templates": [],
            "manual_overrides_applied": [],
        }

    monkeypatch.setattr(render_executor, "run_render", fake_run)
    render_queue.RenderQueue()._execute_render(job_id)
    assert captured["manual_overrides"] == {}


def test_render_queue_passes_source_selection_plan_to_renderer(client, seed_case, monkeypatch, tmp_path):
    from backend import db, render_executor, render_queue

    case_dir = tmp_path / "case-render-selection-plan"
    filenames = ["auto-before.jpg", "auto-after.jpg", "better-before.jpg", "better-after.jpg"]
    _write_case_files(str(case_dir), filenames)
    case_id = seed_case(abs_path=str(case_dir))
    meta = {
        "image_files": filenames,
        "image_review_states": {
            "auto-before.jpg": {"verdict": "deferred"},
            "auto-after.jpg": {"verdict": "deferred"},
            "better-before.jpg": {"verdict": "usable"},
            "better-after.jpg": {"verdict": "usable"},
        },
    }
    skill_metadata = [
        {
            "filename": filename,
            "relative_path": filename,
            "phase": "before" if "before" in filename else "after",
            "phase_source": "filename",
            "angle": "front",
            "view_bucket": "front",
            "angle_source": "pose",
            "angle_confidence": 0.92,
            "issues": [],
            "rejection_reason": None,
        }
        for filename in filenames
    ]
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, skill_image_metadata_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), json.dumps(skill_metadata, ensure_ascii=False), case_id),
        )
        cur = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, ?, ?, 'queued', ?, 'off')""",
            (case_id, "fumei", "single-compare", datetime.now(timezone.utc).isoformat()),
        )
        job_id = cur.lastrowid

    captured: dict[str, object] = {}
    output = tmp_path / "out.jpg"
    output.write_bytes(b"jpeg")

    def fake_run(case_dir, brand, template, semantic_judge, manual_overrides=None, selection_plan=None, **kw):
        captured["manual_overrides"] = manual_overrides
        captured["selection_plan"] = selection_plan
        return {
            "output_path": str(output),
            "manifest_path": "/tmp/m.json",
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "case_mode": "tri-compare",
            "effective_templates": [],
            "manual_overrides_applied": list((manual_overrides or {}).keys()),
            "render_selection_audit": {"applied_slots": [{"slot": "front"}], "overrode": []},
        }

    monkeypatch.setattr(render_executor, "run_render", fake_run)
    render_queue.RenderQueue()._execute_render(job_id)

    plan = captured["selection_plan"]
    assert plan["policy"] == "source_selection_v1"
    assert plan["slots"]["front"]["before"]["filename"] == "better-before.jpg"
    assert plan["slots"]["front"]["after"]["filename"] == "better-after.jpg"
    assert plan["slots"]["front"]["pair_quality"]["label"] == "strong"
    overrides = captured["manual_overrides"]
    assert overrides["better-before.jpg"]["selection_score"] > overrides["auto-before.jpg"]["selection_score"]
    assert overrides["better-after.jpg"]["review_verdict"] == "usable"


def test_render_queue_uses_previous_render_feedback_to_reselect_candidates(client, seed_case, monkeypatch, tmp_path):
    from backend import db, render_executor, render_queue

    case_dir = tmp_path / "case-render-feedback-selection"
    filenames = [
        "bad-before-front.jpg",
        "bad-after-front.jpg",
        "clean-before-front.jpg",
        "clean-after-front.jpg",
    ]
    _write_case_files(str(case_dir), filenames)
    case_id = seed_case(abs_path=str(case_dir))
    meta = {"image_files": filenames}
    skill_metadata = [
        {
            "filename": filename,
            "relative_path": filename,
            "phase": "before" if "before" in filename else "after",
            "phase_source": "filename",
            "angle": "front",
            "view_bucket": "front",
            "angle_source": "pose",
            "angle_confidence": 0.92,
            "issues": [],
            "rejection_reason": None,
        }
        for filename in filenames
    ]
    previous_feedback = {
        "render_selection_source_provenance": [
            {
                "case_id": case_id,
                "filename": "bad-before-front.jpg",
                "render_filename": "bad-before-front.jpg",
                "view": "front",
            },
            {
                "case_id": case_id,
                "filename": "bad-after-front.jpg",
                "render_filename": "bad-after-front.jpg",
                "view": "front",
            },
        ],
        "warning_layers": {
            "selected_actionable": [
                "job-1：bad-before-front.jpg - 面部检测失败: 未检测到面部",
                "job-1：bad-after-front.jpg - 面部检测失败: 未检测到面部",
            ]
        },
        "render_selection_audit": {
            "applied_slots": [
                {
                    "slot": "front",
                    "before": "bad-before-front.jpg",
                    "after": "bad-after-front.jpg",
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
    }
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, skill_image_metadata_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), json.dumps(skill_metadata, ensure_ascii=False), case_id),
        )
        conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, finished_at, semantic_judge, meta_json)
               VALUES (?, ?, ?, 'done_with_issues', ?, ?, 'off', ?)""",
            (
                case_id,
                "fumei",
                "single-compare",
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                json.dumps(previous_feedback, ensure_ascii=False),
            ),
        )
        cur = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, ?, ?, 'queued', ?, 'off')""",
            (case_id, "fumei", "single-compare", datetime.now(timezone.utc).isoformat()),
        )
        job_id = cur.lastrowid

    captured: dict[str, object] = {}
    output = tmp_path / "out-feedback.jpg"
    output.write_bytes(b"jpeg")

    def fake_run(case_dir, brand, template, semantic_judge, manual_overrides=None, selection_plan=None, **kw):
        captured["manual_overrides"] = manual_overrides
        captured["selection_plan"] = selection_plan
        return {
            "output_path": str(output),
            "manifest_path": "/tmp/m-feedback.json",
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "case_mode": "single-compare",
            "effective_templates": [],
            "manual_overrides_applied": list((manual_overrides or {}).keys()),
            "render_selection_audit": {"applied_slots": [{"slot": "front"}], "overrode": []},
        }

    monkeypatch.setattr(render_executor, "run_render", fake_run)
    render_queue.RenderQueue()._execute_render(job_id)

    plan = captured["selection_plan"]
    assert plan["feedback_applied"] is True
    assert plan["slots"]["front"]["before"]["filename"] == "clean-before-front.jpg"
    assert plan["slots"]["front"]["after"]["filename"] == "clean-after-front.jpg"
    assert plan["feedback_summary"]["source_job_id"] > 0
    overrides = captured["manual_overrides"]
    assert overrides["bad-before-front.jpg"]["render_feedback"]["penalty"] >= 30


def test_render_queue_reuses_primary_render_metadata_for_bound_oblique_reselection(
    client, seed_case, monkeypatch, tmp_path
):
    from backend import db, render_executor, render_queue, source_images

    before_dir = tmp_path / "oblique-before"
    after_dir = tmp_path / "oblique-after"
    before_files = ["bad-before-45.jpg", "good-before-45.jpg"]
    after_files = ["after-bad-45.jpg", "after-good-45.jpg"]
    _write_case_files(str(before_dir), before_files)
    _write_case_files(str(after_dir), after_files)
    before_case = seed_case(abs_path=str(before_dir))
    after_case = seed_case(abs_path=str(after_dir))

    before_bad_render = render_queue._safe_link_name(before_case, str(before_dir), "bad-before-45.jpg")
    before_good_render = render_queue._safe_link_name(before_case, str(before_dir), "good-before-45.jpg")
    after_bad_render = render_queue._safe_link_name(after_case, str(after_dir), "after-bad-45.jpg")
    after_good_render = render_queue._safe_link_name(after_case, str(after_dir), "after-good-45.jpg")
    primary_meta = {
        "image_files": before_files,
        source_images.SOURCE_BINDINGS_META_KEY: {"case_ids": [after_case]},
    }
    bound_meta = {"image_files": after_files}
    skill_metadata = [
        {
            "filename": "bad-before-45.jpg",
            "relative_path": before_bad_render,
            "phase": "before",
            "phase_source": "manual",
            "angle": "oblique",
            "view_bucket": "oblique",
            "angle_source": "manual",
            "angle_confidence": 1.0,
            "direction": "right",
            "pose": {"yaw": 30.0, "pitch": 12.0, "roll": -80.0},
            "issues": [],
        },
        {
            "filename": "good-before-45.jpg",
            "relative_path": before_good_render,
            "phase": "before",
            "phase_source": "manual",
            "angle": "oblique",
            "view_bucket": "oblique",
            "angle_source": "manual",
            "angle_confidence": None,
            "direction": "right",
            "pose": {"yaw": 44.0, "pitch": 7.0, "roll": -86.0},
            "issues": [],
        },
        {
            "filename": after_bad_render,
            "relative_path": after_bad_render,
            "phase": "after",
            "phase_source": "manual",
            "angle": "oblique",
            "view_bucket": "oblique",
            "angle_source": "manual",
            "angle_confidence": 1.0,
            "direction": "right",
            "pose": {"yaw": 65.0, "pitch": 20.0, "roll": -60.0},
            "issues": [],
        },
        {
            "filename": after_good_render,
            "relative_path": after_good_render,
            "phase": "after",
            "phase_source": "manual",
            "angle": "oblique",
            "view_bucket": "oblique",
            "angle_source": "manual",
            "angle_confidence": 1.0,
            "direction": "right",
            "pose": {"yaw": 43.0, "pitch": 7.5, "roll": -85.5},
            "issues": [],
        },
    ]
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, skill_image_metadata_json = ? WHERE id = ?",
            (json.dumps(primary_meta, ensure_ascii=False), json.dumps(skill_metadata, ensure_ascii=False), before_case),
        )
        conn.execute(
            "UPDATE cases SET meta_json = ?, skill_image_metadata_json = NULL WHERE id = ?",
            (json.dumps(bound_meta, ensure_ascii=False), after_case),
        )
        for filename in after_files:
            conn.execute(
                """
                INSERT INTO case_image_overrides
                  (case_id, filename, manual_phase, manual_view, updated_at)
                VALUES (?, ?, 'after', 'oblique', ?)
                """,
                (after_case, filename, now),
            )
        cur = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, ?, ?, 'queued', ?, 'off')""",
            (before_case, "fumei", "single-compare", now),
        )
        job_id = cur.lastrowid

    captured: dict[str, object] = {}
    output = tmp_path / "out-oblique-metadata.jpg"
    output.write_bytes(b"jpeg")

    def fake_run(case_dir, brand, template, semantic_judge, manual_overrides=None, selection_plan=None, **kw):
        captured["manual_overrides"] = manual_overrides
        captured["selection_plan"] = selection_plan
        return {
            "output_path": str(output),
            "manifest_path": "/tmp/m-oblique-metadata.json",
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "case_mode": "single-compare",
            "effective_templates": [],
            "render_selection_audit": {"applied_slots": [{"slot": "oblique"}], "overrode": []},
        }

    monkeypatch.setattr(render_executor, "run_render", fake_run)
    render_queue.RenderQueue()._execute_render(job_id)

    oblique = captured["selection_plan"]["slots"]["oblique"]
    assert oblique["before"]["filename"] == "good-before-45.jpg"
    assert oblique["after"]["filename"] == "after-good-45.jpg"
    assert oblique["after"]["selection_metadata_source"] == "primary_render_history"
    assert oblique["pair_quality"]["metrics"]["pose_delta"]["weighted"] < 3


def test_render_queue_uses_source_group_locked_pair_in_selection_plan(client, seed_case, monkeypatch, tmp_path):
    from backend import db, render_executor, render_queue, source_selection

    case_dir = tmp_path / "case-render-source-lock"
    filenames = [
        "front-before-a.jpg",
        "front-after-a.jpg",
        "front-before-z.jpg",
        "front-after-z.jpg",
    ]
    _write_case_files(str(case_dir), filenames)
    case_id = seed_case(abs_path=str(case_dir))
    meta = {
        "image_files": filenames,
        source_selection.SOURCE_GROUP_SELECTION_META_KEY: {
            "locked_slots": {
                "front": {
                    "before": {"case_id": case_id, "filename": "front-before-z.jpg"},
                    "after": {"case_id": case_id, "filename": "front-after-z.jpg"},
                    "reviewer": "test-lock",
                    "reason": "锁定正面备选",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
            "accepted_warnings": [],
        },
    }
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), case_id),
        )
        cur = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, ?, ?, 'queued', ?, 'off')""",
            (case_id, "fumei", "single-compare", datetime.now(timezone.utc).isoformat()),
        )
        job_id = cur.lastrowid

    captured: dict[str, object] = {}
    output = tmp_path / "out-lock.jpg"
    output.write_bytes(b"jpeg")

    def fake_run(case_dir, brand, template, semantic_judge, manual_overrides=None, selection_plan=None, **kw):
        captured["manual_overrides"] = manual_overrides
        captured["selection_plan"] = selection_plan
        return {
            "output_path": str(output),
            "manifest_path": "/tmp/m-lock.json",
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "case_mode": "single-compare",
            "effective_templates": [],
            "manual_overrides_applied": list((manual_overrides or {}).keys()),
            "render_selection_audit": {"applied_slots": [{"slot": "front"}], "overrode": []},
        }

    monkeypatch.setattr(render_executor, "run_render", fake_run)
    render_queue.RenderQueue()._execute_render(job_id)

    plan = captured["selection_plan"]
    assert plan["selection_controls"]["locked_slots"]["front"]["reviewer"] == "test-lock"
    assert plan["slots"]["front"]["before"]["filename"] == "front-before-z.jpg"
    assert plan["slots"]["front"]["after"]["filename"] == "front-after-z.jpg"
    assert plan["slots"]["front"]["pair_quality"]["metrics"]["source_group_lock"]["locked"] is True
    assert captured["manual_overrides"]["front-before-z.jpg"]["source_group_lock"]["locked"] is True


def test_render_queue_blocks_tri_compare_when_source_group_slots_missing(client, seed_case, monkeypatch, tmp_path):
    from backend import db, render_executor, render_queue

    case_dir = tmp_path / "case-render-tri-slot-gate"
    filenames = ["front-before.jpg", "front-after.jpg"]
    _write_case_files(str(case_dir), filenames)
    case_id = seed_case(abs_path=str(case_dir))
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": filenames}, ensure_ascii=False), case_id),
        )
        cur = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, ?, ?, 'queued', ?, 'off')""",
            (case_id, "fumei", "tri-compare", datetime.now(timezone.utc).isoformat()),
        )
        job_id = cur.lastrowid

    def fake_run(*args, **kwargs):
        raise AssertionError("tri-compare missing slots should be blocked before renderer")

    monkeypatch.setattr(render_executor, "run_render", fake_run)
    render_queue.RenderQueue()._execute_render(job_id)

    with db.connect() as conn:
        row = conn.execute("SELECT status, error_message, meta_json FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
    meta = json.loads(row["meta_json"])
    assert row["status"] == "blocked"
    assert "三联正式出图槽位未配齐" in row["error_message"]
    assert meta["ai_usage"]["semantic_judge_effective"] == "blocked-source-group-slot-preflight"
    assert any(
        item["view"] == "oblique" and item["missing"] == ["before", "after"]
        for item in meta["ai_usage"]["missing_slots"]
    )


def test_render_queue_allows_tri_compare_to_downgrade_when_side_pair_has_no_comparison_value(
    client, seed_case, monkeypatch, tmp_path
):
    from backend import db, render_executor, render_queue, source_selection

    case_dir = tmp_path / "case-render-dropped-side"
    filenames = [
        "front-before.jpg",
        "front-after.jpg",
        "oblique-before.jpg",
        "oblique-after.jpg",
        "side-before.jpg",
        "side-after.jpg",
    ]
    _write_case_files(str(case_dir), filenames)
    case_id = seed_case(abs_path=str(case_dir))
    meta = {
        "image_files": filenames,
        source_selection.SOURCE_GROUP_SELECTION_META_KEY: {
            "locked_slots": {
                "side": {
                    "before": {"case_id": case_id, "filename": "side-before.jpg"},
                    "after": {"case_id": case_id, "filename": "side-after.jpg"},
                    "reviewer": "operator",
                    "reason": "旧侧面锁片",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            }
        },
    }
    skill_metadata = [
        {
            "filename": filename,
            "relative_path": filename,
            "phase": "before" if "before" in filename else "after",
            "phase_source": "manual",
            "angle": "front" if "front" in filename else "oblique" if "oblique" in filename else "side",
            "view_bucket": "front" if "front" in filename else "oblique" if "oblique" in filename else "side",
            "angle_source": "manual",
            "angle_confidence": 0.96,
            "direction": "right" if "side" in filename else "center",
            "pose": (
                {"yaw": 20.0, "pitch": 0.0, "roll": 0.0}
                if filename == "side-before.jpg"
                else {"yaw": 82.0, "pitch": 8.0, "roll": 8.0}
                if filename == "side-after.jpg"
                else {"yaw": 1.0, "pitch": 1.0, "roll": 0.0}
            ),
            "issues": [],
            "rejection_reason": None,
        }
        for filename in filenames
    ]
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, skill_image_metadata_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), json.dumps(skill_metadata, ensure_ascii=False), case_id),
        )
        cur = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, ?, ?, 'queued', ?, 'off')""",
            (case_id, "fumei", "tri-compare", datetime.now(timezone.utc).isoformat()),
        )
        job_id = cur.lastrowid

    captured: dict[str, object] = {}
    output = tmp_path / "out-dropped-side.jpg"
    output.write_bytes(b"jpeg")

    def fake_run(case_dir, brand, template, semantic_judge, manual_overrides=None, selection_plan=None, **kw):
        captured["selection_plan"] = selection_plan
        return {
            "output_path": str(output),
            "manifest_path": "/tmp/m-dropped-side.json",
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "case_mode": "bi-compare",
            "effective_templates": ["bi-compare"],
            "manual_overrides_applied": list((manual_overrides or {}).keys()),
            "render_selection_audit": {
                "applied_slots": [{"slot": "front"}, {"slot": "oblique"}],
                "dropped_slots": [{"view": "side"}],
                "overrode": [],
            },
        }

    monkeypatch.setattr(render_executor, "run_render", fake_run)
    render_queue.RenderQueue()._execute_render(job_id)

    plan = captured["selection_plan"]
    assert set(plan["slots"]) == {"front", "oblique"}
    assert plan["effective_template_hint"] == "bi-compare"
    assert plan["dropped_slots"][0]["view"] == "side"
    assert plan["dropped_slots"][0]["reason"]["code"] == "low_comparison_value"
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "done"


def test_render_queue_infers_filename_labels_and_excludes_generated_artifacts(
    client, seed_case, monkeypatch, tmp_path
):
    from backend import db, render_executor, render_queue

    case_dir = tmp_path / "case-render-infer-labels"
    _write_case_files(str(case_dir), ["front-before.jpg", "front-after.jpg", "陈莹-正式品牌版-三联图.jpg"])
    case_id = seed_case(abs_path=str(case_dir))
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "image_files": [
                            "front-before.jpg",
                            "front-after.jpg",
                            "陈莹-正式品牌版-三联图.jpg",
                        ],
                        "image_count_total": 3,
                    },
                    ensure_ascii=False,
                ),
                case_id,
            ),
        )
        cur = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, ?, ?, 'queued', ?, 'auto')""",
            (case_id, "fumei", "single-compare", datetime.now(timezone.utc).isoformat()),
        )
        job_id = cur.lastrowid

    captured: dict[str, object] = {}
    output = tmp_path / "out.jpg"
    output.write_bytes(b"jpeg")

    def fake_run(case_dir, brand, template, semantic_judge, manual_overrides=None, **kw):
        captured["manual_overrides"] = manual_overrides
        return {
            "output_path": str(output),
            "manifest_path": str(tmp_path / "manifest.final.json"),
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "case_mode": "face",
            "effective_templates": ["single-compare"],
            "manual_overrides_applied": list((manual_overrides or {}).keys()),
        }

    monkeypatch.setattr(render_executor, "run_render", fake_run)
    render_queue.RenderQueue()._execute_render(job_id)

    overrides = captured["manual_overrides"]
    assert overrides["front-before.jpg"]["phase"] == "before"
    assert overrides["front-before.jpg"]["view"] == "front"
    assert overrides["front-after.jpg"]["phase"] == "after"
    assert overrides["front-after.jpg"]["view"] == "front"
    assert overrides["陈莹-正式品牌版-三联图.jpg"]["render_excluded"] is True

    with db.connect() as conn:
        row = conn.execute("SELECT status, meta_json FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
    meta = json.loads(row["meta_json"])
    assert row["status"] == "done"
    assert meta["ai_usage"]["source_count"] == 2
    assert meta["ai_usage"]["generated_artifact_count"] == 1
    assert meta["ai_usage"]["inferred_override_count"] == 2


def test_render_queue_blocks_when_only_generated_artifacts_remain(
    client, seed_case, monkeypatch
):
    from backend import db, render_executor, render_queue

    case_id = seed_case(abs_path="/tmp/case-render-generated-only")
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "image_files": [
                            "客户-正式品牌版-三联图.jpg",
                            "poster_v1.jpg",
                        ],
                        "image_count_total": 2,
                    },
                    ensure_ascii=False,
                ),
                case_id,
            ),
        )
        cur = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, ?, ?, 'queued', ?, 'auto')""",
            (case_id, "fumei", "single-compare", datetime.now(timezone.utc).isoformat()),
        )
        job_id = cur.lastrowid

    def fake_run(*args, **kwargs):
        raise AssertionError("generated-only cases should be blocked before renderer")

    monkeypatch.setattr(render_executor, "run_render", fake_run)
    render_queue.RenderQueue()._execute_render(job_id)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, error_message, meta_json FROM render_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    meta = json.loads(row["meta_json"])
    assert row["status"] == "blocked"
    assert "成品图/海报集合" in row["error_message"]
    assert meta["ai_usage"]["semantic_judge_effective"] == "blocked-generated-output-collection"
    assert meta["ai_usage"]["source_count"] == 0
    assert meta["ai_usage"]["generated_artifact_count"] == 2
    assert meta["ai_usage"]["source_profile"]["source_kind"] == "generated_output_collection"


def test_render_queue_blocks_single_source_before_renderer(client, seed_case, monkeypatch, tmp_path):
    from backend import db, render_executor, render_queue

    case_dir = tmp_path / "case-render-one-source"
    _write_case_files(str(case_dir), ["术前-正面.jpg"])
    case_id = seed_case(abs_path=str(case_dir))
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["术前-正面.jpg"]}, ensure_ascii=False), case_id),
        )
        cur = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, ?, ?, 'queued', ?, 'auto')""",
            (case_id, "fumei", "single-compare", datetime.now(timezone.utc).isoformat()),
        )
        job_id = cur.lastrowid

    def fake_run(*args, **kwargs):
        raise AssertionError("single-source cases should be blocked before renderer")

    monkeypatch.setattr(render_executor, "run_render", fake_run)
    render_queue.RenderQueue()._execute_render(job_id)

    with db.connect() as conn:
        row = conn.execute("SELECT status, error_message, meta_json FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
    meta = json.loads(row["meta_json"])
    assert row["status"] == "blocked"
    assert "真实源照片不足" in row["error_message"]
    assert meta["ai_usage"]["semantic_judge_effective"] == "blocked-insufficient-source-photos"
    assert meta["ai_usage"]["source_profile"]["source_kind"] == "insufficient_source_photos"


def test_render_queue_blocks_manual_not_source_marker(client, seed_case, monkeypatch, tmp_path):
    from backend import db, render_executor, render_queue

    case_dir = tmp_path / "case-render-manual-not-source"
    _write_case_files(str(case_dir), ["术前-正面.jpg", "术后-正面.jpg"])
    case_id = seed_case(abs_path=str(case_dir))
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE cases
            SET meta_json = ?, tags_json = ?, manual_blocking_issues_json = ?
            WHERE id = ?
            """,
            (
                json.dumps({"image_files": ["术前-正面.jpg", "术后-正面.jpg"]}, ensure_ascii=False),
                json.dumps(["素材归档"], ensure_ascii=False),
                json.dumps(["not_case_source_directory"], ensure_ascii=False),
                case_id,
            ),
        )
        cur = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, ?, ?, 'queued', ?, 'auto')""",
            (case_id, "fumei", "tri-compare", datetime.now(timezone.utc).isoformat()),
        )
        job_id = cur.lastrowid

    def fake_run(*args, **kwargs):
        raise AssertionError("manual-not-source cases should be blocked before renderer")

    monkeypatch.setattr(render_executor, "run_render", fake_run)
    render_queue.RenderQueue()._execute_render(job_id)

    with db.connect() as conn:
        row = conn.execute("SELECT status, error_message, meta_json FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
    meta = json.loads(row["meta_json"])
    assert row["status"] == "blocked"
    assert "素材归档" in row["error_message"]
    assert meta["ai_usage"]["semantic_judge_effective"] == "blocked-manual-not-source-directory"
    assert meta["ai_usage"]["source_profile"]["source_kind"] == "manual_not_case_source_directory"


def test_render_queue_marks_no_slot_result_blocked(client, seed_case, monkeypatch, tmp_path):
    from backend import db, render_executor, render_queue

    case_id = seed_case(abs_path="/tmp/case-render-no-slots")
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, ?, ?, 'queued', ?, 'off')""",
            (case_id, "fumei", "single-compare", datetime.now(timezone.utc).isoformat()),
        )
        job_id = cur.lastrowid

    manifest = tmp_path / "manifest.final.json"
    issue = "没有可渲染的角度槽位：请先确认术前/术后阶段与正面/45度/侧面角度配对"
    manifest.write_text(
        json.dumps({"blocking_issues": [issue], "warnings": []}, ensure_ascii=False),
        encoding="utf-8",
    )

    def fake_run(case_dir, brand, template, semantic_judge, manual_overrides=None, **kw):
        return {
            "output_path": None,
            "manifest_path": str(manifest),
            "status": "error",
            "blocking_issue_count": 1,
            "warning_count": 0,
            "case_mode": "face",
            "effective_templates": [],
            "manual_overrides_applied": [],
            "render_error": issue,
            "blocking_issues": [issue],
            "warnings": [],
        }

    monkeypatch.setattr(render_executor, "run_render", fake_run)
    render_queue.RenderQueue()._execute_render(job_id)

    with db.connect() as conn:
        job = conn.execute(
            "SELECT status, error_message, output_path, manifest_path FROM render_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        quality = conn.execute(
            "SELECT quality_status, can_publish, metrics_json FROM render_quality WHERE render_job_id = ?",
            (job_id,),
        ).fetchone()

    assert job["status"] == "blocked"
    assert job["output_path"] is None
    assert job["manifest_path"] == str(manifest)
    assert "没有可渲染的角度槽位" in job["error_message"]
    assert quality["quality_status"] == "blocked"
    assert quality["can_publish"] == 0
    metrics = json.loads(quality["metrics_json"])
    assert "没有可渲染的角度槽位" in metrics["render_error"]


def test_render_queue_blocks_large_unlabeled_semantic_auto_before_subprocess(
    client, seed_case, monkeypatch, tmp_path
):
    from backend import db, render_executor, render_queue

    files = [f"07041738_{idx:02}.jpg" for idx in range(35)]
    case_dir = tmp_path / "case-render-large-auto"
    _write_case_files(str(case_dir), files)
    case_id = seed_case(abs_path=str(case_dir))
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (
                json.dumps(
                    {"image_files": files, "image_count_total": len(files)},
                    ensure_ascii=False,
                ),
                case_id,
            ),
        )
        cur = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, ?, ?, 'queued', ?, 'auto')""",
            (case_id, "fumei", "tri-compare", datetime.now(timezone.utc).isoformat()),
        )
        job_id = cur.lastrowid

    def fake_run(*args, **kwargs):
        raise AssertionError("large unlabeled auto render should be preflight-blocked")

    monkeypatch.setattr(render_executor, "run_render", fake_run)
    render_queue.RenderQueue()._execute_render(job_id)

    with db.connect() as conn:
        job = conn.execute(
            "SELECT status, error_message, output_path, manifest_path, meta_json FROM render_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        quality = conn.execute(
            "SELECT quality_status, metrics_json FROM render_quality WHERE render_job_id = ?",
            (job_id,),
        ).fetchone()

    assert job["status"] == "blocked"
    assert job["output_path"] is None
    assert job["manifest_path"] is None
    assert "正式出图已阻断" in job["error_message"]
    assert "照片分类工作台" in job["error_message"]
    meta = json.loads(job["meta_json"])
    assert meta["ai_usage"]["semantic_judge_effective"] == "blocked-classification-preflight"
    assert meta["ai_usage"]["source_count"] == 35
    assert quality["quality_status"] == "blocked"
    metrics = json.loads(quality["metrics_json"])
    assert "正式出图已阻断" in metrics["render_error"]


def test_render_queue_blocks_large_auto_when_manual_pair_leaves_uncertain_images(
    client, seed_case, monkeypatch, tmp_path
):
    from backend import db, render_executor, render_queue

    files = [f"07041738_{idx:02}.jpg" for idx in range(35)]
    case_dir = tmp_path / "case-render-large-manual"
    _write_case_files(str(case_dir), files)
    case_id = seed_case(abs_path=str(case_dir))
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (
                json.dumps(
                    {"image_files": files, "image_count_total": len(files)},
                    ensure_ascii=False,
                ),
                case_id,
            ),
        )
        conn.execute(
            """INSERT INTO case_image_overrides
               (case_id, filename, manual_phase, manual_view, manual_transform_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                case_id,
                "07041738_00.jpg",
                "before",
                "front",
                json.dumps({"enabled": True, "offset_x_pct": 0.02, "offset_y_pct": 0.04, "scale": 0.97}),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.execute(
            """INSERT INTO case_image_overrides
               (case_id, filename, manual_phase, manual_view, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                case_id,
                "07041738_10.jpg",
                "after",
                "front",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        cur = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, ?, ?, 'queued', ?, 'auto')""",
            (case_id, "fumei", "tri-compare", datetime.now(timezone.utc).isoformat()),
        )
        job_id = cur.lastrowid

    captured: dict[str, object] = {}
    output = tmp_path / "out.jpg"
    output.write_bytes(b"jpeg")

    def fake_run(case_dir, brand, template, semantic_judge, manual_overrides=None, **kw):
        captured["semantic_judge"] = semantic_judge
        captured["manual_overrides"] = manual_overrides
        return {
            "output_path": str(output),
            "manifest_path": str(tmp_path / "manifest.final.json"),
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "case_mode": "face",
            "effective_templates": ["single-compare"],
            "manual_overrides_applied": list((manual_overrides or {}).keys()),
        }

    monkeypatch.setattr(render_executor, "run_render", fake_run)
    render_queue.RenderQueue()._execute_render(job_id)

    assert captured == {}
    with db.connect() as conn:
        job = conn.execute(
            "SELECT status, error_message, meta_json FROM render_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert job["status"] == "blocked"
    assert "待补充 33 张" in job["error_message"]
    meta = json.loads(job["meta_json"])
    assert meta["ai_usage"]["semantic_judge_requested"] == "auto"
    assert meta["ai_usage"]["semantic_judge_effective"] == "blocked-classification-preflight"
