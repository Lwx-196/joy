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
    PAUSED_STALE_DAYS,
    RECOMMENDATION_INSUFFICIENT_DATA,
    RECOMMENDATION_MONITORING_PAUSED,
    RECOMMENDATION_ROLLBACK,
    SLOReport,
    evaluate_window,
    load_default_thresholds,
)


# Wave 4 W4-1 (b): the checked-in slo_thresholds.json carries placeholder
# provenance (manual_seed / sample_size=0) which now triggers a
# baseline_unmeasured violation. Tests below that care about specific
# dimensions (failure rate, disagreement, paused state) inject a measured
# provenance overlay to isolate the dimension under test from the placeholder
# signal. This helper is the canonical fresh-provenance overlay.
def _measured_thresholds_override(
    overlay_thresholds: dict | None = None,
) -> dict:
    base: dict = {
        # Structured-override branch is triggered by presence of `thresholds`
        # or `baseline` key in the payload; supply an empty dict so callers
        # that only want a fresh provenance still hit that branch.
        "thresholds": dict(overlay_thresholds or {}),
        "baseline_provenance": {
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "window_hours": 24,
            "sample_size": 100,
            "computed_by": "calibrate_cli",
            "computed_at_main_sha": "wave4-test",
        },
    }
    return base

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
    rejections, no blockers) → recommendation='continue'.

    Inject calibrated-provenance override so the placeholder
    `baseline_unmeasured` violation (Wave 4 W4-1 b) doesn't surface and mask
    the dimension under test here.
    """
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

        report = evaluate_window(
            window_hours=48, conn=conn, thresholds=_measured_thresholds_override()
        )

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

        # Default thresholds → rollback (covers both comfyui_failure_rate and
        # baseline_unmeasured; we only assert the high-level recommendation).
        default_report = evaluate_window(window_hours=48, conn=conn)
        assert default_report.recommendation == "rollback"

        # Override to 20% threshold AND measured provenance → continue
        loose_report = evaluate_window(
            window_hours=48,
            conn=conn,
            thresholds=_measured_thresholds_override(
                overlay_thresholds={"comfyui_failure_rate_max": 0.20}
            ),
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

    # No crash → success. After Wave 3 C-3 the malformed lineage row counts
    # toward `total_rows` (denominator) and `missing_count` (it lacks a usable
    # agreement_rate), but contributes nothing to parsed_count / mean.
    vlm_ev = report.evidence["vlm_disagreement"]
    assert vlm_ev["total_rows"] == 1
    assert vlm_ev["missing_count"] == 1
    assert vlm_ev["parsed_count"] == 0
    assert vlm_ev["mean_disagreement"] == 0.0
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


def test_mixed_timestamp_formats_all_counted_in_window(temp_db: Path) -> None:
    """H-4 hardening (nyquist M-2): SLO cutoff comparison must tolerate
    multiple timestamp formats across writers. Pre-hardening shipped
    lexicographic TEXT compare which mishandles space-separated
    SQLite CURRENT_TIMESTAMP rows mixed with `T+00:00` ISO writers.

    Post-fix uses julianday() on both sides so all of these forms work:
      - '2026-05-27T14:00:00.123456+00:00' (datetime.isoformat tz-aware)
      - '2026-05-27T14:00:00' (naive isoformat)
      - '2026-05-27 14:00:00' (SQLite CURRENT_TIMESTAMP / space-sep)
    """
    base = datetime.now(timezone.utc) - timedelta(hours=1)  # inside 48h window
    aware = base.isoformat()                                # 2026-..T..+00:00
    naive = base.replace(tzinfo=None).isoformat()           # 2026-..T.. (no tz)
    spaced = base.strftime("%Y-%m-%d %H:%M:%S")             # 2026-.. (space sep)

    with db.connect() as conn:
        for ts in (aware, naive, spaced):
            _insert_simulation_job(conn, status="done", when=ts)
        # Pad so we cross min_sample.
        for _ in range(30):
            _insert_simulation_job(conn, status="done")
        conn.commit()
        report = evaluate_window(window_hours=48, conn=conn)

    # All 3 explicit + 30 padding = 33 jobs. Pre-hardening lexicographic
    # compare would drop the spaced + possibly naive rows; terminal_total
    # would be < 33. Post-hardening julianday() compare counts all.
    cfe = report.evidence["comfyui_failure"]
    assert cfe["terminal_total"] == 33, (
        f"Expected 33 terminal jobs (3 mixed-format + 30 pad); "
        f"got {cfe['terminal_total']} — H-4 julianday() regression"
    )


# ---------------------------------------------------------------------------
# Wave 3 C-1 regression — small-sample triage by promotion_state
# ---------------------------------------------------------------------------


def test_c1_small_sample_under_promoted_state_returns_monitoring_paused(
    temp_db: Path,
) -> None:
    """C-1: sample < min_sample AND promotion_state ∈ {p10/p25/p50/p100} →
    recommendation='monitoring_paused', within_slo=None, notes annotates the
    'small_sample_in_promoted_state' condition.

    Pre-fix behavior: returned 'insufficient_data' uniformly regardless of
    state — too aggressive for promoted states (we'd silently disarm the
    auto-rollback gate during the very window operators care most about).
    """
    with db.connect() as conn:
        # Only 5 jobs → way below default min_sample=30
        for _ in range(5):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="p25",
        )

    assert report.sample_size < DEFAULT_MINIMUM_SAMPLE_SIZE
    assert report.recommendation == RECOMMENDATION_MONITORING_PAUSED
    assert report.within_slo is None, "monitoring_paused must signal indeterminate"
    assert report.violations == []
    # Wave 4 W4-2 added `paused_since=...` annotation to the notes field.
    # The leading marker remains stable and is what dashboards key on.
    assert report.notes.startswith("small_sample_in_promoted_state")
    assert "paused_since=" in report.notes
    assert report.evidence["promotion_state"] == "p25"


def test_c1_small_sample_under_shadow_state_returns_insufficient_data(
    temp_db: Path,
) -> None:
    """C-1 regression guard: sample < min_sample + state='shadow' must STILL
    return 'insufficient_data' (legacy cold-start semantics, no promoted
    traffic exists yet)."""
    with db.connect() as conn:
        for _ in range(5):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="shadow",
        )

    assert report.recommendation == RECOMMENDATION_INSUFFICIENT_DATA
    assert report.within_slo is True
    assert report.evidence["promotion_state"] == "shadow"


def test_c1_small_sample_under_rolled_back_state_returns_insufficient_data(
    temp_db: Path,
) -> None:
    """C-1 boundary: 'rolled_back' is NOT a promoted state — small sample
    there must fall back to insufficient_data (no live promoted traffic)."""
    with db.connect() as conn:
        for _ in range(3):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="rolled_back",
        )

    assert report.recommendation == RECOMMENDATION_INSUFFICIENT_DATA
    assert report.within_slo is True


def test_c1_p100_small_sample_returns_monitoring_paused(
    temp_db: Path,
) -> None:
    """C-1: p100 is the highest promoted bucket — small sample there equally
    needs monitoring_paused (same family as p10/p25/p50)."""
    with db.connect() as conn:
        for _ in range(4):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="p100",
        )

    assert report.recommendation == RECOMMENDATION_MONITORING_PAUSED
    assert report.within_slo is None


# ---------------------------------------------------------------------------
# Wave 3 C-3 regression — missing/malformed VLM payload visibility
# ---------------------------------------------------------------------------


def test_c3_missing_rate_violation_when_half_lineage_rows_missing_judge(
    temp_db: Path,
) -> None:
    """C-3: 50% of lineage rows have NULL vlm_judge_result_json → missing_rate
    = 0.50 > 0.10 → vlm_judge_missing_rate violation fires.

    Pre-fix behavior: missing rows were filtered at the SQL layer (`WHERE
    vlm_judge_result_json IS NOT NULL`), making this failure mode invisible.
    Operators saw sample_count=20 and clean disagreement rates while the
    judge was actually broken on half the traffic.
    """
    with db.connect() as conn:
        # 20 lineage rows with valid agreement=0.95 (disagreement 0.05, clean)
        for _ in range(20):
            _insert_lineage(conn, agreement_rate=0.95)
        # 20 lineage rows with NULL vlm_judge_result_json (missing)
        for _ in range(20):
            _insert_lineage(conn, agreement_rate=None)
        # Pad with healthy sim_jobs so total sample_size >= min_sample=30
        for _ in range(20):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(window_hours=48, conn=conn)

    vlm_ev = report.evidence["vlm_disagreement"]
    assert vlm_ev["total_rows"] == 40, "denominator must be FULL lineage row count"
    assert vlm_ev["missing_count"] == 20
    assert vlm_ev["parsed_count"] == 20
    assert vlm_ev["missing_rate"] == pytest.approx(0.50, abs=1e-6)

    dims = {v["dimension"] for v in report.violations}
    assert "vlm_judge_missing_rate" in dims, (
        f"expected vlm_judge_missing_rate violation in {report.violations}"
    )
    mv = next(v for v in report.violations if v["dimension"] == "vlm_judge_missing_rate")
    assert mv["actual"] == pytest.approx(0.50, abs=1e-6)
    assert mv["threshold"] == pytest.approx(0.10, abs=1e-6)
    assert mv["context"]["missing_count"] == 20
    assert mv["context"]["total_rows"] == 40

    # The disagreement dimension itself stays clean (the 20 parsed rows are
    # at 0.05 disagreement, < 0.10 cap).
    assert "vlm_disagreement_rate" not in dims


def test_c3_zero_missing_no_missing_violation_and_full_denominator(
    temp_db: Path,
) -> None:
    """C-3 boundary: 0% missing → vlm_judge_missing_rate violation does NOT
    fire. Disagreement denominator equals total_rows (parsed_count) when no
    payload is dropped, preserving prior arithmetic for the happy path."""
    with db.connect() as conn:
        # 40 lineage rows, all with valid agreement=0.80 → disagreement=0.20
        # (> 0.10 cap, so disagreement_rate WILL fire — useful to prove that
        # full-denominator semantics still triggers correctly)
        for _ in range(40):
            _insert_lineage(conn, agreement_rate=0.80)
        conn.commit()

        report = evaluate_window(window_hours=48, conn=conn)

    vlm_ev = report.evidence["vlm_disagreement"]
    assert vlm_ev["total_rows"] == 40
    assert vlm_ev["missing_count"] == 0
    assert vlm_ev["parsed_count"] == 40
    assert vlm_ev["missing_rate"] == 0.0
    assert vlm_ev["mean_disagreement"] == pytest.approx(0.20, abs=1e-6)

    dims = {v["dimension"] for v in report.violations}
    assert "vlm_judge_missing_rate" not in dims, (
        "0% missing must not trigger missing-rate violation"
    )
    # And the disagreement dimension fires off the full-row denominator.
    assert "vlm_disagreement_rate" in dims


def test_c3_missing_rate_threshold_override_via_kwarg(temp_db: Path) -> None:
    """C-3 wiring: missing_rate_max threshold is overridable via the same
    `thresholds` kwarg surface as other dimensions, so operators can tune
    per-environment without editing the JSON file."""
    with db.connect() as conn:
        # 30 lineage rows: 5 missing + 25 valid → missing_rate ≈ 0.167
        for _ in range(5):
            _insert_lineage(conn, agreement_rate=None)
        for _ in range(25):
            _insert_lineage(conn, agreement_rate=0.95)
        # Pad to cross min_sample
        for _ in range(5):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        # Default threshold 0.10 → 0.167 > 0.10 → violation
        default_report = evaluate_window(window_hours=48, conn=conn)
        default_dims = {v["dimension"] for v in default_report.violations}
        assert "vlm_judge_missing_rate" in default_dims

        # Override to 0.50 → 0.167 < 0.50 → no violation
        loose = evaluate_window(
            window_hours=48,
            conn=conn,
            thresholds={"vlm_judge_missing_rate_max": 0.50},
        )

    loose_dims = {v["dimension"] for v in loose.violations}
    assert "vlm_judge_missing_rate" not in loose_dims


def test_c3_malformed_json_counts_as_missing_not_silently_skipped(
    temp_db: Path,
) -> None:
    """C-3: rows with non-JSON or non-dict payloads count as missing rather
    than being silently dropped from the denominator."""
    when = _now_iso(-5)
    with db.connect() as conn:
        # 10 rows with broken JSON literal
        for i in range(10):
            conn.execute(
                """
                INSERT INTO candidate_lineage (
                  simulation_job_id, case_id, attempt,
                  vlm_judge_result_json, created_at
                )
                VALUES (NULL, NULL, ?, '{not_json', ?)
                """,
                (i + 1, when),
            )
        # 5 rows with valid agreement
        for _ in range(5):
            _insert_lineage(conn, agreement_rate=0.95)
        # Pad
        for _ in range(20):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(window_hours=48, conn=conn)

    vlm_ev = report.evidence["vlm_disagreement"]
    assert vlm_ev["total_rows"] == 15
    assert vlm_ev["missing_count"] == 10, "malformed JSON must count as missing"
    assert vlm_ev["parsed_count"] == 5
    assert vlm_ev["missing_rate"] == pytest.approx(10 / 15, abs=1e-6)

    dims = {v["dimension"] for v in report.violations}
    assert "vlm_judge_missing_rate" in dims


# ---------------------------------------------------------------------------
# Wave 4 W4-1 — production deploy gate (placeholder baseline rejection)
# ---------------------------------------------------------------------------


def _write_thresholds_file(path: Path, provenance: dict) -> Path:
    """Write a minimal but schema-valid thresholds JSON with caller-controlled
    provenance. Used by the W4-1 fail-closed tests."""
    payload = {
        "schema_version": 1,
        "thresholds": {
            "comfyui_failure_rate_max": 0.05,
            "vlm_disagreement_rate_max": 0.10,
            "vlm_judge_missing_rate_max": 0.10,
            "delivery_gate_rejection_rate_multiplier_max": 1.05,
            "pre_render_gate_blocker_multiplier_max": 1.10,
        },
        "baseline": {
            "delivery_gate_rejection_rate": 0.10,
            "pre_render_gate_blocker_count": 5,
        },
        "baseline_provenance": provenance,
        "minimum_sample_size": 30,
        "default_window_hours": 48,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def test_w41_validate_baseline_provenance_rejects_manual_seed_in_prod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production mode (SLO_TEST_MODE unset): a thresholds file whose
    provenance is `computed_by='manual_seed', sample_size=0` must raise
    ValueError on load. This is the release-blocker fail-closed semantics —
    deploy must refuse to come up on a placeholder baseline."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    p = _write_thresholds_file(
        tmp_path / "placeholder.json",
        {
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "window_hours": 24,
            "sample_size": 0,
            "computed_by": "manual_seed",
            "computed_at_main_sha": "abc1234",
        },
    )
    with pytest.raises(ValueError, match=r"placeholder.*manual_seed"):
        load_default_thresholds(p)


def test_w41_validate_baseline_provenance_warns_in_test_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SLO_TEST_MODE=1: same placeholder provenance must NOT raise — it
    must return the dict so downstream `baseline_unmeasured` violation can
    still fire (visibility in test runs)."""
    monkeypatch.setenv("SLO_TEST_MODE", "1")
    p = _write_thresholds_file(
        tmp_path / "placeholder.json",
        {
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "window_hours": 24,
            "sample_size": 0,
            "computed_by": "manual_seed",
            "computed_at_main_sha": "abc1234",
        },
    )
    with caplog.at_level("WARNING", logger="backend.services.promotion_slo_monitor"):
        result = load_default_thresholds(p)
    # Provenance preserved in result so evaluate_window can surface it.
    assert result["baseline_provenance"]["computed_by"] == "manual_seed"
    # Warning recorded for operator visibility.
    assert any("placeholder" in rec.getMessage() for rec in caplog.records), (
        f"expected placeholder warning, got {caplog.text!r}"
    )


