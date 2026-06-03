"""Tests for multi-signal phase fusion."""
from __future__ import annotations

import pytest

from backend.services.phase_fusion import (
    VALID_PHASES,
    VALID_SOURCES,
    FusionResult,
    PhaseSignal,
    build_signals_from_components,
    fuse_phase_signals,
)


class TestFusePhaseSignals:
    """Core fusion logic."""

    def test_empty_signals(self):
        result = fuse_phase_signals([])
        assert result.phase == "unknown"
        assert result.confidence == 0.0
        assert result.signals_used == 0

    def test_single_before_high_confidence(self):
        signals = [PhaseSignal(source="vlm_single", phase="before", confidence=0.90)]
        result = fuse_phase_signals(signals)
        assert result.phase == "before"
        assert result.confidence == min(0.90 * 1.1, 0.98)
        assert result.agreement is True

    def test_single_after_high_confidence(self):
        signals = [PhaseSignal(source="vlm_pair", phase="after", confidence=0.85)]
        result = fuse_phase_signals(signals)
        assert result.phase == "after"
        assert result.confidence == min(0.85 * 1.1, 0.98)

    def test_all_agree_before(self):
        signals = [
            PhaseSignal(source="path_rules", phase="before", confidence=0.92),
            PhaseSignal(source="exif_temporal", phase="before", confidence=0.30),
            PhaseSignal(source="vlm_single", phase="before", confidence=0.88),
            PhaseSignal(source="vlm_pair", phase="before", confidence=0.90),
        ]
        result = fuse_phase_signals(signals)
        assert result.phase == "before"
        assert result.confidence == 0.98  # 0.92 * 1.1 > 0.98, capped
        assert result.agreement is True
        assert result.signals_used == 4
        assert result.conflict_sources == []

    def test_all_agree_after(self):
        signals = [
            PhaseSignal(source="path_rules", phase="after", confidence=0.92),
            PhaseSignal(source="vlm_single", phase="after", confidence=0.80),
        ]
        result = fuse_phase_signals(signals)
        assert result.phase == "after"
        assert result.confidence == min(0.92 * 1.1, 0.98)
        assert result.agreement is True

    def test_conflict_exif_before_vlm_after(self):
        signals = [
            PhaseSignal(source="exif_temporal", phase="before", confidence=0.30),
            PhaseSignal(source="vlm_single", phase="after", confidence=0.85),
        ]
        result = fuse_phase_signals(signals)
        # vlm_single has higher weighted score: 0.85*0.25=0.2125 vs exif 0.30*0.25=0.075
        # conflict → max supporting conf * 0.6 = 0.85 * 0.6 = 0.51
        assert result.confidence == pytest.approx(0.51, abs=0.01)
        assert result.agreement is False
        assert result.phase == "unknown"  # 0.51 < 0.70 → held

    def test_conflict_high_confidence_both_sides(self):
        signals = [
            PhaseSignal(source="path_rules", phase="before", confidence=0.92),
            PhaseSignal(source="vlm_pair", phase="after", confidence=0.90),
        ]
        result = fuse_phase_signals(signals)
        # path_rules weighted: 0.92*0.20=0.184; vlm_pair weighted: 0.90*0.30=0.27
        # vlm_pair wins; conflict → 0.90 * 0.6 = 0.54 → unknown
        assert result.agreement is False
        assert "path_rules" in result.conflict_sources or "vlm_pair" in result.conflict_sources

    def test_all_unknown_signals(self):
        signals = [
            PhaseSignal(source="path_rules", phase="unknown", confidence=0.25),
            PhaseSignal(source="exif_temporal", phase="unknown", confidence=0.0),
        ]
        result = fuse_phase_signals(signals)
        assert result.phase == "unknown"
        assert result.confidence == 0.0

    def test_unknown_signals_ignored_in_vote(self):
        signals = [
            PhaseSignal(source="path_rules", phase="unknown", confidence=0.25),
            PhaseSignal(source="vlm_single", phase="before", confidence=0.88),
        ]
        result = fuse_phase_signals(signals)
        assert result.phase == "before"
        assert result.signals_used == 1  # only vlm_single counted

    def test_agreement_boost_capped(self):
        signals = [
            PhaseSignal(source="vlm_single", phase="before", confidence=0.95),
            PhaseSignal(source="vlm_pair", phase="before", confidence=0.95),
        ]
        result = fuse_phase_signals(signals)
        assert result.confidence == 0.98  # 0.95 * 1.1 = 1.045, capped at 0.98

    def test_low_agreement_below_threshold(self):
        signals = [
            PhaseSignal(source="exif_temporal", phase="before", confidence=0.15),
        ]
        result = fuse_phase_signals(signals)
        # 0.15 * 1.1 = 0.165 < 0.70 → unknown
        assert result.phase == "unknown"
        assert result.confidence == pytest.approx(0.165, abs=0.001)

    def test_intraop_phase_passes_through(self):
        signals = [
            PhaseSignal(source="path_rules", phase="intraop", confidence=0.85),
        ]
        result = fuse_phase_signals(signals)
        assert result.phase == "intraop"

    def test_invalid_source_filtered(self):
        signals = [
            PhaseSignal(source="invalid_source", phase="before", confidence=0.90),
        ]
        result = fuse_phase_signals(signals)
        assert result.phase == "unknown"
        assert result.signals_used == 0

    def test_invalid_phase_normalized_to_unknown(self):
        signals = [
            PhaseSignal(source="vlm_single", phase="garbage", confidence=0.90),
        ]
        result = fuse_phase_signals(signals)
        assert result.phase == "unknown"

    def test_confidence_clamped(self):
        signals = [
            PhaseSignal(source="vlm_single", phase="before", confidence=1.5),
        ]
        result = fuse_phase_signals(signals)
        assert result.confidence <= 0.98

    def test_negative_confidence_clamped(self):
        signals = [
            PhaseSignal(source="vlm_single", phase="before", confidence=-0.5),
        ]
        result = fuse_phase_signals(signals)
        assert result.confidence >= 0.0


