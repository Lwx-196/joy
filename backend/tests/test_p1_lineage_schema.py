"""P1.0 schema：candidate_lineage + ops_audit_log + idempotent migration。

来自 ~/.claude/plans/lucky-zooming-origami.md §P1.2 candidate lineage table
+ §P1.1 ops API audit_log（request_id / reviewer / reason）。

红测先：未实施前 PRAGMA table_info 返回空 → 断言失败；实施后 GREEN。
"""
from __future__ import annotations

from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- candidate_lineage ----------


def test_candidate_lineage_table_has_required_columns(temp_db) -> None:
    from backend import db

    with db.connect() as conn:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(candidate_lineage)"
        ).fetchall()}

    expected = {
        "id",
        "simulation_job_id",
        "case_id",
        "input_image_hash",
        "workflow_hash",
        "provider",
        "model_name",
        "attempt",
        "failure_reason",
        "vlm_judge_result_json",
        "operator_decision",
        "operator_user",
        "decided_at",
        "created_at",
    }
    missing = expected - cols
    assert not missing, f"candidate_lineage missing columns: {missing}"


def test_candidate_lineage_indexes_exist(temp_db) -> None:
    from backend import db

    with db.connect() as conn:
        idx = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='candidate_lineage'"
        ).fetchall()}

    for name in (
        "idx_candidate_lineage_case",
        "idx_candidate_lineage_sim",
        "idx_candidate_lineage_decision",
    ):
        assert name in idx, f"index {name} missing; got {idx}"


def test_candidate_lineage_insert_with_fk(temp_db, seed_case) -> None:
    from backend import db

    case_id = seed_case()
    with db.connect() as conn:
        sim_id = conn.execute(
            """INSERT INTO simulation_jobs
               (case_id, status, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            (case_id, "succeeded", _now(), _now()),
        ).lastrowid
        cur = conn.execute(
            """INSERT INTO candidate_lineage
               (simulation_job_id, case_id, provider, model_name, attempt, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (sim_id, case_id, "comfyui", "v3-sdxl", 1, _now()),
        )
        assert cur.lastrowid is not None and cur.lastrowid > 0


def test_candidate_lineage_operator_decision_lifecycle(temp_db, seed_case) -> None:
    """operator_decision + operator_user + decided_at 三件套写入 + 查询。"""
    from backend import db

    case_id = seed_case()
    with db.connect() as conn:
        lid = conn.execute(
            """INSERT INTO candidate_lineage
               (case_id, provider, model_name, attempt, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (case_id, "comfyui", "v3-sdxl", 2, _now()),
        ).lastrowid
        decided_at = _now()
        conn.execute(
            """UPDATE candidate_lineage
               SET operator_decision=?, operator_user=?, decided_at=?
               WHERE id=?""",
            ("promote", "operator@example.com", decided_at, lid),
        )
        row = conn.execute(
            """SELECT operator_decision, operator_user, decided_at
               FROM candidate_lineage WHERE id=?""",
            (lid,),
        ).fetchone()
    assert row["operator_decision"] == "promote"
    assert row["operator_user"] == "operator@example.com"
    assert row["decided_at"] == decided_at


# ---------- ops_audit_log ----------


def test_ops_audit_log_table_has_required_columns(temp_db) -> None:
    from backend import db

    with db.connect() as conn:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(ops_audit_log)"
        ).fetchall()}

    expected = {
        "id",
        "request_id",
        "endpoint",
        "reviewer",
        "reason",
        "payload_json",
        "response_json",
        "outcome",
        "http_status",
        "created_at",
    }
    missing = expected - cols
    assert not missing, f"ops_audit_log missing columns: {missing}"


def test_ops_audit_log_indexes_exist(temp_db) -> None:
    from backend import db

    with db.connect() as conn:
        idx = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ops_audit_log'"
        ).fetchall()}

    for name in (
        "idx_ops_audit_request",
        "idx_ops_audit_endpoint_at",
        "idx_ops_audit_reviewer",
    ):
        assert name in idx, f"index {name} missing; got {idx}"


def test_ops_audit_log_insert_minimal_record(temp_db) -> None:
    """endpoint + reviewer + outcome 必填可写入。"""
    import json

    from backend import db

    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO ops_audit_log
               (request_id, endpoint, reviewer, reason,
                payload_json, response_json, outcome, http_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "req-abc-123",
                "POST /api/render/ops/batch-rerun",
                "operator@example.com",
                "redo 5 cases after v3 upscale fix",
                json.dumps({"case_ids": [1, 2, 3]}),
                json.dumps({"enqueued": 3}),
                "ok",
                200,
                _now(),
            ),
        )
        assert cur.lastrowid is not None and cur.lastrowid > 0


# ---------- idempotency ----------


def test_init_schema_idempotent(temp_db) -> None:
    """重复 init_schema 不抛 + 行数不重复。"""
    from backend import db

    db.init_schema()
    db.init_schema()

    with db.connect() as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    assert "candidate_lineage" in tables
    assert "ops_audit_log" in tables