def test_w41_validate_baseline_provenance_rejects_seed_label_in_prod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The placeholder set covers more than `manual_seed` — `seed` and
    `placeholder` labels are equally release-blockers."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    for label in ("seed", "placeholder"):
        p = _write_thresholds_file(
            tmp_path / f"{label}.json",
            {
                "measured_at": datetime.now(timezone.utc).isoformat(),
                "window_hours": 24,
                "sample_size": 0,
                "computed_by": label,
                "computed_at_main_sha": "abc1234",
            },
        )
        with pytest.raises(ValueError, match=r"placeholder"):
            load_default_thresholds(p)


def test_w41_validate_provenance_accepts_manual_seed_with_real_sample_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary: `computed_by='manual_seed'` is benign once sample_size >= 1.
    Threshold is `sample_size < 1` so a measured-but-manually-labeled
    baseline still loads. This guards against false-positive rejection of
    edge cases where an operator manually copied a calibration result."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    p = _write_thresholds_file(
        tmp_path / "manual_but_real.json",
        {
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "window_hours": 24,
            "sample_size": 250,  # real measurement
            "computed_by": "manual_seed",
            "computed_at_main_sha": "abc1234",
        },
    )
    config = load_default_thresholds(p)
    assert config["baseline_provenance"]["sample_size"] == 250


