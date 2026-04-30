"""Tests for upgrade queue HTTP endpoints (batch enqueue/get/cancel/retry/undo).

Same `no_job_pool` shielding as test_render_endpoints.py — jobs stay queued
without invoking the real upgrade executor.
"""
from __future__ import annotations


def test_batch_enqueue_400_empty(client, no_job_pool):
    resp = client.post("/api/cases/upgrade/batch", json={"case_ids": []})
    assert resp.status_code == 400
    assert "case_ids cannot be empty" in resp.json()["detail"]


def test_batch_enqueue_400_oversize(client, no_job_pool):
    resp = client.post(
        "/api/cases/upgrade/batch",
        json={"case_ids": list(range(1, 100))},
    )
    assert resp.status_code == 400
    assert "exceeds maximum" in resp.json()["detail"]


def test_batch_enqueue_400_bad_brand(client, seed_case, no_job_pool):
    case_id = seed_case()
    resp = client.post(
        "/api/cases/upgrade/batch",
        json={"case_ids": [case_id], "brand": "wrong"},
    )
    assert resp.status_code == 400
    assert "unsupported brand" in resp.json()["detail"]


def test_batch_enqueue_404_when_all_cases_missing(client, no_job_pool):
    resp = client.post(
        "/api/cases/upgrade/batch",
        json={"case_ids": [9990, 9991]},
    )
    assert resp.status_code == 404
    assert "no valid case ids" in resp.json()["detail"]


def test_batch_enqueue_partial_returns_skipped_count(
    client, seed_case, no_job_pool
):
    a = seed_case(abs_path="/tmp/upgrade-a")
    b = seed_case(abs_path="/tmp/upgrade-b")
    resp = client.post(
        "/api/cases/upgrade/batch",
        json={"case_ids": [a, b, 9999]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["batch_id"].startswith("upgrade-")
    assert len(body["job_ids"]) == 2
    assert body["skipped_count"] == 1


def test_get_batch_summary(client, seed_case, no_job_pool):
    a = seed_case(abs_path="/tmp/upgrade-summary-a")
    b = seed_case(abs_path="/tmp/upgrade-summary-b")
    body = client.post(
        "/api/cases/upgrade/batch", json={"case_ids": [a, b]}
    ).json()
    summary = client.get(
        f"/api/jobs/upgrade/batches/{body['batch_id']}"
    ).json()
    assert summary["batch_id"] == body["batch_id"]
    assert summary["total"] == 2
    assert summary["counts"] == {"queued": 2}
    assert sorted(j["case_id"] for j in summary["jobs"]) == sorted([a, b])


def test_get_batch_summary_404(client, no_job_pool):
    resp = client.get("/api/jobs/upgrade/batches/upgrade-nope")
    assert resp.status_code == 404
    assert "batch not found" in resp.json()["detail"]


def test_get_job_404(client, no_job_pool):
    resp = client.get("/api/jobs/upgrade/9999")
    assert resp.status_code == 404
    assert "job not found" in resp.json()["detail"]


def test_get_job_returns_row(client, seed_case, no_job_pool):
    case_id = seed_case()
    job_id = client.post(
        "/api/cases/upgrade/batch", json={"case_ids": [case_id]}
    ).json()["job_ids"][0]
    detail = client.get(f"/api/jobs/upgrade/{job_id}").json()
    assert detail["id"] == job_id
    assert detail["case_id"] == case_id
    assert detail["status"] == "queued"
    assert detail["brand"] == "fumei"


def test_cancel_queued_job_then_409_on_repeat(client, seed_case, no_job_pool):
    case_id = seed_case()
    job_id = client.post(
        "/api/cases/upgrade/batch", json={"case_ids": [case_id]}
    ).json()["job_ids"][0]

    first = client.post(f"/api/jobs/upgrade/{job_id}/cancel")
    assert first.status_code == 200
    assert first.json() == {"cancelled": True, "job_id": job_id}

    again = client.post(f"/api/jobs/upgrade/{job_id}/cancel")
    assert again.status_code == 409


def test_cancel_unknown_job_409(client, no_job_pool):
    resp = client.post("/api/jobs/upgrade/9999/cancel")
    assert resp.status_code == 409


def test_retry_creates_new_job_row(client, seed_case, no_job_pool):
    """Retry inserts a new job row sharing the original case + batch — the
    historical row stays untouched.
    """
    case_id = seed_case()
    body = client.post(
        "/api/cases/upgrade/batch", json={"case_ids": [case_id]}
    ).json()
    old_id = body["job_ids"][0]

    resp = client.post(f"/api/jobs/upgrade/{old_id}/retry")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["retried"] is True
    assert payload["old_job_id"] == old_id
    assert payload["new_job_id"] != old_id

    # Old row is unchanged; new row is queued under the same batch.
    summary = client.get(
        f"/api/jobs/upgrade/batches/{body['batch_id']}"
    ).json()
    assert summary["total"] == 2
    ids = sorted(j["id"] for j in summary["jobs"])
    assert ids == sorted([old_id, payload["new_job_id"]])


def test_retry_404_when_old_job_unknown(client, no_job_pool):
    resp = client.post("/api/jobs/upgrade/9999/retry")
    assert resp.status_code == 404
    assert "job not found" in resp.json()["detail"]


def test_undo_batch_no_done_jobs_returns_empty(client, seed_case, no_job_pool):
    """Undoing a batch where no job has reached 'done' yet returns the empty
    shape — the route doesn't 404 just because nothing was undoable.
    """
    case_id = seed_case()
    batch_id = client.post(
        "/api/cases/upgrade/batch", json={"case_ids": [case_id]}
    ).json()["batch_id"]
    body = client.post(
        f"/api/jobs/upgrade/batches/{batch_id}/undo"
    ).json()
    assert body == {
        "undone": [],
        "skipped": [],
        "errors": [],
        "batch_id": batch_id,
    }
