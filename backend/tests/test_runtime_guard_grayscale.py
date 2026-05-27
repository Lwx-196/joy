"""Integration tests for plan §P2.3 runtime guard wiring.

Verifies that the promotion-manifest decision (`should_promote`) actually
flows through into:
  * `backend.ai_generation_adapter._build_comfyui_policy` — ComfyUI audit
    `policy` block (replaces the line 2762-2772 hardcoded `candidate_only=True`).
  * `backend.routes.cases._simulation_policy` — `simulation_jobs.policy_json`
    (replaces the line 1628-1639 hardcoded `can_publish_default=False`).

No mocks: the manifest file is materialized on disk via `tmp_path` and the
default path is patched via `monkeypatch`.  Both production code paths share
the same `should_promote` API, so we exercise the helpers directly using
real `case_id` integers.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.ai_generation_adapter import _build_comfyui_policy
from backend.routes.cases import _simulation_policy
from backend.services import promotion_manifest_loader


# ---------------------------------------------------------------------------
# Manifest fixture: redirect DEFAULT_MANIFEST_PATH to a tmp file per test
# ---------------------------------------------------------------------------


@pytest.fixture
def manifest_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the loader's default manifest path to a per-test tmp file.

    Tests then write the manifest body via `_set_state(...)` below.
    """
    path = tmp_path / "manifest.json"
    monkeypatch.setattr(
        promotion_manifest_loader, "DEFAULT_MANIFEST_PATH", path, raising=True
    )
    return path