def test_w41_evaluate_window_emits_baseline_unmeasured_violation_for_placeholder(
    temp_db: Path,
) -> None:
    """End-to-end W4-1 (b): placeholder provenance in loaded thresholds →
    `evaluate_window` emits `baseline_unmeasured` violation in the report
    even in test mode (where (a) only warned). Operators MUST see this
    signal regardless of env."""
    with db.connect() as conn:
        # Push sample size above min_sample so we reach the violation collection
        # path (paused / insufficient_data short-circuits skip this).
        for _ in range(40):
            _insert_simulation_job(conn, status="done")
        conn.commit()
        # Default thresholds file has placeholder provenance (manual_seed).
        # No `thresholds` override → real file load path.
        report = evaluate_window(window_hours=48, conn=conn)

    dims = {v["dimension"] for v in report.violations}
    assert "baseline_unmeasured" in dims, (
        f"expected baseline_unmeasured violation, got {report.violations!r}"
    )
    bu = next(v for v in report.violations if v["dimension"] == "baseline_unmeasured")
    assert bu["context"]["computed_by"] == "manual_seed"
    assert bu["context"]["sample_size"] == 0
    assert "calibrate_slo_baseline" in bu["context"]["hint"]
    assert report.within_slo is False
    assert report.recommendation == RECOMMENDATION_ROLLBACK


