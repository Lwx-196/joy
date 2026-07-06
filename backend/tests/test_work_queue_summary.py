"""Tests for GET /api/work-queue/summary — 服务端 deriveLanes 等价聚合。

车道谓词逐条对照 frontend/src/lib/work-queue.ts:
  todayNew        last_modified 本地时区在今天
  missingLabel    category == non_labeled 且 manual_category 为空
  blockingOpen    merge_codes(auto+manual) 非空 且 review_status != reviewed
  unboundCustomer customer_id 为空 且 customer_raw 非空
  pendingReview   review_status == pending
held(held_until 在未来)与 trashed 的 case 不进任何车道也不进 total。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from backend import db


def _set(case_id: int, **cols) -> None:
    assigns = ", ".join(f"{k} = ?" for k in cols)
    with db.connect() as conn:
        conn.execute(f"UPDATE cases SET {assigns} WHERE id = ?", [*cols.values(), case_id])


def _lanes(client) -> dict:
    resp = client.get("/api/work-queue/summary")
    assert resp.status_code == 200
    return resp.json()


def test_summary_empty_db(client):
    body = _lanes(client)
    assert body["total"] == 0
    assert body["lanes"]["todayNew"] == {"count": 0, "case_ids": []}
    assert body["lanes"]["missingLabel"] == {"count": 0}
    assert body["lanes"]["blockingOpen"] == {"count": 0}
    assert body["lanes"]["unboundCustomer"] == {"count": 0}
    assert body["lanes"]["pendingReview"] == {"count": 0, "case_ids": []}


def test_today_lane_counts_and_ids_desc(client, seed_case):
    # seed_case 的 last_modified = 当前 UTC 时刻 → 本地时区必为今天
    a = seed_case(abs_path="/tmp/wq-a")
    b = seed_case(abs_path="/tmp/wq-b")
    old = seed_case(abs_path="/tmp/wq-old")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    _set(old, last_modified=yesterday)

    body = _lanes(client)
    assert body["total"] == 3
    # id DESC 与列表接口同序,保证前端 slice(0, MAX_BATCH) 行为不变
    assert body["lanes"]["todayNew"] == {"count": 2, "case_ids": [b, a]}


def test_missing_label_requires_no_manual_override(client, seed_case):
    hit = seed_case(abs_path="/tmp/wq-nl", category="non_labeled")
    overridden = seed_case(abs_path="/tmp/wq-nl2", category="non_labeled")
    _set(overridden, manual_category="standard_face")
    seed_case(abs_path="/tmp/wq-other", category="standard_face")

    body = _lanes(client)
    assert body["lanes"]["missingLabel"]["count"] == 1
    assert hit  # 占位:命中行存在即可,车道只返回计数


def test_blocking_open_uses_merge_codes_not_array_length(client, seed_case):
    blocked = seed_case(abs_path="/tmp/wq-blk")
    _set(blocked, blocking_issues_json=json.dumps(["face_detection_failure"]))
    # 已 reviewed 的阻塞 case 不进车道
    reviewed = seed_case(abs_path="/tmp/wq-blk-rev")
    _set(
        reviewed,
        blocking_issues_json=json.dumps(["face_detection_failure"]),
        review_status="reviewed",
    )
    # 数组非空但条目 normalize 后 code 为空 → merge_codes 丢弃,不算阻塞
    # (json_array_length 口径会误判为 1,这里锁死 merge_codes 等价性)
    junk = seed_case(abs_path="/tmp/wq-blk-junk")
    _set(junk, blocking_issues_json=json.dumps([{"files": ["x.jpg"]}]))
    # manual 阻塞单独也算
    manual = seed_case(abs_path="/tmp/wq-blk-manual")
    _set(manual, manual_blocking_issues_json=json.dumps(["manual_reject"]))

    body = _lanes(client)
    assert body["lanes"]["blockingOpen"]["count"] == 2


def test_unbound_customer_requires_raw_nonempty(client, seed_case):
    seed_case(abs_path="/tmp/wq-cu-a", customer_raw="张三")  # customer_id NULL → 命中
    bound = seed_case(abs_path="/tmp/wq-cu-b", customer_raw="李四")
    with db.connect() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cust = conn.execute(
            "INSERT INTO customers (canonical_name, created_at, updated_at) VALUES ('李四', ?, ?)",
            (now, now),
        ).lastrowid
    _set(bound, customer_id=cust)
    seed_case(abs_path="/tmp/wq-cu-c", customer_raw=None)  # raw 为空 → 不命中
    empty_raw = seed_case(abs_path="/tmp/wq-cu-d")
    _set(empty_raw, customer_raw="")  # 空串等价 JS falsy → 不命中

    body = _lanes(client)
    assert body["lanes"]["unboundCustomer"]["count"] == 1


def test_pending_review_lane_returns_ids(client, seed_case):
    p1 = seed_case(abs_path="/tmp/wq-p1")
    p2 = seed_case(abs_path="/tmp/wq-p2")
    _set(p1, review_status="pending")
    _set(p2, review_status="pending")
    done = seed_case(abs_path="/tmp/wq-p3")
    _set(done, review_status="reviewed")

    body = _lanes(client)
    assert body["lanes"]["pendingReview"] == {"count": 2, "case_ids": [p2, p1]}


def test_held_and_trashed_excluded_everywhere(client, seed_case):
    live = seed_case(abs_path="/tmp/wq-live")
    _set(live, review_status="pending")
    held = seed_case(abs_path="/tmp/wq-held")
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    _set(held, review_status="pending", held_until=future)
    trashed = seed_case(abs_path="/tmp/wq-trash")
    now = datetime.now(timezone.utc).isoformat()
    _set(trashed, review_status="pending", trashed_at=now)
    # held_until 在过去 = 不再挂起 → 正常计入
    unheld = seed_case(abs_path="/tmp/wq-unheld")
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    _set(unheld, review_status="pending", held_until=past)

    body = _lanes(client)
    assert body["total"] == 2
    assert body["lanes"]["pendingReview"]["count"] == 2
    assert set(body["lanes"]["pendingReview"]["case_ids"]) == {live, unheld}
    assert body["lanes"]["todayNew"]["count"] == 2
