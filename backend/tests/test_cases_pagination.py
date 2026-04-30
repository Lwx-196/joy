"""Server-side pagination tests for GET /api/cases."""
from __future__ import annotations


def test_response_shape_is_paginated(client, seed_case):
    """Response is now {items, total, page, page_size}, not a bare list."""
    seed_case(abs_path="/tmp/p-1")
    seed_case(abs_path="/tmp/p-2")
    resp = client.get("/api/cases")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "total" in body
    assert "page" in body
    assert "page_size" in body
    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert body["page"] == 1
    assert body["page_size"] == 50


def test_pagination_slices_items(client, seed_case):
    for i in range(5):
        seed_case(abs_path=f"/tmp/page-{i}")
    resp = client.get("/api/cases", params={"page": 1, "page_size": 2})
    body = resp.json()
    assert body["total"] == 5
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert len(body["items"]) == 2

    resp = client.get("/api/cases", params={"page": 3, "page_size": 2})
    body = resp.json()
    assert len(body["items"]) == 1  # tail page


def test_oob_page_returns_empty_items(client, seed_case):
    seed_case(abs_path="/tmp/oob-1")
    resp = client.get("/api/cases", params={"page": 99, "page_size": 50})
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 1
    assert body["page"] == 99


def test_page_size_max_2000(client):
    resp = client.get("/api/cases", params={"page_size": 5000})
    assert resp.status_code == 422