def test_w41_evaluate_window_no_baseline_unmeasured_with_calibrated_provenance(
    temp_db: Path,
) -> None:
    """Counterpoint: when provenance is `calibrate_cli` and sample_size > 0,
    `baseline_unmeasured` must NOT fire. Healthy traffic + calibrated
    baseline → continue."""
    with db.connect() as conn:
        for _ in range(40):
            _insert_simulation_job(conn, status="done")
        conn.commit()
        report = evaluate_window(
            window_hours=48,
            conn=conn,
            thresholds=_measured_thresholds_override(),
        )

    dims = {v["dimension"] for v in report.violations}
    assert "baseline_unmeasured" not in dims, (
        f"calibrated provenance must not fire baseline_unmeasured, got {report.violations!r}"
    )
    assert report.within_slo is True
    assert report.recommendation == "continue"


def test_w41_evaluate_window_emits_baseline_unmeasured_for_empty_provenance(
    temp_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """If a thresholds file lacks a `baseline_provenance` block entirely,
    `load_default_thresholds` returns provenance={} in test mode. The
    `baseline_unmeasured` violation must still fire (empty provenance is
    just as unsafe as placeholder provenance)."""
    monkeypatch.setenv("SLO_TEST_MODE", "1")
    # Build a thresholds file with no baseline_provenance block at all.
    path = tmp_path / "no_prov.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "thresholds": {
                    "comfyui_failure_rate_max": 0.05,
                    "vlm_disagreement_rate_max": 0.10,
                    "vlm_judge_missing_rate_max": 0.10,
                    "delivery_gate_rejection_rate_multiplier_max": 1.05,
                    "pre_render_gate_blocker_multiplier_max": 1.10,
                },
                "baseline": {
                    "delivery_gate_rejection_rate": 0.10,
                    "pre_render_gate_blocker_count": 5,
                },
                "minimum_sample_size": 30,
                "default_window_hours": 48,
            }
        ),
        encoding="utf-8",
    )
    config = load_default_thresholds(path)
    assert config["baseline_provenance"] == {}

    with db.connect() as conn:
        for _ in range(40):
            _insert_simulation_job(conn, status="done")
        conn.commit()
        report = evaluate_window(window_hours=48, conn=conn, thresholds=config)

    dims = {v["dimension"] for v in report.violations}
    assert "baseline_unmeasured" in dims