class TestFusionResultFields:
    """Verify FusionResult structure."""

    def test_fields_present(self):
        result = fuse_phase_signals([
            PhaseSignal(source="vlm_single", phase="after", confidence=0.90),
        ])
        assert isinstance(result, FusionResult)
        assert isinstance(result.phase, str)
        assert isinstance(result.confidence, float)
        assert isinstance(result.reasoning, str)
        assert isinstance(result.signals_used, int)
        assert isinstance(result.agreement, bool)
        assert isinstance(result.conflict_sources, list)

    def test_reasoning_contains_source_info(self):
        result = fuse_phase_signals([
            PhaseSignal(source="vlm_single", phase="before", confidence=0.88),
            PhaseSignal(source="vlm_pair", phase="before", confidence=0.90),
        ])
        assert "vlm_single" in result.reasoning
        assert "vlm_pair" in result.reasoning


class TestWeightedVoting:
    """Weighted voting edge cases."""

    def test_vlm_pair_wins_over_path_rules_same_confidence(self):
        signals = [
            PhaseSignal(source="path_rules", phase="before", confidence=0.90),
            PhaseSignal(source="vlm_pair", phase="after", confidence=0.90),
        ]
        result = fuse_phase_signals(signals)
        # path_rules: 0.90*0.20=0.18; vlm_pair: 0.90*0.30=0.27 → after wins
        assert result.agreement is False
        # conflict → 0.90 * 0.6 = 0.54 < 0.70 → unknown
        assert result.phase == "unknown"

    def test_three_vs_one_majority_wins(self):
        signals = [
            PhaseSignal(source="path_rules", phase="before", confidence=0.92),
            PhaseSignal(source="exif_temporal", phase="before", confidence=0.30),
            PhaseSignal(source="vlm_single", phase="before", confidence=0.88),
            PhaseSignal(source="vlm_pair", phase="after", confidence=0.70),
        ]
        result = fuse_phase_signals(signals)
        # before weighted: 0.92*0.20 + 0.30*0.25 + 0.88*0.25 = 0.184+0.075+0.22 = 0.479
        # after weighted: 0.70*0.30 = 0.21
        # before wins; conflict → 0.92 * 0.6 = 0.552 < 0.70 → unknown
        assert result.agreement is False

    def test_two_strong_agree(self):
        signals = [
            PhaseSignal(source="vlm_single", phase="after", confidence=0.90),
            PhaseSignal(source="vlm_pair", phase="after", confidence=0.92),
        ]
        result = fuse_phase_signals(signals)
        assert result.phase == "after"
        assert result.confidence == 0.98  # 0.92 * 1.1 = 1.012, capped


