"""Smoke tests for `POST /api/cases/render/batch/preview`."""
from __future__ import annotations


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
