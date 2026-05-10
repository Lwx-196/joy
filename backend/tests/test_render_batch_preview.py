"""Smoke tests for `POST /api/cases/render/batch/preview`."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _touch_case_files(abs_path: str, filenames: list[str]) -> None:
    root = Path(abs_path)
    root.mkdir(parents=True, exist_ok=True)
    for filename in filenames:
        target = root / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"real image bytes")


def test_batch_preview_empty_case_ids_400(client):
    resp = client.post("/api/cases/render/batch/preview", json={"case_ids": []})
    assert resp.status_code == 400
    assert "case_ids cannot be empty" in resp.json()["detail"]


def test_batch_preview_oversize_400(client):
    resp = client.post(
        "/api/cases/render/batch/preview",
        json={"case_ids": list(range(1, 100))},
    )
    assert resp.status_code == 400
    assert "exceeds maximum" in resp.json()["detail"]


def test_batch_preview_unknown_brand_400(client, seed_case):
    case_id = seed_case(abs_path="/tmp/case-batch-1")
    resp = client.post(
        "/api/cases/render/batch/preview",
        json={"case_ids": [case_id], "brand": "unknown_brand"},
    )
    assert resp.status_code == 400
    assert "unsupported brand" in resp.json()["detail"]


def test_batch_preview_separates_valid_and_missing(client, seed_case):
    a = seed_case(abs_path="/tmp/case-batch-a")
    b = seed_case(abs_path="/tmp/case-batch-b")
    resp = client.post(
        "/api/cases/render/batch/preview",
        json={"case_ids": [a, b, 9999]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid_count"] == 2
    assert body["invalid_count"] == 1
    assert sorted(body["valid_case_ids"]) == sorted([a, b])
    assert body["invalid"] == [{"case_id": 9999, "reason": "case_not_found"}]
    assert body["brand"] == "fumei"
    assert body["template"] == "tri-compare"


def test_batch_preview_flags_duplicates(client, seed_case):
    case_id = seed_case(abs_path="/tmp/case-batch-dup")
    resp = client.post(
        "/api/cases/render/batch/preview",
        json={"case_ids": [case_id, case_id, case_id]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid_count"] == 1
    assert body["valid_case_ids"] == [case_id]
    assert body["invalid_count"] == 1
    assert body["invalid"] == [{"case_id": case_id, "reason": "duplicate_in_batch"}]


def test_batch_preview_classifies_source_folder_readiness(client, seed_case, tmp_path):
    from backend import db

    generated_dir = tmp_path / "case-generated-only"
    one_source_dir = tmp_path / "case-one-source"
    missing_pair_dir = tmp_path / "case-missing-pair"
    ready_dir = tmp_path / "case-ready"
    _touch_case_files(str(generated_dir), ["客户-正式品牌版-三联图.jpg"])
    _touch_case_files(str(one_source_dir), ["术前-正面.jpg"])
    _touch_case_files(str(missing_pair_dir), ["术前-正面.jpg", "术前-侧面.jpg"])
    _touch_case_files(str(ready_dir), ["术前-正面.jpg", "术后-正面.jpg"])
    generated = seed_case(abs_path=str(generated_dir))
    one_source = seed_case(abs_path=str(one_source_dir))
    missing_pair = seed_case(abs_path=str(missing_pair_dir))
    ready = seed_case(abs_path=str(ready_dir))
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["客户-正式品牌版-三联图.jpg"]}, ensure_ascii=False), generated),
        )
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["术前-正面.jpg"]}, ensure_ascii=False), one_source),
        )
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["术前-正面.jpg", "术前-侧面.jpg"]}, ensure_ascii=False), missing_pair),
        )
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["术前-正面.jpg", "术后-正面.jpg"]}, ensure_ascii=False), ready),
        )

    resp = client.post(
        "/api/cases/render/batch/preview",
        json={"case_ids": [generated, one_source, missing_pair, ready]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid_case_ids"] == [ready]
    reasons = {item["case_id"]: item["reason"] for item in body["invalid"]}
    assert reasons[generated] == "no_real_source_photos"
    assert reasons[one_source] == "insufficient_source_photos"
    assert reasons[missing_pair] == "missing_before_after_pair"
    generated_item = next(item for item in body["invalid"] if item["case_id"] == generated)
    assert generated_item["source_profile"]["source_kind"] == "generated_output_collection"


def test_batch_enqueue_skips_non_source_cases(client, seed_case, no_job_pool, tmp_path):
    from backend import db

    generated_dir = tmp_path / "case-generated-enqueue"
    ready_dir = tmp_path / "case-ready-enqueue"
    _touch_case_files(str(generated_dir), ["poster_v1.jpg"])
    _touch_case_files(str(ready_dir), ["术前-正面.jpg", "术后-正面.jpg"])
    generated = seed_case(abs_path=str(generated_dir))
    ready = seed_case(abs_path=str(ready_dir))
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["poster_v1.jpg"]}, ensure_ascii=False), generated),
        )
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["术前-正面.jpg", "术后-正面.jpg"]}, ensure_ascii=False), ready),
        )

    resp = client.post("/api/cases/render/batch", json={"case_ids": [generated, ready]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped_count"] == 1
    assert len(body["job_ids"]) == 1
    assert body["invalid"][0]["case_id"] == generated
    assert body["invalid"][0]["reason"] == "no_real_source_photos"


def test_source_blockers_queue_and_action(client, seed_case, tmp_path):
    from backend import db

    generated_dir = tmp_path / "case-source-blocker-generated"
    one_source_dir = tmp_path / "case-source-blocker-one"
    missing_pair_dir = tmp_path / "case-source-blocker-pair"
    ready_dir = tmp_path / "case-source-blocker-ready"
    _touch_case_files(str(generated_dir), ["正式品牌版-三联图.jpg"])
    _touch_case_files(str(one_source_dir), ["术前-正面.jpg"])
    _touch_case_files(str(missing_pair_dir), ["术前-正面.jpg", "术前-侧面.jpg"])
    _touch_case_files(str(ready_dir), ["术前-正面.jpg", "术后-正面.jpg"])
    generated = seed_case(abs_path=str(generated_dir))
    one_source = seed_case(abs_path=str(one_source_dir))
    missing_pair = seed_case(abs_path=str(missing_pair_dir))
    ready = seed_case(abs_path=str(ready_dir))
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["正式品牌版-三联图.jpg"]}, ensure_ascii=False), generated),
        )
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["术前-正面.jpg"]}, ensure_ascii=False), one_source),
        )
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["术前-正面.jpg", "术前-侧面.jpg"]}, ensure_ascii=False), missing_pair),
        )
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["术前-正面.jpg", "术后-正面.jpg"]}, ensure_ascii=False), ready),
        )

    resp = client.get("/api/cases/source-blockers")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    reasons = {item["case_id"]: item["reason"] for item in body["items"]}
    assert reasons[generated] == "no_real_source_photos"
    assert reasons[one_source] == "insufficient_source_photos"
    assert reasons[missing_pair] == "missing_before_after_pair"
    assert ready not in reasons

    action = client.post(
        f"/api/cases/source-blockers/{generated}/action",
        json={"action": "mark_not_source", "reviewer": "test", "note": "成品集合"},
    )
    assert action.status_code == 200, action.text
    assert action.json()["marked_not_source"] is True
    with db.connect() as conn:
        row = conn.execute(
            "SELECT tags_json, manual_blocking_issues_json, notes FROM cases WHERE id = ?",
            (generated,),
        ).fetchone()
    assert "素材归档" in json.loads(row["tags_json"])
    assert "not_case_source_directory" in json.loads(row["manual_blocking_issues_json"])
    assert "成品集合" in row["notes"]


def test_batch_preview_respects_manual_not_source_marker(client, seed_case, tmp_path):
    from backend import db

    marked_dir = tmp_path / "case-marked-not-source"
    _touch_case_files(str(marked_dir), ["术前-正面.jpg", "术后-正面.jpg"])
    marked = seed_case(abs_path=str(marked_dir))
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
                marked,
            ),
        )

    resp = client.post("/api/cases/render/batch/preview", json={"case_ids": [marked]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid_count"] == 0
    assert body["invalid"][0]["reason"] == "no_real_source_photos"
    assert body["invalid"][0]["source_profile"]["source_kind"] == "manual_not_case_source_directory"


def test_batch_preview_blocks_stale_source_file_metadata(client, seed_case, tmp_path):
    from backend import db

    case_dir = tmp_path / "case-stale-source-files"
    case_dir.mkdir(parents=True)
    case_id = seed_case(abs_path=str(case_dir))
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (
                json.dumps({"image_files": ["术前-正面.jpg", "术后-正面.jpg"]}, ensure_ascii=False),
                case_id,
            ),
        )

    resp = client.post("/api/cases/render/batch/preview", json={"case_ids": [case_id]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid_count"] == 0
    assert body["invalid"][0]["reason"] == "missing_source_files"
    profile = body["invalid"][0]["source_profile"]
    assert profile["source_kind"] == "missing_source_files"
    assert profile["missing_source_count"] == 2
    assert profile["missing_source_samples"] == ["术前-正面.jpg", "术后-正面.jpg"]


def test_source_binding_candidates_and_batch_preview_use_bound_sibling_dirs(client, seed_case, tmp_path):
    from backend import db

    before_dir = tmp_path / "customer-a" / "2025-01-01" / "术前"
    after_dir = tmp_path / "customer-a" / "2025-01-01" / "术后"
    _touch_case_files(str(before_dir), ["IMG_0001.JPG", "IMG_0002.JPG"])
    _touch_case_files(str(after_dir), ["IMG_0101.JPG", "IMG_0102.JPG"])
    before_case = seed_case(abs_path=str(before_dir), customer_raw="客户A")
    after_case = seed_case(abs_path=str(after_dir), customer_raw="客户A")
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["IMG_0001.JPG", "IMG_0002.JPG"]}, ensure_ascii=False), before_case),
        )
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["IMG_0101.JPG", "IMG_0102.JPG"]}, ensure_ascii=False), after_case),
        )

    before_preview = client.post("/api/cases/render/batch/preview", json={"case_ids": [before_case]})
    assert before_preview.status_code == 200
    assert before_preview.json()["invalid"][0]["reason"] == "missing_before_after_pair"

    candidates = client.get(f"/api/cases/{before_case}/source-binding-candidates")
    assert candidates.status_code == 200, candidates.text
    body = candidates.json()
    assert body["candidates"][0]["case_id"] == after_case
    assert body["candidates"][0]["can_complete_pair"] is True
    assert body["candidates"][0]["merged_source_profile"]["source_kind"] == "ready_source"
    projected = body["candidates"][0]["projected_preflight"]
    assert projected["status"] == "blocked"
    assert projected["needs_manual_count"] == 4
    assert len(projected["slots"]) == 3
    assert {blocker["code"] for blocker in projected["hard_blockers"]} >= {"classification_open", "missing_render_slots"}

    bind = client.post(
        f"/api/cases/{before_case}/source-bindings",
        json={"source_case_ids": [after_case], "reviewer": "test", "note": "同次治疗术后目录"},
    )
    assert bind.status_code == 200, bind.text
    assert bind.json()["effective_source_profile"]["source_kind"] == "ready_source"

    after_preview = client.post("/api/cases/render/batch/preview", json={"case_ids": [before_case]})
    assert after_preview.status_code == 200, after_preview.text
    assert after_preview.json()["valid_case_ids"] == [before_case]
    assert after_preview.json()["invalid"] == []


def test_case_source_group_surfaces_bound_images_and_edits_source_case(client, seed_case, tmp_path):
    from backend import db

    root = tmp_path / "customer-a" / "2025-01-01"
    before_dir = root / "术前"
    after_dir = root / "术后"
    before_dir.mkdir(parents=True)
    after_dir.mkdir(parents=True)
    (before_dir / "IMG_0001.JPG").write_bytes(b"real before image")
    (after_dir / "IMG_0101.JPG").write_bytes(b"real after image")
    before_case = seed_case(abs_path=str(before_dir), customer_raw="客户A")
    after_case = seed_case(abs_path=str(after_dir), customer_raw="客户A")
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["IMG_0001.JPG"]}, ensure_ascii=False), before_case),
        )
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["IMG_0101.JPG"]}, ensure_ascii=False), after_case),
        )

    bind = client.post(
        f"/api/cases/{before_case}/source-bindings",
        json={"source_case_ids": [after_case], "reviewer": "test", "note": "同次治疗拆分目录"},
    )
    assert bind.status_code == 200, bind.text

    primary_queue = client.get(f"/api/image-workbench/queue?case_id={before_case}&status=review_needed")
    assert primary_queue.status_code == 200, primary_queue.text
    assert primary_queue.json()["total"] == 1
    assert {item["case_id"] for item in primary_queue.json()["items"]} == {before_case}

    source_group_queue = client.get(
        f"/api/image-workbench/queue?case_id={before_case}&source_group_case_id={before_case}&status=review_needed"
    )
    assert source_group_queue.status_code == 200, source_group_queue.text
    assert source_group_queue.json()["total"] == 2
    assert {item["case_id"] for item in source_group_queue.json()["items"]} == {before_case, after_case}

    group = client.get(f"/api/cases/{before_case}/source-group")
    assert group.status_code == 200, group.text
    body = group.json()
    assert body["bound_case_ids"] == [after_case]
    assert body["effective_source_profile"]["source_kind"] == "ready_source"
    assert body["image_count"] == 2
    by_case = {item["case_id"]: item for item in body["sources"]}
    assert by_case[before_case]["role"] == "primary"
    assert by_case[after_case]["role"] == "bound"
    before_img = by_case[before_case]["images"][0]
    after_img = by_case[after_case]["images"][0]
    assert before_img["phase"] == "before"
    assert before_img["phase_source"] == "directory"
    assert after_img["phase"] == "after"
    assert after_img["preview_url"].startswith(f"/api/cases/{after_case}/files?name=IMG_0101")

    before_view = client.patch(
        f"/api/cases/{before_case}/images/IMG_0001.JPG",
        json={"manual_view": "front"},
    )
    after_view = client.post(
        "/api/image-workbench/batch",
        json={
            "items": [{"case_id": after_case, "filename": "IMG_0101.JPG"}],
            "manual_view": "front",
            "reviewer": "test",
        },
    )
    assert before_view.status_code == 200, before_view.text
    assert after_view.status_code == 200, after_view.text
    assert after_view.json()["updated"] == 1
    review = client.post(
        f"/api/cases/{after_case}/image-review/IMG_0101.JPG",
        json={"verdict": "usable", "reviewer": "test", "note": "绑定源组复核"},
    )
    assert review.status_code == 200, review.text

    updated = client.get(f"/api/cases/{before_case}/source-group")
    assert updated.status_code == 200, updated.text
    body = updated.json()
    front_slot = next(slot for slot in body["preflight"]["slots"] if slot["view"] == "front")
    assert front_slot["ready"] is True
    assert front_slot["before_count"] == 1
    assert front_slot["after_count"] == 1
    assert front_slot["selected_before"]["case_id"] == before_case
    assert front_slot["selected_after"]["case_id"] == after_case
    assert front_slot["before_candidates"][0]["filename"] == "IMG_0001.JPG"
    assert front_slot["after_candidates"][0]["filename"] == "IMG_0101.JPG"
    assert front_slot["selected_before"]["selection_score"] >= 60
    assert "selection_reasons" in front_slot["selected_before"]
    assert front_slot["pair_quality"]["score"] >= 60
    assert front_slot["pair_quality"]["label"] in {"strong", "review"}
    manifest = body["preflight"]["formal_candidate_manifest"]
    manifest_front = manifest["slots"]["front"]
    assert manifest["readiness_score"] == body["preflight"]["readiness_score"]
    assert manifest["renderable_slot_count"] == 1
    assert manifest["blocking_reasons"]
    assert manifest_front["quality_prediction"]["decision"] == "render"
    assert manifest_front["quality_prediction"]["pair_score"] >= 60
    assert manifest_front["quality_prediction"]["angle_confidence"]["before"] is not None
    assert manifest_front["quality_prediction"]["recommended_action"] == "进入正式出图候选"
    updated_after = next(source for source in body["sources"] if source["case_id"] == after_case)["images"][0]
    assert updated_after["view"] == "front"
    assert updated_after["view_source"] == "manual"
    assert updated_after["review_state"]["verdict"] == "usable"


def test_case_detail_render_gate_uses_ready_source_group_authority(client, seed_case, tmp_path):
    from backend import db

    before_dir = tmp_path / "customer-a" / "术前"
    after_dir = tmp_path / "customer-a" / "术后"
    before_files = ["before-front.jpg", "before-45.jpg", "before-side.jpg"]
    after_files = ["after-front.jpg", "after-45.jpg", "after-side.jpg"]
    _touch_case_files(str(before_dir), before_files)
    _touch_case_files(str(after_dir), after_files)
    before_case = seed_case(abs_path=str(before_dir), customer_raw="客户A")
    after_case = seed_case(abs_path=str(after_dir), customer_raw="客户A")
    before_meta = [
        {"filename": "before-front.jpg", "phase": "before", "view_bucket": "front", "angle": "front"},
        {"filename": "before-45.jpg", "phase": "before", "view_bucket": "oblique", "angle": "oblique"},
        {"filename": "before-side.jpg", "phase": "before", "view_bucket": "side", "angle": "side"},
    ]
    after_meta = [
        {"filename": "after-front.jpg", "phase": "after", "view_bucket": "front", "angle": "front"},
        {"filename": "after-45.jpg", "phase": "after", "view_bucket": "oblique", "angle": "oblique"},
        {"filename": "after-side.jpg", "phase": "after", "view_bucket": "side", "angle": "side"},
    ]
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, skill_image_metadata_json = ? WHERE id = ?",
            (
                json.dumps({"image_files": before_files}, ensure_ascii=False),
                json.dumps(before_meta, ensure_ascii=False),
                before_case,
            ),
        )
        conn.execute(
            "UPDATE cases SET meta_json = ?, skill_image_metadata_json = ? WHERE id = ?",
            (
                json.dumps({"image_files": after_files}, ensure_ascii=False),
                json.dumps(after_meta, ensure_ascii=False),
                after_case,
            ),
        )

    bind = client.post(
        f"/api/cases/{before_case}/source-bindings",
        json={"source_case_ids": [after_case], "reviewer": "test", "note": "同次治疗拆分目录"},
    )
    assert bind.status_code == 200, bind.text
    source_group = client.get(f"/api/cases/{before_case}/source-group")
    assert source_group.status_code == 200, source_group.text
    assert source_group.json()["preflight"]["status"] == "ready"

    detail = client.get(f"/api/cases/{before_case}")
    assert detail.status_code == 200, detail.text
    render_gate = detail.json()["classification_preflight"]["render"]
    assert render_gate["status"] == "ready"
    assert render_gate["ready"] is True
    assert render_gate["blocking"] == []
    assert render_gate["source_group_authority"]["active"] is True
    assert render_gate["source_group_authority"]["status"] == "ready"
    assert render_gate["source_group_authority"]["original_status"] == "blocked"
    assert render_gate["blocking_summary"]["render_pairs"] == 0
    assert render_gate["blocking_summary"]["primary_render_pairs"] == 3


def test_case_source_group_reuses_primary_render_metadata_for_bound_oblique_reselection(
    client, seed_case, tmp_path
):
    from backend import db, render_queue, source_images

    before_dir = tmp_path / "customer-a" / "术前"
    after_dir = tmp_path / "customer-a" / "术后"
    before_files = ["bad-before-45.jpg", "good-before-45.jpg"]
    after_files = ["after-bad-45.jpg", "after-good-45.jpg"]
    _touch_case_files(str(before_dir), before_files)
    _touch_case_files(str(after_dir), after_files)
    before_case = seed_case(abs_path=str(before_dir), customer_raw="客户A")
    after_case = seed_case(abs_path=str(after_dir), customer_raw="客户A")
    after_bad_render = render_queue._safe_link_name(after_case, str(after_dir), "after-bad-45.jpg")
    after_good_render = render_queue._safe_link_name(after_case, str(after_dir), "after-good-45.jpg")
    skill_metadata = [
        {
            "filename": "bad-before-45.jpg",
            "relative_path": "bad-before-45.jpg",
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
            "relative_path": "good-before-45.jpg",
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
            (
                json.dumps(
                    {
                        "image_files": before_files,
                        source_images.SOURCE_BINDINGS_META_KEY: {"case_ids": [after_case]},
                    },
                    ensure_ascii=False,
                ),
                json.dumps(skill_metadata, ensure_ascii=False),
                before_case,
            ),
        )
        conn.execute(
            "UPDATE cases SET meta_json = ?, skill_image_metadata_json = NULL WHERE id = ?",
            (json.dumps({"image_files": after_files}, ensure_ascii=False), after_case),
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

    resp = client.get(f"/api/cases/{before_case}/source-group")
    assert resp.status_code == 200, resp.text
    oblique = next(slot for slot in resp.json()["preflight"]["slots"] if slot["view"] == "oblique")
    assert oblique["selected_before"]["filename"] == "good-before-45.jpg"
    assert oblique["selected_after"]["filename"] == "after-good-45.jpg"
    assert oblique["selected_after"]["selection_metadata_source"] == "primary_render_history"
    assert oblique["pair_quality"]["metrics"]["pose_delta"]["weighted"] < 3


def test_case_source_group_slot_lock_forces_selected_pair_and_audits(client, seed_case, tmp_path):
    from backend import db

    case_dir = tmp_path / "case-source-lock"
    filenames = [
        "front-before-a.jpg",
        "front-after-a.jpg",
        "front-before-z.jpg",
        "front-after-z.jpg",
    ]
    _touch_case_files(str(case_dir), filenames)
    case_id = seed_case(abs_path=str(case_dir))
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": filenames}, ensure_ascii=False), case_id),
        )

    baseline = client.get(f"/api/cases/{case_id}/source-group")
    assert baseline.status_code == 200, baseline.text
    front_slot = next(slot for slot in baseline.json()["preflight"]["slots"] if slot["view"] == "front")
    assert front_slot["selected_before"]["filename"] == "front-before-a.jpg"
    assert front_slot["selected_after"]["filename"] == "front-after-a.jpg"

    locked = client.post(
        f"/api/cases/{case_id}/source-group/slot-locks",
        json={
            "view": "front",
            "before": {"case_id": case_id, "filename": "front-before-z.jpg"},
            "after": {"case_id": case_id, "filename": "front-after-z.jpg"},
            "reviewer": "test-lock",
            "reason": "人工复核后锁定姿态更接近的正面配对",
        },
    )
    assert locked.status_code == 200, locked.text
    body = locked.json()
    locked_front = next(slot for slot in body["preflight"]["slots"] if slot["view"] == "front")
    assert locked_front["selected_before"]["filename"] == "front-before-z.jpg"
    assert locked_front["selected_after"]["filename"] == "front-after-z.jpg"
    assert locked_front["selection_lock"]["locked"] is True
    assert locked_front["pair_quality"]["metrics"]["source_group_lock"]["reviewer"] == "test-lock"
    assert body["audit"]["source_group_selection"]["locked_slots"]["front"]["reason"] == "人工复核后锁定姿态更接近的正面配对"


def test_case_source_group_accept_warning_scopes_to_job_selected_pair(client, seed_case, tmp_path):
    from backend import db

    case_dir = tmp_path / "case-source-acceptance"
    filenames = ["side-before.jpg", "side-after.jpg"]
    _touch_case_files(str(case_dir), filenames)
    case_id = seed_case(abs_path=str(case_dir))
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": filenames}, ensure_ascii=False), case_id),
        )

    manifest_path = tmp_path / "manifest.final.json"
    manifest_path.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "selected_slots": {
                            "side": {
                                "before": {
                                    "name": "side-before.jpg",
                                    "relative_path": "side-before.jpg",
                                    "group_relative_path": "case114-术前-side-before.jpg",
                                },
                                "after": {
                                    "name": "side-after.jpg",
                                    "relative_path": "side-after.jpg",
                                    "group_relative_path": "case116-术后-side-after.jpg",
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
    with db.connect() as conn:
        row = conn.execute(
            """
            INSERT INTO render_jobs(
              case_id, brand, template, status, enqueued_at, started_at, finished_at,
              output_path, manifest_path, semantic_judge, meta_json
            )
            VALUES (?, 'fumei', 'tri-compare', 'done_with_issues', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP, ?, ?, 'off', '{}')
            RETURNING id
            """,
            (case_id, str(tmp_path / "final-board.jpg"), str(manifest_path)),
        ).fetchone()
        job_id = int(row["id"])

    accepted = client.post(
        f"/api/cases/{case_id}/source-group/accepted-warnings",
        json={
            "slot": "side",
            "code": "direction_mismatch",
            "job_id": job_id,
            "message_contains": "方向不一致",
            "reviewer": "test-review",
            "note": "同一入选 pair 复核通过",
        },
    )
    assert accepted.status_code == 200, accepted.text
    audit = accepted.json()["audit"]["source_group_selection"]["accepted_warnings"][0]
    assert audit["job_id"] == job_id
    assert "case114-术前-side-before.jpg" in audit["selected_files"]
    assert "case116-术后-side-after.jpg" in audit["selected_files"]


def test_case_source_group_phase_only_override_preserves_filename_view(client, seed_case, tmp_path):
    from backend import db, render_queue

    case_dir = tmp_path / "customer-a" / "术前"
    case_dir.mkdir(parents=True)
    filename = "IMG_2634.JPG"
    (case_dir / filename).write_bytes(b"real before oblique image")
    case_id = seed_case(abs_path=str(case_dir), customer_raw="客户A")
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": [filename]}, ensure_ascii=False), case_id),
        )

    batch = client.post(
        "/api/image-workbench/batch",
        json={
            "items": [{"case_id": case_id, "filename": filename}],
            "manual_phase": "before",
            "reviewer": "test",
        },
    )
    assert batch.status_code == 200, batch.text

    group = client.get(f"/api/cases/{case_id}/source-group")
    assert group.status_code == 200, group.text
    image = group.json()["sources"][0]["images"][0]
    assert image["phase"] == "before"
    assert image["phase_source"] == "manual"
    assert image["view"] == "oblique"
    assert image["view_source"] == "filename"
    oblique_slot = next(slot for slot in group.json()["preflight"]["slots"] if slot["view"] == "oblique")
    assert oblique_slot["before_count"] == 1

    render_preflight = render_queue._classification_blocking_preflight(
        case_meta_json=json.dumps({"image_files": [filename]}, ensure_ascii=False),
        skill_image_metadata_json="[]",
        image_files=["case114-术前-IMG_2634.JPG"],
        manual_overrides={},
        semantic_judge="auto",
    )
    assert render_preflight is None


def test_case_source_group_prioritizes_reviewed_candidate(client, seed_case, tmp_path):
    from backend import db

    case_dir = tmp_path / "quality-priority"
    case_dir.mkdir(parents=True)
    filenames = ["术前-正面-A.jpg", "术前-正面-B.jpg", "术后-正面-A.jpg"]
    for filename in filenames:
        (case_dir / filename).write_bytes(b"real image bytes")
    case_id = seed_case(abs_path=str(case_dir), customer_raw="客户A")
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": filenames}, ensure_ascii=False), case_id),
        )

    review = client.post(
        f"/api/cases/{case_id}/image-review/%E6%9C%AF%E5%89%8D-%E6%AD%A3%E9%9D%A2-B.jpg",
        json={"verdict": "usable", "reviewer": "test", "note": "优先候选"},
    )
    assert review.status_code == 200, review.text

    group = client.get(f"/api/cases/{case_id}/source-group")
    assert group.status_code == 200, group.text
    front_slot = next(slot for slot in group.json()["preflight"]["slots"] if slot["view"] == "front")
    assert front_slot["selected_before"]["filename"] == "术前-正面-B.jpg"
    assert front_slot["selected_before"]["review_verdict"] == "usable"
    assert "人工复核可用" in front_slot["selected_before"]["selection_reasons"]
    assert front_slot["selected_before"]["selection_score"] > front_slot["before_candidates"][1]["selection_score"]
    assert front_slot["pair_quality"]["metrics"]["same_source_case"] is True
