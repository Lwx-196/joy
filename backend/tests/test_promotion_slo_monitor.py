"""Boundary tests for promotion_slo_monitor (plan §P2.4).

Uses the conftest `temp_db` fixture which calls `backend.db.init_schema()` to
build a real SQLite schema (simulation_jobs / candidate_lineage /
ops_audit_log). No mocks — we INSERT rows directly and call
`evaluate_window(conn=...)`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend import db
from backend.services.promotion_slo_monitor import (
    DEFAULT_MINIMUM_SAMPLE_SIZE,
    SLOReport,
    evaluate_window,
    load_default_thresholds,
)

# ---------------------------------------------------------------------------
# Insert helpers
# ---------------------------------------------------------------------------


def _now_iso(delta_minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).isoformat()


def _insert_simulation_job(
    conn,
    *,
    status: str,
    failure_stage: str | None = None,
    when: str | None = None,
) -> int:
    audit: dict = {}
    if failure_stage:
        audit["failure"] = {"failure_stage": failure_stage}
    at = when or _now_iso(-5)
    cur = conn.execute(
        """
        INSERT INTO simulation_jobs (
          status, focus_targets_json, policy_json, model_plan_json,
          input_refs_json, output_refs_json, watermarked, audit_json,
          error_message, can_publish, created_at, updated_at
        )
        VALUES (?, '[]', '{}', '{}', '[]', '[]', 0, ?, NULL, 0, ?, ?)
        """,
        (status, json.dumps(audit), at, at),
    )
    return int(cur.lastrowid)


def _insert_lineage(
    conn,
    *,
    agreement_rate: float | None,
    when: str | None = None,
) -> int:
    payload: dict = {}
    if agreement_rate is not None:
        payload["agreement_rate"] = agreement_rate
    at = when or _now_iso(-5)
    cur = conn.execute(
        """
        INSERT INTO candidate_lineage (
          simulation_job_id, case_id, attempt, vlm_judge_result_json, created_at
        )
        VALUES (NULL, NULL, 1, ?, ?)
        """,
        (json.dumps(payload) if payload else None, at),
    )
    return int(cur.lastrowid)


def _insert_ops_audit(
    conn,
    *,
    endpoint: str,
    outcome: str,
    when: str | None = None,
) -> int:
    at = when or _now_iso(-5)
    cur = conn.execute(
        """
        INSERT INTO ops_audit_log (
          request_id, endpoint, reviewer, reason,
          payload_json, response_json, outcome, http_status, created_at
        )
        VALUES (?, ?, 'tester', NULL, '{}', '{}', ?, 200, ?)
        """,
        (f"req-{at}-{endpoint}-{outcome}", endpoint, outcome, at),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_window_returns_insufficient_data(temp_db: Path) -> None:
    """0 samples in window → recommendation='insufficient_data', within_slo=True."""
    with db.connect() as conn:
        report = evaluate_window(window_hours=48, conn=conn)

    assert isinstance(report, SLOReport)
    assert report.recommendation == "insufficient_data"
    assert report.within_slo is True
    assert report.sample_size == 0
    assert report.violations == []
    assert report.window_hours == 48


def test_all_within_slo_returns_continue(temp_db: Path) -> None:
    """Healthy window (failure rate well under 5%, vlm agreement high, no
    rejections, no blockers) → recommendation='continue'."""
    with db.connect() as conn:
        # 50 simulation_jobs: 1 failed (non-pre_render_gate), 49 done = 2% failure
        _insert_simulation_job(conn, status="failed", failure_stage="provider_call")
        for _ in range(49):
            _insert_simulation_job(conn, status="done")
        # 20 lineage rows w/ agreement=0.95 → disagreement 0.05 (< 0.10)
        for _ in range(20):
            _insert_lineage(conn, agreement_rate=0.95)
        # 20 ops_audit rows on delivery/batch-rerun endpoints, all ok
        for i in range(20):
            ep = "POST /api/render/ops/batch-rerun" if i % 2 == 0 else "POST /api/delivery/foo"
            _insert_ops_audit(conn, endpoint=ep, outcome="ok")
        conn.commit()

        report = evaluate_window(window_hours=48, conn=conn)

    assert report.sample_size >= DEFAULT_MINIMUM_SAMPLE_SIZE
    assert report.within_slo is True
    assert report.recommendation == "continue"
    assert report.violations == []
    assert report.evidence["comfyui_failure"]["rate"] == pytest.approx(0.02, abs=0.001)


def test_high_failure_rate_triggers_rollback(temp_db: Path) -> None:
    """ComfyUI failure_rate > 5% → violation + rollback."""
    with db.connect() as conn:
        # 100 jobs, 20 failed = 20% failure
        for _ in range(20):
            _insert_simulation_job(conn, status="failed", failure_stage="provider_call")
        for _ in range(80):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(window_hours=48, conn=conn)

    assert report.within_slo is False
    assert report.recommendation == "rollback"
    dims = {v["dimension"] for v in report.violations}
    assert "comfyui_failure_rate" in dims
    fr_violation = next(v for v in report.violations if v["dimension"] == "comfyui_failure_rate")
    assert fr_violation["actual"] == pytest.approx(0.20, abs=0.001)
    assert fr_violation["threshold"] == 0.05


def test_high_vlm_disagreement_triggers_rollback(temp_db: Path) -> None:
    """vlm disagreement > 10% → violation + rollback."""
    with db.connect() as conn:
        # 40 lineage rows w/ agreement=0.70 → disagreement 0.30 > 0.10
        for _ in range(40):
            _insert_lineage(conn, agreement_rate=0.70)
        # Keep failure rate clean so other dim doesn't fire
        for _ in range(5):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(window_hours=48, conn=conn)

    assert report.within_slo is False
    assert report.recommendation == "rollback"
    dims = {v["dimension"] for v in report.violations}
    assert "vlm_disagreement_rate" in dims
    vd = next(v for v in report.violations if v["dimension"] == "vlm_disagreement_rate")
    assert vd["actual"] == pytest.approx(0.30, abs=0.001)


def test_boundary_exactly_at_threshold_is_within_slo(temp_db: Path) -> None:
    """Boundary case: failure rate = exactly 5% (=≤ threshold) → within_slo=True.

    Critical for "≤ vs <" semantics: plan §P2.4 says target ≤ 5% so equality
    must NOT trigger rollback (otherwise SLO violates itself constantly).
    """
    with db.connect() as conn:
        # 100 jobs: 5 failed (no pre_render_gate) + 95 done = 5% exactly
        for _ in range(5):
            _insert_simulation_job(conn, status="failed", failure_stage="provider_call")
        for _ in range(95):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(window_hours=48, conn=conn)

    assert report.evidence["comfyui_failure"]["rate"] == pytest.approx(0.05, abs=1e-9)
    fr_violations = [v for v in report.violations if v["dimension"] == "comfyui_failure_rate"]
    assert fr_violations == [], "exactly-at-threshold must be within SLO"


def test_multiple_dimensions_violated_lists_all(temp_db: Path) -> None:
    """Two dims violated → violations list contains both entries."""
    with db.connect() as conn:
        # comfyui: 30 failed / 100 = 30%
        for _ in range(30):
            _insert_simulation_job(conn, status="failed", failure_stage="provider_call")
        for _ in range(70):
            _insert_simulation_job(conn, status="done")
        # vlm: 30 lineage @ agreement=0.50 → 0.50 disagreement
        for _ in range(30):
            _insert_lineage(conn, agreement_rate=0.50)
        conn.commit()

        report = evaluate_window(window_hours=48, conn=conn)

    assert report.within_slo is False
    assert report.recommendation == "rollback"
    dims = {v["dimension"] for v in report.violations}
    assert "comfyui_failure_rate" in dims
    assert "vlm_disagreement_rate" in dims
    assert len(report.violations) >= 2


def test_thresholds_override_via_kwarg(temp_db: Path) -> None:
    """User-supplied thresholds overlay → looser threshold turns violation into pass."""
    with db.connect() as conn:
        # 10% failure rate — violates default 5%
        for _ in range(10):
            _insert_simulation_job(conn, status="failed", failure_stage="provider_call")
        for _ in range(90):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        # Default thresholds → rollback
        default_report = evaluate_window(window_hours=48, conn=conn)
        assert default_report.recommendation == "rollback"

        # Override to 20% → continue
        override = {"comfyui_failure_rate_max": 0.20}
        loose_report = evaluate_window(
            window_hours=48,
            conn=conn,
            thresholds=override,
        )

    assert loose_report.within_slo is True
    assert loose_report.recommendation == "continue"


def test_delivery_gate_rejection_baseline_multiplier(temp_db: Path) -> None:
    """delivery_gate_rejection_rate > baseline * multiplier → violation."""
    with db.connect() as conn:
        # baseline 0.10 * 1.05 = 0.105 → set rejection rate to 0.40 (clearly over)
        # 10 rejected + 15 ok on delivery endpoints = 25 counted total
        for _ in range(10):
            _insert_ops_audit(
                conn,
                endpoint="POST /api/render/ops/batch-rerun",
                outcome="error",
            )
        for _ in range(15):
            _insert_ops_audit(
                conn,
                endpoint="POST /api/render/ops/batch-rerun",
                outcome="ok",
            )
        # Pad sample size with healthy simulation_jobs so min_sample check passes
        for _ in range(10):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(window_hours=48, conn=conn)

    dims = {v["dimension"] for v in report.violations}
    assert "delivery_gate_rejection_rate" in dims, (
        f"expected delivery_gate_rejection_rate in {report.violations}"
    )
    dg = next(v for v in report.violations if v["dimension"] == "delivery_gate_rejection_rate")
    assert dg["actual"] == pytest.approx(0.40, abs=0.01)
    # baseline 0.10 * multiplier 1.05 = 0.105
    assert dg["threshold"] == pytest.approx(0.105, abs=0.001)


def test_pre_render_gate_blocker_count_vs_baseline(temp_db: Path) -> None:
    """pre_render_gate blocker count > baseline * 1.10 (=5*1.1=5.5) → violation
    when 7 such failures present."""
    with db.connect() as conn:
        # 7 failed sims with failure_stage='pre_render_gate'
        for _ in range(7):
            _insert_simulation_job(
                conn,
                status="failed",
                failure_stage="pre_render_gate",
            )
        # Pad sample size with successes so failure_rate not violated AND total >=30
        for _ in range(143):  # 7/(7+143)=4.67% < 5% threshold ✅
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(window_hours=48, conn=conn)

    dims = {v["dimension"] for v in report.violations}
    assert "pre_render_gate_blocker_count" in dims
    pg = next(v for v in report.violations if v["dimension"] == "pre_render_gate_blocker_count")
    assert pg["actual"] == 7
    assert pg["threshold"] == pytest.approx(5.5, abs=0.01)


def test_dry_run_outcomes_excluded_from_rejection_denominator(temp_db: Path) -> None:
    """dry_run outcome should NOT count toward delivery rejection denominator —
    dry_run is a planning call, not a real attempt."""
    with db.connect() as conn:
        # All 50 audits are dry_run on delivery endpoint
        for _ in range(50):
            _insert_ops_audit(
                conn,
                endpoint="POST /api/render/ops/batch-rerun",
                outcome="dry_run",
            )
        # Pad with healthy sim_jobs
        for _ in range(35):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(window_hours=48, conn=conn)

    assert report.evidence["delivery_gate_rejection"]["counted"] == 0
    assert report.evidence["delivery_gate_rejection"]["rate"] == 0.0
    dims = {v["dimension"] for v in report.violations}
    assert "delivery_gate_rejection_rate" not in dims


def test_old_records_outside_window_excluded(temp_db: Path) -> None:
    """Rows older than window_hours must not contribute to sample/violation."""
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=200)).isoformat()
    with db.connect() as conn:
        # 50 old failed jobs → would massively violate; must be excluded
        for _ in range(50):
            _insert_simulation_job(conn, status="failed", when=old_ts)
        conn.commit()

        report = evaluate_window(window_hours=48, conn=conn)

    assert report.evidence["comfyui_failure"]["terminal_total"] == 0
    assert report.sample_size == 0
    assert report.recommendation == "insufficient_data"


def test_malformed_audit_json_skipped_gracefully(temp_db: Path) -> None:
    """Corrupt audit_json / vlm_judge_result_json must not crash; counted as no-info."""
    with db.connect() as conn:
        # Raw insert with malformed json — direct SQL bypasses helpers
        when = _now_iso(-5)
        conn.execute(
            """
            INSERT INTO simulation_jobs (
              status, focus_targets_json, policy_json, model_plan_json,
              input_refs_json, output_refs_json, watermarked, audit_json,
              error_message, can_publish, created_at, updated_at
            )
            VALUES ('failed', '[]', '{}', '{}', '[]', '[]', 0, '{not json',
                    NULL, 0, ?, ?)
            """,
            (when, when),
        )
        conn.execute(
            """
            INSERT INTO candidate_lineage (
              simulation_job_id, case_id, attempt, vlm_judge_result_json, created_at
            )
            VALUES (NULL, NULL, 1, '{not_json', ?)
            """,
            (when,),
        )
        # Padding so we cross min_sample
        for _ in range(40):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(window_hours=48, conn=conn)

    # No crash → success. Malformed lineage skipped (0 sample_count).
    assert report.evidence["vlm_disagreement"]["sample_count"] == 0
    # Malformed audit on failed job still counts toward failure_rate denominator
    # (status='failed' is observable), just no failure_stage match for gate dim.
    assert report.evidence["pre_render_gate_blocker"]["blocker_count"] == 0


def test_load_default_thresholds_file_exists(temp_db: Path) -> None:
    """slo_thresholds.json must load and contain all 4 required threshold keys."""
    config = load_default_thresholds()
    th = config["thresholds"]
    assert "comfyui_failure_rate_max" in th
    assert "vlm_disagreement_rate_max" in th
    assert "delivery_gate_rejection_rate_multiplier_max" in th
    assert "pre_render_gate_blocker_multiplier_max" in th
    bl = config["baseline"]
    assert "delivery_gate_rejection_rate" in bl
    assert "pre_render_gate_blocker_count" in bl
    assert config["minimum_sample_size"] == 30
    assert config["default_window_hours"] == 48


def test_window_hours_must_be_positive(temp_db: Path) -> None:
    with db.connect() as conn:
        with pytest.raises(ValueError):
            evaluate_window(window_hours=0, conn=conn)
        with pytest.raises(ValueError):
            evaluate_window(window_hours=-5, conn=conn)
