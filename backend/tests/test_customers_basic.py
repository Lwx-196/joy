"""Smoke tests for `/api/customers` endpoints."""
from __future__ import annotations


def test_list_customers_empty(client):
    resp = client.get("/api/customers")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_then_list_customer(client):
    resp = client.post(
        "/api/customers",
        json={"canonical_name": "稀饭", "aliases": ["xifan"], "notes": "regular"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["canonical_name"] == "稀饭"
    assert body["aliases"] == ["xifan"]
    assert body["case_count"] == 0

    resp2 = client.get("/api/customers")
    assert resp2.status_code == 200
    rows = resp2.json()
    assert len(rows) == 1
    assert rows[0]["canonical_name"] == "稀饭"


def test_create_customer_duplicate_409(client):
    first = client.post("/api/customers", json={"canonical_name": "Alice"})
    assert first.status_code == 200
    dup = client.post("/api/customers", json={"canonical_name": "Alice"})
    assert dup.status_code == 409
    assert "already exists" in dup.json()["detail"]


def test_create_customer_blank_name_400(client):
    resp = client.post("/api/customers", json={"canonical_name": "   "})
    assert resp.status_code == 400
    assert "canonical_name required" in resp.json()["detail"]


def test_customer_detail_404_when_missing(client):
    resp = client.get("/api/customers/9999")
    assert resp.status_code == 404
    assert "customer not found" in resp.json()["detail"]


def test_update_customer_changes_canonical_name(client):
    created = client.post(
        "/api/customers",
        json={"canonical_name": "Bob", "aliases": ["bobby"]},
    ).json()
    cid = created["id"]
    resp = client.patch(f"/api/customers/{cid}", json={"canonical_name": "Robert"})
    assert resp.status_code == 200
    assert resp.json()["canonical_name"] == "Robert"

    detail = client.get(f"/api/customers/{cid}").json()
    assert detail["canonical_name"] == "Robert"
