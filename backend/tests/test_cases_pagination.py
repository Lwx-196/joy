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


def test_tag_filter_exact_match(client, seed_case):
    """tag 精确匹配 tags_json 数组成员(JSON1 LIKE token)。"""
    from backend import db
    a = seed_case(abs_path="/tmp/tag-a")
    b = seed_case(abs_path="/tmp/tag-b")
    with db.connect() as conn:
        conn.execute("UPDATE cases SET tags_json = ? WHERE id = ?", ('["urgent","术后"]', a))
        conn.execute("UPDATE cases SET tags_json = ? WHERE id = ?", ('["术前"]', b))
    resp = client.get("/api/cases", params={"tag": "术后"})
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["abs_path"] == "/tmp/tag-a"


def test_since_today(client, seed_case):
    """since=today 取 indexed_at >= 当日 00:00 UTC。"""
    from backend import db
    from datetime import datetime, timezone, timedelta
    seed_case(abs_path="/tmp/today")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET indexed_at = ? WHERE abs_path = '/tmp/today'",
            (datetime.now(timezone.utc).isoformat(),),
        )
        old_id = conn.execute(
            "INSERT INTO scans (started_at, root_paths, mode, case_count) VALUES (?, '[]', 'test', 1)",
            (yesterday,),
        ).lastrowid
        conn.execute(
            "INSERT INTO cases (scan_id, abs_path, category, last_modified, indexed_at, blocking_issues_json) VALUES (?, '/tmp/old', 'A', ?, ?, '[]')",
            (old_id, yesterday, yesterday),
        )

    resp = client.get("/api/cases", params={"since": "today"})
    body = resp.json()
    paths = {it["abs_path"] for it in body["items"]}
    assert "/tmp/today" in paths
    assert "/tmp/old" not in paths


def test_blocking_open_filter(client, seed_case):
    """blocking=open 取 blocking_issues_json 长度 > 0。"""
    from backend import db
    a = seed_case(abs_path="/tmp/has-block")
    seed_case(abs_path="/tmp/clean")
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET blocking_issues_json=? WHERE id=?",
            ('[{"code":"err1"}]', a),
        )
    resp = client.get("/api/cases", params={"blocking": "open"})
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["abs_path"] == "/tmp/has-block"


def test_include_held_default_hides(client, seed_case):
    """default: 隐藏 held_until > now 的 case。include_held=1 显示。"""
    from backend import db
    from datetime import datetime, timezone, timedelta
    seed_case(abs_path="/tmp/active")
    b = seed_case(abs_path="/tmp/held")
    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    with db.connect() as conn:
        conn.execute("UPDATE cases SET held_until=? WHERE id=?", (future, b))
    resp = client.get("/api/cases")
    body = resp.json()
    paths = {it["abs_path"] for it in body["items"]}
    assert paths == {"/tmp/active"}

    resp = client.get("/api/cases", params={"include_held": 1})
    body = resp.json()
    paths = {it["abs_path"] for it in body["items"]}
    assert paths == {"/tmp/active", "/tmp/held"}


def test_include_held_accepts_true_string(client, seed_case):
    """include_held should accept FastAPI's standard bool query strings (true/1)."""
    from datetime import datetime, timezone, timedelta
    from backend import db
    seed_case(abs_path="/tmp/active2")
    b = seed_case(abs_path="/tmp/held2")
    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    with db.connect() as conn:
        conn.execute("UPDATE cases SET held_until=? WHERE id=?", (future, b))

    # bool query: "true" (string) should also include held cases
    resp = client.get("/api/cases", params={"include_held": "true"})
    body = resp.json()
    paths = {it["abs_path"] for it in body["items"]}
    assert paths == {"/tmp/active2", "/tmp/held2"}