# ---------------------------------------------------------------------------
# Wave 4 W4-2 — monitoring_paused stop-loss (sidecar state file)
# ---------------------------------------------------------------------------


def test_w42_paused_state_first_pause_writes_paused_since(
    temp_db: Path,
    tmp_path: Path,
) -> None:
    """First time a promoted state hits small-sample, the sidecar must be
    written with paused_since=now and a non-stale recommendation."""
    sidecar = tmp_path / "paused_state.json"

    with db.connect() as conn:
        for _ in range(5):  # below min_sample=30
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="p25",
            paused_state_path=sidecar,
        )

    assert report.recommendation == RECOMMENDATION_MONITORING_PAUSED
    assert report.within_slo is None
    assert sidecar.exists(), "first pause must write sidecar"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["promotion_state_at_pause"] == "p25"
    assert payload["last_sample_size"] == report.sample_size
    # paused_since is ISO8601 with timezone — parseable
    parsed = datetime.fromisoformat(payload["paused_since"])
    assert parsed.tzinfo is not None


def test_w42_paused_state_stale_exceeds_7d_emits_violation_and_rollback(
    temp_db: Path,
    tmp_path: Path,
) -> None:
    """Sidecar with paused_since older than PAUSED_STALE_DAYS days → next
    eval emits `monitoring_paused_stale` violation, escalates recommendation
    to `rollback`, within_slo=False. This is the stop-loss release gate."""
    sidecar = tmp_path / "paused_state.json"
    eight_days_ago = (
        datetime.now(timezone.utc) - timedelta(days=PAUSED_STALE_DAYS + 1)
    ).isoformat()
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "paused_since": eight_days_ago,
                "promotion_state_at_pause": "p10",
                "last_sample_size": 7,
                "minimum_sample_size": 30,
            }
        ),
        encoding="utf-8",
    )

    with db.connect() as conn:
        for _ in range(5):  # still small sample
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="p10",
            paused_state_path=sidecar,
        )

    assert report.recommendation == RECOMMENDATION_ROLLBACK
    assert report.within_slo is False
    dims = {v["dimension"] for v in report.violations}
    assert "monitoring_paused_stale" in dims
    mps = next(
        v for v in report.violations if v["dimension"] == "monitoring_paused_stale"
    )
    assert mps["threshold_days"] == PAUSED_STALE_DAYS
    assert mps["actual_days"] > PAUSED_STALE_DAYS
    assert mps["context"]["promotion_state"] == "p10"
    assert mps["context"]["paused_since"] == eight_days_ago
    assert "paused_since=" in report.notes
    assert "paused_duration_days=" in report.notes


