"""Tests for case group diagnosis and safe AI simulation intake."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_rescan_groups_merges_stage_cases(client, seed_case, tmp_path):
    root = tmp_path / "客户A" / "2025.8.1 下颌线"
    before = root / "术前"
    after = root / "术后"
    before.mkdir(parents=True)
    after.mkdir(parents=True)
    (before / "正面.jpg").write_bytes(b"x")
    (after / "正面.jpg").write_bytes(b"x")
    before_id = seed_case(
        abs_path=str(before),
        category="non_labeled",
        template_tier=None,
        customer_raw="客户A",
    )
    after_id = seed_case(
        abs_path=str(after),
        category="non_labeled",
        template_tier=None,
        customer_raw="客户A",
    )
    from backend import db

    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, source_count = 1 WHERE id = ?",
            (json.dumps({"image_files": ["正面.jpg"], "image_count_total": 1}, ensure_ascii=False), before_id),
        )
        conn.execute(
            "UPDATE cases SET meta_json = ?, source_count = 1 WHERE id = ?",
            (json.dumps({"image_files": ["正面.jpg"], "image_count_total": 1}, ensure_ascii=False), after_id),
        )

    resp = client.post("/api/cases/rescan-groups")
    assert resp.status_code == 200, resp.text
    assert resp.json()["group_count"] == 1

    listing = client.get("/api/case-groups").json()
    assert listing["total"] == 1
    group = listing["items"][0]
    assert sorted(group["case_ids"]) == sorted([before_id, after_id])

    detail = client.get(f"/api/case-groups/{group['id']}/diagnosis").json()
    assert len(detail["image_observations"]) == 2
    assert {o["phase"] for o in detail["image_observations"]} == {"before", "after"}
    assert detail["pair_candidates"][0]["status"] == "ok"


def test_simulate_after_requires_focus_targets(client, seed_case):
    case_id = seed_case(abs_path="/tmp/case-sim")
    from backend import db

    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO case_groups
              (group_key, primary_case_id, title, root_path, case_ids_json, status,
               diagnosis_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'auto', '{}', ?, ?)
            """,
            ("/tmp/case-sim", case_id, "case-sim", "/tmp/case-sim", json.dumps([case_id]), _now(), _now()),
        )
        group_id = conn.execute("SELECT id FROM case_groups").fetchone()["id"]

    resp = client.post(
        f"/api/case-groups/{group_id}/simulate-after",
        json={"focus_targets": [], "ai_generation_authorized": True},
    )
    assert resp.status_code == 400
    assert "focus_targets" in resp.json()["detail"]


