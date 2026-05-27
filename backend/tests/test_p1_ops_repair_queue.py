"""P1.1.D POST /api/render/ops/repair-queue — identity/tone 修复批入口。

设计同 ab-sample / batch-rerun / vlm-shadow：
- reviewer 必填，repair_type ∈ {identity|tone|both}
- 走 ops_audit_log
- dry_run=true（默认）规划要修哪些 case，不入队
- 真实修复 pipeline pending owner integration（render_executor +
  ai_generation_adapter T90 路径），dry_run=false 当前返 501
"""
from __future__ import annotations

import json


def test_repair_queue_dry_run_returns_plan_and_audits(client, seed_case) -> None:
    cases = [seed_case(abs_path=f"/tmp/rep/{i}") for i in range(2)]
    resp = client.post(
        "/api/render/ops/repair-queue",
        json={
            "case_ids": cases,
            "repair_type": "identity",
            "dry_run": True,
            "reviewer": "repair@example.com",
            "reason": "fix identity drift batch",
        },
        headers={"X-Request-Id": "req-repair-1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["repair_type"] == "identity"
    assert body["planned_case_ids"] == cases
    assert body["request_id"] == "req-repair-1"
    # No real enqueue in dry_run
    assert body["enqueued_job_ids"] == []

    from backend import db
    with db.connect() as conn:
        row = conn.execute(
            "SELECT request_id, endpoint, reviewer, outcome, http_status, payload_json "
            "FROM ops_audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["request_id"] == "req-repair-1"
    assert row["endpoint"] == "POST /api/render/ops/repair-queue"
    assert row["reviewer"] == "repair@example.com"
    assert row["outcome"] == "dry_run"
    assert row["http_status"] == 200
    payload = json.loads(row["payload_json"])
    assert payload["repair_type"] == "identity"


def test_repair_queue_rejects_unknown_type_422(client, seed_case) -> None:
    case_id = seed_case()
    resp = client.post(
        "/api/render/ops/repair-queue",
        json={
            "case_ids": [case_id],
            "repair_type": "bogus",
            "dry_run": True,
            "reviewer": "x",
            "reason": "y",
        },
    )
    assert resp.status_code == 422


def test_repair_queue_real_fire_returns_501_pending_owner(client, seed_case) -> None:
    case_id = seed_case()
    resp = client.post(
        "/api/render/ops/repair-queue",
        json={
            "case_ids": [case_id],
            "repair_type": "tone",
            "dry_run": False,
            "reviewer": "op@example.com",
            "reason": "fire",
        },
    )
    assert resp.status_code == 501, resp.text
    from backend import db
    with db.connect() as conn:
        row = conn.execute(
            "SELECT outcome, http_status FROM ops_audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["outcome"] == "error"
    assert row["http_status"] == 501


def test_repair_queue_reviewer_required_422(client, seed_case) -> None:
    case_id = seed_case()
    resp = client.post(
        "/api/render/ops/repair-queue",
        json={
            "case_ids": [case_id],
            "repair_type": "identity",
            "dry_run": True,
            "reason": "y",
        },
    )
    assert resp.status_code == 422


def test_repair_queue_drops_invalid_case_ids(client, seed_case) -> None:
    """dry_run 应保留入参 case_ids（无 DB validation 直接写计划），
    real fire 不在本 endpoint 范围。"""
    real_id = seed_case()
    resp = client.post(
        "/api/render/ops/repair-queue",
        json={
            "case_ids": [real_id, 999999],
            "repair_type": "both",
            "dry_run": True,
            "reviewer": "op@x.com",
            "reason": "y",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # invalid 拆开列出，planned 仅含真实存在的 case_id
    assert body["planned_case_ids"] == [real_id]
    assert any(item.get("case_id") == 999999 for item in body.get("invalid", []))