class TestBuildSignalsFromComponents:
    """Test the convenience builder."""

    def test_all_none_returns_empty(self):
        signals = build_signals_from_components()
        assert signals == []

    def test_partial_signals(self):
        signals = build_signals_from_components(
            path_phase="before",
            path_confidence=0.92,
            vlm_single_phase="before",
            vlm_single_confidence=0.88,
        )
        assert len(signals) == 2
        assert signals[0].source == "path_rules"
        assert signals[1].source == "vlm_single"

    def test_all_signals(self):
        signals = build_signals_from_components(
            path_phase="before",
            path_confidence=0.92,
            path_reasoning="path token",
            exif_phase="before",
            exif_confidence=0.30,
            exif_reasoning="earliest session",
            vlm_single_phase="before",
            vlm_single_confidence=0.88,
            vlm_single_reasoning="redness detected",
            vlm_pair_phase="before",
            vlm_pair_confidence=0.90,
            vlm_pair_reasoning="comparative analysis",
        )
        assert len(signals) == 4
        sources = [s.source for s in signals]
        assert sources == ["path_rules", "exif_temporal", "vlm_single", "vlm_pair"]

    def test_phase_none_skips_signal(self):
        signals = build_signals_from_components(
            path_phase=None,
            path_confidence=0.92,
            vlm_single_phase="after",
            vlm_single_confidence=0.85,
        )
        assert len(signals) == 1
        assert signals[0].source == "vlm_single"

    def test_confidence_none_skips_signal(self):
        signals = build_signals_from_components(
            path_phase="before",
            path_confidence=None,
        )
        assert len(signals) == 0


class TestDecisionMatrix:
    """Test cases from the plan's decision matrix."""

    def test_all_before_high_confidence(self):
        """All 4 signals = before → before (0.95+)."""
        signals = [
            PhaseSignal(source="path_rules", phase="before", confidence=0.92),
            PhaseSignal(source="exif_temporal", phase="before", confidence=0.30),
            PhaseSignal(source="vlm_single", phase="before", confidence=0.88),
            PhaseSignal(source="vlm_pair", phase="before", confidence=0.90),
        ]
        result = fuse_phase_signals(signals)
        assert result.phase == "before"
        assert result.confidence >= 0.95

    def test_exif_before_vlm_after_conflict_held(self):
        """EXIF=before + VLM=after → conflict → held."""
        signals = [
            PhaseSignal(source="exif_temporal", phase="before", confidence=0.30),
            PhaseSignal(source="vlm_single", phase="after", confidence=0.85),
        ]
        result = fuse_phase_signals(signals)
        assert result.phase == "unknown"
        assert result.agreement is False

    def test_vlm_single_and_pair_agree_before(self):
        """VLM single + pair agree before (no EXIF, no path) → before ~0.85+."""
        signals = [
            PhaseSignal(source="vlm_single", phase="before", confidence=0.88),
            PhaseSignal(source="vlm_pair", phase="before", confidence=0.85),
        ]
        result = fuse_phase_signals(signals)
        assert result.phase == "before"
        assert result.confidence >= 0.85

    def test_vlm_uncertain_plus_pair_before(self):
        """VLM single=uncertain + pair=before → before (0.70+)."""
        signals = [
            PhaseSignal(source="vlm_single", phase="unknown", confidence=0.30),
            PhaseSignal(source="vlm_pair", phase="before", confidence=0.80),
        ]
        result = fuse_phase_signals(signals)
        assert result.phase == "before"
        assert result.confidence >= 0.70

    def test_both_uncertain_held(self):
        """VLM single=uncertain + pair=uncertain → unknown → held."""
        signals = [
            PhaseSignal(source="vlm_single", phase="unknown", confidence=0.30),
            PhaseSignal(source="vlm_pair", phase="unknown", confidence=0.40),
        ]
        result = fuse_phase_signals(signals)
        assert result.phase == "unknown"


class TestEdgeCases:

    def test_duplicate_source_both_counted(self):
        signals = [
            PhaseSignal(source="vlm_single", phase="before", confidence=0.80),
            PhaseSignal(source="vlm_single", phase="after", confidence=0.90),
        ]
        result = fuse_phase_signals(signals)
        assert result.agreement is False

    def test_single_low_conf_exif_only(self):
        signals = [
            PhaseSignal(source="exif_temporal", phase="before", confidence=0.15),
        ]
        result = fuse_phase_signals(signals)
        assert result.phase == "unknown"  # 0.15 * 1.1 = 0.165 < 0.70

    def test_reasoning_present_on_conflict(self):
        signals = [
            PhaseSignal(source="vlm_single", phase="before", confidence=0.85, reasoning="no redness"),
            PhaseSignal(source="vlm_pair", phase="after", confidence=0.88, reasoning="volume increase"),
        ]
        result = fuse_phase_signals(signals)
        assert "conflict" in result.reasoning

    def test_frozen_result(self):
        result = fuse_phase_signals([
            PhaseSignal(source="vlm_single", phase="before", confidence=0.90),
        ])
        with pytest.raises(AttributeError):
            result.phase = "after"  # type: ignore[misc]
