"""Boundary tests for baseline_provenance gate + calibrate_slo_baseline CLI.

Covers Wave 3 Agent C scope (eval-auditor C-2):
  - load_default_thresholds() fail-closed on missing/malformed provenance
  - baseline_stale dimension fires when measured_at > 60d old
  - SLO_TEST_MODE=1 escape hatch for legacy fixtures
  - calibrate_slo_baseline CLI: --dry-run emits valid JSON patch, no file change
  - calibrate_slo_baseline CLI: --apply atomically writes back + refreshes provenance
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend import db
from backend.scripts import calibrate_slo_baseline as calibrate_cli
from backend.services.promotion_slo_monitor import (
    BASELINE_STALE_DAYS,
    _check_baseline_stale,
    _validate_baseline_provenance,
    evaluate_window,
    load_default_thresholds,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso(delta_minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).isoformat()


def _write_thresholds(path: Path, provenance: dict | None) -> Path:
    payload: dict = {
        "schema_version": 1,
        "thresholds": {
            "comfyui_failure_rate_max": 0.05,
            "vlm_disagreement_rate_max": 0.10,
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
    if provenance is not None:
        payload["baseline_provenance"] = provenance
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _fresh_provenance(measured_at: str | None = None) -> dict:
    return {
        "measured_at": measured_at or _now_iso(0),
        "window_hours": 24,
        "sample_size": 100,
        "computed_by": "calibrate_cli",
        "computed_at_main_sha": "d6dc5b1",
    }


def _stale_provenance() -> dict:
    old = (datetime.now(timezone.utc) - timedelta(days=BASELINE_STALE_DAYS + 5)).isoformat()
    return _fresh_provenance(measured_at=old)


# ---------------------------------------------------------------------------
# 1) Missing measured_at → ValueError in production mode
# ---------------------------------------------------------------------------


def test_load_default_thresholds_missing_measured_at_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production fail-closed: provenance missing required field → ValueError."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    bad = {
        "window_hours": 24,
        "sample_size": 0,
        "computed_by": "manual_seed",
        "computed_at_main_sha": "abc1234",
        # measured_at intentionally omitted
    }
    p = _write_thresholds(tmp_path / "bad.json", bad)
    with pytest.raises(ValueError, match=r"baseline_provenance.*measured_at"):
        load_default_thresholds(p)


def test_load_default_thresholds_naive_datetime_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """measured_at must be timezone-aware (no naive datetimes per dev-spec)."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    bad = _fresh_provenance(measured_at="2026-05-28T00:00:00")  # no tz
    p = _write_thresholds(tmp_path / "naive.json", bad)
    with pytest.raises(ValueError, match=r"timezone-aware"):
        load_default_thresholds(p)


def test_slo_test_mode_demotes_provenance_error_to_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SLO_TEST_MODE=1 → invalid provenance returns empty dict + warning."""
    monkeypatch.setenv("SLO_TEST_MODE", "1")
    p = _write_thresholds(tmp_path / "bad.json", {})  # empty provenance
    result = load_default_thresholds(p)
    assert result["baseline_provenance"] == {}
    assert result["thresholds"]["comfyui_failure_rate_max"] == 0.05


# ---------------------------------------------------------------------------
# 2) Stale baseline → baseline_stale violation
# ---------------------------------------------------------------------------


def test_baseline_stale_violation_fires_when_measured_at_old(
    temp_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """measured_at > 60d ago → baseline_stale violation in evaluate_window."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    p = _write_thresholds(tmp_path / "stale.json", _stale_provenance())
    config = load_default_thresholds(p)

    # Inject sample size > min_sample so we reach the violation collection path
    with db.connect() as conn:
        # 40 healthy done jobs — no other dim violates
        for _ in range(40):
            conn.execute(
                """
                INSERT INTO simulation_jobs (
                  status, focus_targets_json, policy_json, model_plan_json,
                  input_refs_json, output_refs_json, watermarked, audit_json,
                  error_message, can_publish, created_at, updated_at
                )
                VALUES ('done', '[]', '{}', '{}', '[]', '[]', 0, '{}',
                        NULL, 0, ?, ?)
                """,
                (_now_iso(-5), _now_iso(-5)),
            )
        conn.commit()
        report = evaluate_window(window_hours=48, conn=conn, thresholds=config)

    assert report.within_slo is False
    assert report.recommendation == "rollback"
    dims = {v["dimension"] for v in report.violations}
    assert "baseline_stale" in dims
    stale = next(v for v in report.violations if v["dimension"] == "baseline_stale")
    assert stale["threshold_days"] == BASELINE_STALE_DAYS
    assert stale["actual_days"] > BASELINE_STALE_DAYS


