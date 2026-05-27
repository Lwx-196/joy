"""P1.1.B POST /api/render/ops/ab-sample — workflow A/B 抽样计划 + 审计。

actual ComfyUI fire 仍 pending owner ai_generation_adapter；本 endpoint 在
audit log 中记录 A/B 规格 + picked_case_ids，由 owner 后续 fire 路径消费。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_PATH_COUNTER = [0]


def _seed_simulation_done(seed_case_factory, count: int, can_publish: bool = False) -> list[int]:
    """Seed `count` (case + done simulation_job) pairs and return case_ids."""
    from backend import db
    case_ids: list[int] = []
    for _ in range(count):
        _PATH_COUNTER[0] += 1
        cid = seed_case_factory(abs_path=f"/tmp/case-ab-{_PATH_COUNTER[0]}")
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO simulation_jobs
                   (case_id, status, can_publish, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (cid, "done", 1 if can_publish else 0, _now(), _now()),
            )
        case_ids.append(cid)
    return case_ids


def test_ab_sample_recent_done_picks_candidates(client, seed_case) -> None:
    seeded = _seed_simulation_done(seed_case, count=5, can_publish=False)
    resp = client.post(
        "/api/render/ops/ab-sample",
        json={
            "workflow_a": "v2-baseline",
            "workflow_b": "v3-sdxl",
            "sample_size": 3,
            "source_pool": "recent_done",
            "reviewer": "operator@example.com",
            "reason": "weekly A/B compare",
        },
        headers={"X-Request-Id": "req-ab-1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "plan_recorded"
    assert body["workflow_a"] == "v2-baseline"
    assert body["workflow_b"] == "v3-sdxl"
    assert body["sample_size_requested"] == 3
    assert body["picked_count"] == 3
    assert len(body["picked_case_ids"]) == 3
    assert all(cid in seeded for cid in body["picked_case_ids"])
    assert body["ab_sample_run_id"].startswith("absamp-")

    from backend import db
    with db.connect() as conn:
        row = conn.execute(
            "SELECT request_id, endpoint, reviewer, outcome, http_status, payload_json "
            "FROM ops_audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["request_id"] == "req-ab-1"
    assert row["endpoint"] == "POST /api/render/ops/ab-sample"
    assert row["outcome"] == "ok"
    payload = json.loads(row["payload_json"])
    assert payload["workflow_a"] == "v2-baseline"


def test_ab_sample_candidate_layer_filters_can_publish(client, seed_case) -> None:
    publishable = _seed_simulation_done(seed_case, count=2, can_publish=True)
    candidate = _seed_simulation_done(seed_case, count=3, can_publish=False)
    resp = client.post(
        "/api/render/ops/ab-sample",
        json={
            "workflow_a": "wf-a",
            "workflow_b": "wf-b",
            "sample_size": 10,
            "source_pool": "candidate_layer",
            "reviewer": "op@x.com",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["picked_count"] == 3
    for cid in body["picked_case_ids"]:
        assert cid in candidate
        assert cid not in publishable


def test_ab_sample_explicit_case_ids(client, seed_case) -> None:
    cids = [seed_case(abs_path=f"/tmp/case-ab-explicit-{i}") for i in range(4)]
    resp = client.post(
        "/api/render/ops/ab-sample",
        json={
            "workflow_a": "wf-a",
            "workflow_b": "wf-b",
            "sample_size": 2,
            "source_pool": "case_ids",
            "case_ids": cids,
            "reviewer": "op@x.com",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["picked_count"] == 2
    assert body["picked_case_ids"] == cids[:2]


def test_ab_sample_invalid_source_pool_400(client) -> None:
    resp = client.post(
        "/api/render/ops/ab-sample",
        json={
            "workflow_a": "a",
            "workflow_b": "b",
            "sample_size": 1,
            "source_pool": "garbage",
            "reviewer": "op@x.com",
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


def test_ab_sample_same_workflow_400(client) -> None:
    resp = client.post(
        "/api/render/ops/ab-sample",
        json={
            "workflow_a": "same",
            "workflow_b": "same",
            "sample_size": 1,
            "source_pool": "recent_done",
            "reviewer": "op@x.com",
        },
    )
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert "must differ" in body["error"]
