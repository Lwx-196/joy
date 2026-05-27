"""Wave 3 P0.5 H-6 integration test — end-to-end联动 monitoring_paused +
baseline_provenance + vlm_judge_missing_rate.

Eval-auditor N-3 + Wave 3 P0.5 H-6: the three Wave 3 SLO mechanics
(promotion_state-aware triage, provenance gate, missing_rate dimension) all
live in `evaluate_window` but each previously had only an isolated unit test.
This file drives all three through ONE call with real data so we catch
regressions where the call paths only work individually.

No mocks — temp SQLite, real manifest fixture, real thresholds JSON. The only
test-side trick is providing `promotion_state` via the kwarg so we don't have
to monkey-patch the manifest loader (already covered by the unit suite).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend import db
from backend.services.promotion_slo_monitor import (
    RECOMMENDATION_MONITORING_PAUSED,
    RECOMMENDATION_ROLLBACK,
    evaluate_window,
    load_default_thresholds,
)


# ---------------------------------------------------------------------------
# Helpers (kept local to avoid coupling with the unit-test files)
# ---------------------------------------------------------------------------


def _now_iso(delta_minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).isoformat()


def _fresh_thresholds_doc(*, measured_days_ago: int = 30) -> dict:
    measured_at = (
        datetime.now(timezone.utc) - timedelta(days=measured_days_ago)
    ).isoformat()
    return {
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
        "baseline_provenance": {
            "measured_at": measured_at,
            "window_hours": 24,
            "sample_size": 100,
            "computed_by": "calibrate_cli",
            "computed_at_main_sha": "d6dc5b1",
        },
        "minimum_sample_size": 30,
        "default_window_hours": 48,
    }


def _write_thresholds(path: Path, doc: dict) -> Path:
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def _insert_lineage(conn, *, vlm_payload: dict | None) -> None:
    raw = json.dumps(vlm_payload) if vlm_payload is not None else None
    conn.execute(
        """
        INSERT INTO candidate_lineage (
          simulation_job_id, case_id, attempt, vlm_judge_result_json, created_at
        )
        VALUES (NULL, NULL, 1, ?, ?)
        """,
        (raw, _now_iso(-5)),
    )


# ---------------------------------------------------------------------------
# Path 1: paused 联动 (sample<30 + promoted state) — verifies that even when
# evaluate_window short-circuits to monitoring_paused, the *evidence* surfaces
# the missing_rate signal AND provenance is intact AND no baseline_stale fires.
# ---------------------------------------------------------------------------


def test_wave3_联动_paused_provenance_missing_rate_evidence(
    temp_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sample=25 (<30) + promotion_state='p25' + 50% lineage missing VLM payload.

    Expectations (联动 all 3 Wave 3 mechanics):
      - recommendation == 'monitoring_paused' (C-1)
      - within_slo is None (paused semantics)
      - evidence carries baseline_provenance with all 5 fields (C-2)
      - evidence.vlm_disagreement.missing_rate == 0.5 (C-3) — signal preserved
        even though paused short-circuit skips the violation collection loop
      - violations list is empty (paused short-circuits before collection;
        documents the trade-off — see notes field)
      - notes == 'small_sample_in_promoted_state'
    """
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)

    thresholds_path = _write_thresholds(
        tmp_path / "thresholds_fresh.json",
        _fresh_thresholds_doc(measured_days_ago=30),
    )
    config = load_default_thresholds(thresholds_path)

    # Seed 20 lineage rows: 10 valid (agreement_rate=0.95), 10 missing payload.
    # Total sample_size = 20 (vlm parsed_count=10) < 30 → triggers paused branch.
    with db.connect() as conn:
        for _ in range(10):
            _insert_lineage(conn, vlm_payload={"agreement_rate": 0.95})
        for _ in range(10):
            _insert_lineage(conn, vlm_payload=None)
        conn.commit()

        report = evaluate_window(
            window_hours=48,
            conn=conn,
            thresholds=config,
            promotion_state="p25",
        )

    # --- C-1: paused triage ------------------------------------------------
    assert report.recommendation == RECOMMENDATION_MONITORING_PAUSED
    assert report.within_slo is None
    assert report.notes == "small_sample_in_promoted_state"
    assert report.violations == []
    assert report.sample_size < 30

    # --- C-2: provenance preserved on the report -------------------------
    prov = report.evidence["baseline_provenance"]
    assert prov["computed_by"] == "calibrate_cli"
    for field in (
        "measured_at",
        "window_hours",
        "sample_size",
        "computed_by",
        "computed_at_main_sha",
    ):
        assert field in prov

    # --- C-3: missing_rate signal surfaces in evidence even when paused ---
    vlm_ev = report.evidence["vlm_disagreement"]
    assert vlm_ev["total_rows"] == 20
    assert vlm_ev["missing_count"] == 10
    assert vlm_ev["missing_rate"] == pytest.approx(0.5, abs=1e-6)
    assert vlm_ev["parsed_count"] == 10

    # --- Negative: no baseline_stale violation (30d < 60d threshold) ------
    # (paused returned early so violations is empty by design; this assertion
    # documents that even if you bypass paused, provenance is fresh)
    assert "baseline_stale" not in {v["dimension"] for v in report.violations}


