"""P2.1 boundary tests for `backend.scripts.compute_manifest_hashes`.

Five required cases per plan §P2.1:
  1. hash mismatch
  2. scope invalid
  3. approver missing (when state != draft)
  4. expired approved_at (older than TTL)
  5. promotion_state invalid

Each test isolates the validator with a tmp manifest dict + injected `now` so
no DB / FS state is touched.
"""
from __future__ import annotations

import datetime as _dt
import json
from copy import deepcopy
from pathlib import Path

import pytest

from backend.scripts.compute_manifest_hashes import (
    APPROVAL_TTL_DAYS,
    BINDING_NAMES,
    compute_all_bindings,
    compute_production_gate_hash,
    compute_vlm_calibration_hash,
    read_manifest,
    validate_manifest,
    write_manifest_bindings,
)


FROZEN_NOW = _dt.datetime(2026, 5, 27, 15, 0, 0, tzinfo=_dt.timezone.utc)


def _good_manifest() -> dict:
    """Baseline that should pass all checks given matching expected bindings."""
    bindings = {name: f"sha256:{'a' * 64}" for name in BINDING_NAMES}
    return {
        "schema_version": 1,
        "version": "v1.0.0",
        "scope": "production",
        "approver": "linweixiang0301@gmail.com",
        "approved_at": "2026-05-20T10:00:00+00:00",
        "promotion_state": "approved",
        "bindings": deepcopy(bindings),
        "rollback_baseline": {
            "manifest_ref": None,
            "bindings": {name: None for name in BINDING_NAMES},
        },
    }


def _good_expected() -> dict[str, str]:
    return {name: f"sha256:{'a' * 64}" for name in BINDING_NAMES}


# ----------------------------- baseline sanity -----------------------------


def test_baseline_passes_when_hashes_match():
    issues = validate_manifest(
        _good_manifest(),
        expected_bindings=_good_expected(),
        now=FROZEN_NOW,
    )
    assert issues == [], f"unexpected issues: {[i.as_dict() for i in issues]}"


# ----------------------------- boundary cases ------------------------------


def test_boundary_hash_mismatch_flagged_per_binding():
    manifest = _good_manifest()
    # Flip one binding to wrong value; others stay correct.
    manifest["bindings"]["vlm_calibration_hash"] = "sha256:" + ("b" * 64)
    issues = validate_manifest(
        manifest,
        expected_bindings=_good_expected(),
        now=FROZEN_NOW,
    )
    codes = [(i.code, i.field) for i in issues]
    assert ("hash_mismatch", "bindings.vlm_calibration_hash") in codes
    # The other three bindings must NOT be flagged.
    for name in BINDING_NAMES:
        if name == "vlm_calibration_hash":
            continue
        assert ("hash_mismatch", f"bindings.{name}") not in codes


def test_boundary_scope_invalid_flagged():
    manifest = _good_manifest()
    manifest["scope"] = "yolo-prod"  # not in {production, staging, canary}
    issues = validate_manifest(
        manifest,
        expected_bindings=_good_expected(),
        now=FROZEN_NOW,
    )
    codes = [i.code for i in issues]
    assert "scope_invalid" in codes


def test_boundary_approver_missing_when_state_pending_review():
    manifest = _good_manifest()
    manifest["approver"] = ""  # empty string treated as missing
    manifest["promotion_state"] = "pending_review"
    # approved_at no longer required at pending_review — drop to keep test focused.
    manifest["approved_at"] = None
    issues = validate_manifest(
        manifest,
        expected_bindings=_good_expected(),
        now=FROZEN_NOW,
    )
    codes = [i.code for i in issues]
    assert "approver_missing" in codes


