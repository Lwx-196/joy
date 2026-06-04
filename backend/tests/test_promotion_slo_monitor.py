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
    RECOMMENDATION_STOP_LOSS_HALT,
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
    overlay_baseline: dict | None = None,
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
    if overlay_baseline is not None:
        base["baseline"] = dict(overlay_baseline)
    return base


def _placeholder_thresholds_override() -> dict:
    """W4-1: mimic the legacy hand-seeded thresholds shape (manual_seed +
    sample_size=0) so tests that explicitly verify the W4-1 placeholder gate
    keep working after the live `slo_thresholds.json` was calibrated."""
    return {
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
        "baseline_provenance": {
            "measured_at": "2026-05-28T00:00:00+00:00",
            "window_hours": 24,
            "sample_size": 0,
            "computed_by": "manual_seed",
            "computed_at_main_sha": "wave4-test",
        },
    }

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

        # Inject 5% threshold via override so test asserts code behavior
        # independent of whatever the live slo_thresholds.json calibrated to.
        report = evaluate_window(
            window_hours=48,
            conn=conn,
            thresholds=_measured_thresholds_override(
                overlay_thresholds={"comfyui_failure_rate_max": 0.05}
            ),
        )

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
        # 10% failure rate — violates 5% baseline threshold
        for _ in range(10):
            _insert_simulation_job(conn, status="failed", failure_stage="provider_call")
        for _ in range(90):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        # Strict thresholds (5% + placeholder provenance) → rollback (covers
        # both comfyui_failure_rate and baseline_unmeasured; we only assert
        # the high-level recommendation). Inject placeholder explicitly so
        # the test is independent of the live calibrated thresholds file.
        default_report = evaluate_window(
            window_hours=48, conn=conn, thresholds=_placeholder_thresholds_override()
        )
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

        # Inject explicit baseline (0.10) + multiplier (1.05) so the threshold
        # arithmetic is independent of the live calibrated baselines file.
        report = evaluate_window(
            window_hours=48,
            conn=conn,
            thresholds=_measured_thresholds_override(
                overlay_thresholds={"delivery_gate_rejection_rate_multiplier_max": 1.05},
                overlay_baseline={
                    "delivery_gate_rejection_rate": 0.10,
                    "pre_render_gate_blocker_count": 5,
                },
            ),
        )

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

        # Inject explicit baseline (5) + multiplier (1.10) + comfyui threshold
        # 0.05 (so 4.67% fails not, 7 blockers does) — independent of live file.
        report = evaluate_window(
            window_hours=48,
            conn=conn,
            thresholds=_measured_thresholds_override(
                overlay_thresholds={
                    "comfyui_failure_rate_max": 0.05,
                    "pre_render_gate_blocker_multiplier_max": 1.10,
                },
                overlay_baseline={
                    "delivery_gate_rejection_rate": 0.10,
                    "pre_render_gate_blocker_count": 5,
                },
            ),
        )

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
        # Inject placeholder (manual_seed + sample_size=0) provenance explicitly
        # — independent of whatever the live slo_thresholds.json has been
        # calibrated to. The test asserts the W4-1 (b) violation-emit path,
        # not the live file content.
        report = evaluate_window(
            window_hours=48,
            conn=conn,
            thresholds=_placeholder_thresholds_override(),
        )

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
    to ``stop_loss_halt`` (K-5: NOT ``rollback`` — manifest stays as-is, only
    audit alert), within_slo=False. This is the stop-loss release gate."""
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

    # K-5: stale paused window now escalates to STOP_LOSS_HALT (audit
    # alert) rather than ROLLBACK (real manifest mutation).
    assert report.recommendation == RECOMMENDATION_STOP_LOSS_HALT
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


# ---------------------------------------------------------------------------
# Wave 4 hardening — K-1..K-8 (single hardening commit)
# ---------------------------------------------------------------------------


# K-1 — sidecar concurrency lock --------------------------------------------


def test_w42_paused_state_lock_prevents_concurrent_overwrite(
    temp_db: Path,
    tmp_path: Path,
) -> None:
    """K-1: with two evaluators racing to land a fresh paused anchor,
    `_paused_state_lock` must serialize the writes so the winner's
    paused_since is preserved (not overwritten by a later starter that
    arrives slightly after) and the loser observes 'lock_contention'
    in its notes path.

    We can't easily fork real processes inside pytest, so we simulate the
    contention by holding the lock manually and then invoking
    `_evaluate_paused_state` — the inner call must observe contention
    and bail without writing.
    """
    import fcntl as _fcntl
    import os as _os

    from backend.services import promotion_slo_monitor as psm

    sidecar = tmp_path / "paused_state.json"
    lock_path = tmp_path / "paused_state.json.lock"

    # Pre-hold the lock by opening a separate fd and acquiring LOCK_EX.
    holder_fd = _os.open(str(lock_path), _os.O_RDWR | _os.O_CREAT, 0o644)
    _fcntl.flock(holder_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)

    try:
        violation, notes = psm._evaluate_paused_state(
            promotion_state="p10",
            sample_size=5,
            min_sample=30,
            paused_state_path=sidecar,
        )
    finally:
        _fcntl.flock(holder_fd, _fcntl.LOCK_UN)
        _os.close(holder_fd)

    # Loser must not have written the sidecar (lock holder owns the write).
    assert not sidecar.exists(), (
        "loser must NOT write sidecar — contention should be silent + no-op"
    )
    assert violation is None
    assert notes == "paused_state_lock_contention"


def test_w42_paused_state_lock_failure_does_not_break_eval(
    temp_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K-1: even if lock acquisition raises BlockingIOError (someone else
    has it), evaluate_window must still return a valid SLOReport.
    The lock skipping path is logged but never crashes the SLO loop —
    that would defeat the entire monitoring purpose.
    """
    import fcntl as _fcntl

    sidecar = tmp_path / "paused_state.json"

    def _always_blocking(fd, op):
        if op & _fcntl.LOCK_NB:
            raise BlockingIOError("simulated contention")

    monkeypatch.setattr(_fcntl, "flock", _always_blocking)

    with db.connect() as conn:
        for _ in range(5):
            _insert_simulation_job(conn, status="done")
        conn.commit()
        report = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="p10",
            paused_state_path=sidecar,
        )

    # SLO loop must produce a report despite lock contention.
    assert isinstance(report, SLOReport)
    assert report.recommendation == RECOMMENDATION_MONITORING_PAUSED