def test_w42_paused_state_within_7d_stays_paused_no_violation(
    temp_db: Path,
    tmp_path: Path,
) -> None:
    """Sidecar with paused_since 3 days ago → stop-loss window not yet
    reached → still monitoring_paused, no stale violation, within_slo=None."""
    sidecar = tmp_path / "paused_state.json"
    three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "paused_since": three_days_ago,
                "promotion_state_at_pause": "p25",
                "last_sample_size": 9,
                "minimum_sample_size": 30,
            }
        ),
        encoding="utf-8",
    )

    with db.connect() as conn:
        for _ in range(6):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="p25",
            paused_state_path=sidecar,
        )

    assert report.recommendation == RECOMMENDATION_MONITORING_PAUSED
    assert report.within_slo is None
    assert report.violations == []
    # paused_since anchor preserved, last_sample_size refreshed
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["paused_since"] == three_days_ago
    assert payload["last_sample_size"] == report.sample_size


def test_w42_paused_state_promotion_transition_resets_paused_since(
    temp_db: Path,
    tmp_path: Path,
) -> None:
    """If the promoted state transitions (e.g. operator goes p10 → p25),
    the stop-loss clock must reset — we only stop-loss when a single state
    is stuck, not across legitimate progression."""
    sidecar = tmp_path / "paused_state.json"
    five_days_ago = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "paused_since": five_days_ago,
                "promotion_state_at_pause": "p10",
                "last_sample_size": 4,
                "minimum_sample_size": 30,
            }
        ),
        encoding="utf-8",
    )

    with db.connect() as conn:
        for _ in range(7):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="p25",  # transitioned
            paused_state_path=sidecar,
        )

    assert report.recommendation == RECOMMENDATION_MONITORING_PAUSED
    assert report.within_slo is None
    assert report.violations == []
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["promotion_state_at_pause"] == "p25"
    # new paused_since within last minute (reset to now)
    new_since = datetime.fromisoformat(payload["paused_since"])
    age = datetime.now(timezone.utc) - new_since
    assert age < timedelta(minutes=1)
    assert "paused_state_reset" in report.notes


