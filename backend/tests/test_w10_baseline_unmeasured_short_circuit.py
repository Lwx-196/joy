"""Wave 10 eval M-3: applier short-circuits when only violation is baseline_unmeasured.

Per ~/.claude/plans/p10-deploy-gate-unlock.md follow-ups and Wave 4 W4-1 eval
finding: an SLO report with placeholder thresholds (computed_by=manual_seed)
emits ``baseline_unmeasured`` and recommends ``rollback`` even when actual
metrics are healthy. Rolling back here punishes the operator for not yet
calibrating, instead of for a real quality regression. This test guards the
short-circuit added in Wave 10 (no rollback, dedicated reason +
operator-facing warning pointing at calibrate_slo_baseline CLI).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.services import promotion_rollback_applier as applier


def _decision(violations: list[dict[str, Any]], recommendation: str = "rollback") -> dict:
    return {
        "recommendation": recommendation,
        "violations": violations,
        "within_slo": False,
        "sample_size": 100,
        "window_hours": 48,
    }


class TestBaselineUnmeasuredShortCircuit:

    def test_only_baseline_unmeasured_violation_short_circuits(self, tmp_path: Path):
        """Single baseline_unmeasured violation → no rollback, dedicated reason."""
        manifest_path = tmp_path / "manifest.json"
        # Manifest doesn't need to exist for this short-circuit
        decision = _decision([
            {"dimension": "baseline_unmeasured", "actual": "N/A", "threshold": "N/A",
             "context": {"computed_by": "manual_seed", "sample_size": 0}}
        ])
        result = applier.apply_rollback_decision(decision, manifest_path=manifest_path, dry_run=False)
        assert result["applied"] is False
        assert result["reason"] == applier.REASON_BASELINE_UNMEASURED_ONLY
        assert "calibrate_slo_baseline" in (result.get("warning") or "")
        # Manifest not touched
        assert not manifest_path.exists()

    def test_baseline_unmeasured_plus_real_violation_proceeds(self, tmp_path: Path):
        """Mixed violations (baseline_unmeasured + real metric) → NOT short-circuited."""
        # We can't easily reach the real rollback path without a full manifest +
        # bindings setup, but the short-circuit branch should fall through.
        # If the function returns ``baseline_unmeasured_only`` reason here it's a
        # FALSE POSITIVE — the test catches that by asserting the short-circuit
        # is NOT taken.
        manifest_path = tmp_path / "missing_manifest.json"
        decision = _decision([
            {"dimension": "baseline_unmeasured", "actual": "N/A", "threshold": "N/A",
             "context": {"computed_by": "manual_seed", "sample_size": 0}},
            {"dimension": "comfyui_failure_rate", "actual": 0.25, "threshold": 0.05},
        ])
        result = applier.apply_rollback_decision(decision, manifest_path=manifest_path, dry_run=True)
        # Should NOT short-circuit on baseline_unmeasured (real violation present)
        assert result["reason"] != applier.REASON_BASELINE_UNMEASURED_ONLY
        # Will hit no_manifest because manifest path doesn't exist — that's fine
        assert result["reason"] == applier.REASON_NO_MANIFEST

    def test_dry_run_respects_short_circuit(self, tmp_path: Path):
        decision = _decision([
            {"dimension": "baseline_unmeasured", "actual": "N/A", "threshold": "N/A"},
        ])
        result = applier.apply_rollback_decision(decision, manifest_path=tmp_path / "m.json", dry_run=True)
        assert result["applied"] is False
        assert result["reason"] == applier.REASON_BASELINE_UNMEASURED_ONLY
        assert result["dry_run"] is True

    def test_empty_violations_falls_through(self, tmp_path: Path):
        """recommendation=rollback with empty violations list — short-circuit shouldn't fire."""
        decision = _decision([])  # empty
        manifest_path = tmp_path / "missing.json"
        result = applier.apply_rollback_decision(decision, manifest_path=manifest_path, dry_run=True)
        # Falls through to normal rollback path → no_manifest because path doesn't exist
        assert result["reason"] == applier.REASON_NO_MANIFEST

    def test_violations_with_missing_dimension_field_falls_through(self, tmp_path: Path):
        """Malformed violations dict without 'dimension' key — short-circuit shouldn't false-positive."""
        decision = _decision([
            {"not_dimension": "garbage"},
        ])
        manifest_path = tmp_path / "missing.json"
        result = applier.apply_rollback_decision(decision, manifest_path=manifest_path, dry_run=True)
        # Set of dimensions becomes {""} → not equal to {"baseline_unmeasured"} → falls through
        assert result["reason"] != applier.REASON_BASELINE_UNMEASURED_ONLY

    def test_continue_recommendation_not_affected(self, tmp_path: Path):
        """recommendation='continue' short-circuits before baseline_unmeasured check."""
        decision = _decision([
            {"dimension": "baseline_unmeasured"},
        ], recommendation="continue")
        result = applier.apply_rollback_decision(decision, manifest_path=tmp_path / "m.json", dry_run=False)
        assert result["reason"] == applier.REASON_NO_ROLLBACK_NEEDED