def test_w42_paused_state_write_uses_unique_tmp_filename(
    temp_db: Path,
    tmp_path: Path,
) -> None:
    """K-1: tmp filename must embed pid+uuid so concurrent writes don't
    collide on the canonical `.tmp` name (which the pre-K-1 code used)."""
    from backend.services import promotion_slo_monitor as psm

    sidecar = tmp_path / "paused_state.json"
    psm._write_paused_state(
        {"schema_version": 1, "paused_since": "2026-01-01T00:00:00+00:00"},
        sidecar,
    )
    assert sidecar.exists()
    # No fixed `.tmp` left over (write succeeded; tmp was renamed).
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == [], (
        f"K-1: tmp must be renamed atomically, not orphaned; got {leftover!r}"
    )


# K-2 — _merge_thresholds fallback guard ------------------------------------


def test_w41_prod_merge_thresholds_no_fallback_when_unsafe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K-2: production mode + on-disk thresholds rejected (W4-1 placeholder)
    + user override has NO baseline_provenance → must re-raise the original
    ValueError. Pre-K-2 ANY user override would trigger code-default
    fallback, silently bypassing W4-1."""
    from backend.services import promotion_slo_monitor as psm

    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    # Point file path at a placeholder-provenance file so load fails.
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
    monkeypatch.setattr(psm, "_DEFAULT_THRESHOLDS_FILE", p)

    # Flat override (no provenance) — must NOT trigger fallback.
    with pytest.raises(ValueError, match=r"placeholder"):
        psm._merge_thresholds({"comfyui_failure_rate_max": 0.99})

    # Structured override missing provenance — also must NOT fallback.
    with pytest.raises(ValueError, match=r"placeholder"):
        psm._merge_thresholds({"thresholds": {"comfyui_failure_rate_max": 0.99}})


def test_w41_test_mode_merge_thresholds_allows_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K-2: in test mode the W4-1 file rejection is logged (not raised),
    so _merge_thresholds returns the code defaults + flat overlay
    correctly. This is the developer / CI escape hatch."""
    from backend.services import promotion_slo_monitor as psm

    monkeypatch.setenv("SLO_TEST_MODE", "1")
    config = psm._merge_thresholds({"comfyui_failure_rate_max": 0.42})
    assert config["thresholds"]["comfyui_failure_rate_max"] == 0.42


def test_w41_prod_merge_thresholds_allows_fallback_with_valid_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K-2: prod mode + user override with valid calibrate_cli provenance
    must allow fallback (caller is taking explicit responsibility for the
    measured baseline). This is the calibrate CLI's sanity-load path."""
    from backend.services import promotion_slo_monitor as psm

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
    monkeypatch.setattr(psm, "_DEFAULT_THRESHOLDS_FILE", p)

    config = psm._merge_thresholds(
        {
            "thresholds": {"comfyui_failure_rate_max": 0.07},
            "baseline_provenance": {
                "measured_at": datetime.now(timezone.utc).isoformat(),
                "window_hours": 48,
                "sample_size": 200,
                "computed_by": "calibrate_cli",
                "computed_at_main_sha": "xyz9876",
            },
        }
    )
    assert config["thresholds"]["comfyui_failure_rate_max"] == 0.07


# K-3 — SLO_TEST_MODE whitelist ----------------------------------------------


@pytest.mark.parametrize(
    "raw_value,expected",
    [
        # Truthy literals (whitelist)
        ("1", True),
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("yes", True),
        ("Yes", True),
        ("YES", True),
        # Falsy / non-truthy (production mode)
        ("", False),
        ("0", False),
        ("false", False),
        ("False", False),
        ("production", False),
        ("off", False),
        ("disable", False),
        ("2", False),
        ("on", False),
        ("  ", False),
        ("y", False),  # not in whitelist (yes vs y)
        ("YeS", False),  # case must match whitelist exactly
    ],
)
def test_w41_k3_is_test_mode_whitelist(
    monkeypatch: pytest.MonkeyPatch, raw_value: str, expected: bool
) -> None:
    """K-3: strict allowlist — only canonical truthy literals enable
    test-mode. Any other value (incl. 'production', 'off', '2', 'on', 'y',
    'YeS') is treated as production."""
    from backend.services import promotion_slo_monitor as psm

    monkeypatch.setenv("SLO_TEST_MODE", raw_value)
    assert psm._is_test_mode() is expected, (
        f"K-3: SLO_TEST_MODE={raw_value!r} should be {expected!r}"
    )