def _set_state(path: Path, state: str | None) -> None:
    """Write a minimal but valid manifest with the requested promotion_state.

    `state=None` removes the file entirely (simulates missing manifest).
    """
    if state is None:
        if path.exists():
            path.unlink()
        return
    payload = {
        "schema_version": 1,
        "version": "v-test",
        "scope": "production",
        "promotion_state": state,
        "bindings": {},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# A. _build_comfyui_policy (ai_generation_adapter.py replacement)
# ---------------------------------------------------------------------------


def test_comfyui_policy_no_case_id_defaults_to_candidate_only(manifest_path: Path) -> None:
    """BC: when case_id is None the policy must match the legacy hardcoded block."""
    _set_state(manifest_path, "p100")  # even with full promotion enabled
    policy = _build_comfyui_policy(case_id=None, focus_regions=None)
    assert policy["candidate_only"] is True
    assert policy["mix_with_real_case"] is False
    assert policy["can_publish_default"] is False
    assert policy["promote_to_default"] is False
    # Static metadata still present
    assert policy["artifact_mode"] == "ai_after_simulation"
    assert policy["t90_gate_scope"] == "local_region_repair_retest"
    assert policy["watermark_required"] is True


def test_comfyui_policy_missing_manifest_is_candidate_only(manifest_path: Path) -> None:
    _set_state(manifest_path, None)
    policy = _build_comfyui_policy(case_id=42, focus_regions=None)
    assert policy["candidate_only"] is True
    assert policy["can_publish_default"] is False
    assert policy["mix_with_real_case"] is False


def test_comfyui_policy_shadow_state_is_candidate_only(manifest_path: Path) -> None:
    _set_state(manifest_path, "shadow")
    for cid in (1, 42, 999):
        policy = _build_comfyui_policy(case_id=cid, focus_regions=None)
        assert policy["candidate_only"] is True
        assert policy["can_publish_default"] is False


def test_comfyui_policy_p100_promotes_every_case(manifest_path: Path) -> None:
    _set_state(manifest_path, "p100")
    for cid in (1, 42, 999, 100_000):
        policy = _build_comfyui_policy(case_id=cid, focus_regions=None)
        assert policy["candidate_only"] is False
        assert policy["can_publish_default"] is True
        assert policy["mix_with_real_case"] is True
        assert policy["promote_to_default"] is True


def test_comfyui_policy_rolled_back_falls_back_to_shadow(manifest_path: Path) -> None:
    _set_state(manifest_path, "rolled_back")
    policy = _build_comfyui_policy(case_id=7, focus_regions=None)
    assert policy["candidate_only"] is True
    assert policy["can_publish_default"] is False


def test_comfyui_policy_focus_regions_scope_unchanged_by_promotion(
    manifest_path: Path,
) -> None:
    """focus_scope is a property of focus_regions input, not of promotion state."""
    _set_state(manifest_path, "p100")
    with_regions = _build_comfyui_policy(
        case_id=1, focus_regions=[{"x": 0, "y": 0, "w": 10, "h": 10}]
    )
    without = _build_comfyui_policy(case_id=1, focus_regions=None)
    assert with_regions["focus_scope"] == "region-locked-light"
    assert without["focus_scope"] == "whole-image-light"


# ---------------------------------------------------------------------------
# B. _simulation_policy (routes/cases.py replacement)
# ---------------------------------------------------------------------------


def test_simulation_policy_no_case_id_keeps_legacy_default(manifest_path: Path) -> None:
    _set_state(manifest_path, "p100")
    policy = _simulation_policy()
    assert policy["mix_with_real_case"] is False
    assert policy["can_publish_default"] is False
    assert policy["artifact_mode"] == "ai_after_simulation"
    assert policy["watermark_required"] is True


def test_simulation_policy_shadow_keeps_legacy_default(manifest_path: Path) -> None:
    _set_state(manifest_path, "shadow")
    policy = _simulation_policy(case_id=42)
    assert policy["mix_with_real_case"] is False
    assert policy["can_publish_default"] is False


def test_simulation_policy_p100_promotes(manifest_path: Path) -> None:
    _set_state(manifest_path, "p100")
    for cid in (1, 5, 999):
        policy = _simulation_policy(case_id=cid)
        assert policy["mix_with_real_case"] is True, cid
        assert policy["can_publish_default"] is True, cid


def test_simulation_policy_p10_grayscale_distribution(manifest_path: Path) -> None:
    """Across 1000 cases at p10 we expect ~10% promoted, ±30 per spec."""
    _set_state(manifest_path, "p10")
    promoted = sum(
        1 for cid in range(1, 1001) if _simulation_policy(case_id=cid)["can_publish_default"]
    )
    assert 70 <= promoted <= 130, f"p10 promoted={promoted}"


def test_simulation_policy_invalid_state_fails_closed(manifest_path: Path) -> None:
    _set_state(manifest_path, "promoted")  # not in VALID_STATES
    policy = _simulation_policy(case_id=1)
    assert policy["mix_with_real_case"] is False
    assert policy["can_publish_default"] is False


def test_simulation_policy_focus_regions_independent_of_promotion(
    manifest_path: Path,
) -> None:
    _set_state(manifest_path, "p100")
    with_regions = _simulation_policy(
        focus_regions=[{"x": 0, "y": 0, "w": 10, "h": 10}], case_id=1
    )
    assert with_regions["focus_scope"] == "region-locked-light"
    assert with_regions["non_target_policy"] == "preserve-no-global-retouch"
    without = _simulation_policy(case_id=1)
    assert without["focus_scope"] == "whole-image-light"
    assert without["non_target_policy"] == "whole-image-light-retouch"


# ---------------------------------------------------------------------------
# C. Cross-state agreement — both helpers must agree per case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["shadow", "p10", "p25", "p50", "p100", "rolled_back"])
def test_two_policy_helpers_agree_on_promotion_decision(
    manifest_path: Path, state: str
) -> None:
    """Both helpers route through `should_promote`, so per-case decisions match."""
    _set_state(manifest_path, state)
    for cid in (1, 7, 42, 100, 999, 1_000_000):
        comfy = _build_comfyui_policy(case_id=cid, focus_regions=None)
        sim = _simulation_policy(case_id=cid)
        assert comfy["can_publish_default"] == sim["can_publish_default"], (
            f"state={state} case_id={cid} comfy={comfy['can_publish_default']} sim={sim['can_publish_default']}"
        )
        assert comfy["mix_with_real_case"] == sim["mix_with_real_case"]
