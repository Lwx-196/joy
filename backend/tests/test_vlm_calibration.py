"""Unit tests for detect_distribution_collapse."""
from __future__ import annotations

from backend.services.vlm_calibration import (
    COLLAPSE_RATIO_THRESHOLD,
    WARN_RATIO_THRESHOLD,
    detect_distribution_collapse,
)


def _record(phase: str | None, view: str | None, body_part: str | None,
            confidence: float | None) -> dict:
    return {
        "phase": phase,
        "view": view,
        "body_part": body_part,
        "confidence": confidence,
    }


def test_empty_window_returns_ok():
    status = detect_distribution_collapse([])
    assert status.status == "ok"
    assert status.sample_size == 0
    assert status.evidence == []
    assert "empty" in status.recommendation


def test_balanced_distribution_returns_ok():
    records = (
        [_record("before", "front", "face", 0.91)] * 5
        + [_record("after", "side", "face", 0.92)] * 5
    )
    status = detect_distribution_collapse(records)
    assert status.status == "ok"
    assert status.evidence == []


def test_collapsed_phase_high_confidence_is_uncalibrated():
    records = (
        [_record("before", "front", "face", 0.95)] * 95
        + [_record("after", "front", "face", 0.95)] * 5
    )
    status = detect_distribution_collapse(records)
    assert status.status == "uncalibrated"
    phase_alerts = [a for a in status.evidence if a.dimension == "phase"]
    assert len(phase_alerts) == 1
    assert phase_alerts[0].dominant_class == "before"
    assert phase_alerts[0].dominant_ratio >= COLLAPSE_RATIO_THRESHOLD
    assert phase_alerts[0].severity == "uncalibrated"
    assert "live-no-apply" in status.recommendation


def test_skewed_under_collapse_threshold_is_warn():
    # 85% before / 15% after; conf p50 = 0.87 (>= warn 0.85, < collapse 0.9)
    records = (
        [_record("before", "front", "face", 0.87)] * 85
        + [_record("after", "front", "face", 0.87)] * 15
    )
    status = detect_distribution_collapse(records)
    assert status.status == "warn"
    phase_alerts = [a for a in status.evidence if a.dimension == "phase"]
    assert phase_alerts[0].severity == "warn"
    assert phase_alerts[0].dominant_ratio >= WARN_RATIO_THRESHOLD


def test_high_ratio_low_confidence_is_ok():
    # 95% concentration but confidence too low to trip warn or uncalibrated.
    records = (
        [_record("before", "front", "face", 0.60)] * 95
        + [_record("after", "front", "face", 0.60)] * 5
    )
    status = detect_distribution_collapse(records)
    assert status.status == "ok"
    assert status.evidence == []


def test_per_dimension_alerts_phase_only():
    # phase 95/5 (collapsed); view 50/50 (balanced); body_part 50/50 (balanced).
    records = (
        [_record("before", "front", "face", 0.95)] * 47
        + [_record("before", "side", "breast", 0.95)] * 48
        + [_record("after", "front", "face", 0.95)] * 3
        + [_record("after", "side", "breast", 0.95)] * 2
    )
    status = detect_distribution_collapse(records)
    phase_alerts = [a for a in status.evidence if a.dimension == "phase"]
    view_alerts = [a for a in status.evidence if a.dimension == "view"]
    body_alerts = [a for a in status.evidence if a.dimension == "body_part"]
    assert phase_alerts and phase_alerts[0].severity == "uncalibrated"
    assert view_alerts == []
    assert body_alerts == []
    assert status.status == "uncalibrated"


def test_single_class_dimension_is_skipped():
    # body_part is structurally single-valued across this case set.
    # It must NOT trigger an alert just because every row is "face".
    records = [_record("before", "front", "face", 0.95)] * 50 + [
        _record("after", "side", "face", 0.95)
    ] * 50
    status = detect_distribution_collapse(records)
    body_alerts = [a for a in status.evidence if a.dimension == "body_part"]
    assert body_alerts == []
    assert status.status == "ok"


def test_missing_dimension_value_skipped():
    records = [
        _record("before", "front", "face", 0.95),
        _record(None, "side", "face", 0.95),
        _record("after", "side", "face", 0.95),
    ]
    status = detect_distribution_collapse(records)
    # phase population: 1 before + 1 after (None skipped) → 50/50 → no alert
    phase_alerts = [a for a in status.evidence if a.dimension == "phase"]
    assert phase_alerts == []


def test_missing_confidence_does_not_escalate():
    # Collapsed ratio across two classes but no confidence signal → no
    # escalation (we require explicit p50 to act).
    records = (
        [_record("before", "front", "face", None)] * 95
        + [_record("after", "side", "neck", None)] * 5
    )
    status = detect_distribution_collapse(records)
    assert status.status == "ok"


def test_custom_dimensions():
    records = [{"my_dim": "x", "confidence": 0.95}] * 95 + [
        {"my_dim": "y", "confidence": 0.95}
    ] * 5
    status = detect_distribution_collapse(records, dimensions=("my_dim",))
    assert status.status == "uncalibrated"
    assert status.evidence[0].dimension == "my_dim"
    assert status.evidence[0].dominant_class == "x"
