"""P0.4 — /api/render/ops/vlm-comfyui/status 聚合 endpoint。

oncall 一个 endpoint 看清 3 段状态：vlm 校准/调用量、comfyui 任务分布、gate
阻断 top10 与待处理 accepted_warnings。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_simulation_job(conn, *, status: str, audit: dict | None = None,
                         workflow: str | None = None, created_at: datetime | None = None,
                         can_publish: int = 0) -> int:
    when = (created_at or datetime.now(timezone.utc)).isoformat()
    audit_obj = dict(audit or {})
    if workflow:
        audit_obj.setdefault("model_name", workflow)
        audit_obj.setdefault("workflow_name", workflow)
    cur = conn.execute(
        """
        INSERT INTO simulation_jobs (
          status, focus_targets_json, policy_json, model_plan_json,
          input_refs_json, output_refs_json, watermarked, audit_json,
          error_message, can_publish, created_at, updated_at
        )
        VALUES (?, '[]', '{}', ?, '[]', '[]', 0, ?, ?, ?, ?, ?)
        """,
        (
            status,
            json.dumps({"workflow_name": workflow} if workflow else {}),
            json.dumps(audit_obj),
            None if status != "failed" else "stub error",
            can_publish,
            when,
            when,
        ),
    )
    return int(cur.lastrowid)


def _seed_vlm_usage(conn, *, status: str = "success", purpose: str = "classifier",
                    created_at: datetime | None = None) -> int:
    when = (created_at or datetime.now(timezone.utc)).isoformat()
    cur = conn.execute(
        """
        INSERT INTO vlm_usage_log (
          purpose, provider, model, case_id, input_tokens, output_tokens,
          cost_usd, cost_source, latency_ms, status, error_detail,
          error_json, usage_raw_json, created_at
        ) VALUES (?, 'stub-p', 'stub-m', NULL, 0, 0, 0, 'unknown', 0, ?, NULL, NULL, '{}', ?)
        """,
        (purpose, status, when),
    )
    return int(cur.lastrowid)


def _seed_case(conn, *, abs_path: str = "/tmp/c", meta: dict | None = None) -> int:
    now = _now()
    scan_id = conn.execute(
        "INSERT INTO scans (started_at, root_paths, mode) VALUES (?, ?, ?)",
        (now, "[]", "unit"),
    ).lastrowid
    return int(
        conn.execute(
            """
            INSERT INTO cases (scan_id, abs_path, category, last_modified, indexed_at, meta_json)
            VALUES (?, ?, 'standard_face', ?, ?, ?)
            """,
            (scan_id, abs_path, now, now, json.dumps(meta or {})),
        ).lastrowid
    )


def _seed_ticket(conn, *, case_id: int, ticket_type: str, reason_code: str,
                 stage: str = "pre_render_gate", blocks_render: int = 1,
                 blocks_publish: int = 0, status: str = "open") -> int:
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO review_tickets (
          case_id, ticket_type, stage, status, blocks_render, blocks_publish,
          reason_code, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (case_id, ticket_type, stage, status, blocks_render, blocks_publish, reason_code, now, now),
    )
    return int(cur.lastrowid)


def test_ops_status_returns_three_top_level_sections(client, temp_db: Path) -> None:
    """endpoint 必有 vlm / comfyui / gate 三段。"""
    resp = client.get("/api/render/ops/vlm-comfyui/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "vlm" in body
    assert "comfyui" in body
    assert "gate" in body


def test_ops_status_vlm_section_has_required_fields(client, temp_db: Path) -> None:
    """vlm: calibration_status / total_calls_7d / fail_rate / confidence_distribution / last_shadow_run."""
    from backend import db

    with db.connect() as conn:
        for _ in range(3):
            _seed_vlm_usage(conn, status="success")
        _seed_vlm_usage(conn, status="error")
    resp = client.get("/api/render/ops/vlm-comfyui/status")
    body = resp.json()
    vlm = body["vlm"]
    for key in ("calibration_status", "total_calls_7d", "fail_rate",
                "confidence_distribution", "last_shadow_run"):
        assert key in vlm, f"vlm.{key} missing"
    assert vlm["total_calls_7d"] == 4
    # 1 error / 4 total = 0.25
    assert abs(vlm["fail_rate"] - 0.25) < 0.001


def test_ops_status_comfyui_section_buckets_jobs(client, temp_db: Path) -> None:
    """comfyui: simulation_jobs_7d {done, failed, by_workflow} + candidate_only_pending + failure_breakdown."""
    from backend import db

    with db.connect() as conn:
        _seed_simulation_job(conn, status="done", workflow="wf_a", can_publish=0)
        _seed_simulation_job(conn, status="done", workflow="wf_b", can_publish=1)
        _seed_simulation_job(conn, status="failed", workflow="wf_a",
                             audit={"failure": {"failure_stage": "provider_call",
                                               "error_class": "TimeoutError",
                                               "error_message": "x",
                                               "provider_attempts": [],
                                               "workflow_name": "wf_a",
                                               "retry_trace": [],
                                               "traceback": ""}})
        _seed_simulation_job(conn, status="failed", workflow="wf_a",
                             audit={"failure": {"failure_stage": "watermark",
                                               "error_class": "OSError",
                                               "error_message": "y",
                                               "provider_attempts": [],
                                               "workflow_name": "wf_a",
                                               "retry_trace": [],
                                               "traceback": ""}})
    resp = client.get("/api/render/ops/vlm-comfyui/status")
    comfyui = resp.json()["comfyui"]
    assert comfyui["simulation_jobs_7d"]["done"] == 2
    assert comfyui["simulation_jobs_7d"]["failed"] == 2
    by_wf = comfyui["simulation_jobs_7d"]["by_workflow"]
    by_wf_map = {row["key"]: row["count"] for row in by_wf}
    assert by_wf_map.get("wf_a") == 3  # 1 done + 2 failed
    assert by_wf_map.get("wf_b") == 1
    assert comfyui["candidate_only_pending"] == 1  # 1 done with can_publish=0
    assert "failure_breakdown" in comfyui
    breakdown = {row["key"]: row["count"] for row in comfyui["failure_breakdown"]}
    assert breakdown.get("provider_call") == 1
    assert breakdown.get("watermark") == 1


def test_ops_status_gate_section_returns_blockers_and_accepted_pending(
    client, temp_db: Path
) -> None:
    """gate: pre_render_blockers_top10 / delivery_gate_blockers_top10 / accepted_warnings_pending."""
    from backend import db

    with db.connect() as conn:
        c1 = _seed_case(conn, abs_path="/tmp/c1")
        c2 = _seed_case(conn, abs_path="/tmp/c2",
                        meta={"source_group_selection": {
                            "accepted_warnings": [
                                {"slot": "front", "code": "crop_touches_frame"}
                            ]
                        }})
        # Pre-render gate tickets — 2 of crop_touches_frame, 1 of identity
        _seed_ticket(conn, case_id=c1, ticket_type="source_quality_review",
                     reason_code="crop_touches_frame", stage="pre_render_gate",
                     blocks_render=1)
        _seed_ticket(conn, case_id=c2, ticket_type="source_quality_review",
                     reason_code="crop_touches_frame", stage="pre_render_gate",
                     blocks_render=1)
        _seed_ticket(conn, case_id=c1, ticket_type="identity_review",
                     reason_code="identity_embedding_mismatch", stage="pre_render_gate",
                     blocks_render=1)
        # Delivery gate ticket
        _seed_ticket(conn, case_id=c1, ticket_type="delivery_review",
                     reason_code="render_quality_below_threshold", stage="delivery_gate",
                     blocks_publish=1)
    resp = client.get("/api/render/ops/vlm-comfyui/status")
    gate = resp.json()["gate"]
    for key in ("pre_render_blockers_top10", "delivery_gate_blockers_top10",
                "accepted_warnings_pending"):
        assert key in gate
    pre = {row["key"]: row["count"] for row in gate["pre_render_blockers_top10"]}
    assert pre.get("crop_touches_frame") == 2
    assert pre.get("identity_embedding_mismatch") == 1
    deliv = {row["key"]: row["count"] for row in gate["delivery_gate_blockers_top10"]}
    assert deliv.get("render_quality_below_threshold") == 1
    # 1 case has 1 accepted warning → pending count 1
    assert gate["accepted_warnings_pending"] == 1


def test_ops_status_empty_db_returns_safe_defaults(client, temp_db: Path) -> None:
    """空 DB → 不抛错，返回 0/空 list 默认值。"""
    resp = client.get("/api/render/ops/vlm-comfyui/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["vlm"]["total_calls_7d"] == 0
    assert body["vlm"]["fail_rate"] == 0.0
    assert body["comfyui"]["simulation_jobs_7d"]["done"] == 0
    assert body["comfyui"]["simulation_jobs_7d"]["failed"] == 0
    assert body["gate"]["pre_render_blockers_top10"] == []
    assert body["gate"]["accepted_warnings_pending"] == 0