def test_w41_k3_prod_path_full_stack_rejects_placeholder(
    temp_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """K-3 integration: with SLO_TEST_MODE unset and a real on-disk
    thresholds JSON containing placeholder provenance, evaluate_window
    must raise ValueError citing calibrate_slo_baseline. This is the
    full-stack production-path coverage that conftest's session-wide
    setdefault otherwise hides."""
    from backend.services import promotion_slo_monitor as psm

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
    monkeypatch.setattr(psm, "_DEFAULT_THRESHOLDS_FILE", p)

    with db.connect() as conn:
        for _ in range(40):
            _insert_simulation_job(conn, status="done")
        conn.commit()
        with pytest.raises(ValueError, match=r"calibrate_slo_baseline"):
            evaluate_window(window_hours=48, conn=conn)


def test_w41_k3_prod_path_full_stack_accepts_calibrated(
    temp_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """K-3 integration counterpoint: prod mode + calibrate_cli provenance
    must produce a valid SLOReport (no raise, no baseline_unmeasured)."""
    from backend.services import promotion_slo_monitor as psm

    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    p = _write_thresholds_file(
        tmp_path / "calibrated.json",
        {
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "window_hours": 24,
            "sample_size": 200,
            "computed_by": "calibrate_cli",
            "computed_at_main_sha": "xyz9876",
        },
    )
    monkeypatch.setattr(psm, "_DEFAULT_THRESHOLDS_FILE", p)

    with db.connect() as conn:
        for _ in range(40):
            _insert_simulation_job(conn, status="done")
        conn.commit()
        report = evaluate_window(window_hours=48, conn=conn)

    assert isinstance(report, SLOReport)
    dims = {v["dimension"] for v in report.violations}
    assert "baseline_unmeasured" not in dims
    assert report.recommendation == "continue"


# K-4 — producer allowlist + baseline_undersampled --------------------------


def test_k4_validate_rejects_unknown_computed_by(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K-4: producer allowlist — `computed_by='ops_seed'` (not in
    placeholder set, not in legitimate set) must be rejected in prod
    mode. Pre-K-4 it would silently load."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    p = _write_thresholds_file(
        tmp_path / "unknown_producer.json",
        {
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "window_hours": 24,
            "sample_size": 500,  # plenty of samples; not a placeholder
            "computed_by": "ops_seed",  # unknown producer
            "computed_at_main_sha": "abc1234",
        },
    )
    with pytest.raises(ValueError, match=r"unknown computed_by"):
        load_default_thresholds(p)


def test_k4_validate_accepts_calibrate_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K-4 boundary: the canonical legitimate producer must still load
    cleanly (no false-positive rejection on the happy path)."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    p = _write_thresholds_file(
        tmp_path / "legit.json",
        {
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "window_hours": 24,
            "sample_size": 500,
            "computed_by": "calibrate_cli",
            "computed_at_main_sha": "abc1234",
        },
    )
    config = load_default_thresholds(p)
    assert config["baseline_provenance"]["computed_by"] == "calibrate_cli"


def test_k4_evaluate_window_emits_baseline_undersampled(
    temp_db: Path,
) -> None:
    """K-4: legitimate (calibrate_cli) provenance but sample_size below
    minimum_sample_size → `baseline_undersampled` violation surfaces. This
    is parallel to baseline_unmeasured but distinct cause (calibration
    actually ran, just on too few rows)."""
    with db.connect() as conn:
        for _ in range(40):
            _insert_simulation_job(conn, status="done")
        conn.commit()
        report = evaluate_window(
            window_hours=48,
            conn=conn,
            thresholds={
                "thresholds": {},
                "baseline_provenance": {
                    "measured_at": datetime.now(timezone.utc).isoformat(),
                    "window_hours": 24,
                    "sample_size": 5,  # below default min_sample=30
                    "computed_by": "calibrate_cli",
                    "computed_at_main_sha": "wave4-test",
                },
            },
        )

    dims = {v["dimension"] for v in report.violations}
    assert "baseline_undersampled" in dims, (
        f"expected baseline_undersampled, got {report.violations!r}"
    )
    bu = next(
        v for v in report.violations if v["dimension"] == "baseline_undersampled"
    )
    assert bu["context"]["sample_size"] == 5
    assert bu["context"]["minimum_sample_size"] == DEFAULT_MINIMUM_SAMPLE_SIZE


def test_k4_evaluate_window_no_undersampled_with_adequate_sample(
    temp_db: Path,
) -> None:
    """K-4: when calibration sample meets/exceeds min_sample, the
    undersampled violation must NOT fire (true negative case)."""
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
    assert "baseline_undersampled" not in dims


# K-5 — RECOMMENDATION_STOP_LOSS_HALT semantics -----------------------------


def test_k5_stale_escalation_uses_stop_loss_halt(
    temp_db: Path,
    tmp_path: Path,
) -> None:
    """K-5: paused window > stale_days under same state → recommendation
    is STOP_LOSS_HALT (NOT ROLLBACK). within_slo=False for actionability,
    but the applier must NOT mutate the manifest on this token."""
    sidecar = tmp_path / "paused_state.json"
    nine_days_ago = (
        datetime.now(timezone.utc) - timedelta(days=PAUSED_STALE_DAYS + 2)
    ).isoformat()
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "paused_since": nine_days_ago,
                "promotion_state_at_pause": "p25",
                "last_sample_size": 4,
                "minimum_sample_size": 30,
            }
        ),
        encoding="utf-8",
    )

    with db.connect() as conn:
        for _ in range(4):
            _insert_simulation_job(conn, status="done")
        conn.commit()
        report = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="p25",
            paused_state_path=sidecar,
        )

    assert report.recommendation == RECOMMENDATION_STOP_LOSS_HALT
    assert report.within_slo is False
    # The monitoring_paused_stale violation still surfaces (operator visibility).
    dims = {v["dimension"] for v in report.violations}
    assert "monitoring_paused_stale" in dims


# K-6 — _load_paused_state tri-state ----------------------------------------


def test_k6_paused_state_missing_returns_missing_status(
    tmp_path: Path,
) -> None:
    """K-6: nonexistent file → ('missing') status, not 'unreadable'."""
    from backend.services import promotion_slo_monitor as psm

    payload, status = psm._load_paused_state(tmp_path / "does_not_exist.json")
    assert payload is None
    assert status == psm.PAUSED_STATE_STATUS_MISSING


def test_k6_paused_state_corrupt_returns_corrupt_status(
    tmp_path: Path,
) -> None:
    """K-6: half-written JSON → ('corrupt') status; caller writes fresh."""
    from backend.services import promotion_slo_monitor as psm

    sidecar = tmp_path / "paused_state.json"
    sidecar.write_text("{not valid json", encoding="utf-8")
    payload, status = psm._load_paused_state(sidecar)
    assert payload is None
    assert status == psm.PAUSED_STATE_STATUS_CORRUPT


def test_k6_paused_state_non_dict_root_returns_corrupt(
    tmp_path: Path,
) -> None:
    """K-6: valid JSON but root is array, not object → corrupt (we expect
    a dict payload schema)."""
    from backend.services import promotion_slo_monitor as psm

    sidecar = tmp_path / "paused_state.json"
    sidecar.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    payload, status = psm._load_paused_state(sidecar)
    assert payload is None
    assert status == psm.PAUSED_STATE_STATUS_CORRUPT


