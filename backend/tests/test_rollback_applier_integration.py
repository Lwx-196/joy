"""End-to-end integration tests — plan §P2.4 H-6 (Wave 3 A track).

Real-data discipline (no mocks): seed the temp SQLite ``temp_db`` with
simulation_jobs / candidate_lineage / ops_audit_log rows that the real
``promotion_slo_monitor.evaluate_window`` will pick up, then pass the live
``SLOReport`` straight into ``promotion_rollback_applier.apply_rollback_decision``
on a real manifest file. Asserts cover:

1. SLO monitor → applier wiring produces the expected ``rollback`` decision
   from seeded violations.
2. Applier writes a manifest on disk where:
   - ``promotion_state == 'rolled_back'``
   - ``rollback_forensics.failed_snapshot.bindings`` carries the prior
     bindings (Wave 3 H-2).
   - ``rollback_baseline.bindings`` is **untouched** (preserved revert
     target, Wave 3 H-2 semantic split).
3. ``ops_audit_log`` carries the audit-first pair: ``rollback_started`` +
   ``rollback_completed`` sharing one ``request_id`` correlation id.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend import db
from backend.services.promotion_rollback_applier import (
    REASON_APPLIED,
    REC_ROLLBACK,
    ROLLED_BACK_STATE,
    REVIEWER,
    apply_rollback_decision,
)
from backend.services.promotion_slo_monitor import (
    RECOMMENDATION_ROLLBACK,
    evaluate_window,
)
from backend.tests.test_promotion_slo_monitor import (
    _insert_lineage,
    _insert_ops_audit,
    _insert_simulation_job,
)


# Pre-existing healthy baseline manifest (the revert target preserved by the
# applier across the rollback — Wave 3 H-2 semantic split).
_PRIOR_BASELINE = {
    "manifest_ref": "sha-prior-healthy-v0.9.0",
    "captured_at": "2026-05-01T00:00:00+00:00",
    "bindings": {
        "vlm_calibration_hash": "sha256:prior-vlm",
        "production_gate_hash": "sha256:prior-gate",
        "ab_report_hash": "sha256:prior-ab",
        "render_quality_baseline_hash": "sha256:prior-rq",
    },
}

# Active production bindings at the time of the rollback trigger.
_ACTIVE_BINDINGS = {
    "vlm_calibration_hash": "sha256:active-vlm-v1.0.0",
    "production_gate_hash": "sha256:active-gate-v1.0.0",
    "ab_report_hash": "sha256:active-ab-v1.0.0",
    "render_quality_baseline_hash": "sha256:active-rq-v1.0.0",
}


def _write_manifest_with_baseline(path: Path) -> None:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "version": "v1.0.0",
        "scope": "production",
        "approver": "alice@example.com",
        "approved_at": "2026-05-15T00:00:00+00:00",
        "expires_at": None,
        "promotion_state": "p25",
        "bindings": dict(_ACTIVE_BINDINGS),
        "rollback_baseline": dict(_PRIOR_BASELINE),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _seed_violation_window(conn: Any) -> None:
    """Push ``comfyui_failure_rate`` over the 5% threshold with a meaningful
    sample size (≥30 to dodge ``monitoring_paused``)."""
    # 20 provider_call failures + 80 successes → rate = 0.20 > 0.05.
    for _ in range(20):
        _insert_simulation_job(conn, status="failed", failure_stage="provider_call")
    for _ in range(80):
        _insert_simulation_job(conn, status="done")
    # Lineage agreement (real candidates so vlm_disagreement isn't NaN).
    for _ in range(20):
        _insert_lineage(conn, agreement_rate=0.95)
    for i in range(20):
        ep = (
            "POST /api/render/ops/batch-rerun"
            if i % 2 == 0
            else "POST /api/delivery/foo"
        )
        _insert_ops_audit(conn, endpoint=ep, outcome="ok")
    conn.commit()


def test_end_to_end_slo_monitor_to_applier_rollback(
    temp_db: Path, tmp_path: Path
) -> None:
    """Seed real SQLite + manifest → evaluate_window emits 'rollback' →
    apply_rollback_decision atomically rewrites the manifest + lands the
    audit-first pair (rollback_started + rollback_completed) sharing one
    request_id correlation id."""
    # 1) Seed real DB rows that push the SLO over threshold.
    with db.connect() as conn:
        _seed_violation_window(conn)

    # 2) Real evaluate_window — no mocking.
    report = evaluate_window(window_hours=48)
    assert report.recommendation == RECOMMENDATION_ROLLBACK, (
        f"expected real evaluate_window to emit rollback; got {report.recommendation} "
        f"violations={report.violations}"
    )
    assert any(
        v.get("dimension") == "comfyui_failure_rate" for v in report.violations
    ), "seeded comfyui failure rate must surface as a violation"

    # 3) Real manifest file with both `bindings` (active) and
    #    `rollback_baseline` (prior healthy revert target).
    manifest_path = tmp_path / "manifest.json"
    _write_manifest_with_baseline(manifest_path)

    # 4) Apply the live SLOReport through the applier (real DB conn).
    request_id = "integration-test-rollback-001"
    with db.connect() as conn:
        result = apply_rollback_decision(
            report,
            dry_run=False,
            manifest_path=manifest_path,
            conn=conn,
            request_id=request_id,
        )

    # 5) Result contract checks.
    assert result["applied"] is True
    assert result["reason"] == REASON_APPLIED
    assert result["recommendation"] == REC_ROLLBACK
    assert result["request_id"] == request_id
    assert "audit_log_ids" in result
    started_id = result["audit_log_ids"]["rollback_started"]
    completed_id = result["audit_log_ids"]["rollback_completed"]
    assert started_id >= 1 and completed_id > started_id

    # 6) Manifest on disk: promotion_state flipped + forensics snapshot
    #    captured + rollback_baseline preserved untouched.
    new_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert new_payload["promotion_state"] == ROLLED_BACK_STATE

    forensics = new_payload["rollback_forensics"]["failed_snapshot"]
    assert forensics["bindings"] == _ACTIVE_BINDINGS
    assert forensics["from_state"] == "p25"
    assert forensics["rolled_back_at"]

    # Critical revert-target preservation (Wave 3 H-2 semantic split):
    assert new_payload["rollback_baseline"] == _PRIOR_BASELINE, (
        "rollback_baseline MUST be untouched by the applier; it carries the "
        "prior-approved-healthy revert target"
    )

    # 7) Audit-first pair under one correlation id.
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, outcome, http_status, reviewer, endpoint, response_json "
            "FROM ops_audit_log WHERE request_id = ? ORDER BY id ASC",
            (request_id,),
        ).fetchall()
    outcomes = [r["outcome"] for r in rows]
    assert outcomes == ["rollback_started", "rollback_completed"], (
        f"expected audit-first pair under correlation id; got {outcomes}"
    )
    started, completed = rows[0], rows[1]
    assert started["http_status"] == 202
    assert completed["http_status"] == 200
    for row in (started, completed):
        assert row["reviewer"] == REVIEWER
        assert row["endpoint"] == "promotion_rollback_applier.apply"

    completed_response = json.loads(completed["response_json"])
    assert completed_response["phase"] == "rollback_completed"
    assert completed_response["correlation_started_id"] == started_id
    assert completed_response["to_state"] == ROLLED_BACK_STATE
    assert completed_response["bindings_snapshot"] == _ACTIVE_BINDINGS


def test_integration_idempotency_replay_after_apply(
    temp_db: Path, tmp_path: Path
) -> None:
    """Re-running the applier against a manifest already in rolled_back state
    must be a clean no-op even when the SLO monitor would still emit
    'rollback' (operator hasn't yet restored the baseline). Forensics
    snapshot from the first apply must remain on disk untouched."""
    with db.connect() as conn:
        _seed_violation_window(conn)

    manifest_path = tmp_path / "manifest.json"
    _write_manifest_with_baseline(manifest_path)

    # First apply: real rollback.
    report = evaluate_window(window_hours=48)
    assert report.recommendation == RECOMMENDATION_ROLLBACK
    with db.connect() as conn:
        result1 = apply_rollback_decision(
            report,
            dry_run=False,
            manifest_path=manifest_path,
            conn=conn,
            request_id="integration-replay-first",
        )
    assert result1["applied"] is True
    first_payload = manifest_path.read_text(encoding="utf-8")

    # Second apply against the same manifest (now rolled_back).
    report2 = evaluate_window(window_hours=48)
    with db.connect() as conn:
        result2 = apply_rollback_decision(
            report2,
            dry_run=False,
            manifest_path=manifest_path,
            conn=conn,
            request_id="integration-replay-second",
        )
    assert result2["applied"] is False
    assert result2["reason"] == "already_rolled_back"

    # Manifest bytes identical to post-first-apply (forensics preserved).
    assert manifest_path.read_text(encoding="utf-8") == first_payload

    # No audit rows under the second request_id (idempotent no-op writes
    # neither a started nor a completed row).
    with db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM ops_audit_log WHERE request_id = ?",
            ("integration-replay-second",),
        ).fetchone()["n"]
    assert n == 0
