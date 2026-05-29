"""C0.5.3 — manifest-binding double-check inside the ComfyUI AB validation gate.

These tests cover the new ``_manifest_binding_blockers`` path in
``backend.ai_generation_adapter`` and its wiring into
``_comfyui_ab_validation_gate``. We exercise the gate as a closed black box
to lock in the behaviour the rollout runbook depends on:

  - missing manifest         → fails closed with ``manifest_unavailable_for_binding_check``
  - missing binding slot     → ``manifest_binding_*_missing``
  - missing report file      → ``manifest_binding_*_file_missing``
  - hash drift               → ``manifest_binding_*_drift``
  - aligned bindings + hashes → no manifest blockers, only the upstream ones (if any)
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


def _approval(approved_evidence_sha256: dict[str, str]) -> dict:
    return {
        "status": "approved",
        "approved": True,
        "scope": "comfyui_default_promotion_v1",
        "decision": "approve_default_promotion",
        "approver": "qa@example.com",
        "approved_at": "2026-05-28T22:55:00+00:00",
        "approved_workflows": ["portrait_focal_enhance_v1"],
        "approved_evidence_sha256": approved_evidence_sha256,
    }


def _ab_report(approved_evidence_sha256: dict[str, str]) -> dict:
    return {
        "validation_status": "ready_for_human_review",
        "ready_for_human_review": True,
        "promote_to_default": True,
        "comparable_pair_count": 32,
        "winner_evidence_count": 24,
        "candidate_win_count": 22,
        "promotion_approval": _approval(approved_evidence_sha256),
    }


def _vlm_report() -> dict:
    return {
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


def _pg_report() -> dict:
    return {
        "production_gate": {
            "reason_code": "promotion_approval_required",
            "hard_defect_codes": [],
            "candidate_win_count": 22,
            "required_candidate_wins_min": 20,
        }
    }


@pytest.fixture()
def gate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Stand up a self-contained manifest + 3 canonical reports under tmp_path."""
    repo = tmp_path
    promotion_dir = repo / "case-workbench-ai" / "promotion"
    ab_dir = repo / "case-workbench-ai" / "ab_runs"
    promotion_dir.mkdir(parents=True)
    ab_dir.mkdir(parents=True)

    ab_path = ab_dir / cmh.CANONICAL_AB_VALIDATION_REPORT.name
    vlm_path = ab_dir / cmh.CANONICAL_VLM_GUARDRAIL_REPORT.name
    pg_path = ab_dir / cmh.CANONICAL_PRODUCTION_GATE_REPORT.name
    vlm_path.write_text(json.dumps(_vlm_report()), encoding="utf-8")
    pg_path.write_text(json.dumps(_pg_report()), encoding="utf-8")

    vlm_sha = _sha256_hex(vlm_path.read_bytes())
    pg_sha = _sha256_hex(pg_path.read_bytes())

    ab_path.write_text(
        json.dumps(
            _ab_report({
                "vlm_guardrail_report": vlm_sha,
                "production_gate_report": pg_sha,
            })
        ),
        encoding="utf-8",
    )
    ab_sha = _sha256_hex(ab_path.read_bytes())

    manifest_path = promotion_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps({
            "schema_version": 1,
            "promotion_state": "shadow",
            "scope": "production",
            "bindings": {
                "ab_validation_report_hash": ab_sha,
                "vlm_guardrail_report_hash": vlm_sha,
                "production_gate_report_hash": pg_sha,
            },
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(manifest_loader, "DEFAULT_MANIFEST_PATH", manifest_path)
    return {
        "manifest": manifest_path,
        "ab_path": ab_path,
        "vlm_path": vlm_path,
        "pg_path": pg_path,
    }


def _run_gate(env: dict[str, Path]) -> dict:
    return adapter._comfyui_ab_validation_gate(
        report_path=env["ab_path"],
        vlm_guardrail_report_path=env["vlm_path"],
        production_gate_report_path=env["pg_path"],
        model_name="portrait_focal_enhance_v1",
    )


def test_aligned_bindings_emit_no_manifest_blockers(gate_env):
    result = _run_gate(gate_env)
    blockers = result.get("default_promotion_blockers") or []
    manifest_blockers = [b for b in blockers if b.startswith("manifest_binding_")]
    assert manifest_blockers == [], blockers
    assert result["promote_to_default"] is True


def test_manifest_missing_blocks_promotion(gate_env, monkeypatch: pytest.MonkeyPatch):
    gate_env["manifest"].unlink()
    result = _run_gate(gate_env)
    blockers = result.get("default_promotion_blockers") or []
    assert "manifest_unavailable_for_binding_check" in blockers
    assert result["promote_to_default"] is False


def test_binding_drift_blocks_promotion(gate_env):
    manifest = json.loads(gate_env["manifest"].read_text(encoding="utf-8"))
    manifest["bindings"]["vlm_guardrail_report_hash"] = "sha256:" + ("0" * 64)
    gate_env["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    result = _run_gate(gate_env)
    blockers = result.get("default_promotion_blockers") or []
    assert "manifest_binding_vlm_guardrail_report_hash_drift" in blockers
    assert result["promote_to_default"] is False


def test_binding_slot_missing_blocks_promotion(gate_env):
    manifest = json.loads(gate_env["manifest"].read_text(encoding="utf-8"))
    del manifest["bindings"]["ab_validation_report_hash"]
    gate_env["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    result = _run_gate(gate_env)
    blockers = result.get("default_promotion_blockers") or []
    assert "manifest_binding_ab_validation_report_hash_missing" in blockers


def test_binding_file_missing_blocks_promotion(gate_env):
    gate_env["vlm_path"].unlink()
    result = _run_gate(gate_env)
    blockers = result.get("default_promotion_blockers") or []
    # Upstream report-missing fires before binding-file check, but both
    # signal the same outcome to operators: cannot promote.
    assert result["promote_to_default"] is False
    assert any(
        b
        in {
            "vlm_guardrail_report_missing",
            "manifest_binding_vlm_guardrail_report_hash_file_missing",
        }
        for b in blockers
    )
