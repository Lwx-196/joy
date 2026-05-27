"""Unit tests for `backend.services.promotion_manifest_loader`.

Plan §P2.3 — Runtime Guard.  Covers the 8 boundary cases enumerated in the
agent brief plus a stability test on the hash bucket function.

No mocks: every test uses real JSON files via pytest's `tmp_path`.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend.services.promotion_manifest_loader import (
    DEFAULT_MANIFEST_PATH,
    FAIL_CLOSED_STATE,
    VALID_STATES,
    _stable_bucket,
    get_promotion_state,
    load_manifest,
    should_promote,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_manifest(path: Path, state: str | None, **extra) -> Path:
    payload = {
        "schema_version": 1,
        "version": "v-test",
        "scope": "production",
        "bindings": {},
    }
    if state is not None:
        payload["promotion_state"] = state
    payload.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _count_promoted(state: str, *, n: int, tmp_path: Path) -> int:
    """Count how many of `case_id ∈ [1, n]` are promoted under `state`."""
    path = _write_manifest(tmp_path / "manifest.json", state)
    manifest = load_manifest(path)
    return sum(1 for cid in range(1, n + 1) if should_promote(cid, manifest=manifest))


# ---------------------------------------------------------------------------
# 1. manifest 不存在 → all shadow (should_promote False)
# ---------------------------------------------------------------------------


def test_missing_manifest_returns_none(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.json"
    assert load_manifest(missing) is None
    # get_promotion_state on a None manifest → fail-closed shadow
    assert get_promotion_state(None) in {"shadow"}  # may resolve via default path
    # Pass explicit None manifest path forces shadow regardless of repo default
    assert get_promotion_state(load_manifest(missing)) == FAIL_CLOSED_STATE


def test_missing_manifest_promotes_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.json"
    manifest = load_manifest(missing)
    for cid in (1, 42, 999, 10_000):
        assert should_promote(cid, manifest=manifest) is False


# ---------------------------------------------------------------------------
# 2. state=shadow → all False
# ---------------------------------------------------------------------------


def test_state_shadow_promotes_nothing(tmp_path: Path) -> None:
    assert _count_promoted("shadow", n=1000, tmp_path=tmp_path) == 0


# ---------------------------------------------------------------------------
# 3-5. Grayscale buckets: p10 ~10% / p25 ~25% / p100 = 100%
# ---------------------------------------------------------------------------


def test_state_p10_buckets_to_about_ten_percent(tmp_path: Path) -> None:
    promoted = _count_promoted("p10", n=1000, tmp_path=tmp_path)
    # SHA256 over 1..1000 mod 100 → expect ~100 with statistical noise.
    # Brief bound: 100 ± 30.  Verify empirically also stays inside.
    assert 70 <= promoted <= 130, f"p10 promoted={promoted}"


def test_state_p25_buckets_to_about_twentyfive_percent(tmp_path: Path) -> None:
    promoted = _count_promoted("p25", n=1000, tmp_path=tmp_path)
    assert 200 <= promoted <= 300, f"p25 promoted={promoted}"


def test_state_p50_buckets_to_about_half(tmp_path: Path) -> None:
    promoted = _count_promoted("p50", n=1000, tmp_path=tmp_path)
    assert 425 <= promoted <= 575, f"p50 promoted={promoted}"


def test_state_p100_promotes_all(tmp_path: Path) -> None:
    promoted = _count_promoted("p100", n=1000, tmp_path=tmp_path)
    assert promoted == 1000


# ---------------------------------------------------------------------------
# 6. state=rolled_back → all False
# ---------------------------------------------------------------------------


def test_state_rolled_back_promotes_nothing(tmp_path: Path) -> None:
    assert _count_promoted("rolled_back", n=1000, tmp_path=tmp_path) == 0


# ---------------------------------------------------------------------------
# 7. state=invalid / unknown → fail-closed all False
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_state",
    [
        "p75",          # not in VALID_STATES
        "promoted",     # ad-hoc
        "ROLLED_BACK",  # case mismatch — VALID_STATES is lowercase only
        "",             # empty string
        "shadow ",      # trailing space
    ],
)
def test_unknown_state_fails_closed(tmp_path: Path, bad_state: str) -> None:
    path = _write_manifest(tmp_path / "manifest.json", bad_state)
    manifest = load_manifest(path)
    assert get_promotion_state(manifest) == FAIL_CLOSED_STATE
    for cid in (1, 7, 250):
        assert should_promote(cid, manifest=manifest) is False


def test_non_string_state_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"schema_version": 1, "promotion_state": 42}, ensure_ascii=False),
        encoding="utf-8",
    )
    manifest = load_manifest(path)
    assert get_promotion_state(manifest) == FAIL_CLOSED_STATE
    assert should_promote(1, manifest=manifest) is False


def test_missing_promotion_state_key_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"schema_version": 1}, ensure_ascii=False), encoding="utf-8")
    manifest = load_manifest(path)
    assert get_promotion_state(manifest) == FAIL_CLOSED_STATE


def test_invalid_json_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{ not valid json", encoding="utf-8")
    assert load_manifest(path) is None
    assert get_promotion_state(load_manifest(path)) == FAIL_CLOSED_STATE


def test_non_dict_top_level_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert load_manifest(path) is None


# ---------------------------------------------------------------------------
# 8. hash 稳定性: same case_id → same bucket across calls/processes
# ---------------------------------------------------------------------------


def test_hash_bucket_is_deterministic() -> None:
    # Multi-call stability
    for cid in (0, 1, 7, 42, 999, 1_000_000):
        first = _stable_bucket(cid)
        for _ in range(50):
            assert _stable_bucket(cid) == first

    # Independently recompute via the same algorithm — guarantees we never
    # accidentally swap in Python's builtin hash() (which is process-randomized).
    for cid in (0, 1, 42, 999):
        expected = int(hashlib.sha256(str(cid).encode("utf-8")).hexdigest()[:8], 16) % 100
        assert _stable_bucket(cid) == expected


def test_should_promote_is_deterministic_per_case(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path / "manifest.json", "p25")
    manifest = load_manifest(path)
    for cid in (1, 5, 100, 9999):
        results = [should_promote(cid, manifest=manifest) for _ in range(20)]
        assert len(set(results)) == 1, f"case_id {cid} flipped: {results}"


def test_grayscale_monotonicity(tmp_path: Path) -> None:
    """p10 ⊆ p25 ⊆ p50 ⊆ p100 — anything promoted at lower % stays promoted higher."""
    sample = list(range(1, 501))

    def promoted_set(state: str) -> set[int]:
        m = load_manifest(_write_manifest(tmp_path / f"manifest-{state}.json", state))
        return {cid for cid in sample if should_promote(cid, manifest=m)}

    p10 = promoted_set("p10")
    p25 = promoted_set("p25")
    p50 = promoted_set("p50")
    p100 = promoted_set("p100")

    assert p10 <= p25 <= p50 <= p100
    assert p100 == set(sample)


# ---------------------------------------------------------------------------
# Sanity: real repo default manifest path resolves & parses
# ---------------------------------------------------------------------------


def test_default_manifest_path_exists_and_parses() -> None:
    """The real repo manifest produced by Wave 1 should load cleanly."""
    assert DEFAULT_MANIFEST_PATH.exists(), DEFAULT_MANIFEST_PATH
    m = load_manifest()
    assert m is not None
    assert m.get("promotion_state") in VALID_STATES
    # Manifest ships in shadow; treat as fail-closed expectation.
    assert get_promotion_state(m) == m["promotion_state"]