# ---------------------------------------------------------------------------
# Path 2: violation path 联动 — sample ≥ 30 + promoted state → reaches the
# violation collection loop, where missing_rate + (fresh) provenance both
# drive correct emit. Mirrors the "spec intent" of Path 1 but with the data
# shape that actually exercises violation collection.
# ---------------------------------------------------------------------------


def test_wave3_联动_violations_missing_rate_fires_with_fresh_provenance(
    temp_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sample=40 (>=30) + promotion_state='p25' + 50% lineage missing payload.

    Expectations:
      - recommendation == 'rollback' (missing_rate violation)
      - within_slo is False
      - violations contains vlm_judge_missing_rate dim (C-3)
      - violations does NOT contain baseline_stale (fresh provenance)
      - evidence.baseline_provenance complete (C-2)
      - thresholds dict carries vlm_judge_missing_rate_max key (H-1: JSON reified)
    """
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)

    thresholds_path = _write_thresholds(
        tmp_path / "thresholds_fresh2.json",
        _fresh_thresholds_doc(measured_days_ago=20),
    )
    config = load_default_thresholds(thresholds_path)
    # H-1 闭环: threshold key now exists in JSON, not just code fallback
    assert "vlm_judge_missing_rate_max" in config["thresholds"]

    # 40 lineage rows: 20 valid (agreement=0.95), 20 missing — sample size ≥ 30,
    # missing_rate = 0.50 (above 0.10 threshold). Other dims kept clean.
    with db.connect() as conn:
        for _ in range(20):
            _insert_lineage(conn, vlm_payload={"agreement_rate": 0.95})
        for _ in range(20):
            _insert_lineage(conn, vlm_payload=None)
        conn.commit()

        report = evaluate_window(
            window_hours=48,
            conn=conn,
            thresholds=config,
            promotion_state="p25",
        )

    assert report.sample_size >= 30
    assert report.recommendation == RECOMMENDATION_ROLLBACK
    assert report.within_slo is False

    dims = {v["dimension"] for v in report.violations}
    assert "vlm_judge_missing_rate" in dims, (
        f"missing_rate violation should fire on 50% missing; got dims={dims}"
    )
    assert "baseline_stale" not in dims, (
        "fresh provenance (20d old) must NOT trigger baseline_stale (60d threshold)"
    )

    # H-1 闭环: violation context references the JSON-sourced threshold (not the
    # code fallback). Verify the actual threshold value matches what was loaded.
    miss = next(v for v in report.violations if v["dimension"] == "vlm_judge_missing_rate")
    assert miss["threshold"] == config["thresholds"]["vlm_judge_missing_rate_max"]
    assert miss["actual"] == pytest.approx(0.5, abs=1e-6)

    # C-2: provenance still echoed in evidence
    prov = report.evidence["baseline_provenance"]
    assert prov["computed_by"] == "calibrate_cli"
    assert prov["computed_at_main_sha"] == "d6dc5b1"