def test_k6_paused_state_oserror_returns_unreadable_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K-6: OSError on read → ('unreadable'); does NOT collapse to
    ('missing'). Pre-K-6 a permission flip would silently reset the
    stop-loss clock by being treated as 'first paused episode'."""
    from backend.services import promotion_slo_monitor as psm

    sidecar = tmp_path / "paused_state.json"
    sidecar.write_text("{}", encoding="utf-8")  # exists, but read will fail

    def _raise_oserror(self, *args, **kwargs):
        raise OSError("simulated permission denied")

    monkeypatch.setattr(Path, "read_text", _raise_oserror)
    payload, status = psm._load_paused_state(sidecar)
    assert payload is None
    assert status == psm.PAUSED_STATE_STATUS_UNREADABLE


def test_k6_evaluate_window_unreadable_sidecar_emits_violation(
    temp_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K-6 integration: when sidecar is unreadable during evaluate_window,
    the loop must emit a `paused_state_unreadable` violation rather than
    silently resetting the stop-loss clock to now."""

    sidecar = tmp_path / "paused_state.json"
    sidecar.write_text("{}", encoding="utf-8")

    original_read_text = Path.read_text

    def _selective_oserror(self, *args, **kwargs):
        # Only fail on sidecar reads; allow other Path.read_text to proceed
        # (e.g. thresholds JSON reads in load_default_thresholds).
        if str(self) == str(sidecar):
            raise OSError("simulated permission denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _selective_oserror)

    with db.connect() as conn:
        for _ in range(4):
            _insert_simulation_job(conn, status="done")
        conn.commit()
        report = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="p10",
            paused_state_path=sidecar,
        )

    dims = {v["dimension"] for v in report.violations}
    assert "paused_state_unreadable" in dims, (
        f"K-6: expected paused_state_unreadable violation, got "
        f"{report.violations!r}"
    )
    # Conservative: surface as stop-loss halt (audit alert) rather than
    # silently green-lighting.
    assert report.recommendation == RECOMMENDATION_STOP_LOSS_HALT


# ---------------------------------------------------------------------------
# Wave 5 followup #1 — paused_stale_days configurable (eval-M1)
# ---------------------------------------------------------------------------
#
# The Wave 4 W4-2 stop-loss window (`PAUSED_STALE_DAYS = 7`) was a module
# constant requiring code edit + redeploy for any operator tuning. Wave 5
# follow-up #1 promotes it to a top-level field in
# `case-workbench-ai/promotion/slo_thresholds.json` so operators can drop the
# 7-day window to 3d (e.g. mid-incident "we need faster stop-loss") without
# touching code. The module constant remains as the fallback default for
# back-compat + import-time semantics.


def test_paused_stale_days_default_from_json(tmp_path: Path) -> None:
    """Wave 5 #1: the checked-in slo_thresholds.json now carries
    `paused_stale_days: 7`. load_default_thresholds must surface it on the
    returned dict so callers see the JSON-driven value (not the module
    constant) by default."""
    from backend.services.promotion_slo_monitor import load_default_thresholds

    config = load_default_thresholds()  # default path → on-disk JSON
    assert "paused_stale_days" in config
    assert config["paused_stale_days"] == 7
    assert isinstance(config["paused_stale_days"], int)


def test_paused_stale_days_overridable_via_thresholds_kwarg(
    temp_db: Path,
    tmp_path: Path,
) -> None:
    """Wave 5 #1: operator can shrink the stop-loss window to 3 days via the
    structured `thresholds` kwarg (mirrors what JSON edit would do). With a
    paused_since 4 days ago + paused_stale_days=3, we must escalate to
    STOP_LOSS_HALT; the same fixture under the legacy 7-day default would
    still be `monitoring_paused`."""
    sidecar = tmp_path / "paused_state.json"
    four_days_ago = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "paused_since": four_days_ago,
                "promotion_state_at_pause": "p10",
                "last_sample_size": 5,
                "minimum_sample_size": 30,
            }
        ),
        encoding="utf-8",
    )

    with db.connect() as conn:
        for _ in range(5):  # below min_sample=30
            _insert_simulation_job(conn, status="done")
        conn.commit()

        # Override to 3 days — paused_since=4d ago now exceeds the window.
        override_3d = _measured_thresholds_override()
        override_3d["paused_stale_days"] = 3
        report_3d = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="p10",
            paused_state_path=sidecar,
            thresholds=override_3d,
        )

    assert report_3d.recommendation == RECOMMENDATION_STOP_LOSS_HALT, (
        f"with paused_stale_days=3 + paused_since 4d ago, expected "
        f"STOP_LOSS_HALT; got {report_3d.recommendation}"
    )
    dims_3d = {v["dimension"] for v in report_3d.violations}
    assert "monitoring_paused_stale" in dims_3d
    stale_v = next(
        v for v in report_3d.violations if v["dimension"] == "monitoring_paused_stale"
    )
    assert stale_v["threshold_days"] == 3, (
        "violation must echo the operator-supplied stale_days, not the "
        "module default 7"
    )

    # Same fixture, different paused_since 1d ago + paused_stale_days=3 →
    # still within window → monitoring_paused (no stop-loss).
    one_day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "paused_since": one_day_ago,
                "promotion_state_at_pause": "p10",
                "last_sample_size": 5,
                "minimum_sample_size": 30,
            }
        ),
        encoding="utf-8",
    )

    with db.connect() as conn:
        report_within = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="p10",
            paused_state_path=sidecar,
            thresholds=override_3d,
        )

    assert report_within.recommendation == RECOMMENDATION_MONITORING_PAUSED
    assert report_within.violations == []