# ---------------------------------------------------------------------------
# 3) Fresh + complete provenance loads cleanly + no baseline_stale
# ---------------------------------------------------------------------------


def test_fresh_complete_provenance_loads_and_no_stale_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production mode: complete fresh provenance loads w/o error, no stale."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    fresh = _fresh_provenance()
    p = _write_thresholds(tmp_path / "fresh.json", fresh)

    config = load_default_thresholds(p)
    assert config["baseline_provenance"] == fresh
    # _check_baseline_stale returns None for fresh provenance
    assert _check_baseline_stale(config["baseline_provenance"]) is None


def test_validate_provenance_accepts_correct_shape() -> None:
    """Unit test on the validator itself — all 5 fields present + typed correctly."""
    result = _validate_baseline_provenance(_fresh_provenance())
    assert result["computed_by"] == "calibrate_cli"
    assert result["sample_size"] == 100


# ---------------------------------------------------------------------------
# 4) calibrate CLI --dry-run emits valid JSON patch + does NOT modify file
# ---------------------------------------------------------------------------


def test_calibrate_cli_dry_run_emits_patch_without_writing(
    temp_db: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--dry-run prints JSON patch with full provenance + leaves file untouched."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    target = _write_thresholds(tmp_path / "before.json", _fresh_provenance())
    before_bytes = target.read_bytes()

    # Seed minimal real data so observed numbers are non-noise
    with db.connect() as conn:
        for _ in range(5):
            conn.execute(
                """
                INSERT INTO simulation_jobs (
                  status, focus_targets_json, policy_json, model_plan_json,
                  input_refs_json, output_refs_json, watermarked, audit_json,
                  error_message, can_publish, created_at, updated_at
                )
                VALUES ('done', '[]', '{}', '{}', '[]', '[]', 0, '{}',
                        NULL, 0, ?, ?)
                """,
                (_now_iso(-5), _now_iso(-5)),
            )
        conn.commit()

    exit_code = calibrate_cli.main(
        ["--window", "24", "--thresholds-path", str(target)]
    )
    assert exit_code == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["dry_run"] is True
    assert payload["target_path"] == str(target)

    patch = payload["patch"]
    assert patch["thresholds"]["comfyui_failure_rate_max"] >= 0.05  # floor honored
    prov = patch["baseline_provenance"]
    for field in (
        "measured_at",
        "window_hours",
        "sample_size",
        "computed_by",
        "computed_at_main_sha",
    ):
        assert field in prov, f"patch missing provenance field {field}"
    assert prov["computed_by"] == "calibrate_cli"
    # Dry-run must NOT touch the file
    assert target.read_bytes() == before_bytes


# ---------------------------------------------------------------------------
# 5) calibrate CLI --apply writes back + refreshes provenance atomically
# ---------------------------------------------------------------------------


def test_calibrate_cli_apply_overwrites_and_refreshes_provenance(
    temp_db: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--apply atomically writes new thresholds JSON; provenance.measured_at and
    computed_by updated; subsequent load_default_thresholds returns the new doc."""
    monkeypatch.delenv("SLO_TEST_MODE", raising=False)
    stale_prov = _stale_provenance()
    target = _write_thresholds(tmp_path / "to_apply.json", stale_prov)

    # Seed real shadow data
    with db.connect() as conn:
        for _ in range(10):
            conn.execute(
                """
                INSERT INTO simulation_jobs (
                  status, focus_targets_json, policy_json, model_plan_json,
                  input_refs_json, output_refs_json, watermarked, audit_json,
                  error_message, can_publish, created_at, updated_at
                )
                VALUES ('done', '[]', '{}', '{}', '[]', '[]', 0, '{}',
                        NULL, 0, ?, ?)
                """,
                (_now_iso(-5), _now_iso(-5)),
            )
        conn.commit()

    exit_code = calibrate_cli.main(
        ["--apply", "--window", "24", "--thresholds-path", str(target)]
    )
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "applied" in captured.out

    # File now contains refreshed provenance
    after = json.loads(target.read_text(encoding="utf-8"))
    new_prov = after["baseline_provenance"]
    assert new_prov["computed_by"] == "calibrate_cli"
    assert new_prov["measured_at"] != stale_prov["measured_at"]
    parsed = datetime.fromisoformat(new_prov["measured_at"])
    assert parsed.tzinfo is not None  # tz-aware required
    assert new_prov["window_hours"] == 24

    # load_default_thresholds against the new file passes production validation
    fresh_config = load_default_thresholds(target)
    assert fresh_config["baseline_provenance"]["computed_by"] == "calibrate_cli"
    assert _check_baseline_stale(fresh_config["baseline_provenance"]) is None