def test_w42_paused_state_cleared_when_sample_recovers(
    temp_db: Path,
    tmp_path: Path,
) -> None:
    """When sample crosses min_sample again, the sidecar must be deleted so
    a future re-paused episode starts a fresh stop-loss clock (no leftover
    paused_since=days-ago)."""
    sidecar = tmp_path / "paused_state.json"
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "paused_since": datetime.now(timezone.utc).isoformat(),
                "promotion_state_at_pause": "p10",
                "last_sample_size": 5,
                "minimum_sample_size": 30,
            }
        ),
        encoding="utf-8",
    )
    assert sidecar.exists()

    with db.connect() as conn:
        # 40 done jobs → sample size >= min_sample → recovery path
        for _ in range(40):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="p10",
            paused_state_path=sidecar,
            thresholds=_measured_thresholds_override(),
        )

    assert report.sample_size >= DEFAULT_MINIMUM_SAMPLE_SIZE
    assert report.recommendation == "continue"
    assert not sidecar.exists(), (
        "sidecar must be removed when sample recovers — leftover state would "
        "carry stale paused_since into the next paused episode"
    )


def test_w42_paused_state_cleared_on_demotion_to_shadow(
    temp_db: Path,
    tmp_path: Path,
) -> None:
    """Sidecar exists with p10 paused state; operator demotes to shadow.
    Next eval (small sample, non-promoted state) must clear the sidecar so
    a future re-promotion starts fresh."""
    sidecar = tmp_path / "paused_state.json"
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "paused_since": datetime.now(timezone.utc).isoformat(),
                "promotion_state_at_pause": "p10",
                "last_sample_size": 3,
                "minimum_sample_size": 30,
            }
        ),
        encoding="utf-8",
    )
    assert sidecar.exists()

    with db.connect() as conn:
        for _ in range(3):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="shadow",
            paused_state_path=sidecar,
        )

    assert report.recommendation == RECOMMENDATION_INSUFFICIENT_DATA
    assert not sidecar.exists(), (
        "sidecar must be cleared when state demotes off the promoted set"
    )


def test_w42_paused_state_corrupt_sidecar_is_repaired(
    temp_db: Path,
    tmp_path: Path,
) -> None:
    """Corrupt JSON in the sidecar must not crash the SLO loop — the
    function repairs it with a fresh paused_since on the next eval. This
    keeps the stop-loss circuit resilient to disk corruption / unfinished
    writes from prior runs."""
    sidecar = tmp_path / "paused_state.json"
    sidecar.write_text("{not valid json", encoding="utf-8")

    with db.connect() as conn:
        for _ in range(4):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        report = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="p50",
            paused_state_path=sidecar,
        )

    assert report.recommendation == RECOMMENDATION_MONITORING_PAUSED
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["promotion_state_at_pause"] == "p50"
    # paused_since rewritten to a parseable timestamp
    datetime.fromisoformat(payload["paused_since"])