def test_paused_stale_days_evidence_echoes_effective_value(
    temp_db: Path,
    tmp_path: Path,
) -> None:
    """Wave 5 #1: the evaluate_window evidence dict must surface the
    effective stop-loss window so operators inspecting an SLO report can
    confirm config-driven override actually took effect (not silently
    fallen back to the module constant)."""
    sidecar = tmp_path / "paused_state.json"

    with db.connect() as conn:
        for _ in range(3):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        override = _measured_thresholds_override()
        override["paused_stale_days"] = 5
        report = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="p10",
            paused_state_path=sidecar,
            thresholds=override,
        )

    assert report.evidence.get("paused_stale_days") == 5, (
        f"evidence must echo effective paused_stale_days; got "
        f"{report.evidence.get('paused_stale_days')!r}"
    )


def test_paused_stale_days_invalid_type_raises_in_prod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave 5 #1: production mode (SLO_TEST_MODE unset) must reject a JSON
    `paused_stale_days` whose type is wrong (string / float / bool). This
    closes a footgun where an operator typo (`"seven"` instead of `7`)
    would silently revert to a default value mid-incident."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    from backend.services.promotion_slo_monitor import load_default_thresholds

    bad_json_path = tmp_path / "slo_thresholds.json"
    bad_json_path.write_text(
        json.dumps(
            {
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
                "baseline_provenance": {
                    "measured_at": datetime.now(timezone.utc).isoformat(),
                    "window_hours": 24,
                    "sample_size": 100,
                    "computed_by": "calibrate_cli",
                    "computed_at_main_sha": "wave5-test",
                },
                "minimum_sample_size": 30,
                "default_window_hours": 48,
                "paused_stale_days": "seven",  # ← invalid type
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"paused_stale_days.*must be positive int"):
        load_default_thresholds(bad_json_path)


def test_paused_stale_days_invalid_test_mode_warns_and_fallbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Wave 5 #1: test mode (SLO_TEST_MODE=1) must warn but NOT raise on a
    bad paused_stale_days; load must fall back to the module default 7 so
    legacy fixtures don't break CI. This is the parity-with-baseline_provenance
    escape hatch documented across W4 hardening."""
    monkeypatch.setenv("SLO_TEST_MODE", "1")
    import logging as _logging

    from backend.services.promotion_slo_monitor import (
        PAUSED_STALE_DAYS,
        load_default_thresholds,
    )

    bad_json_path = tmp_path / "slo_thresholds.json"
    bad_json_path.write_text(
        json.dumps(
            {
                "thresholds": {},
                "baseline": {},
                "baseline_provenance": {
                    "measured_at": datetime.now(timezone.utc).isoformat(),
                    "window_hours": 24,
                    "sample_size": 100,
                    "computed_by": "calibrate_cli",
                    "computed_at_main_sha": "wave5-test",
                },
                "paused_stale_days": 3.5,  # ← float (not int)
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(_logging.WARNING):
        config = load_default_thresholds(bad_json_path)

    assert config["paused_stale_days"] == PAUSED_STALE_DAYS
    assert any(
        "paused_stale_days" in rec.message for rec in caplog.records
    ), f"expected paused_stale_days warning in caplog; got {[r.message for r in caplog.records]}"


def test_paused_stale_days_negative_raises_in_prod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave 5 #1: zero / negative paused_stale_days is nonsensical (a
    stop-loss window of 0 days would fire on first paused episode, an
    accidental anti-pattern). Production must reject; test mode warns."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    from backend.services.promotion_slo_monitor import load_default_thresholds

    for bad_value in (0, -1):
        bad_json_path = tmp_path / f"slo_thresholds_{bad_value}.json"
        bad_json_path.write_text(
            json.dumps(
                {
                    "thresholds": {},
                    "baseline": {},
                    "baseline_provenance": {
                        "measured_at": datetime.now(timezone.utc).isoformat(),
                        "window_hours": 24,
                        "sample_size": 100,
                        "computed_by": "calibrate_cli",
                        "computed_at_main_sha": "wave5-test",
                    },
                    "paused_stale_days": bad_value,
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(
            ValueError, match=r"paused_stale_days.*must be positive int"
        ):
            load_default_thresholds(bad_json_path)


def test_paused_stale_days_bool_rejected_in_prod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave 5 #1: Python booleans are technically `int` subclasses (True == 1,
    False == 0) — without an explicit `isinstance(v, bool)` rejection a JSON
    `true` would silently parse as `paused_stale_days=1`. Operator intent is
    nonsensical, so we reject explicitly. Mirrors the same defensive check in
    `_validate_baseline_provenance` for window_hours / sample_size."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    from backend.services.promotion_slo_monitor import load_default_thresholds

    bad_json_path = tmp_path / "slo_thresholds.json"
    bad_json_path.write_text(
        json.dumps(
            {
                "thresholds": {},
                "baseline": {},
                "baseline_provenance": {
                    "measured_at": datetime.now(timezone.utc).isoformat(),
                    "window_hours": 24,
                    "sample_size": 100,
                    "computed_by": "calibrate_cli",
                    "computed_at_main_sha": "wave5-test",
                },
                "paused_stale_days": True,  # ← bool, not int
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError, match=r"paused_stale_days.*must be positive int"
    ):
        load_default_thresholds(bad_json_path)


def test_bc_paused_stale_days_missing_falls_back_to_module_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave 5 #1: BC — a thresholds JSON omitting `paused_stale_days`
    entirely (pre-Wave 5 file) must still load successfully and surface the
    module default PAUSED_STALE_DAYS=7 (no warning, no raise).

    Wave 5 followup #2 (I-3): renamed with ``_bc_`` prefix so grep /
    `pytest -k bc_` can isolate the backward-compatibility test set from
    feature tests. The ``_bc_`` convention applies to any test that asserts
    "legacy JSON / pre-feature payload still loads and falls back to a
    sensible default".
    """
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    from backend.services.promotion_slo_monitor import (
        PAUSED_STALE_DAYS,
        load_default_thresholds,
    )

    legacy_json_path = tmp_path / "slo_thresholds.json"
    legacy_json_path.write_text(
        json.dumps(
            {
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
                "baseline_provenance": {
                    "measured_at": datetime.now(timezone.utc).isoformat(),
                    "window_hours": 24,
                    "sample_size": 100,
                    "computed_by": "calibrate_cli",
                    "computed_at_main_sha": "wave5-test",
                },
                "minimum_sample_size": 30,
                "default_window_hours": 48,
                # ← no paused_stale_days key
            }
        ),
        encoding="utf-8",
    )

    config = load_default_thresholds(legacy_json_path)
    assert config["paused_stale_days"] == PAUSED_STALE_DAYS


# ---------------------------------------------------------------------------
# Wave 5 followup #2 — _validate_positive_int helper unit tests (resolves
# followup-1 I-1: shared validator for paused_stale_days / baseline_stale_days
# / minimum_sample_size).
# ---------------------------------------------------------------------------
#
# This helper is module-private (``_validate_positive_int``); the three public
# wrappers ``_validate_paused_stale_days`` / ``_validate_baseline_stale_days``
# / ``_validate_minimum_sample_size`` delegate. Unit-testing the helper
# directly catches regressions across all three callers in one place.


def test_validate_positive_int_valid_returns_value() -> None:
    """Wave 5 #2: valid positive int → returned as-is."""
    from backend.services.promotion_slo_monitor import _validate_positive_int

    assert _validate_positive_int(7, default=99, field_name="foo") == 7
    assert _validate_positive_int(1, default=99, field_name="foo") == 1
    assert _validate_positive_int(1_000_000, default=99, field_name="foo") == 1_000_000


def test_validate_positive_int_none_returns_default() -> None:
    """Wave 5 #2: absent (None) → fallback default (BC for legacy JSONs)."""
    from backend.services.promotion_slo_monitor import _validate_positive_int

    assert _validate_positive_int(None, default=42, field_name="foo") == 42


def test_validate_positive_int_bool_raises_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave 5 #2: bool is technically int subclass but operator intent is
    nonsense. Both True and False rejected in prod."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    from backend.services.promotion_slo_monitor import _validate_positive_int

    with pytest.raises(ValueError, match=r"my_field.*must be positive int"):
        _validate_positive_int(True, default=10, field_name="my_field")
    with pytest.raises(ValueError, match=r"my_field.*must be positive int"):
        _validate_positive_int(False, default=10, field_name="my_field")


def test_validate_positive_int_negative_raises_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave 5 #2: negative values rejected in prod (a stop-loss window of -3
    days is nonsensical)."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    from backend.services.promotion_slo_monitor import _validate_positive_int

    with pytest.raises(ValueError, match=r"some_field.*must be positive int"):
        _validate_positive_int(-1, default=10, field_name="some_field")
    with pytest.raises(ValueError, match=r"some_field.*must be positive int"):
        _validate_positive_int(-1000, default=10, field_name="some_field")


def test_validate_positive_int_zero_raises_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave 5 #2: zero rejected in prod (a stop-loss of 0 days = fire on first
    observation; a minimum sample of 0 = no minimum protection)."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    from backend.services.promotion_slo_monitor import _validate_positive_int

    with pytest.raises(ValueError, match=r"zero_field.*must be positive int"):
        _validate_positive_int(0, default=10, field_name="zero_field")


