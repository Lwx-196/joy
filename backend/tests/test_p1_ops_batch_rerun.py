"""P1.1.A POST /api/render/ops/batch-rerun — render 真实入队 + simulation dry_run。

来自 ~/.claude/plans/lucky-zooming-origami.md §P1.1：把 force_render_*.py /
formal_render_repair_execution.py 收编为 audit log + queue API。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_batch_rerun_render_dry_run_emits_audit_log(client, seed_case, no_job_pool) -> None:
    case_id = seed_case()
    resp = client.post(
        "/api/render/ops/batch-rerun",
        json={
            "case_ids": [case_id],
            "scope": "render",
            "dry_run": True,
            "reviewer": "operator@example.com",
            "reason": "dry_run check before fire",
        },
        headers={"X-Request-Id": "req-test-dr-1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "render"
    assert body["dry_run"] is True
    assert body["request_id"] == "req-test-dr-1"
    assert body["would_enqueue_case_ids"] == [case_id]
    assert body["would_enqueue_count"] == 1
    # audit log written
    from backend import db
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT request_id, endpoint, reviewer, outcome, http_status "
            "FROM ops_audit_log ORDER BY id DESC LIMIT 1"
        ).fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["request_id"] == "req-test-dr-1"
    assert r["endpoint"] == "POST /api/render/ops/batch-rerun"
    assert r["reviewer"] == "operator@example.com"
    assert r["outcome"] == "dry_run"
    assert r["http_status"] == 200


def test_batch_rerun_render_real_enqueues_jobs(client, seed_case, no_job_pool) -> None:
    case_id = seed_case()
    resp = client.post(
        "/api/render/ops/batch-rerun",
        json={
            "case_ids": [case_id, case_id, 999_999],  # dup + missing
            "scope": "render",
            "dry_run": False,
            "reviewer": "operator@example.com",
            "reason": "re-fire after v3 upscale fix",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "render"
    assert body["dry_run"] is False
    assert body["enqueued_count"] == 1
    assert body["batch_id"]
    assert len(body["job_ids"]) == 1
    assert body["duplicate_count"] == 1
    assert any(item.get("reason") == "case_not_found" for item in body["invalid"])
    # job row inserted
    from backend import db
    with db.connect() as conn:
        job = conn.execute(
            "SELECT id, status, case_id, batch_id FROM render_jobs WHERE id = ?",
            (body["job_ids"][0],),
        ).fetchone()
    assert job is not None
    assert job["case_id"] == case_id
    assert job["status"] == "queued"
    # outcome = partial (dup + invalid present)
    with db.connect() as conn:
        outcome = conn.execute(
            "SELECT outcome FROM ops_audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert outcome["outcome"] == "partial"


def test_batch_rerun_simulation_dry_run_returns_plan(client, seed_case) -> None:
    case_id = seed_case()
    from backend import db
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO simulation_jobs
               (case_id, status, model_plan_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (case_id, "failed",
             json.dumps({"workflow_name": "v3-sdxl"}), _now(), _now()),
        )
        conn.execute(
            """INSERT INTO simulation_jobs
               (case_id, status, model_plan_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (case_id, "done",
             json.dumps({"workflow_name": "v2-baseline"}), _now(), _now()),
        )

    resp = client.post(
        "/api/render/ops/batch-rerun",
        json={
            "case_ids": [case_id],
            "scope": "simulation",
            "dry_run": True,
            "workflow_filter": "v3-sdxl",
            "reviewer": "operator@example.com",
            "reason": "shadow simulation rerun plan",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "simulation"
    assert body["dry_run"] is True
    assert body["planned_count"] == 1
    assert body["planned"][0]["workflow"] == "v3-sdxl"
    assert body["planned"][0]["status"] == "failed"


def test_batch_rerun_simulation_fire_returns_501(client, seed_case) -> None:
    case_id = seed_case()
    resp = client.post(
        "/api/render/ops/batch-rerun",
        json={
            "case_ids": [case_id],
            "scope": "simulation",
            "dry_run": False,
            "reviewer": "operator@example.com",
            "reason": "try real fire",
        },
    )
    assert resp.status_code == 501
    body = resp.json()["detail"]
    assert "ai_generation_adapter" in body["error"]
    # audit log still written with outcome=error / http_status=501
    from backend import db
    with db.connect() as conn:
        row = conn.execute(
            "SELECT outcome, http_status FROM ops_audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["outcome"] == "error"
    assert row["http_status"] == 501


def test_batch_rerun_invalid_scope_400(client, seed_case) -> None:
    case_id = seed_case()
    resp = client.post(
        "/api/render/ops/batch-rerun",
        json={
            "case_ids": [case_id],
            "scope": "garbage",
            "reviewer": "operator@example.com",
        },
    )
    assert resp.status_code == 400
    from backend import db
    with db.connect() as conn:
        row = conn.execute(
            "SELECT outcome, http_status FROM ops_audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["outcome"] == "error"
    assert row["http_status"] == 400


def test_batch_rerun_reviewer_required(client, seed_case) -> None:
    case_id = seed_case()
    resp = client.post(
        "/api/render/ops/batch-rerun",
        json={"case_ids": [case_id], "scope": "render"},
    )
    # missing reviewer → 422 Pydantic; not audited (validation happens pre-handler)
    assert resp.status_code == 422
