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


def test_trash_cases_moves_directory_and_hides_from_list(client, seed_case, tmp_path):
    from backend import db

    case_dir = tmp_path / "客户A" / "2026.5.2-不要的案例"
    case_dir.mkdir(parents=True)
    (case_dir / "术前-正面.jpg").write_bytes(b"real-image")
    case_id = seed_case(abs_path=str(case_dir), category="standard_face")

    resp = client.post(
        "/api/cases/trash",
        json={"case_ids": [case_id], "reason": "测试移入案例废弃区"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trashed"] == 1
    assert body["case_ids"] == [case_id]
    assert body["skipped"] == []
    assert not case_dir.exists()

    with db.connect() as conn:
        row = conn.execute(
            "SELECT abs_path, original_abs_path, trashed_at, trash_reason FROM cases WHERE id = ?",
            (case_id,),
        ).fetchone()
    assert row["original_abs_path"] == str(case_dir)
    assert row["trashed_at"]
    assert row["trash_reason"] == "测试移入案例废弃区"
    trash_path = row["abs_path"]
    assert ".case-workbench-trash" in trash_path
    assert (tmp_path / "客户A" / ".case-workbench-trash" / "cases").is_dir()

    list_resp = client.get("/api/cases")
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 0
    assert client.get(f"/api/cases/{case_id}").status_code == 404
    assert client.get("/api/cases/stats").json()["total"] == 0


def test_trash_cases_skips_missing_directory(client, seed_case):
    case_id = seed_case(abs_path="/tmp/not-a-real-case-directory-for-trash")
    resp = client.post("/api/cases/trash", json={"case_ids": [case_id]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trashed"] == 0
    assert body["case_ids"] == []
    assert body["skipped"] == [{"case_id": case_id, "reason": "directory_missing"}]