def test_validate_positive_int_invalid_test_mode_warns_fallback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Wave 5 #2: SLO_TEST_MODE=1 escape hatch — invalid value → warn + return
    default (so legacy fixtures keep loading in CI / dev)."""
    monkeypatch.setenv("SLO_TEST_MODE", "1")
    import logging as _logging

    from backend.services.promotion_slo_monitor import _validate_positive_int

    with caplog.at_level(_logging.WARNING):
        result = _validate_positive_int(
            "not-a-number", default=42, field_name="test_mode_field"
        )

    assert result == 42
    assert any(
        "test_mode_field" in rec.message for rec in caplog.records
    ), f"expected test_mode_field warning; got {[r.message for r in caplog.records]}"


def test_validate_positive_int_float_rejected_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave 5 #2: float is not int (Python `3.0 != 3` semantically for type
    purposes — operator intent for a discrete count of days is integer)."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    from backend.services.promotion_slo_monitor import _validate_positive_int

    with pytest.raises(ValueError, match=r"float_field.*must be positive int"):
        _validate_positive_int(3.5, default=10, field_name="float_field")
    with pytest.raises(ValueError, match=r"float_field.*must be positive int"):
        _validate_positive_int(3.0, default=10, field_name="float_field")


# ---------------------------------------------------------------------------
# Wave 5 followup #2 — baseline_stale_days configurable (eval-M1 sibling)
# ---------------------------------------------------------------------------
#
# The Wave 3 ``BASELINE_STALE_DAYS = 60`` module constant required code edit +
# redeploy to retune. Wave 5 followup #2 promotes it to a top-level JSON field
# parallel to ``paused_stale_days`` (followup #1). The constant remains the
# fallback default for back-compat + import-time semantics.


def test_baseline_stale_days_default_from_json(tmp_path: Path) -> None:
    """Wave 5 #2: the checked-in slo_thresholds.json now carries
    `baseline_stale_days: 60`. load_default_thresholds must surface it on the
    returned dict so callers see the JSON-driven value (not the module
    constant) by default."""
    from backend.services.promotion_slo_monitor import load_default_thresholds

    config = load_default_thresholds()  # default path → on-disk JSON
    assert "baseline_stale_days" in config
    assert config["baseline_stale_days"] == 60
    assert isinstance(config["baseline_stale_days"], int)


def test_baseline_stale_days_overridable_via_thresholds_kwarg(
    temp_db: Path,
    tmp_path: Path,
) -> None:
    """Wave 5 #2: operator can shrink the baseline-stale window to 30 days via
    the structured `thresholds` kwarg. With a provenance.measured_at 35 days
    ago + baseline_stale_days=30, the baseline_stale violation must fire; the
    same fixture under the default 60-day window would not fire.
    """
    from backend.services.promotion_slo_monitor import BASELINE_STALE_DAYS

    # Insert enough rows to clear min_sample so we hit the post-paused
    # evaluation path (where baseline_stale fires).
    with db.connect() as conn:
        for _ in range(50):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        # 35-day-old baseline measurement
        thirty_five_days_ago = (
            datetime.now(timezone.utc) - timedelta(days=35)
        ).isoformat()
        override_30d = {
            "thresholds": {},
            "baseline_provenance": {
                "measured_at": thirty_five_days_ago,
                "window_hours": 24,
                "sample_size": 100,
                "computed_by": "calibrate_cli",
                "computed_at_main_sha": "wave5-followup2-test",
            },
            "baseline_stale_days": 30,
        }
        report_30d = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="p10",
            thresholds=override_30d,
        )

        dims_30d = {v["dimension"] for v in report_30d.violations}
        assert "baseline_stale" in dims_30d, (
            f"with baseline_stale_days=30 + measured 35d ago, expected "
            f"baseline_stale violation; got dims={dims_30d}"
        )
        stale_v = next(
            v for v in report_30d.violations if v["dimension"] == "baseline_stale"
        )
        assert stale_v["threshold_days"] == 30, (
            "violation must echo operator-supplied stale_days, not module "
            f"default {BASELINE_STALE_DAYS}"
        )

        # Same fixture, default 60-day window → measured 35d ago is fresh.
        override_default = {
            "thresholds": {},
            "baseline_provenance": {
                "measured_at": thirty_five_days_ago,
                "window_hours": 24,
                "sample_size": 100,
                "computed_by": "calibrate_cli",
                "computed_at_main_sha": "wave5-followup2-test",
            },
            # ← no baseline_stale_days → falls back to 60
        }
        report_default = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="p10",
            thresholds=override_default,
        )

    dims_default = {v["dimension"] for v in report_default.violations}
    assert "baseline_stale" not in dims_default, (
        f"with default 60d window + measured 35d ago, baseline must NOT be "
        f"stale; got dims={dims_default}"
    )


def test_baseline_stale_days_evidence_echoes_effective_value(
    temp_db: Path,
    tmp_path: Path,
) -> None:
    """Wave 5 #2: evaluate_window evidence dict must surface the effective
    baseline-stale window (mirrors the paused_stale_days evidence echo) so
    operators inspecting an SLO report can confirm config-driven override
    actually took effect.
    """
    with db.connect() as conn:
        for _ in range(3):
            _insert_simulation_job(conn, status="done")
        conn.commit()

        override = _measured_thresholds_override()
        override["baseline_stale_days"] = 45
        report = evaluate_window(
            window_hours=48,
            conn=conn,
            promotion_state="p10",
            thresholds=override,
        )

    assert report.evidence.get("baseline_stale_days") == 45, (
        f"evidence must echo effective baseline_stale_days; got "
        f"{report.evidence.get('baseline_stale_days')!r}"
    )


def test_baseline_stale_days_invalid_type_raises_in_prod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave 5 #2: production mode (SLO_TEST_MODE unset) must reject a JSON
    `baseline_stale_days` whose type is wrong (string / float). Closes a
    footgun where an operator typo (`"sixty"` instead of `60`) would
    silently revert to a default mid-incident."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    from backend.services.promotion_slo_monitor import load_default_thresholds

    bad_json_path = tmp_path / "slo_thresholds.json"
    bad_json_path.write_text(
        json.dumps(
            {
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
                "baseline_provenance": {
                    "measured_at": datetime.now(timezone.utc).isoformat(),
                    "window_hours": 24,
                    "sample_size": 100,
                    "computed_by": "calibrate_cli",
                    "computed_at_main_sha": "wave5-test",
                },
                "minimum_sample_size": 30,
                "default_window_hours": 48,
                "baseline_stale_days": "sixty",  # ← invalid type
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError, match=r"baseline_stale_days.*must be positive int"
    ):
        load_default_thresholds(bad_json_path)


def test_baseline_stale_days_invalid_test_mode_warns_and_fallbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Wave 5 #2: test mode (SLO_TEST_MODE=1) must warn but NOT raise on a bad
    baseline_stale_days; load must fall back to BASELINE_STALE_DAYS=60.
    Parity with paused_stale_days escape hatch."""
    monkeypatch.setenv("SLO_TEST_MODE", "1")
    import logging as _logging

    from backend.services.promotion_slo_monitor import (
        BASELINE_STALE_DAYS,
        load_default_thresholds,
    )

    bad_json_path = tmp_path / "slo_thresholds.json"
    bad_json_path.write_text(
        json.dumps(
            {
                "thresholds": {},
                "baseline": {},
                "baseline_provenance": {
                    "measured_at": datetime.now(timezone.utc).isoformat(),
                    "window_hours": 24,
                    "sample_size": 100,
                    "computed_by": "calibrate_cli",
                    "computed_at_main_sha": "wave5-test",
                },
                "baseline_stale_days": 45.5,  # ← float (not int)
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(_logging.WARNING):
        config = load_default_thresholds(bad_json_path)

    assert config["baseline_stale_days"] == BASELINE_STALE_DAYS
    assert any(
        "baseline_stale_days" in rec.message for rec in caplog.records
    ), f"expected baseline_stale_days warning in caplog; got {[r.message for r in caplog.records]}"


def test_baseline_stale_days_negative_raises_in_prod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave 5 #2: zero / negative baseline_stale_days rejected in prod (a
    stale window of -10 days is nonsensical; a 0-day window would fire on
    every report)."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    from backend.services.promotion_slo_monitor import load_default_thresholds

    for bad_value in (0, -1, -1000):
        bad_json_path = tmp_path / f"slo_thresholds_{bad_value}.json"
        bad_json_path.write_text(
            json.dumps(
                {
                    "thresholds": {},
                    "baseline": {},
                    "baseline_provenance": {
                        "measured_at": datetime.now(timezone.utc).isoformat(),
                        "window_hours": 24,
                        "sample_size": 100,
                        "computed_by": "calibrate_cli",
                        "computed_at_main_sha": "wave5-test",
                    },
                    "baseline_stale_days": bad_value,
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(
            ValueError, match=r"baseline_stale_days.*must be positive int"
        ):
            load_default_thresholds(bad_json_path)


def test_baseline_stale_days_bool_rejected_in_prod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave 5 #2: bool is technically int subclass but JSON `true` would
    silently parse as baseline_stale_days=1. Rejected explicitly. Mirrors
    paused_stale_days bool guard."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    from backend.services.promotion_slo_monitor import load_default_thresholds

    bad_json_path = tmp_path / "slo_thresholds.json"
    bad_json_path.write_text(
        json.dumps(
            {
                "thresholds": {},
                "baseline": {},
                "baseline_provenance": {
                    "measured_at": datetime.now(timezone.utc).isoformat(),
                    "window_hours": 24,
                    "sample_size": 100,
                    "computed_by": "calibrate_cli",
                    "computed_at_main_sha": "wave5-test",
                },
                "baseline_stale_days": True,  # ← bool, not int
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError, match=r"baseline_stale_days.*must be positive int"
    ):
        load_default_thresholds(bad_json_path)


def test_bc_baseline_stale_days_missing_falls_back_to_module_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave 5 #2: BC — a thresholds JSON omitting `baseline_stale_days`
    entirely (pre-Wave 5 #2 file) must still load successfully and surface
    the module default BASELINE_STALE_DAYS=60 (no warning, no raise).

    ``_bc_`` prefix per I-3 convention — grep / `pytest -k bc_` isolates BC
    tests from feature tests.
    """
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    from backend.services.promotion_slo_monitor import (
        BASELINE_STALE_DAYS,
        load_default_thresholds,
    )

    legacy_json_path = tmp_path / "slo_thresholds.json"
    legacy_json_path.write_text(
        json.dumps(
            {
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
                "baseline_provenance": {
                    "measured_at": datetime.now(timezone.utc).isoformat(),
                    "window_hours": 24,
                    "sample_size": 100,
                    "computed_by": "calibrate_cli",
                    "computed_at_main_sha": "wave5-test",
                },
                "minimum_sample_size": 30,
                "default_window_hours": 48,
                "paused_stale_days": 7,
                # ← no baseline_stale_days key
            }
        ),
        encoding="utf-8",
    )

    config = load_default_thresholds(legacy_json_path)
    assert config["baseline_stale_days"] == BASELINE_STALE_DAYS


# ---------------------------------------------------------------------------
# Wave 5 followup #2 — minimum_sample_size validation upgrade
# ---------------------------------------------------------------------------
#
# Pre-Wave 5 #2 the field was loaded with a bare ``int()`` cast which silently
# coerced bool → 1/0, accepted negatives, and accepted other anti-patterns.
# Wave 5 #2 routes it through ``_validate_positive_int`` for parity with the
# other two positive-int JSON fields. Behavior change is fail-closed in prod
# (with test-mode fallback for legacy fixtures).


def test_minimum_sample_size_bool_now_rejected_in_prod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave 5 #2: pre-fix a JSON `minimum_sample_size: true` silently
    coerced to 1 via ``int(True)``; post-fix it raises in prod (parity with
    paused_stale_days / baseline_stale_days)."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    from backend.services.promotion_slo_monitor import load_default_thresholds

    bad_json_path = tmp_path / "slo_thresholds.json"
    bad_json_path.write_text(
        json.dumps(
            {
                "thresholds": {},
                "baseline": {},
                "baseline_provenance": {
                    "measured_at": datetime.now(timezone.utc).isoformat(),
                    "window_hours": 24,
                    "sample_size": 100,
                    "computed_by": "calibrate_cli",
                    "computed_at_main_sha": "wave5-test",
                },
                "minimum_sample_size": True,  # ← bool, not int
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError, match=r"minimum_sample_size.*must be positive int"
    ):
        load_default_thresholds(bad_json_path)


def test_minimum_sample_size_negative_now_raises_in_prod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave 5 #2: pre-fix a JSON `minimum_sample_size: -5` silently loaded
    as -5 (and then short-circuited every report into insufficient_data
    semantics for a state.sample_size >= 0 baseline). Post-fix it raises in
    prod."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    from backend.services.promotion_slo_monitor import load_default_thresholds

    bad_json_path = tmp_path / "slo_thresholds.json"
    bad_json_path.write_text(
        json.dumps(
            {
                "thresholds": {},
                "baseline": {},
                "baseline_provenance": {
                    "measured_at": datetime.now(timezone.utc).isoformat(),
                    "window_hours": 24,
                    "sample_size": 100,
                    "computed_by": "calibrate_cli",
                    "computed_at_main_sha": "wave5-test",
                },
                "minimum_sample_size": -5,  # ← negative
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError, match=r"minimum_sample_size.*must be positive int"
    ):
        load_default_thresholds(bad_json_path)


def test_bc_minimum_sample_size_missing_falls_back_to_default_30(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wave 5 #2: BC — a thresholds JSON omitting `minimum_sample_size`
    entirely (theoretically possible in custom test fixtures) falls back to
    DEFAULT_MINIMUM_SAMPLE_SIZE=30 without warning or raise. The shipped
    slo_thresholds.json has always carried the key, but the validator
    contract must preserve the absent-key default for parity with the other
    positive-int fields.

    ``_bc_`` prefix per I-3 convention.
    """
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    from backend.services.promotion_slo_monitor import (
        DEFAULT_MINIMUM_SAMPLE_SIZE,
        load_default_thresholds,
    )

    legacy_json_path = tmp_path / "slo_thresholds.json"
    legacy_json_path.write_text(
        json.dumps(
            {
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
                "baseline_provenance": {
                    "measured_at": datetime.now(timezone.utc).isoformat(),
                    "window_hours": 24,
                    "sample_size": 100,
                    "computed_by": "calibrate_cli",
                    "computed_at_main_sha": "wave5-test",
                },
                "default_window_hours": 48,
                # ← no minimum_sample_size key
            }
        ),
        encoding="utf-8",
    )

    config = load_default_thresholds(legacy_json_path)
    assert config["minimum_sample_size"] == DEFAULT_MINIMUM_SAMPLE_SIZE
