"""C0.5.2 schema tests for the three A/B validation reports.

Covers the public surface of
``backend.scripts.validate_ab_validation_reports``:

  - Bundled samples validate cleanly against the GA contract.
  - Each per-report validator emits a stable issue code for the documented
    failure modes (calibration drift, count negativity, range violations,
    approval invariants, cross-report drift).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.scripts.validate_ab_validation_reports import (
    CANONICAL_AB_REPORT_FILENAME,
    CANONICAL_PRODUCTION_GATE_FILENAME,
    CANONICAL_VLM_GUARDRAIL_FILENAME,
    REPO_ROOT_DEFAULT,
    SAMPLE_AB_REPORT_FILENAME,
    SAMPLE_PRODUCTION_GATE_FILENAME,
    SAMPLE_VLM_GUARDRAIL_FILENAME,
    SAMPLES_DIR_RELPATH,
    _ReportPaths,
    validate_ab_validation_report,
    validate_all,
    validate_cross_report_invariants,
    validate_production_gate_report,
    validate_vlm_guardrail_report,
)


SAMPLES_DIR = REPO_ROOT_DEFAULT / SAMPLES_DIR_RELPATH


def _sample(name: str) -> dict:
    return json.loads((SAMPLES_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture()
def ab_sample() -> dict:
    return _sample(SAMPLE_AB_REPORT_FILENAME)


@pytest.fixture()
def vlm_sample() -> dict:
    return _sample(SAMPLE_VLM_GUARDRAIL_FILENAME)


@pytest.fixture()
def pg_sample() -> dict:
    return _sample(SAMPLE_PRODUCTION_GATE_FILENAME)


# ----------------------------- baseline ------------------------------------


def test_samples_validate_clean(ab_sample, vlm_sample, pg_sample, tmp_path: Path):
    paths = _ReportPaths(
        ab_report=tmp_path / CANONICAL_AB_REPORT_FILENAME,
        vlm_guardrail=tmp_path / CANONICAL_VLM_GUARDRAIL_FILENAME,
        production_gate=tmp_path / CANONICAL_PRODUCTION_GATE_FILENAME,
    )
    paths.ab_report.write_text(json.dumps(ab_sample), encoding="utf-8")
    paths.vlm_guardrail.write_text(json.dumps(vlm_sample), encoding="utf-8")
    paths.production_gate.write_text(json.dumps(pg_sample), encoding="utf-8")
    results = validate_all(paths, required_workflow="portrait_focal_enhance_v1")
    flat = [i for issues in results.values() for i in issues]
    assert flat == [], [i.as_dict() for i in flat]


# ----------------------------- AB report -----------------------------------


def test_ab_report_rejects_negative_candidate_wins(ab_sample):
    ab_sample["candidate_win_count"] = -1
    issues = validate_ab_validation_report(
        ab_sample, required_workflow="portrait_focal_enhance_v1"
    )
    codes = {i.code for i in issues}
    assert "count_invalid" in codes


def test_ab_report_rejects_promote_without_approval(ab_sample):
    ab_sample["promote_to_default"] = True
    ab_sample.pop("promotion_approval")
    issues = validate_ab_validation_report(
        ab_sample, required_workflow="portrait_focal_enhance_v1"
    )
    codes = {i.code for i in issues}
    assert "approval_missing" in codes


def test_ab_report_rejects_missing_ga_workflow(ab_sample):
    ab_sample["promotion_approval"]["approved_workflows"] = ["local_region_enhance_v1"]
    issues = validate_ab_validation_report(
        ab_sample, required_workflow="portrait_focal_enhance_v1"
    )
    codes = {i.code for i in issues}
    assert "approved_workflows_missing_ga_scope" in codes


def test_ab_report_rejects_bad_evidence_sha(ab_sample):
    ab_sample["promotion_approval"]["approved_evidence_sha256"]["vlm_guardrail_report"] = (
        "sha256:zz"
    )
    issues = validate_ab_validation_report(
        ab_sample, required_workflow="portrait_focal_enhance_v1"
    )
    codes = {i.code for i in issues}
    assert "evidence_sha256_invalid" in codes


def test_ab_report_rejects_invalid_approver_when_promoting(ab_sample):
    ab_sample["promotion_approval"]["approver"] = ""
    ab_sample["promotion_approval"].pop("approved_by", None)
    issues = validate_ab_validation_report(
        ab_sample, required_workflow="portrait_focal_enhance_v1"
    )
    codes = {i.code for i in issues}
    assert "approver_missing" in codes


def test_ab_report_winner_below_wins(ab_sample):
    ab_sample["winner_evidence_count"] = 1
    ab_sample["candidate_win_count"] = 5
    issues = validate_ab_validation_report(
        ab_sample, required_workflow="portrait_focal_enhance_v1"
    )
    codes = {i.code for i in issues}
    assert "winner_evidence_below_wins" in codes


# ----------------------------- VLM guardrail -------------------------------


def test_vlm_rejects_uncalibrated(vlm_sample):
    vlm_sample["calibration_status"] = "not_calibrated_fail_closed"
    issues = validate_vlm_guardrail_report(vlm_sample)
    # uncalibrated is a *legal* value (fail-closed signal), so no schema issue.
    assert "calibration_status_invalid" not in {i.code for i in issues}


def test_vlm_rejects_unknown_calibration(vlm_sample):
    vlm_sample["calibration_status"] = "magic"
    issues = validate_vlm_guardrail_report(vlm_sample)
    assert "calibration_status_invalid" in {i.code for i in issues}


def test_vlm_rejects_agreement_out_of_range(vlm_sample):
    vlm_sample["agreement_rate"] = 1.5
    issues = validate_vlm_guardrail_report(vlm_sample)
    assert "agreement_rate_out_of_range" in {i.code for i in issues}


def test_vlm_rejects_guardrail_hard_veto_when_field_missing(vlm_sample):
    vlm_sample["candidate_promotion_guardrail"]["guardrail_status"] = "unknown"
    issues = validate_vlm_guardrail_report(vlm_sample)
    assert "guardrail_status_invalid" in {i.code for i in issues}


def test_vlm_nested_field_fallback(vlm_sample):
    # The validator should allow the canonical top-level field to satisfy
    # the requirement even when the nested form is absent.
    vlm_sample.pop("candidate_promotion_guardrail", None)
    issues = validate_vlm_guardrail_report(vlm_sample)
    codes = {i.code for i in issues}
    assert "candidate_promotion_guardrail_missing" in codes


# ----------------------------- Production gate -----------------------------


def test_production_gate_rejects_wrong_reason_code(pg_sample):
    pg_sample["production_gate"]["reason_code"] = "ready_for_review"
    issues = validate_production_gate_report(pg_sample)
    assert "reason_code_invalid" in {i.code for i in issues}


def test_production_gate_rejects_non_list_hard_defects(pg_sample):
    pg_sample["production_gate"]["hard_defect_codes"] = "none"
    issues = validate_production_gate_report(pg_sample)
    assert "hard_defect_codes_invalid" in {i.code for i in issues}


def test_production_gate_rejects_negative_wins(pg_sample):
    pg_sample["production_gate"]["candidate_win_count"] = -1
    issues = validate_production_gate_report(pg_sample)
    assert "count_invalid" in {i.code for i in issues}


# ----------------------------- Cross-report --------------------------------


def test_cross_report_detects_candidate_win_drift(ab_sample, pg_sample):
    ab_sample["candidate_win_count"] = 22
    pg_sample["production_gate"]["candidate_win_count"] = 19
    issues = validate_cross_report_invariants(ab_sample, pg_sample)
    assert any(i.code == "cross_report_drift" for i in issues)


def test_cross_report_clean_when_aligned(ab_sample, pg_sample):
    assert ab_sample["candidate_win_count"] == pg_sample["production_gate"]["candidate_win_count"]
    issues = validate_cross_report_invariants(ab_sample, pg_sample)
    assert issues == []


# ----------------------------- IO surfaces ---------------------------------


def test_validate_all_reports_missing_files(tmp_path: Path):
    paths = _ReportPaths(
        ab_report=tmp_path / CANONICAL_AB_REPORT_FILENAME,
        vlm_guardrail=tmp_path / CANONICAL_VLM_GUARDRAIL_FILENAME,
        production_gate=tmp_path / CANONICAL_PRODUCTION_GATE_FILENAME,
    )
    results = validate_all(paths, required_workflow="portrait_focal_enhance_v1")
    codes_per_report = {
        name: {i.code for i in issues} for name, issues in results.items() if issues
    }
    assert codes_per_report["ab_report"] == {"report_missing"}
    assert codes_per_report["vlm_guardrail_report"] == {"report_missing"}
    assert codes_per_report["production_gate_report"] == {"report_missing"}


def test_validate_all_rejects_invalid_json(tmp_path: Path, vlm_sample, pg_sample):
    paths = _ReportPaths(
        ab_report=tmp_path / CANONICAL_AB_REPORT_FILENAME,
        vlm_guardrail=tmp_path / CANONICAL_VLM_GUARDRAIL_FILENAME,
        production_gate=tmp_path / CANONICAL_PRODUCTION_GATE_FILENAME,
    )
    paths.ab_report.write_text("{not json", encoding="utf-8")
    paths.vlm_guardrail.write_text(json.dumps(vlm_sample), encoding="utf-8")
    paths.production_gate.write_text(json.dumps(pg_sample), encoding="utf-8")
    results = validate_all(paths, required_workflow="portrait_focal_enhance_v1")
    assert any(i.code == "report_unreadable" for i in results["ab_report"])
