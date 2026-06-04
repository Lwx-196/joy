"""Pair-based comparative classification for same-group medical-aesthetic images."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .vlm_provider import VLMProvider, VLMRequest, VLMResponse
from .vlm_source_classifier import _float_0_1

logger = logging.getLogger(__name__)

PAIR_CLASSIFICATION_PROMPT = """You are a medical-aesthetic clinical photography analyst.
You are given TWO photos of the SAME patient taken at different times.
Determine which is the pre-treatment (术前) photo and which is the post-treatment (术后) photo.

Return strict JSON:
{"image_a_phase":"before|after|uncertain","image_b_phase":"before|after|uncertain","confidence":0.0,"reasoning":"comparative visual evidence","comparative_cues":[]}

## How to Compare
- Look for RELATIVE differences between the two images:
  - Volume changes: which image shows more fullness in treatment areas?
  - Skin quality: which has more/less wrinkles, hollowing, sagging?
  - Treatment signs: does either show redness, bruising, swelling, injection marks?
  - Contour changes: which shows improved facial contour/symmetry?
- The "before" photo typically shows: natural aging signs, hollowing, asymmetry, untreated texture
- The "after" photo typically shows: improved volume, smoother contours, possible residual treatment signs
- If both look nearly identical → "uncertain" for both
- If one clearly shows post-procedure signs (swelling/bruising) → that one is "after"

## Rules
- confidence 0-1; higher when comparative difference is clear
- comparative_cues: list what differs between the two (e.g. ["image_a has tear trough hollowing, image_b shows fill"])
"""

VALID_PAIR_PHASES = {"before", "after", "uncertain"}
_COMPARATIVE_CUES_CAP = 10
_HIGH_CONFIDENCE_THRESHOLD = 0.85


@dataclass(frozen=True)
class ImageCandidate:
    observation_id: int
    image_path: Path
    phase: str
    confidence: float


@dataclass(frozen=True)
class PairClassificationResult:
    image_a_path: Path
    image_b_path: Path
    image_a_phase: str
    image_b_phase: str
    confidence: float
    reasoning: str
    comparative_cues: list[str]
    provider: str
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int


def select_comparison_pairs(
    candidates: list[ImageCandidate],
    *,
    max_pairs: int = 5,
) -> list[tuple[ImageCandidate, ImageCandidate]]:
    """Select the most informative pairs from candidates within the same group.

    Strategy:
    1. Skip groups where all candidates have confidence > threshold (already settled).
    2. Pair the lowest-confidence candidate with the highest-confidence one.
    3. Continue greedily until *max_pairs* reached or candidates exhausted.
    """
    if len(candidates) < 2:
        return []

    sorted_by_conf = sorted(candidates, key=lambda c: c.confidence)

    # If every candidate is already high-confidence, nothing to compare.
    if sorted_by_conf[0].confidence > _HIGH_CONFIDENCE_THRESHOLD:
        return []

    used: set[int] = set()
    pairs: list[tuple[ImageCandidate, ImageCandidate]] = []

    low_pool = [c for c in sorted_by_conf if c.confidence <= _HIGH_CONFIDENCE_THRESHOLD]
    high_pool = list(reversed(sorted_by_conf))

    for low in low_pool:
        if len(pairs) >= max_pairs:
            break
        if low.observation_id in used:
            continue
        for high in high_pool:
            if high.observation_id == low.observation_id:
                continue
            if high.observation_id in used:
                continue
            pairs.append((low, high))
            used.add(low.observation_id)
            used.add(high.observation_id)
            break

    return pairs


def _parse_pair_result(
    image_a: Path,
    image_b: Path,
    response: VLMResponse,
) -> PairClassificationResult:
    parsed: dict[str, Any] = response.parsed if isinstance(response.parsed, dict) else {}

    image_a_phase = str(parsed.get("image_a_phase") or "uncertain").strip().lower()
    image_b_phase = str(parsed.get("image_b_phase") or "uncertain").strip().lower()

    if image_a_phase not in VALID_PAIR_PHASES:
        image_a_phase = "uncertain"
    if image_b_phase not in VALID_PAIR_PHASES:
        image_b_phase = "uncertain"

    confidence = _float_0_1(parsed.get("confidence", 0.0), field_name="confidence")
    reasoning = str(parsed.get("reasoning") or "").strip()

    comparative_cues = parsed.get("comparative_cues", [])
    if not isinstance(comparative_cues, list):
        comparative_cues = []
    comparative_cues = [str(c) for c in comparative_cues if c][:_COMPARATIVE_CUES_CAP]

    return PairClassificationResult(
        image_a_path=image_a,
        image_b_path=image_b,
        image_a_phase=image_a_phase,
        image_b_phase=image_b_phase,
        confidence=confidence,
        reasoning=reasoning,
        comparative_cues=comparative_cues,
        provider=response.provider,
        model=response.model,
        latency_ms=response.latency_ms,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )


def classify_pair(
    image_a: Path,
    image_b: Path,
    provider: VLMProvider,
    *,
    timeout: float = 45.0,
) -> PairClassificationResult:
    """Send a pair of images to VLM for comparative before/after classification."""
    response = provider.call_vision(
        PAIR_CLASSIFICATION_PROMPT,
        [Path(image_a), Path(image_b)],
        timeout=timeout,
        purpose="pair_classifier",
    )
    return _parse_pair_result(Path(image_a), Path(image_b), response)


def classify_pairs_batch(
    pairs: list[tuple[Path, Path]],
    provider: VLMProvider,
    *,
    concurrency: int = 2,
    timeout: float = 45.0,
) -> list[PairClassificationResult | BaseException]:
    """Batch comparative classification via VLM."""
    requests = [
        VLMRequest(
            prompt=PAIR_CLASSIFICATION_PROMPT,
            images=[Path(a), Path(b)],
            timeout=timeout,
            purpose="pair_classifier",
        )
        for a, b in pairs
    ]
    responses = provider.call_vision_batch(
        requests,
        concurrency=max(1, int(concurrency or 1)),
        return_exceptions=True,
    )
    results: list[PairClassificationResult | BaseException] = []
    for (path_a, path_b), response in zip(pairs, responses):
        if isinstance(response, BaseException):
            results.append(response)
            continue
        try:
            results.append(_parse_pair_result(Path(path_a), Path(path_b), response))
        except (ValueError, TypeError, AttributeError) as exc:
            results.append(exc)
    return results
