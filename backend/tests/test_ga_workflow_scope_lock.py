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


def test_runtime_renderer_threads_workflow_name_into_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """S1: the sole production renderer ``_run_comfyui_workflow_once`` must pass
    its resolved workflow name into preflight. Before the fix it called
    ``_comfyui_preflight()`` with no arg, so ``effective_workflow`` always fell
    back to ``GA_APPROVED_WORKFLOW`` — making the scope check a tautology that a
    routing mistake (e.g. local_region_enhance_v1) would silently bypass."""
    captured: dict[str, object] = {}

    def fake_preflight(*, workflow_name=None):
        captured["workflow_name"] = workflow_name
        # Short-circuit the renderer immediately after preflight so we never
        # touch the network (core node "missing" raises in the next line).
        return {"node_status": {"core_missing": ["__stop__"]}}

    monkeypatch.setattr(adapter, "_comfyui_preflight", fake_preflight)
    with pytest.raises(RuntimeError, match="missing required nodes"):
        adapter._run_comfyui_workflow_once(
            tmp_path / "in.png",
            output_dir=tmp_path,
            workflow_name="local_region_enhance_v1",
        )
    assert captured["workflow_name"] == "local_region_enhance_v1"


def _stub_comfyui_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the ComfyUI HTTP boundary + model profile so ``_comfyui_preflight``
    runs hermetically (no live server, no real model registry)."""

    def fake_json(path, payload=None, *, timeout=30):
        if path == "/system_stats":
            return {"system": {"comfyui_version": "test"}}
        if path == "/object_info":
            return {n: {} for n in ("LoadImage", "SaveImage", "KSampler", "VAEEncodeForInpaint")}
        raise AssertionError(f"unexpected ComfyUI call in unit test: {path}")

    monkeypatch.setattr(adapter, "_comfyui_json", fake_json)
    monkeypatch.setattr(
        adapter,
        "_build_comfyui_model_profile",
        lambda *a, **k: {
            "production_ready": True,
            "readiness_reasons": [],
            "capabilities": {},
            "models": {},
        },
    )


def test_preflight_non_ga_runtime_workflow_surfaces_scope_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """S1 production-path assertion: a non-GA runtime workflow routed through
    the real ``_comfyui_preflight`` must surface ``workflow_not_approved_for_default``
    (the approval is signed for the GA workflow only). This is the end-to-end
    proof — through preflight, not the gate directly — that the threaded
    workflow name reaches the scope check."""
    env = _build_aligned_env(tmp_path, approved_workflows=[adapter.GA_APPROVED_WORKFLOW])
    monkeypatch.setattr(manifest_loader, "DEFAULT_MANIFEST_PATH", env["manifest"])
    monkeypatch.setattr(adapter, "COMFYUI_AB_VALIDATION_REPORT_PATH", env["ab_path"])
    monkeypatch.setattr(adapter, "COMFYUI_VLM_GUARDRAIL_REPORT_PATH", env["vlm_path"])
    monkeypatch.setattr(adapter, "COMFYUI_PRODUCTION_GATE_REPORT_PATH", env["pg_path"])
    _stub_comfyui_probe(monkeypatch)

    result = adapter._comfyui_preflight(workflow_name="local_region_enhance_v1")
    assert result["gated_workflow"] == "local_region_enhance_v1"
    blockers = result["ab_validation"]["default_promotion_blockers"]
    assert "workflow_not_approved_for_default" in blockers


def test_preflight_gated_workflow_reflects_input_else_defaults_to_ga(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Low (preflight behavior): ``gated_workflow`` echoes the passed workflow
    name, and falls back to ``GA_APPROVED_WORKFLOW`` when omitted or blank."""
    missing = tmp_path / "nope.json"
    monkeypatch.setattr(adapter, "COMFYUI_AB_VALIDATION_REPORT_PATH", missing)
    monkeypatch.setattr(adapter, "COMFYUI_VLM_GUARDRAIL_REPORT_PATH", missing)
    monkeypatch.setattr(adapter, "COMFYUI_PRODUCTION_GATE_REPORT_PATH", missing)
    _stub_comfyui_probe(monkeypatch)

    assert adapter._comfyui_preflight(workflow_name="custom_wf_v9")["gated_workflow"] == "custom_wf_v9"
    assert adapter._comfyui_preflight()["gated_workflow"] == adapter.GA_APPROVED_WORKFLOW
    assert adapter._comfyui_preflight(workflow_name="   ")["gated_workflow"] == adapter.GA_APPROVED_WORKFLOW


def test_validator_cli_default_required_workflow_binds_ga_constant():
    """Low (validator argparse default): the ``--required-workflow`` default must
    be the named GA constant — and that constant must equal the gate's SoT — so
    the standalone validator and the runtime gate cannot drift apart."""
    from backend.scripts import validate_ab_validation_reports as v

    assert v.GA_APPROVED_WORKFLOW == adapter.GA_APPROVED_WORKFLOW
    parser = v._build_arg_parser()
    assert parser.get_default("required_workflow") == adapter.GA_APPROVED_WORKFLOW
