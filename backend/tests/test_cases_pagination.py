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


def test_q_matches_abs_path(client, seed_case):
    seed_case(abs_path="/tmp/alice-2026", customer_raw="bob")
    seed_case(abs_path="/tmp/charlie", customer_raw="dave")
    resp = client.get("/api/cases", params={"q": "alice"})
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["abs_path"] == "/tmp/alice-2026"


def test_q_matches_customer_canonical(client, seed_case):
    """q 跨 abs_path / customer.canonical_name / customer_raw / notes / tags 匹配。"""
    from backend import db
    # seed customer "Alice" then bind a case
    case_id = seed_case(abs_path="/tmp/case-a", customer_raw="raw-name")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO customers (canonical_name, aliases_json, created_at, updated_at) VALUES ('Alice Wonderland', '[]', ?, ?)",
            (now, now),
        )
        cust_id = conn.execute("SELECT id FROM customers WHERE canonical_name='Alice Wonderland'").fetchone()[0]
        conn.execute("UPDATE cases SET customer_id=? WHERE id=?", (cust_id, case_id))
    seed_case(abs_path="/tmp/case-b", customer_raw="other")

    resp = client.get("/api/cases", params={"q": "wonderland"})
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["abs_path"] == "/tmp/case-a"


def test_q_matches_customer_raw_and_notes(client, seed_case):
    seed_case(abs_path="/tmp/x", customer_raw="needle-raw")
    seed_case(abs_path="/tmp/y", notes="something needle in notes")
    seed_case(abs_path="/tmp/z", customer_raw="other")
    resp = client.get("/api/cases", params={"q": "needle"})
    body = resp.json()
    assert body["total"] == 2
    paths = {it["abs_path"] for it in body["items"]}
    assert paths == {"/tmp/x", "/tmp/y"}


def test_q_empty_string_returns_all(client, seed_case):
    seed_case(abs_path="/tmp/a")
    seed_case(abs_path="/tmp/b")
    resp = client.get("/api/cases", params={"q": ""})
    assert resp.json()["total"] == 2