def test_simulate_after_records_blocked_job_by_default(client, seed_case):
    case_id = seed_case(abs_path="/tmp/case-sim-blocked")
    from backend import db

    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO case_groups
              (group_key, primary_case_id, title, root_path, case_ids_json, status,
               diagnosis_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'auto', '{}', ?, ?)
            """,
            ("/tmp/case-sim-blocked", case_id, "case-sim-blocked", "/tmp/case-sim-blocked", json.dumps([case_id]), _now(), _now()),
        )
        group_id = conn.execute("SELECT id FROM case_groups").fetchone()["id"]

    resp = client.post(
        f"/api/case-groups/{group_id}/simulate-after",
        json={"focus_targets": ["下颌线"], "ai_generation_authorized": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "blocked"
    assert body["focus_targets"] == ["下颌线"]

    with db.connect() as conn:
        sim_count = conn.execute("SELECT COUNT(*) FROM simulation_jobs").fetchone()[0]
        ai_count = conn.execute("SELECT COUNT(*) FROM ai_runs").fetchone()[0]
    assert sim_count == 1
    assert ai_count == 1


def test_image_workbench_queue_and_batch_update_real_images(client, seed_case, tmp_path):
    case_dir = tmp_path / "客户B" / "2026.5.1 下颌线"
    case_dir.mkdir(parents=True)
    (case_dir / "mystery.jpg").write_bytes(b"x")
    case_id = seed_case(
        abs_path=str(case_dir),
        category="standard_face",
        template_tier="tri",
        customer_raw="客户B",
    )
    from backend import db

    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, source_count = 1 WHERE id = ?",
            (json.dumps({"image_files": ["mystery.jpg"], "image_count_total": 1}, ensure_ascii=False), case_id),
        )

    rescan = client.post("/api/cases/rescan-groups")
    assert rescan.status_code == 200, rescan.text

    queue = client.get("/api/image-workbench/queue", params={"status": "review_needed", "limit": 20})
    assert queue.status_code == 200, queue.text
    body = queue.json()
    item = next(x for x in body["items"] if x["case_id"] == case_id)
    assert item["queue_state"] == "needs_manual"
    assert item["needs_manual"] is True
    assert item["preview_url"].endswith("name=mystery.jpg")

    batch = client.post(
        "/api/image-workbench/batch",
        json={
            "items": [{"case_id": case_id, "filename": "mystery.jpg"}],
            "manual_phase": "before",
            "manual_view": "side",
            "body_part": "face",
            "treatment_area": "下颌线",
            "verdict": "usable",
            "reviewer": "tester",
        },
    )
    assert batch.status_code == 200, batch.text
    assert batch.json()["updated"] == 1

    updated = client.get("/api/image-workbench/queue", params={"status": "usable", "case_id": case_id}).json()
    assert updated["total"] == 1
    item = updated["items"][0]
    assert item["phase"] == "before"
    assert item["phase_source"] == "manual"
    assert item["view"] == "side"
    assert item["body_part"] == "face"
    assert item["treatment_area"] == "下颌线"
    assert item["review_state"]["verdict"] == "usable"

    with db.connect() as conn:
        override = conn.execute(
            "SELECT manual_phase, manual_view FROM case_image_overrides WHERE case_id = ? AND filename = ?",
            (case_id, "mystery.jpg"),
        ).fetchone()
        meta = json.loads(conn.execute("SELECT meta_json FROM cases WHERE id = ?", (case_id,)).fetchone()["meta_json"])
    assert dict(override) == {"manual_phase": "before", "manual_view": "side"}
    assert meta["image_review_states"]["mystery.jpg"]["treatment_area"] == "下颌线"


def test_image_workbench_queue_suggests_source_directory_phase_without_guessing_angle(client, seed_case, tmp_path):
    case_dir = tmp_path / "客户B" / "术前"
    case_dir.mkdir(parents=True)
    for filename in ["IMG_2601.JPG", "IMG_2602.JPG"]:
        (case_dir / filename).write_bytes(b"x")
    case_id = seed_case(
        abs_path=str(case_dir),
        category="standard_face",
        template_tier="tri",
        customer_raw="客户B",
    )
    from backend import db

    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, source_count = 2 WHERE id = ?",
            (
                json.dumps(
                    {"image_files": ["IMG_2601.JPG", "IMG_2602.JPG"], "image_count_total": 2},
                    ensure_ascii=False,
                ),
                case_id,
            ),
        )

    queue = client.get(
        "/api/image-workbench/queue",
        params={"status": "review_needed", "case_id": case_id, "limit": 20},
    )
    assert queue.status_code == 200, queue.text
    body = queue.json()
    assert body["total"] == 2
    item = body["items"][0]
    assert item["phase"] == "unknown"
    assert item["view"] == "unknown"
    assert item["case_preflight"]["reason"] == "missing_before_after_pair"
    assert item["case_preflight"]["source_phase_hint"] == "before"
    assert item["classification_suggestion"]["suggested_labels"]["phase"] == "before"
    assert item["classification_suggestion"]["suggested_labels"]["view"] is None
    assert item["classification_suggestion"]["classification_layers"]["deterministic"]["source_phase_hint"] == "before"
    assert item["recommended_actions"][0]["code"] == "confirm_source_phase"

    group = next(group for group in body["batch_groups"] if group["filename_bucket"] == "IMG_#")
    assert group["source_phase_hint"] == "before"
    assert group["recommended_patch"] == {"manual_phase": "before"}
    assert group["can_bulk_apply_suggestion"] is False


def test_image_workbench_queue_exposes_cross_case_task_lanes_and_local_visual_layers(client, seed_case, tmp_path):
    case_dir = tmp_path / "客户B" / "2026.5.2 法令纹"
    case_dir.mkdir(parents=True)
    (case_dir / "mystery.jpg").write_bytes(b"real image bytes")
    case_id = seed_case(
        abs_path=str(case_dir),
        category="standard_face",
        template_tier="tri",
        customer_raw="客户B",
    )
    from backend import db

    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, source_count = 1 WHERE id = ?",
            (json.dumps({"image_files": ["mystery.jpg"], "image_count_total": 1}, ensure_ascii=False), case_id),
        )

    queue = client.get("/api/image-workbench/queue", params={"status": "review_needed", "limit": 20})
    assert queue.status_code == 200, queue.text
    body = queue.json()
    item = next(row for row in body["items"] if row["case_id"] == case_id)

    lanes = body["task_queues"]
    assert lanes["missing_phase"]["count"] >= 1
    assert lanes["missing_phase"]["blocks_render"] is True
    assert lanes["missing_view"]["count"] >= 1
    assert lanes["blocked_case"]["count"] >= 1
    assert lanes["blocked_case"]["recommended_action"]
    assert body["production_summary"]["review_needed_total"] == body["total"]
    assert body["production_summary"]["blocking_image_count"] >= 1
    assert body["production_summary"]["bulk_group_count"] >= 1

    suggestion = item["classification_suggestion"]
    assert suggestion["confidence_band"] == "low"
    assert suggestion["render_gate"]["blocks_render"] is True
    assert suggestion["classification_layers"]["deterministic"]["source_phase_hint"] is None
    assert suggestion["classification_layers"]["local_visual"]["decision"] == "needs_review"
    assert suggestion["classification_layers"]["local_visual"]["signals"]["source"] in {"case_meta", "auto", None}
    assert item["recommended_actions"][0]["code"] == "set_phase"


def test_image_workbench_queue_groups_missing_view_images_by_real_thumbnail_similarity(client, seed_case, tmp_path):
    from PIL import Image, ImageDraw

    case_dir = tmp_path / "客户B" / "2026.5.3" / "术前"
    case_dir.mkdir(parents=True)
    filenames = ["IMG_1001.JPG", "IMG_1002.JPG", "IMG_2001.JPG", "IMG_2002.JPG"]
    specs = [
        ("IMG_1001.JPG", (230, 210, 190), "oval"),
        ("IMG_1002.JPG", (228, 212, 192), "oval"),
        ("IMG_2001.JPG", (70, 105, 150), "stripe"),
        ("IMG_2002.JPG", (72, 108, 152), "stripe"),
    ]
    for filename, background, shape in specs:
        image = Image.new("RGB", (120, 180), background)
        draw = ImageDraw.Draw(image)
        if shape == "oval":
            draw.ellipse((35, 28, 85, 78), fill=(120, 90, 72))
            draw.rectangle((28, 82, 92, 170), fill=(245, 244, 240))
        else:
            draw.rectangle((0, 0, 120, 180), outline=(25, 40, 65), width=8)
            draw.line((15, 0, 100, 180), fill=(230, 240, 250), width=12)
        image.save(case_dir / filename, format="JPEG", quality=92)

    case_id = seed_case(
        abs_path=str(case_dir),
        category="standard_face",
        template_tier="tri",
        customer_raw="客户B",
    )
    from backend import db

    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, source_count = 4 WHERE id = ?",
            (
                json.dumps({"image_files": filenames, "image_count_total": 4}, ensure_ascii=False),
                case_id,
            ),
        )

    batch = client.post(
        "/api/image-workbench/batch",
        json={
            "items": [{"case_id": case_id, "filename": filename} for filename in filenames],
            "manual_phase": "before",
            "reviewer": "tester",
        },
    )
    assert batch.status_code == 200, batch.text

    queue = client.get(
        "/api/image-workbench/queue",
        params={"status": "review_needed", "case_id": case_id, "limit": 20},
    )
    assert queue.status_code == 200, queue.text
    body = queue.json()
    assert body["summary"]["missing_view"] == 4
    groups = body["angle_sort_groups"]
    assert len(groups) >= 2
    assert sum(len(group["filenames"]) for group in groups) == 4
    first_group = groups[0]
    assert first_group["case_id"] == case_id
    assert first_group["orientation_label"] == "竖图"
    assert first_group["sample_images"][0]["preview_url"].startswith(f"/api/cases/{case_id}/files?")
    assert len(first_group["images"]) == len(first_group["filenames"])
    assert first_group["recommended_action"] in {
        "人工查看样张后批量设为正面、45°或侧面",
        f"本地建议判为{first_group['suggested_view_label']}，进入大图复核后可批量确认",
    }


def test_image_workbench_queue_adds_local_multi_signal_angle_suggestions(client, seed_case, tmp_path):
    from PIL import Image, ImageDraw

    case_dir = tmp_path / "客户B" / "2026.5.4" / "术前"
    case_dir.mkdir(parents=True)

    front = Image.new("RGB", (180, 240), (238, 238, 232))
    draw = ImageDraw.Draw(front)
    draw.ellipse((55, 28, 125, 112), fill=(192, 142, 108))
    draw.ellipse((72, 62, 80, 70), fill=(35, 35, 35))
    draw.ellipse((100, 62, 108, 70), fill=(35, 35, 35))
    draw.rectangle((48, 122, 132, 228), fill=(245, 244, 240))
    front.save(case_dir / "IMG_3101.JPG", format="JPEG", quality=92)

    side = Image.new("RGB", (180, 240), (238, 238, 232))
    draw = ImageDraw.Draw(side)
    draw.ellipse((30, 34, 108, 124), fill=(194, 142, 108))
    draw.polygon([(98, 64), (142, 78), (98, 92)], fill=(194, 142, 108))
    draw.rectangle((34, 132, 92, 228), fill=(245, 244, 240))
    side.save(case_dir / "IMG_3102.JPG", format="JPEG", quality=92)

    case_id = seed_case(
        abs_path=str(case_dir),
        category="standard_face",
        template_tier="tri",
        customer_raw="客户B",
    )
    from backend import db

    filenames = ["IMG_3101.JPG", "IMG_3102.JPG"]
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, source_count = 2 WHERE id = ?",
            (
                json.dumps({"image_files": filenames, "image_count_total": 2}, ensure_ascii=False),
                case_id,
            ),
        )

    batch = client.post(
        "/api/image-workbench/batch",
        json={
            "items": [{"case_id": case_id, "filename": filename} for filename in filenames],
            "manual_phase": "before",
            "reviewer": "tester",
        },
    )
    assert batch.status_code == 200, batch.text

    queue = client.get(
        "/api/image-workbench/queue",
        params={"status": "review_needed", "case_id": case_id, "limit": 20},
    )
    assert queue.status_code == 200, queue.text
    items = {item["filename"]: item for item in queue.json()["items"]}

    front_local = items["IMG_3101.JPG"]["classification_suggestion"]["classification_layers"]["local_visual"]
    side_local = items["IMG_3102.JPG"]["classification_suggestion"]["classification_layers"]["local_visual"]

    assert front_local["view_suggestion"]["suggested_view"] == "front"
    assert side_local["view_suggestion"]["suggested_view"] == "side"
    assert front_local["view_suggestion"]["confidence"] >= 0.55
    assert side_local["view_suggestion"]["confidence"] >= 0.55
    assert "subject_bbox" in front_local["view_suggestion"]["signals"]
    assert "symmetry_delta" in side_local["view_suggestion"]["signals"]
    assert items["IMG_3101.JPG"]["classification_suggestion"]["render_gate"]["blocks_render"] is True


def test_angle_sort_groups_vote_local_angle_suggestions_for_bulk_review(client, seed_case, tmp_path):
    from PIL import Image, ImageDraw

    case_dir = tmp_path / "客户B" / "2026.5.5" / "术前"
    case_dir.mkdir(parents=True)
    specs = [
        ("IMG_4101.JPG", "front", (238, 236, 230)),
        ("IMG_4102.JPG", "front", (240, 237, 232)),
        ("IMG_5101.JPG", "side", (216, 226, 236)),
        ("IMG_5102.JPG", "side", (218, 228, 238)),
    ]
    for filename, view, background in specs:
        image = Image.new("RGB", (180, 240), background)
        draw = ImageDraw.Draw(image)
        if view == "front":
            draw.ellipse((55, 28, 125, 112), fill=(192, 142, 108))
            draw.ellipse((72, 62, 80, 70), fill=(35, 35, 35))
            draw.ellipse((100, 62, 108, 70), fill=(35, 35, 35))
            draw.rectangle((48, 122, 132, 228), fill=(245, 244, 240))
        else:
            draw.ellipse((30, 34, 108, 124), fill=(194, 142, 108))
            draw.polygon([(98, 64), (142, 78), (98, 92)], fill=(194, 142, 108))
            draw.rectangle((34, 132, 92, 228), fill=(245, 244, 240))
        image.save(case_dir / filename, format="JPEG", quality=92)

    case_id = seed_case(
        abs_path=str(case_dir),
        category="standard_face",
        template_tier="tri",
        customer_raw="客户B",
    )
    from backend import db

    filenames = [filename for filename, _view, _background in specs]
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, source_count = 4 WHERE id = ?",
            (
                json.dumps({"image_files": filenames, "image_count_total": 4}, ensure_ascii=False),
                case_id,
            ),
        )

    batch = client.post(
        "/api/image-workbench/batch",
        json={
            "items": [{"case_id": case_id, "filename": filename} for filename in filenames],
            "manual_phase": "before",
            "reviewer": "tester",
        },
    )
    assert batch.status_code == 200, batch.text

    queue = client.get(
        "/api/image-workbench/queue",
        params={"status": "review_needed", "case_id": case_id, "limit": 20},
    )
    assert queue.status_code == 200, queue.text
    groups = queue.json()["angle_sort_groups"]
    assert len(groups) >= 2

    by_suggestion = {group["suggested_view"]: group for group in groups if group.get("suggested_view")}
    assert {"front", "side"}.issubset(set(by_suggestion))
    assert by_suggestion["front"]["suggested_view_confidence"] >= 0.55
    assert by_suggestion["side"]["suggested_view_confidence"] >= 0.55
    assert by_suggestion["front"]["local_angle_votes"]["front"] == by_suggestion["front"]["item_count"]
    assert by_suggestion["side"]["local_angle_votes"]["side"] == by_suggestion["side"]["item_count"]
    assert by_suggestion["front"]["recommended_patch"] == {"manual_view": "front"}
    assert by_suggestion["side"]["can_quick_confirm_angle"] is True

    global_queue = client.get("/api/image-workbench/queue", params={"status": "review_needed", "limit": 20})
    assert global_queue.status_code == 200, global_queue.text
    global_groups = global_queue.json()["angle_sort_groups"]
    assert any(group["case_id"] == case_id and group.get("suggested_view") == "front" for group in global_groups)


def test_render_queue_blocks_unresolved_uncertain_images(seed_case, tmp_path, monkeypatch):
    case_dir = tmp_path / "客户C" / "2026.5.2"
    case_dir.mkdir(parents=True)
    (case_dir / "术前正面.jpg").write_bytes(b"x")
    (case_dir / "术后正面.jpg").write_bytes(b"x")
    (case_dir / "mystery.jpg").write_bytes(b"x")
    case_id = seed_case(abs_path=str(case_dir), category="standard_face", template_tier="tri")
    from backend import db, render_queue

    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ?, source_count = 3 WHERE id = ?",
            (
                json.dumps(
                    {"image_files": ["术前正面.jpg", "术后正面.jpg", "mystery.jpg"], "image_count_total": 3},
                    ensure_ascii=False,
                ),
                case_id,
            ),
        )

    def fail_run_render(*args, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("unresolved classification should block before renderer")

    monkeypatch.setattr(render_queue.render_executor, "run_render", fail_run_render)
    with db.connect() as conn:
        job_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, semantic_judge)
            VALUES (?, 'fumei', 'tri-compare', 'queued', ?, 'auto')
            """,
            (case_id, _now()),
        ).lastrowid

    render_queue.RENDER_QUEUE._execute_render(job_id)

    with db.connect() as conn:
        job = conn.execute("SELECT status, error_message, meta_json FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
        quality = conn.execute("SELECT quality_status, blocking_count FROM render_quality WHERE render_job_id = ?", (job_id,)).fetchone()
    assert job["status"] == "blocked"
    assert "正式出图已阻断" in job["error_message"]
    assert json.loads(job["meta_json"])["ai_usage"]["semantic_judge_effective"] == "blocked-classification-preflight"
    assert quality["quality_status"] == "blocked"
    assert quality["blocking_count"] >= 2
