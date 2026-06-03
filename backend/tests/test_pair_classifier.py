"""Tests for pair-based comparative classification."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from backend.services.pair_classifier import (
    PAIR_CLASSIFICATION_PROMPT,
    ImageCandidate,
    PairClassificationResult,
    _parse_pair_result,
    classify_pair,
    classify_pairs_batch,
    select_comparison_pairs,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

@dataclass
class FakeVLMResponse:
    text: str = ""
    parsed: dict = field(default_factory=dict)
    provider: str = "fake"
    model: str = "fake-model"
    latency_ms: int = 100
    input_tokens: int = 500
    output_tokens: int = 100
    usage_raw: dict = field(default_factory=dict)
    response_id: str | None = None


class FakeVLMProvider:
    def __init__(self, response: FakeVLMResponse | BaseException):
        self._response = response
        self.call_count = 0

    def call_vision(self, prompt, images, *, timeout=30.0, purpose=""):
        self.call_count += 1
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response

    def call_vision_batch(self, requests, *, concurrency=2, return_exceptions=False):
        results = []
        for _req in requests:
            try:
                results.append(self.call_vision(_req.prompt, _req.images, timeout=_req.timeout, purpose=_req.purpose))
            except BaseException as exc:
                if not return_exceptions:
                    raise
                results.append(exc)
        return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _candidate(obs_id: int, phase: str = "unknown", confidence: float = 0.3) -> ImageCandidate:
    return ImageCandidate(
        observation_id=obs_id,
        image_path=Path(f"/img/{obs_id}.jpg"),
        phase=phase,
        confidence=confidence,
    )


def _good_parsed() -> dict:
    return {
        "image_a_phase": "before",
        "image_b_phase": "after",
        "confidence": 0.92,
        "reasoning": "image_a shows hollowing, image_b shows fill",
        "comparative_cues": ["tear trough hollowing in A", "volume increase in B"],
    }


# ---------------------------------------------------------------------------
# select_comparison_pairs
# ---------------------------------------------------------------------------

class TestSelectComparisonPairs:
    def test_empty_candidates(self):
        assert select_comparison_pairs([]) == []

    def test_single_candidate(self):
        assert select_comparison_pairs([_candidate(1)]) == []

    def test_all_high_confidence(self):
        candidates = [
            _candidate(1, "before", 0.95),
            _candidate(2, "after", 0.90),
        ]
        assert select_comparison_pairs(candidates) == []

    def test_mixed_confidence_returns_pairs(self):
        candidates = [
            _candidate(1, "unknown", 0.2),
            _candidate(2, "before", 0.9),
            _candidate(3, "unknown", 0.4),
        ]
        pairs = select_comparison_pairs(candidates)
        assert len(pairs) >= 1
        low_ids = {p[0].observation_id for p in pairs}
        assert 1 in low_ids or 3 in low_ids

    def test_max_pairs_limit(self):
        candidates = [_candidate(i, "unknown", 0.1 * i) for i in range(1, 11)]
        pairs = select_comparison_pairs(candidates, max_pairs=2)
        assert len(pairs) <= 2

    def test_pairs_low_with_high(self):
        candidates = [
            _candidate(1, "unknown", 0.1),
            _candidate(2, "after", 0.95),
        ]
        pairs = select_comparison_pairs(candidates)
        assert len(pairs) == 1
        low, high = pairs[0]
        assert low.observation_id == 1
        assert high.observation_id == 2

    def test_no_self_pairing(self):
        candidates = [_candidate(1, "unknown", 0.3), _candidate(2, "unknown", 0.4)]
        pairs = select_comparison_pairs(candidates)
        for a, b in pairs:
            assert a.observation_id != b.observation_id


# ---------------------------------------------------------------------------
# _parse_pair_result
# ---------------------------------------------------------------------------

class TestParsePairResult:
    def test_normal_parse(self):
        response = FakeVLMResponse(parsed=_good_parsed())
        result = _parse_pair_result(Path("/a.jpg"), Path("/b.jpg"), response)
        assert result.image_a_phase == "before"
        assert result.image_b_phase == "after"
        assert result.confidence == 0.92
        assert len(result.comparative_cues) == 2
        assert result.provider == "fake"

    def test_invalid_phase_falls_back_to_uncertain(self):
        parsed = _good_parsed()
        parsed["image_a_phase"] = "bogus"
        parsed["image_b_phase"] = "nonsense"
        response = FakeVLMResponse(parsed=parsed)
        result = _parse_pair_result(Path("/a.jpg"), Path("/b.jpg"), response)
        assert result.image_a_phase == "uncertain"
        assert result.image_b_phase == "uncertain"

    def test_missing_phase_defaults_uncertain(self):
        response = FakeVLMResponse(parsed={"confidence": 0.5})
        result = _parse_pair_result(Path("/a.jpg"), Path("/b.jpg"), response)
        assert result.image_a_phase == "uncertain"
        assert result.image_b_phase == "uncertain"

    def test_comparative_cues_non_list_fallback(self):
        parsed = _good_parsed()
        parsed["comparative_cues"] = "not a list"
        response = FakeVLMResponse(parsed=parsed)
        result = _parse_pair_result(Path("/a.jpg"), Path("/b.jpg"), response)
        assert result.comparative_cues == []

    def test_comparative_cues_cap_at_10(self):
        parsed = _good_parsed()
        parsed["comparative_cues"] = [f"cue_{i}" for i in range(20)]
        response = FakeVLMResponse(parsed=parsed)
        result = _parse_pair_result(Path("/a.jpg"), Path("/b.jpg"), response)
        assert len(result.comparative_cues) == 10

    def test_confidence_out_of_range_raises(self):
        parsed = _good_parsed()
        parsed["confidence"] = 1.5
        response = FakeVLMResponse(parsed=parsed)
        with pytest.raises(ValueError, match="must be a number between 0 and 1"):
            _parse_pair_result(Path("/a.jpg"), Path("/b.jpg"), response)

    def test_confidence_negative_raises(self):
        parsed = _good_parsed()
        parsed["confidence"] = -0.1
        response = FakeVLMResponse(parsed=parsed)
        with pytest.raises(ValueError, match="must be a number between 0 and 1"):
            _parse_pair_result(Path("/a.jpg"), Path("/b.jpg"), response)

    def test_empty_parsed_dict(self):
        response = FakeVLMResponse(parsed={})
        result = _parse_pair_result(Path("/a.jpg"), Path("/b.jpg"), response)
        assert result.image_a_phase == "uncertain"
        assert result.image_b_phase == "uncertain"
        assert result.confidence == 0.0

    def test_parsed_not_dict(self):
        response = FakeVLMResponse(parsed="not a dict")
        result = _parse_pair_result(Path("/a.jpg"), Path("/b.jpg"), response)
        assert result.image_a_phase == "uncertain"
        assert result.image_b_phase == "uncertain"


# ---------------------------------------------------------------------------
# classify_pair (via mock provider)
# ---------------------------------------------------------------------------

class TestClassifyPair:
    def test_success(self):
        response = FakeVLMResponse(parsed=_good_parsed())
        provider = FakeVLMProvider(response)
        result = classify_pair(Path("/a.jpg"), Path("/b.jpg"), provider, timeout=10.0)
        assert isinstance(result, PairClassificationResult)
        assert result.image_a_phase == "before"
        assert result.image_b_phase == "after"
        assert provider.call_count == 1

    def test_provider_raises(self):
        provider = FakeVLMProvider(RuntimeError("VLM down"))
        with pytest.raises(RuntimeError, match="VLM down"):
            classify_pair(Path("/a.jpg"), Path("/b.jpg"), provider)


# ---------------------------------------------------------------------------
# classify_pairs_batch
# ---------------------------------------------------------------------------

class TestClassifyPairsBatch:
    def test_batch_success(self):
        response = FakeVLMResponse(parsed=_good_parsed())
        provider = FakeVLMProvider(response)
        pairs = [(Path("/a1.jpg"), Path("/b1.jpg")), (Path("/a2.jpg"), Path("/b2.jpg"))]
        results = classify_pairs_batch(pairs, provider, concurrency=1)
        assert len(results) == 2
        for r in results:
            assert isinstance(r, PairClassificationResult)

    def test_batch_with_exception(self):
        provider = FakeVLMProvider(RuntimeError("boom"))
        pairs = [(Path("/a.jpg"), Path("/b.jpg"))]
        results = classify_pairs_batch(pairs, provider)
        assert len(results) == 1
        assert isinstance(results[0], RuntimeError)

    def test_batch_empty(self):
        response = FakeVLMResponse(parsed=_good_parsed())
        provider = FakeVLMProvider(response)
        results = classify_pairs_batch([], provider)
        assert results == []


# ---------------------------------------------------------------------------
# PAIR_CLASSIFICATION_PROMPT content
# ---------------------------------------------------------------------------

class TestPromptContent:
    def test_contains_two_photos(self):
        assert "TWO photos" in PAIR_CLASSIFICATION_PROMPT

    def test_contains_same_patient(self):
        assert "SAME patient" in PAIR_CLASSIFICATION_PROMPT

    def test_contains_image_a_phase(self):
        assert "image_a_phase" in PAIR_CLASSIFICATION_PROMPT

    def test_contains_image_b_phase(self):
        assert "image_b_phase" in PAIR_CLASSIFICATION_PROMPT

    def test_contains_comparative_cues(self):
        assert "comparative_cues" in PAIR_CLASSIFICATION_PROMPT
