"""Wave 13 manifest + SLO eval hardening (nyquist H-4 / H-5 / H-6).

Three closely-related defensive gates landed together because they all
guard the manifest → SLO → rollback pipeline against partial / corrupt /
hostile inputs that earlier waves left fall-through:

  H-4  promotion_manifest_loader.load_manifest rejects manifests carrying
       an unsupported ``schema_version`` so a future schema bump cannot be
       silently consumed by an older loader. Missing schema_version is
       tolerated (BC).

  H-5  promotion_slo_monitor.evaluate_window contains OSError from the
       paused_state sidecar so a disk-full / permission-denied write
       cannot crash the entire SLO eval. Emits a synthetic
       ``paused_state_write_failed`` violation + escalates to
       ``stop_loss_halt`` so on-call sees the issue.

  H-6  promotion_rollback_applier.apply_rollback_decision refuses to
       transition out of an unknown / invalid ``promotion_state``
       (missing field, typo, future state) instead of writing
       ``from_state=None`` into the audit trail.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from backend.services import (
    promotion_manifest_loader as manifest_loader,
    promotion_rollback_applier as applier,
    promotion_slo_monitor as slo,
)


# ---------------------------------------------------------------------------
# H-4: schema_version drift detection
# ---------------------------------------------------------------------------


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestH4SchemaVersionDrift:

    def test_supported_version_loads(self, tmp_path: Path):
        target = tmp_path / "manifest.json"
        _write_manifest(target, {"schema_version": 1, "promotion_state": "p10"})
        m = manifest_loader.load_manifest(target)
        assert isinstance(m, dict)
        assert m["promotion_state"] == "p10"

    def test_missing_schema_version_tolerated(self, tmp_path: Path):
        # Backward-compat: hand-rolled manifests without schema_version
        # continue to load. ``None`` is intentional pass-through.
        target = tmp_path / "manifest.json"
        _write_manifest(target, {"promotion_state": "shadow"})
        m = manifest_loader.load_manifest(target)
        assert isinstance(m, dict)

    def test_future_version_rejected(self, tmp_path: Path):
        target = tmp_path / "manifest.json"
        _write_manifest(target, {"schema_version": 2, "promotion_state": "p10"})
        assert manifest_loader.load_manifest(target) is None

    def test_negative_version_rejected(self, tmp_path: Path):
        target = tmp_path / "manifest.json"
        _write_manifest(target, {"schema_version": -1, "promotion_state": "p10"})
        assert manifest_loader.load_manifest(target) is None

    def test_boolean_version_rejected(self, tmp_path: Path):
        target = tmp_path / "manifest.json"
        # JSON ``true`` survives ``isinstance(x, int)`` so we explicitly
        # screen booleans before the membership check.
        _write_manifest(target, {"schema_version": True, "promotion_state": "p10"})
        assert manifest_loader.load_manifest(target) is None

    def test_string_version_rejected(self, tmp_path: Path):
        target = tmp_path / "manifest.json"
        _write_manifest(target, {"schema_version": "1", "promotion_state": "p10"})
        assert manifest_loader.load_manifest(target) is None

    def test_drift_propagates_to_should_promote(self, tmp_path: Path):
        target = tmp_path / "manifest.json"
        # Fully populated p100 manifest but with unsupported version
        _write_manifest(target, {
            "schema_version": 99,
            "promotion_state": "p100",
            "bindings": {"vlm_calibration_hash": "sha256:abc"},
        })
        m = manifest_loader.load_manifest(target)
        # load_manifest rejects → callers see None → should_promote=False
        assert m is None
        # End-to-end: pass through manifest=None override path
        assert manifest_loader.should_promote(42, manifest=None) is False

    def test_supported_versions_constant_exported(self):
        assert 1 in manifest_loader.SUPPORTED_SCHEMA_VERSIONS
        # Future bumps require lockstep update
        assert isinstance(manifest_loader.SUPPORTED_SCHEMA_VERSIONS, frozenset)


# ---------------------------------------------------------------------------
# H-5: evaluate_window contains paused_state OSError
# ---------------------------------------------------------------------------


class TestH5PausedStateWriteFailureContainment:

    def test_oserror_in_paused_state_synthesizes_violation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """If ``_evaluate_paused_state`` raises OSError (e.g. disk full),
        evaluate_window MUST still return an SLOReport — with a
        synthetic ``paused_state_write_failed`` violation +
        ``stop_loss_halt`` recommendation so on-call sees the issue."""

        # SLO_TEST_MODE so placeholder seed thresholds load + minimum_sample_size
        # path stays predictable
        monkeypatch.setenv("SLO_TEST_MODE", "1")

        def raising_evaluate(**kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(slo, "_evaluate_paused_state", raising_evaluate)

        # Stub out DB sample counters so sample_size < min_sample
        for fn in (
            "_compute_comfyui_failure_rate",
            "_compute_vlm_disagreement_rate",
            "_compute_delivery_gate_rejection_rate",
        ):
            monkeypatch.setattr(slo, fn, lambda *a, **kw: {
                "terminal_total": 0, "sample_count": 0, "counted": 0,
                "failed": 0, "rate": 0.0, "denominator_label": "test",
            })
        monkeypatch.setattr(slo, "_compute_pre_render_gate_blocker_count",
                            lambda *a, **kw: {"blocker_count": 0, "by_reason": {}})

        class _NoopConn:
            def close(self):
                pass

        report = slo.evaluate_window(
            window_hours=24,
            conn=_NoopConn(),
            promotion_state="p10",
            paused_state_path=tmp_path / "paused.json",
        )

        # Eval survived rather than propagating OSError
        assert report.within_slo is False
        # Single synthetic violation surfaces the problem
        dims = {(v.get("dimension") if isinstance(v, dict) else None)
                for v in report.violations}
        assert "paused_state_write_failed" in dims
        # Escalated to stop_loss_halt (not silently rolled back)
        assert report.recommendation == slo.RECOMMENDATION_STOP_LOSS_HALT
        # Notes capture the root cause for the operator
        assert "paused_state_write_failed" in (report.notes or "")

    def test_paused_state_write_failed_context_has_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("SLO_TEST_MODE", "1")
        monkeypatch.setattr(slo, "_evaluate_paused_state",
                            lambda **kw: (_ for _ in ()).throw(PermissionError(13, "Permission denied")))
        for fn in ("_compute_comfyui_failure_rate", "_compute_vlm_disagreement_rate",
                   "_compute_delivery_gate_rejection_rate"):
            monkeypatch.setattr(slo, fn, lambda *a, **kw: {
                "terminal_total": 0, "sample_count": 0, "counted": 0,
                "failed": 0, "rate": 0.0, "denominator_label": "test",
            })
        monkeypatch.setattr(slo, "_compute_pre_render_gate_blocker_count",
                            lambda *a, **kw: {"blocker_count": 0, "by_reason": {}})

        class _NoopConn:
            def close(self):
                pass

        report = slo.evaluate_window(
            window_hours=24, conn=_NoopConn(),
            promotion_state="p25",
            paused_state_path=tmp_path / "paused.json",
        )
        v = next(v for v in report.violations
                 if isinstance(v, dict) and v.get("dimension") == "paused_state_write_failed")
        ctx = v.get("context") or {}
        assert "hint" in ctx
        assert "writable" in ctx["hint"] or "disk" in ctx["hint"]
        assert v["actual"] == "PermissionError"


# ---------------------------------------------------------------------------
# H-6: applier refuses to roll back from unknown promotion_state
# ---------------------------------------------------------------------------


def _decision(rec: str = "rollback") -> dict[str, Any]:
    return {
        "recommendation": rec,
        "violations": [{"dimension": "comfyui_failure_rate",
                        "actual": 0.5, "threshold": 0.05}],
        "within_slo": False,
        "sample_size": 100,
        "window_hours": 24,
    }


def _good_bindings() -> dict[str, str]:
    return {"vlm_calibration_hash": "sha256:abc"}


class TestH6InvalidManifestStateRefusal:

    def test_missing_promotion_state_refused(self, tmp_path: Path):
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            # NO promotion_state field
            "bindings": _good_bindings(),
        }))
        result = applier.apply_rollback_decision(
            _decision(), manifest_path=manifest_path, dry_run=False,
        )
        assert result["applied"] is False
        assert result["reason"] == applier.REASON_INVALID_MANIFEST_STATE
        # Manifest left untouched
        loaded = json.loads(manifest_path.read_text())
        assert "promotion_state" not in loaded  # we did not write one in

    def test_invalid_promotion_state_refused(self, tmp_path: Path):
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "promotion_state": "wibble",  # typo / future state
            "bindings": _good_bindings(),
        }))
        result = applier.apply_rollback_decision(
            _decision(), manifest_path=manifest_path, dry_run=False,
        )
        assert result["applied"] is False
        assert result["reason"] == applier.REASON_INVALID_MANIFEST_STATE
        # Error message names the offending value
        assert "'wibble'" in (result.get("error") or "")
        # Manifest left untouched (still 'wibble', not 'rolled_back')
        loaded = json.loads(manifest_path.read_text())
        assert loaded["promotion_state"] == "wibble"

    def test_non_string_promotion_state_refused(self, tmp_path: Path):
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "promotion_state": 42,
            "bindings": _good_bindings(),
        }))
        result = applier.apply_rollback_decision(
            _decision(), manifest_path=manifest_path, dry_run=False,
        )
        assert result["reason"] == applier.REASON_INVALID_MANIFEST_STATE

    def test_valid_promotion_state_still_proceeds(self, tmp_path: Path):
        """Sanity: a manifest with a valid promotion_state still gets through
        the H-6 gate (and lands on subsequent gates downstream)."""
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "promotion_state": "p10",
            "bindings": _good_bindings(),
        }))
        result = applier.apply_rollback_decision(
            _decision(), manifest_path=manifest_path, dry_run=True,
        )
        # dry_run preview path — applier reached the plan-build stage
        # (which only happens AFTER H-6 succeeds).
        assert result["reason"] == applier.REASON_DRY_RUN
        assert result.get("would_apply") is True

    def test_dry_run_respects_h6_refusal(self, tmp_path: Path):
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "promotion_state": None,  # explicit null
            "bindings": _good_bindings(),
        }))
        result = applier.apply_rollback_decision(
            _decision(), manifest_path=manifest_path, dry_run=True,
        )
        assert result["reason"] == applier.REASON_INVALID_MANIFEST_STATE

    def test_already_rolled_back_short_circuits_before_h6(self, tmp_path: Path):
        """``ROLLED_BACK_STATE`` is in VALID_STATES, so the already-rolled-back
        short-circuit fires before the H-6 invalid-state gate would."""
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "promotion_state": "rolled_back",
            "bindings": _good_bindings(),
        }))
        result = applier.apply_rollback_decision(
            _decision(), manifest_path=manifest_path, dry_run=True,
        )
        assert result["reason"] == applier.REASON_ALREADY_ROLLED_BACK

    def test_invalid_state_reason_exported(self):
        assert hasattr(applier, "REASON_INVALID_MANIFEST_STATE")
        assert applier.REASON_INVALID_MANIFEST_STATE == "invalid_manifest_state"
