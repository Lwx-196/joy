"""P0.1 read API — simulation_jobs.audit_json.failure 聚合/单 job trace 查询。

oncall 必须能在 30 秒内 SQL 归因到 failure_stage / error_class，
两个端点：
- GET /api/render/jobs/failures/recent?days=7&group_by=stage|error_class|workflow
- GET /api/render/jobs/{id}/failure-trace
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _seed_simulation_job(
    conn,
    *,
    status: str = "failed",
    audit: dict | None = None,
    error_message: str | None = "boom",
    created_at: datetime | None = None,
    case_id: int | None = None,
) -> int:
    when = (created_at or datetime.now(timezone.utc)).isoformat()
    audit_json = json.dumps(audit or {}, ensure_ascii=False)
    cur = conn.execute(
        """
        INSERT INTO simulation_jobs (
          status, focus_targets_json, policy_json, model_plan_json,
          input_refs_json, output_refs_json, watermarked, audit_json,
          error_message, can_publish, created_at, updated_at, case_id
        )
        VALUES (?, '[]', '{}', '{}', '[]', '[]', 0, ?, ?, 0, ?, ?, ?)
        """,
        (status, audit_json, error_message, when, when, case_id),
    )
    return int(cur.lastrowid)


def _failure_block(
    *,
    stage: str = "provider_call",
    error_class: str = "RuntimeError",
    error_message: str = "boom",
    provider: str = "ps_model_router",
    workflow: str = "stub-model",
    traceback_text: str = "Traceback...\nRuntimeError: boom\n",
) -> dict:
    return {
        "failure_stage": stage,
        "error_class": error_class,
        "error_message": error_message,
        "provider_attempts": [
            {"provider": provider, "model_name": workflow, "attempt": 1, "error_class": error_class}
        ],
        "workflow_name": workflow,
        "retry_trace": [],
        "traceback": traceback_text,
    }


def test_failures_recent_returns_grouped_counts_by_stage(client, temp_db: Path) -> None:
    """failures/recent 默认 group_by=stage，对 7 天内 failed jobs 聚合。"""
    from backend import db

    with db.connect() as conn:
        _seed_simulation_job(
            conn,
            status="failed",
            audit={"failure": _failure_block(stage="provider_call", error_class="TimeoutError")},
        )
        _seed_simulation_job(
            conn,
            status="failed",
            audit={"failure": _failure_block(stage="provider_call", error_class="RuntimeError")},
        )
        _seed_simulation_job(
            conn,
            status="failed",
            audit={"failure": _failure_block(stage="watermark", error_class="OSError")},
        )
        _seed_simulation_job(
            conn,
            status="done",
            audit={"failure": None},
            error_message=None,
        )

    resp = client.get("/api/render/jobs/failures/recent?days=7&group_by=stage")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_failed"] == 3
    by_stage = {row["key"]: row["count"] for row in body["groups"]}
    assert by_stage.get("provider_call") == 2
    assert by_stage.get("watermark") == 1
    assert body["group_by"] == "stage"


def test_failures_recent_group_by_error_class(client, temp_db: Path) -> None:
    from backend import db

    with db.connect() as conn:
        _seed_simulation_job(
            conn,
            status="failed",
            audit={"failure": _failure_block(error_class="TimeoutError")},
        )
        _seed_simulation_job(
            conn,
            status="failed",
            audit={"failure": _failure_block(error_class="TimeoutError")},
        )
        _seed_simulation_job(
            conn,
            status="failed",
            audit={"failure": _failure_block(error_class="HTTPError")},
        )

    resp = client.get("/api/render/jobs/failures/recent?days=7&group_by=error_class")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_failed"] == 3
    by_class = {row["key"]: row["count"] for row in body["groups"]}
    assert by_class["TimeoutError"] == 2
    assert by_class["HTTPError"] == 1


def test_failures_recent_group_by_workflow(client, temp_db: Path) -> None:
    from backend import db

    with db.connect() as conn:
        _seed_simulation_job(
            conn,
            status="failed",
            audit={"failure": _failure_block(workflow="ps_model_router_v1")},
        )
        _seed_simulation_job(
            conn,
            status="failed",
            audit={"failure": _failure_block(workflow="comfyui_local_region_v3")},
        )
        _seed_simulation_job(
            conn,
            status="failed",
            audit={"failure": _failure_block(workflow="ps_model_router_v1")},
        )

    resp = client.get("/api/render/jobs/failures/recent?days=7&group_by=workflow")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_wf = {row["key"]: row["count"] for row in body["groups"]}
    assert by_wf["ps_model_router_v1"] == 2
    assert by_wf["comfyui_local_region_v3"] == 1


def test_failures_recent_window_excludes_old_rows(client, temp_db: Path) -> None:
    """超出 days 窗口的失败行不计入。"""
    from backend import db

    fresh = datetime.now(timezone.utc) - timedelta(days=1)
    stale = datetime.now(timezone.utc) - timedelta(days=30)
    with db.connect() as conn:
        _seed_simulation_job(
            conn,
            status="failed",
            audit={"failure": _failure_block()},
            created_at=fresh,
        )
        _seed_simulation_job(
            conn,
            status="failed",
            audit={"failure": _failure_block()},
            created_at=stale,
        )

    resp = client.get("/api/render/jobs/failures/recent?days=7&group_by=stage")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_failed"] == 1


def test_failures_recent_invalid_group_by_returns_400(client, temp_db: Path) -> None:
    resp = client.get("/api/render/jobs/failures/recent?group_by=ship-it-yolo")
    assert resp.status_code in {400, 422}


def test_failure_trace_returns_full_chain(client, temp_db: Path) -> None:
    """单 job /failure-trace 返回结构化 failure 块 + status + legacy error_message。"""
    from backend import db

    with db.connect() as conn:
        job_id = _seed_simulation_job(
            conn,
            status="failed",
            audit={
                "provider": "ps_model_router",
                "model_name": "stub",
                "failure": _failure_block(
                    stage="provider_call",
                    error_class="RuntimeError",
                    error_message="provider timed out after 30s",
                    traceback_text="Traceback (most recent call last):\n  File 'x.py', line 1\nRuntimeError: provider timed out\n",
                ),
            },
            error_message="provider timed out after 30s",
        )

    resp = client.get(f"/api/render/jobs/{job_id}/failure-trace")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["simulation_job_id"] == job_id
    assert body["status"] == "failed"
    assert body["error_message"] == "provider timed out after 30s"
    assert body["failure"] is not None
    assert body["failure"]["error_class"] == "RuntimeError"
    assert body["failure"]["failure_stage"] == "provider_call"
    assert "provider timed out" in body["failure"]["traceback"]


def test_failure_trace_for_success_job_returns_null_failure(client, temp_db: Path) -> None:
    """成功 job 也可查 trace（失败为 null），便于 UI 统一调用。"""
    from backend import db

    with db.connect() as conn:
        job_id = _seed_simulation_job(
            conn,
            status="done",
            audit={"provider": "ps", "failure": None},
            error_message=None,
        )

    resp = client.get(f"/api/render/jobs/{job_id}/failure-trace")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "done"
    assert body["failure"] is None
    assert body["error_message"] is None


def test_failure_trace_404_for_missing_job(client, temp_db: Path) -> None:
    resp = client.get("/api/render/jobs/999999/failure-trace")
    assert resp.status_code == 404
