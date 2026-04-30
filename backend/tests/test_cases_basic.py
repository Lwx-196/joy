"""Smoke tests for `GET /api/cases` and `GET /api/cases/{id}`."""
from __future__ import annotations


def test_list_cases_empty(client):
    resp = client.get("/api/cases")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["page"] == 1
    assert body["page_size"] == 50


def test_list_cases_returns_seeded_rows(client, seed_case):
    seed_case(abs_path="/tmp/case-a", category="A")
    seed_case(abs_path="/tmp/case-b", category="B")
    resp = client.get("/api/cases")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    paths = {row["abs_path"] for row in body["items"]}
    assert paths == {"/tmp/case-a", "/tmp/case-b"}


def test_list_cases_filters_by_category(client, seed_case):
    seed_case(abs_path="/tmp/case-a", category="A")
    seed_case(abs_path="/tmp/case-b", category="B")
    resp = client.get("/api/cases", params={"category": "A"})
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["abs_path"] == "/tmp/case-a"


def test_list_cases_page_size_capped_at_2000(client):
    """`page_size` Query has le=2000 — values above must 422."""
    resp = client.get("/api/cases", params={"page_size": 5000})
    assert resp.status_code == 422


def test_case_detail_returns_seeded_case(client, seed_case):
    case_id = seed_case(abs_path="/tmp/case-x", category="A", customer_raw="Bob")
    resp = client.get(f"/api/cases/{case_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == case_id
    assert body["abs_path"] == "/tmp/case-x"
    assert body["customer_raw"] == "Bob"


def test_case_detail_404_when_missing(client):
    resp = client.get("/api/cases/9999")
    assert resp.status_code == 404
    assert "case not found" in resp.json()["detail"]