def test_boundary_approved_at_expired_beyond_ttl():
    manifest = _good_manifest()
    # 31d old vs 30d TTL → must trigger approved_at_expired.
    expired = FROZEN_NOW - _dt.timedelta(days=APPROVAL_TTL_DAYS + 1)
    manifest["approved_at"] = expired.isoformat()
    issues = validate_manifest(
        manifest,
        expected_bindings=_good_expected(),
        now=FROZEN_NOW,
    )
    codes = [i.code for i in issues]
    assert "approved_at_expired" in codes
    # Boundary: exactly at TTL must NOT trigger expiry.
    on_edge = FROZEN_NOW - _dt.timedelta(days=APPROVAL_TTL_DAYS)
    manifest["approved_at"] = on_edge.isoformat()
    issues2 = validate_manifest(
        manifest,
        expected_bindings=_good_expected(),
        now=FROZEN_NOW,
    )
    assert "approved_at_expired" not in [i.code for i in issues2]


def test_boundary_promotion_state_invalid_value():
    manifest = _good_manifest()
    manifest["promotion_state"] = "kinda-approved"
    issues = validate_manifest(
        manifest,
        expected_bindings=_good_expected(),
        now=FROZEN_NOW,
    )
    codes = [i.code for i in issues]
    assert "promotion_state_invalid" in codes


# --------- additional structural assertions (cheap, share fixture) ---------


def test_compute_production_gate_hash_is_deterministic(tmp_path: Path):
    """Production-gate hash should depend only on file content, not order
    or working directory."""
    repo = tmp_path / "repo"
    (repo / "backend" / "services").mkdir(parents=True)
    (repo / "backend").mkdir(exist_ok=True)
    (repo / "backend" / "services" / "pre_render_gate.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    (repo / "backend" / "simulation_quality.py").write_text(
        "y = 2\n", encoding="utf-8"
    )
    h1 = compute_production_gate_hash(repo)
    h2 = compute_production_gate_hash(repo)
    assert h1 == h2
    assert h1.startswith("sha256:")
    # Mutating one source flips the hash.
    (repo / "backend" / "simulation_quality.py").write_text(
        "y = 3\n", encoding="utf-8"
    )
    assert compute_production_gate_hash(repo) != h1


def test_compute_vlm_calibration_hash_handles_missing_outputs_dir(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "backend" / "services").mkdir(parents=True)
    (repo / "backend" / "services" / "vlm_calibration.py").write_text(
        "calib = True\n", encoding="utf-8"
    )
    # No vlm_calibration_outputs/ dir → still produces a sha256.
    h = compute_vlm_calibration_hash(repo)
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 64


def test_write_manifest_bindings_preserves_approval_fields(tmp_path: Path):
    manifest_path = tmp_path / "manifest.json"
    initial = _good_manifest()
    initial["bindings"] = {name: None for name in BINDING_NAMES}
    manifest_path.write_text(json.dumps(initial), encoding="utf-8")

    new_bindings = {name: f"sha256:{'c' * 64}" for name in BINDING_NAMES}
    result = write_manifest_bindings(manifest_path, new_bindings)

    on_disk = read_manifest(manifest_path)
    assert on_disk["bindings"] == new_bindings
    # Approval fields untouched by --write.
    assert on_disk["approver"] == initial["approver"]
    assert on_disk["approved_at"] == initial["approved_at"]
    assert on_disk["promotion_state"] == initial["promotion_state"]
    assert result == on_disk


def test_compute_all_bindings_returns_all_four(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "backend" / "services").mkdir(parents=True)
    (repo / "backend" / "services" / "vlm_calibration.py").write_text(
        "a = 1\n", encoding="utf-8"
    )
    (repo / "backend" / "services" / "pre_render_gate.py").write_text(
        "b = 1\n", encoding="utf-8"
    )
    (repo / "backend" / "simulation_quality.py").write_text(
        "c = 1\n", encoding="utf-8"
    )
    bindings = compute_all_bindings(repo)
    assert set(bindings.keys()) == set(BINDING_NAMES)
    for v in bindings.values():
        assert v.startswith("sha256:")
        assert len(v) == len("sha256:") + 64
