"""Tests for render queue HTTP endpoints (enqueue/list/get/cancel).

These tests exercise the route layer + DB-row insertion behaviour. The
shared `no_job_pool` fixture replaces `_job_pool.submit` with a no-op so
enqueued jobs stay in 'queued' status — we never actually run mediapipe.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from backend import db


def test_enqueue_single_404_for_missing_case(client, no_job_pool):
    resp = client.post("/api/cases/9999/render", json={"brand": "fumei"})
    assert resp.status_code == 404
    assert "case 9999 not found" in resp.json()["detail"]


def test_enqueue_single_400_for_bad_brand(client, seed_case, no_job_pool):
    case_id = seed_case()
    resp = client.post(f"/api/cases/{case_id}/render", json={"brand": "nope"})
    assert resp.status_code == 400
    assert "unsupported brand" in resp.json()["detail"]


def test_enqueue_single_400_for_bad_semantic_judge(client, seed_case, no_job_pool):
    case_id = seed_case()
    resp = client.post(
        f"/api/cases/{case_id}/render",
        json={"brand": "fumei", "semantic_judge": "wrong"},
    )
    assert resp.status_code == 400
    assert "semantic_judge must be one of" in resp.json()["detail"]


def test_enqueue_single_inserts_queued_row(client, seed_case, no_job_pool):
    case_id = seed_case()
    resp = client.post(f"/api/cases/{case_id}/render", json={"brand": "fumei"})
    assert resp.status_code == 200
    body = resp.json()
    job_id = body["job_id"]
    assert isinstance(job_id, int) and job_id > 0
    assert body["batch_id"] is None

    # Verify via GET /api/render/jobs/{id}
    detail = client.get(f"/api/render/jobs/{job_id}").json()
    assert detail["case_id"] == case_id
    assert detail["status"] == "queued"
    assert detail["brand"] == "fumei"
    assert detail["template"] == "tri-compare"
    assert detail["semantic_judge"] == "auto"


def test_batch_enqueue_400_empty(client, no_job_pool):
    resp = client.post("/api/cases/render/batch", json={"case_ids": []})
    assert resp.status_code == 400
    assert "case_ids cannot be empty" in resp.json()["detail"]


def test_batch_enqueue_400_oversize(client, no_job_pool):
    resp = client.post(
        "/api/cases/render/batch",
        json={"case_ids": list(range(1, 100))},
    )
    assert resp.status_code == 400
    assert "exceeds maximum" in resp.json()["detail"]


def test_batch_enqueue_404_when_all_cases_missing(client, no_job_pool):
    resp = client.post(
        "/api/cases/render/batch",
        json={"case_ids": [9990, 9991, 9992]},
    )
    assert resp.status_code == 404
    assert "no valid case ids" in resp.json()["detail"]


def test_batch_enqueue_partial_success_returns_skipped_count(
    client, seed_case, no_job_pool
):
    a = seed_case(abs_path="/tmp/case-a")
    b = seed_case(abs_path="/tmp/case-b")
    resp = client.post(
        "/api/cases/render/batch",
        json={"case_ids": [a, b, 9999]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["batch_id"], str) and body["batch_id"].startswith("batch-")
    assert len(body["job_ids"]) == 2
    assert body["skipped_count"] == 1

    # Both jobs visible in batch detail
    summary = client.get(f"/api/render/batches/{body['batch_id']}").json()
    assert summary["total"] == 2
    assert summary["counts"] == {"queued": 2}


def test_list_case_jobs_empty_then_populated(client, seed_case, no_job_pool):
    case_id = seed_case()
    empty = client.get(f"/api/cases/{case_id}/render/jobs").json()
    assert empty == []

    job_id = client.post(
        f"/api/cases/{case_id}/render", json={"brand": "fumei"}
    ).json()["job_id"]
    listing = client.get(f"/api/cases/{case_id}/render/jobs").json()
    assert len(listing) == 1
    assert listing[0]["id"] == job_id
    assert listing[0]["status"] == "queued"


def test_latest_job_null_when_no_history(client, seed_case, no_job_pool):
    case_id = seed_case()
    body = client.get(f"/api/cases/{case_id}/render/latest").json()
    assert body == {"job": None}


def test_latest_job_returns_most_recent(client, seed_case, no_job_pool):
    case_id = seed_case()
    j1 = client.post(f"/api/cases/{case_id}/render", json={"brand": "fumei"}).json()[
        "job_id"
    ]
    j2 = client.post(f"/api/cases/{case_id}/render", json={"brand": "fumei"}).json()[
        "job_id"
    ]
    assert j2 > j1
    body = client.get(f"/api/cases/{case_id}/render/latest").json()
    assert body["job"]["id"] == j2


def test_render_queue_passes_review_exclusions_to_runner(seed_case, tmp_path, monkeypatch):
    from backend import db, render_queue

    case_dir = tmp_path / "case-render-exclusion"
    case_dir.mkdir()
    filenames = [
        "术前正面.jpg",
        "术后正面.jpg",
        "术前45.jpg",
        "术后45.jpg",
        "术前侧面.jpg",
        "术后侧面.jpg",
        "术后正面-废片.jpg",
    ]
    for filename in filenames:
        (case_dir / filename).write_bytes(b"fake")
    case_id = seed_case(abs_path=str(case_dir))
    meta = {
        "image_files": filenames,
        "image_review_states": {
            "术后正面-废片.jpg": {
                "verdict": "excluded",
                "render_excluded": True,
                "reviewer": "tester",
            }
        },
    }
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), case_id),
        )
        job_id = conn.execute(
            """
            INSERT INTO render_jobs
                (case_id, brand, template, status, enqueued_at, semantic_judge)
            VALUES (?, 'fumei', 'tri-compare', 'queued', ?, 'off')
            """,
            (case_id, now),
        ).lastrowid

    captured: dict[str, dict] = {}

    def fake_run_render(case_dir_arg, **kwargs):
        captured.update(kwargs.get("manual_overrides") or {})
        out = tmp_path / "final-board.jpg"
        manifest = tmp_path / "manifest.final.json"
        out.write_bytes(b"fake")
        manifest.write_text(json.dumps({"groups": []}), encoding="utf-8")
        return {
            "output_path": str(out),
            "manifest_path": str(manifest),
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "case_mode": "face",
            "effective_templates": ["tri-compare"],
            "ai_usage": {},
            "blocking_issues": [],
            "warnings": [],
            "composition_alerts": [],
        }

    monkeypatch.setattr(render_queue.render_executor, "run_render", fake_run_render)
    worker = render_queue.RenderQueue()
    worker._execute_render(job_id)

    assert captured["术后正面-废片.jpg"]["render_excluded"] is True
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "done"


def test_latest_job_prefers_visible_output_over_newer_failed_job(client, seed_case, tmp_path, no_job_pool):
    case_dir = tmp_path / "case-with-output"
    case_dir.mkdir()
    out_dir = case_dir / ".case-layout-output" / "fumei" / "tri-compare" / "render"
    out_dir.mkdir(parents=True)
    output_path = out_dir / "final-board.jpg"
    output_path.write_bytes(b"fake-jpg")
    now = datetime.now(timezone.utc).isoformat()
    case_id = seed_case(abs_path=str(case_dir))
    with db.connect() as conn:
        done_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, batch_id, enqueued_at, started_at, finished_at,
               output_path, manifest_path, error_message, semantic_judge, meta_json)
            VALUES (?, 'fumei', 'tri-compare', 'done_with_issues', NULL, ?, ?, ?,
                    ?, NULL, NULL, 'auto', ?)
            """,
            (case_id, now, now, now, str(output_path), json.dumps({"status": "ok"})),
        ).lastrowid
        failed_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, batch_id, enqueued_at, started_at, finished_at,
               output_path, manifest_path, error_message, semantic_judge, meta_json)
            VALUES (?, 'fumei', 'tri-compare', 'failed', NULL, datetime(?, '+1 second'), ?, datetime(?, '+1 second'),
                    NULL, NULL, 'render failed', 'auto', '{}')
            """,
            (case_id, now, now, now),
        ).lastrowid
    assert failed_id > done_id

    body = client.get(f"/api/cases/{case_id}/render/latest").json()
    assert body["job"]["id"] == done_id
    assert body["job"]["status"] == "done_with_issues"
    assert body["job"]["output_path"] == str(output_path)
    assert body["job"]["output_mtime"] is not None


def test_get_job_404(client, no_job_pool):
    resp = client.get("/api/render/jobs/9999")
    assert resp.status_code == 404
    assert "job not found" in resp.json()["detail"]


def test_get_batch_404(client, no_job_pool):
    resp = client.get("/api/render/batches/batch-doesnotexist")
    assert resp.status_code == 404
    assert "batch not found" in resp.json()["detail"]


def test_cancel_queued_job_then_cancel_again_409(client, seed_case, no_job_pool):
    case_id = seed_case()
    job_id = client.post(
        f"/api/cases/{case_id}/render", json={"brand": "fumei"}
    ).json()["job_id"]

    first = client.post(f"/api/render/jobs/{job_id}/cancel")
    assert first.status_code == 200
    assert first.json() == {"cancelled": True, "job_id": job_id}

    again = client.post(f"/api/render/jobs/{job_id}/cancel")
    assert again.status_code == 409
    assert "not cancellable" in again.json()["detail"]

    detail = client.get(f"/api/render/jobs/{job_id}").json()
    assert detail["status"] == "cancelled"


def test_cancel_unknown_job_returns_409(client, no_job_pool):
    """Route maps `cancel(...) -> False` (job not found OR not queued) to 409.

    The current behaviour treats both as the same error; this test pins it.
    """
    resp = client.post("/api/render/jobs/9999/cancel")
    assert resp.status_code == 409


def test_undo_render_404_when_no_revision_to_undo(client, seed_case, no_job_pool):
    case_id = seed_case()
    resp = client.post(f"/api/cases/{case_id}/render/undo")
    assert resp.status_code == 404
    assert "nothing to undo" in resp.json()["detail"]
