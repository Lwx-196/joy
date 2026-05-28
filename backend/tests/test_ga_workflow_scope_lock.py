"""C0.5.4 — GA workflow scope lock at the gate boundary.

The gate must:

  - know that the GA-approved workflow is exactly ``portrait_focal_enhance_v1``;
  - default ``_comfyui_preflight`` to that constant when no runtime override
    is supplied (transparent to status-endpoint callers);
  - block promotion when the runtime workflow does not appear in the
    approval's ``approved_workflows`` list, even if every other signal
    (calibration, hashes, manifest bindings) is green.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend import ai_generation_adapter as adapter
from backend.scripts import compute_manifest_hashes as cmh
from backend.services import promotion_manifest_loader as manifest_loader


def _sha256_hex(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def test_ga_approved_workflow_constant_is_locked():
    assert adapter.GA_APPROVED_WORKFLOW == "portrait_focal_enhance_v1"


def test_validator_default_required_workflow_matches_gate():
    """The standalone validator must default to the same workflow name the
    gate enforces; drifting the two would let approvals signed only for
    ``local_region_enhance_v1`` slip past either layer."""
    from backend.scripts.validate_ab_validation_reports import (
        validate_ab_validation_report,
    )

    bad_report = {
        "validation_status": "ready_for_human_review",
        "ready_for_human_review": True,
        "promote_to_default": True,
        "comparable_pair_count": 1,
        "winner_evidence_count": 1,
        "candidate_win_count": 1,
        "promotion_approval": {
            "status": "approved",
            "approved": True,
            "scope": "comfyui_default_promotion_v1",
            "decision": "approve_default_promotion",
            "approver": "qa",
            "approved_at": "2026-05-28T22:55:00+00:00",
            "approved_workflows": ["local_region_enhance_v1"],
            "approved_evidence_sha256": {
                "vlm_guardrail_report": "sha256:" + "a" * 64,
                "production_gate_report": "sha256:" + "b" * 64,
            },
        },
    }
    issues = validate_ab_validation_report(
        bad_report, required_workflow=adapter.GA_APPROVED_WORKFLOW
    )
    assert any(i.code == "approved_workflows_missing_ga_scope" for i in issues)


def _build_aligned_env(tmp_path: Path, *, approved_workflows: list[str]) -> dict[str, Path]:
    promotion_dir = tmp_path / "case-workbench-ai" / "promotion"
    ab_dir = tmp_path / "case-workbench-ai" / "ab_runs"
    promotion_dir.mkdir(parents=True)
    ab_dir.mkdir(parents=True)
    vlm_path = ab_dir / cmh.CANONICAL_VLM_GUARDRAIL_REPORT.name
    pg_path = ab_dir / cmh.CANONICAL_PRODUCTION_GATE_REPORT.name
    ab_path = ab_dir / cmh.CANONICAL_AB_VALIDATION_REPORT.name
    vlm_path.write_text(
        json.dumps(
            {
                "calibration_status": "calibrated_for_fail_closed_review",
                "accepted_judgment_count": 64,
                "required_judgment_count_min": 50,
                "agreement_rate": 0.94,
                "required_agreement_rate_min": 0.9,
                "false_candidate_promotion_count": 0,
                "candidate_promotion_guardrail": {
                    "guardrail_status": "pass",
                    "manual_review_required_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    pg_path.write_text(
        json.dumps(
            {
                "production_gate": {
                    "reason_code": "promotion_approval_required",
                    "hard_defect_codes": [],
                    "candidate_win_count": 22,
                    "required_candidate_wins_min": 20,
                }
            }
        ),
        encoding="utf-8",
    )
    vlm_sha = _sha256_hex(vlm_path.read_bytes())
    pg_sha = _sha256_hex(pg_path.read_bytes())
    ab_report = {
        "validation_status": "ready_for_human_review",
        "ready_for_human_review": True,
        "promote_to_default": True,
        "comparable_pair_count": 32,
        "winner_evidence_count": 24,
        "candidate_win_count": 22,
        "promotion_approval": {
            "status": "approved",
            "approved": True,
            "scope": "comfyui_default_promotion_v1",
            "decision": "approve_default_promotion",
            "approver": "qa@example.com",
            "approved_at": "2026-05-28T22:55:00+00:00",
            "approved_workflows": approved_workflows,
            "approved_evidence_sha256": {
                "vlm_guardrail_report": vlm_sha,
                "production_gate_report": pg_sha,
            },
        },
    }
    ab_path.write_text(json.dumps(ab_report), encoding="utf-8")
    ab_sha = _sha256_hex(ab_path.read_bytes())
    manifest_path = promotion_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "promotion_state": "shadow",
                "scope": "production",
                "bindings": {
                    "ab_validation_report_hash": ab_sha,
                    "vlm_guardrail_report_hash": vlm_sha,
                    "production_gate_report_hash": pg_sha,
                },
            }
        ),
        encoding="utf-8",
    )
    return {
        "manifest": manifest_path,
        "ab_path": ab_path,
        "vlm_path": vlm_path,
        "pg_path": pg_path,
    }


def test_gate_blocks_promotion_when_runtime_workflow_not_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    env = _build_aligned_env(tmp_path, approved_workflows=["local_region_enhance_v1"])
    monkeypatch.setattr(manifest_loader, "DEFAULT_MANIFEST_PATH", env["manifest"])
    result = adapter._comfyui_ab_validation_gate(
        report_path=env["ab_path"],
        vlm_guardrail_report_path=env["vlm_path"],
        production_gate_report_path=env["pg_path"],
        model_name=adapter.GA_APPROVED_WORKFLOW,
    )
    blockers = result.get("default_promotion_blockers") or []
    assert "workflow_not_approved_for_default" in blockers
    assert result["promote_to_default"] is False


def test_gate_allows_when_ga_workflow_in_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    env = _build_aligned_env(
        tmp_path, approved_workflows=[adapter.GA_APPROVED_WORKFLOW]
    )
    monkeypatch.setattr(manifest_loader, "DEFAULT_MANIFEST_PATH", env["manifest"])
    result = adapter._comfyui_ab_validation_gate(
        report_path=env["ab_path"],
        vlm_guardrail_report_path=env["vlm_path"],
        production_gate_report_path=env["pg_path"],
        model_name=adapter.GA_APPROVED_WORKFLOW,
    )
    assert "workflow_not_approved_for_default" not in (
        result.get("default_promotion_blockers") or []
    )
    assert result["promote_to_default"] is True


def test_preflight_signature_accepts_workflow_kwarg():
    """The constant must be reachable from the preflight signature even if we
    cannot actually probe ComfyUI in the unit-test sandbox (no live server).
    """
    import inspect

    sig = inspect.signature(adapter._comfyui_preflight)
    assert "workflow_name" in sig.parameters
    assert sig.parameters["workflow_name"].default is None
