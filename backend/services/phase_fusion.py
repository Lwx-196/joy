"""Multi-signal phase fusion for medical-aesthetic image classification.

Combines four independent classification signals into a single phase + confidence:
  - path_rules:    filename/directory keyword matching (0.20 weight)
  - exif_temporal:  EXIF timestamp clustering (0.25 weight)
  - vlm_single:    single-image VLM classification (0.25 weight)
  - vlm_pair:      paired comparative VLM classification (0.30 weight)

All functions are pure — no DB, no network, no side effects.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

VALID_SOURCES = frozenset({"path_rules", "exif_temporal", "vlm_single", "vlm_pair"})
VALID_PHASES = frozenset({"before", "after", "intraop", "unknown"})

SOURCE_WEIGHTS: dict[str, float] = {
    "path_rules": 0.20,
    "exif_temporal": 0.25,
    "vlm_single": 0.25,
    "vlm_pair": 0.30,
}

_AGREEMENT_BOOST = 1.1
_MAX_CONFIDENCE = 0.98
_HELD_THRESHOLD = 0.70
_WEAK_SIGNAL_THRESHOLD = 0.25


@dataclass(frozen=True)
class PhaseSignal:
    source: str
    phase: str
    confidence: float
    reasoning: str = ""


@dataclass(frozen=True)
class FusionResult:
    phase: str
    confidence: float
    reasoning: str
    signals_used: int
    agreement: bool
    conflict_sources: list[str] = field(default_factory=list)


def _normalize_phase(phase: str) -> str:
    p = phase.strip().lower()
    if p in VALID_PHASES:
        return p
    return "unknown"


def fuse_phase_signals(signals: list[PhaseSignal]) -> FusionResult:
    """Fuse multiple classification signals into a single phase + confidence.

    Rules:
    1. Filter out signals with phase == "unknown" (they carry no directional info).
    2. Weighted vote across remaining signals; highest-weight phase wins.
    3. All non-unknown signals agree → boost confidence by 1.1x (cap 0.98).
    4. Signals conflict → confidence capped at max individual × 0.6.
    5. Final confidence < 0.70 → phase forced to "unknown" (enters held queue).
    """
    if not signals:
        return FusionResult(
            phase="unknown",
            confidence=0.0,
            reasoning="no signals provided",
            signals_used=0,
            agreement=False,
        )

    valid_signals = [
        PhaseSignal(
            source=s.source,
            phase=_normalize_phase(s.phase),
            confidence=max(0.0, min(1.0, s.confidence)),
            reasoning=s.reasoning,
        )
        for s in signals
        if s.source in VALID_SOURCES
    ]

    if not valid_signals:
        return FusionResult(
            phase="unknown",
            confidence=0.0,
            reasoning="no valid signal sources",
            signals_used=0,
            agreement=False,
        )

    directional = [s for s in valid_signals if s.phase != "unknown"]

    if not directional:
        return FusionResult(
            phase="unknown",
            confidence=0.0,
            reasoning="all signals are unknown",
            signals_used=len(valid_signals),
            agreement=False,
        )

    phase_scores: dict[str, float] = {}
    for s in directional:
        weight = SOURCE_WEIGHTS.get(s.source, 0.0)
        weighted = s.confidence * weight
        phase_scores[s.phase] = phase_scores.get(s.phase, 0.0) + weighted

    winning_phase = max(phase_scores, key=lambda p: phase_scores[p])

    supporting = [s for s in directional if s.phase == winning_phase]
    opposing_all = [s for s in directional if s.phase != winning_phase]
    opposing = [s for s in opposing_all if s.confidence >= _WEAK_SIGNAL_THRESHOLD]
    weak_opposing = [s for s in opposing_all if s.confidence < _WEAK_SIGNAL_THRESHOLD]

    all_agree = len(opposing) == 0
    conflict_sources = [s.source for s in opposing]

    if all_agree:
        max_conf = max(s.confidence for s in supporting)
        fused_confidence = min(max_conf * _AGREEMENT_BOOST, _MAX_CONFIDENCE)
        parts = [f"{s.source}={s.phase}({s.confidence:.2f})" for s in supporting]
        reasoning = f"agreement: {', '.join(parts)}"
        if weak_opposing:
            weak_parts = [f"{s.source}={s.phase}({s.confidence:.2f})" for s in weak_opposing]
            reasoning += f" (weak opposing ignored: {', '.join(weak_parts)})"
    else:
        max_supporting_conf = max(s.confidence for s in supporting)
        fused_confidence = max_supporting_conf * 0.6
        sup_parts = [f"{s.source}={s.phase}({s.confidence:.2f})" for s in supporting]
        opp_parts = [f"{s.source}={s.phase}({s.confidence:.2f})" for s in opposing]
        reasoning = f"conflict: support=[{', '.join(sup_parts)}] vs oppose=[{', '.join(opp_parts)}]"

    fused_confidence = round(fused_confidence, 4)

    if fused_confidence < _HELD_THRESHOLD:
        return FusionResult(
            phase="unknown",
            confidence=fused_confidence,
            reasoning=f"below threshold ({fused_confidence:.2f} < {_HELD_THRESHOLD}): {reasoning}",
            signals_used=len(directional),
            agreement=all_agree,
            conflict_sources=conflict_sources,
        )

    return FusionResult(
        phase=winning_phase,
        confidence=fused_confidence,
        reasoning=reasoning,
        signals_used=len(directional),
        agreement=all_agree,
        conflict_sources=conflict_sources,
    )


def build_signals_from_components(
    *,
    path_phase: str | None = None,
    path_confidence: float | None = None,
    path_reasoning: str = "",
    exif_phase: str | None = None,
    exif_confidence: float | None = None,
    exif_reasoning: str = "",
    vlm_single_phase: str | None = None,
    vlm_single_confidence: float | None = None,
    vlm_single_reasoning: str = "",
    vlm_pair_phase: str | None = None,
    vlm_pair_confidence: float | None = None,
    vlm_pair_reasoning: str = "",
) -> list[PhaseSignal]:
    """Build a list of PhaseSignals from individual component outputs.

    Convenience helper that skips signals where phase is None (tier not run).
    """
    signals: list[PhaseSignal] = []

    if path_phase is not None and path_confidence is not None:
        signals.append(PhaseSignal(
            source="path_rules",
            phase=path_phase,
            confidence=path_confidence,
            reasoning=path_reasoning,
        ))

    if exif_phase is not None and exif_confidence is not None:
        signals.append(PhaseSignal(
            source="exif_temporal",
            phase=exif_phase,
            confidence=exif_confidence,
            reasoning=exif_reasoning,
        ))

    if vlm_single_phase is not None and vlm_single_confidence is not None:
        signals.append(PhaseSignal(
            source="vlm_single",
            phase=vlm_single_phase,
            confidence=vlm_single_confidence,
            reasoning=vlm_single_reasoning,
        ))

    if vlm_pair_phase is not None and vlm_pair_confidence is not None:
        signals.append(PhaseSignal(
            source="vlm_pair",
            phase=vlm_pair_phase,
            confidence=vlm_pair_confidence,
            reasoning=vlm_pair_reasoning,
        ))

    return signals
