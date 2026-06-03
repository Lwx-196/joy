"""Enhanced multi-tier classification orchestrator.

Coordinates path_rules, exif_temporal, vlm_single, and vlm_pair tiers
and fuses their signals per image using phase_fusion.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..case_grouping import _phase_from_text
from .exif_extractor import cluster_sessions, extract_exif, infer_temporal_phase
from .pair_classifier import ImageCandidate, classify_pairs_batch, select_comparison_pairs
from .phase_fusion import build_signals_from_components, fuse_phase_signals
from .vlm_provider import VLMProvider
from .vlm_source_classifier import classify_batch

logger = logging.getLogger(__name__)

ALL_TIERS = frozenset({"path_rules", "exif", "vlm_single", "vlm_pair"})


@dataclass(frozen=True)
class ObservationRecord:
    observation_id: int
    case_id: int
    group_id: int
    image_path: str
    image_abs_path: Path
    phase: str
    confidence: float
    source: str


def fetch_case_observations(
    conn: sqlite3.Connection,
    case_id: int,
) -> list[ObservationRecord]:
    rows = conn.execute(
        """
        SELECT o.id, o.case_id, o.group_id, o.image_path, o.phase,
               o.confidence, o.source, g.root_path
        FROM image_observations o
        JOIN case_groups g ON g.id = o.group_id
        WHERE o.case_id = ?
        ORDER BY o.id
        """,
        (case_id,),
    ).fetchall()
    records: list[ObservationRecord] = []
    for row in rows:
        root = Path(str(row["root_path"] or ""))
        raw = Path(str(row["image_path"] or ""))
        abs_path = raw if raw.is_absolute() else root / raw
        records.append(ObservationRecord(
            observation_id=int(row["id"]),
            case_id=int(row["case_id"]) if row["case_id"] else case_id,
            group_id=int(row["group_id"]),
            image_path=str(row["image_path"]),
            image_abs_path=abs_path,
            phase=str(row["phase"] or "unknown"),
            confidence=float(row["confidence"] or 0),
            source=str(row["source"] or "rules"),
        ))
    return records


def _run_path_rules_tier(
    observations: list[ObservationRecord],
) -> dict[str, dict[str, Any]]:
    signals: dict[str, dict[str, Any]] = {}
    for obs in observations:
        filename = Path(obs.image_path).name
        phase, confidence, reasoning = _phase_from_text(filename)
        if phase == "unknown":
            stem = Path(obs.image_path).stem
            phase, confidence, reasoning = _phase_from_text(stem)
        if phase == "unknown":
            phase, confidence, reasoning = _phase_from_text(obs.image_path)
        if phase == "unknown":
            parent = obs.image_abs_path.parent.name
            phase, confidence, reasoning = _phase_from_text(parent)
        signals[obs.image_path] = {
            "phase": phase,
            "confidence": confidence,
            "reasoning": reasoning,
        }
    return signals


def _run_exif_tier(
    observations: list[ObservationRecord],
) -> dict[str, dict[str, Any]]:
    images = []
    for obs in observations:
        if obs.image_abs_path.is_file():
            meta = extract_exif(obs.image_abs_path)
            images.append((obs.image_abs_path, meta))

    if not images:
        return {}

    sessions = cluster_sessions(images)
    hints = infer_temporal_phase(sessions)

    abs_to_rel: dict[str, str] = {
        str(obs.image_abs_path): obs.image_path for obs in observations
    }
    signals: dict[str, dict[str, Any]] = {}
    for hint in hints:
        rel_path = abs_to_rel.get(str(hint.image_path))
        if rel_path:
            signals[rel_path] = {
                "phase": hint.phase_hint,
                "confidence": hint.confidence,
                "reasoning": hint.reasoning,
            }
    return signals


def _run_vlm_single_tier(
    observations: list[ObservationRecord],
    provider: VLMProvider,
    concurrency: int,
    timeout: float,
) -> dict[str, dict[str, Any]]:
    existing = [obs for obs in observations if obs.image_abs_path.is_file()]
    if not existing:
        return {}

    paths = [obs.image_abs_path for obs in existing]
    results = classify_batch(
        paths, provider, concurrency=concurrency, timeout=timeout, return_exceptions=True,
    )

    signals: dict[str, dict[str, Any]] = {}
    for obs, result in zip(existing, results):
        if isinstance(result, BaseException):
            logger.warning("VLM single failed for %s: %s", obs.image_path, result)
            continue
        signals[obs.image_path] = {
            "phase": result.phase,
            "confidence": result.confidence,
            "reasoning": result.reasoning,
            "visual_cues": result.visual_cues,
        }
    return signals


def _run_vlm_pair_tier(
    observations: list[ObservationRecord],
    provider: VLMProvider,
    timeout: float,
) -> dict[str, dict[str, Any]]:
    candidates = [
        ImageCandidate(
            observation_id=obs.observation_id,
            image_path=obs.image_abs_path,
            phase=obs.phase,
            confidence=obs.confidence,
        )
        for obs in observations
        if obs.image_abs_path.is_file()
    ]

    pairs = select_comparison_pairs(candidates)
    if not pairs:
        return {}

    path_pairs = [(a.image_path, b.image_path) for a, b in pairs]
    results = classify_pairs_batch(path_pairs, provider, timeout=timeout)

    abs_to_rel: dict[str, str] = {
        str(obs.image_abs_path): obs.image_path for obs in observations
    }
    phase_map = {"uncertain": "unknown"}
    signals: dict[str, dict[str, Any]] = {}
    for (c_a, c_b), result in zip(pairs, results):
        if isinstance(result, BaseException):
            logger.warning("VLM pair failed: %s", result)
            continue
        for candidate, pair_phase in [
            (c_a, result.image_a_phase),
            (c_b, result.image_b_phase),
        ]:
            rel = abs_to_rel.get(str(candidate.image_path))
            if rel:
                signals[rel] = {
                    "phase": phase_map.get(pair_phase, pair_phase),
                    "confidence": result.confidence,
                    "reasoning": result.reasoning,
                }
    return signals


def run_enhanced_classification(
    conn: sqlite3.Connection,
    case_id: int,
    *,
    tiers: list[str] | None = None,
    mode: str = "dry-run",
    provider: VLMProvider | None = None,
    concurrency: int = 2,
    timeout: float = 45.0,
) -> dict[str, Any]:
    enabled = frozenset(tiers) & ALL_TIERS if tiers else ALL_TIERS
    if mode == "dry-run":
        enabled = enabled - {"vlm_single", "vlm_pair"}

    observations = fetch_case_observations(conn, case_id)
    if not observations:
        return {
            "case_id": case_id,
            "mode": mode,
            "tiers_enabled": sorted(enabled),
            "image_count": 0,
            "results": [],
            "summary": {"total": 0, "before": 0, "after": 0, "intraop": 0, "unknown_held": 0},
        }

    path_signals = _run_path_rules_tier(observations) if "path_rules" in enabled else {}
    exif_signals = _run_exif_tier(observations) if "exif" in enabled else {}
    vlm_single_signals = (
        _run_vlm_single_tier(observations, provider, concurrency, timeout)
        if "vlm_single" in enabled and provider
        else {}
    )
    vlm_pair_signals = (
        _run_vlm_pair_tier(observations, provider, timeout)
        if "vlm_pair" in enabled and provider
        else {}
    )

    results = []
    for obs in observations:
        p = path_signals.get(obs.image_path, {})
        e = exif_signals.get(obs.image_path, {})
        vs = vlm_single_signals.get(obs.image_path, {})
        vp = vlm_pair_signals.get(obs.image_path, {})

        fused_signals = build_signals_from_components(
            path_phase=p.get("phase"), path_confidence=p.get("confidence"),
            path_reasoning=p.get("reasoning", ""),
            exif_phase=e.get("phase"), exif_confidence=e.get("confidence"),
            exif_reasoning=e.get("reasoning", ""),
            vlm_single_phase=vs.get("phase"), vlm_single_confidence=vs.get("confidence"),
            vlm_single_reasoning=vs.get("reasoning", ""),
            vlm_pair_phase=vp.get("phase"), vlm_pair_confidence=vp.get("confidence"),
            vlm_pair_reasoning=vp.get("reasoning", ""),
        )
        fusion = fuse_phase_signals(fused_signals)
        results.append({
            "image_path": obs.image_path,
            "observation_id": obs.observation_id,
            "current_phase": obs.phase,
            "current_confidence": obs.confidence,
            "fusion": {
                "phase": fusion.phase,
                "confidence": fusion.confidence,
                "reasoning": fusion.reasoning,
                "signals_used": fusion.signals_used,
                "agreement": fusion.agreement,
                "conflict_sources": fusion.conflict_sources,
            },
            "tier_signals": {
                "path_rules": p or None,
                "exif_temporal": e or None,
                "vlm_single": vs or None,
                "vlm_pair": vp or None,
            },
        })

    phase_counts: dict[str, int] = {}
    for r in results:
        ph = r["fusion"]["phase"]
        phase_counts[ph] = phase_counts.get(ph, 0) + 1

    return {
        "case_id": case_id,
        "mode": mode,
        "tiers_enabled": sorted(enabled),
        "image_count": len(observations),
        "results": results,
        "summary": {
            "total": len(results),
            "before": phase_counts.get("before", 0),
            "after": phase_counts.get("after", 0),
            "intraop": phase_counts.get("intraop", 0),
            "unknown_held": phase_counts.get("unknown", 0),
        },
    }
