"""Tests for enhanced VLM source classifier prompt and related parsing."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.services.vlm_source_classifier import (
    CLASSIFICATION_PROMPT,
    ClassificationResult,
    _normalize_phase,
    _parse_result,
)
from backend.services.vlm_provider import VLMResponse


# ---------------------------------------------------------------------------
# 1. _normalize_phase — new mappings
# ---------------------------------------------------------------------------

class TestNormalizePhaseNewMappings:
    """New healing/uncertain mappings plus existing ones stay intact."""

    def test_healing_maps_to_after(self) -> None:
        assert _normalize_phase("healing") == "after"

    def test_healing_case_insensitive(self) -> None:
        assert _normalize_phase("Healing") == "after"

    def test_recovery_chinese_maps_to_after(self) -> None:
        assert _normalize_phase("恢复期") == "after"

    def test_uncertain_maps_to_unknown(self) -> None:
        assert _normalize_phase("uncertain") == "unknown"

    def test_uncertain_case_insensitive(self) -> None:
        assert _normalize_phase("Uncertain") == "unknown"

    def test_uncertain_chinese_maps_to_unknown(self) -> None:
        assert _normalize_phase("不确定") == "unknown"

    # Existing mappings must remain intact
    def test_before_unchanged(self) -> None:
        assert _normalize_phase("before") == "before"

    def test_after_unchanged(self) -> None:
        assert _normalize_phase("after") == "after"

    def test_pre_maps_to_before(self) -> None:
        assert _normalize_phase("pre") == "before"

    def test_post_maps_to_after(self) -> None:
        assert _normalize_phase("post") == "after"

    def test_during_maps_to_intraop(self) -> None:
        assert _normalize_phase("during") == "intraop"

    def test_intraop_unchanged(self) -> None:
        assert _normalize_phase("intraop") == "intraop"

    def test_chinese_before(self) -> None:
        assert _normalize_phase("术前") == "before"

    def test_chinese_after(self) -> None:
        assert _normalize_phase("术后") == "after"

    def test_chinese_during(self) -> None:
        assert _normalize_phase("术中") == "intraop"

    def test_invalid_phase_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid VLM classification phase"):
            _normalize_phase("nonsense")


# ---------------------------------------------------------------------------
# 2. _parse_result — visual_cues parsing
# ---------------------------------------------------------------------------

def _make_vlm_response(parsed: dict) -> VLMResponse:
    """Helper: build a VLMResponse with the given parsed dict."""
    return VLMResponse(
        text="",
        parsed=parsed,
        provider="test",
        model="test-model",
        latency_ms=100,
        input_tokens=50,
        output_tokens=20,
    )


class TestParseResultVisualCues:
    """_parse_result correctly extracts visual_cues from VLM output."""

    def test_visual_cues_parsed(self) -> None:
        response = _make_vlm_response({
            "phase": "after",
            "view": "front",
            "body_part": "face",
            "confidence": 0.9,
            "reasoning": "redness visible",
            "visual_cues": ["localized redness at tear trough", "no bruising"],
        })
        result = _parse_result(Path("/img.jpg"), response)
        assert result.visual_cues == ["localized redness at tear trough", "no bruising"]

    def test_visual_cues_missing_defaults_empty(self) -> None:
        response = _make_vlm_response({
            "phase": "before",
            "view": "front",
            "body_part": "face",
            "confidence": 0.8,
            "reasoning": "no signs",
        })
        result = _parse_result(Path("/img.jpg"), response)
        assert result.visual_cues == []

    def test_visual_cues_non_list_defaults_empty(self) -> None:
        response = _make_vlm_response({
            "phase": "before",
            "view": "side",
            "body_part": "face",
            "confidence": 0.7,
            "reasoning": "ok",
            "visual_cues": "not a list",
        })
        result = _parse_result(Path("/img.jpg"), response)
        assert result.visual_cues == []

    def test_visual_cues_truncated_at_10(self) -> None:
        cues = [f"cue_{i}" for i in range(15)]
        response = _make_vlm_response({
            "phase": "after",
            "view": "45deg",
            "body_part": "face",
            "confidence": 0.95,
            "reasoning": "many cues",
            "visual_cues": cues,
        })
        result = _parse_result(Path("/img.jpg"), response)
        assert len(result.visual_cues) == 10
        assert result.visual_cues == [f"cue_{i}" for i in range(10)]

    def test_visual_cues_filters_empty_strings(self) -> None:
        response = _make_vlm_response({
            "phase": "after",
            "view": "front",
            "body_part": "face",
            "confidence": 0.85,
            "reasoning": "filter test",
            "visual_cues": ["redness", "", None, "swelling", ""],
        })
        result = _parse_result(Path("/img.jpg"), response)
        assert result.visual_cues == ["redness", "swelling"]

    def test_healing_phase_parsed_as_after(self) -> None:
        response = _make_vlm_response({
            "phase": "healing",
            "view": "front",
            "body_part": "face",
            "confidence": 0.75,
            "reasoning": "yellowing bruise",
            "visual_cues": ["yellowing bruise"],
        })
        result = _parse_result(Path("/img.jpg"), response)
        assert result.phase == "after"
        assert result.visual_cues == ["yellowing bruise"]

    def test_uncertain_phase_parsed_as_unknown(self) -> None:
        response = _make_vlm_response({
            "phase": "uncertain",
            "view": "front",
            "body_part": "face",
            "confidence": 0.4,
            "reasoning": "no clear cues",
            "visual_cues": [],
        })
        result = _parse_result(Path("/img.jpg"), response)
        assert result.phase == "unknown"


# ---------------------------------------------------------------------------
# 3. CLASSIFICATION_PROMPT content checks
# ---------------------------------------------------------------------------

class TestClassificationPromptContent:
    """Prompt includes the new phase options and visual_cues field."""

    def test_prompt_contains_healing_option(self) -> None:
        assert "healing" in CLASSIFICATION_PROMPT

    def test_prompt_contains_uncertain_option(self) -> None:
        assert "uncertain" in CLASSIFICATION_PROMPT

    def test_prompt_contains_visual_cues_field(self) -> None:
        assert "visual_cues" in CLASSIFICATION_PROMPT

    def test_prompt_contains_post_treatment_cues(self) -> None:
        assert "POST-treatment" in CLASSIFICATION_PROMPT
        assert "erythema" in CLASSIFICATION_PROMPT

    def test_prompt_contains_pre_treatment_cues(self) -> None:
        assert "PRE-treatment" in CLASSIFICATION_PROMPT
        assert "Natural facial hollowing" in CLASSIFICATION_PROMPT

    def test_prompt_contains_healing_cues(self) -> None:
        assert "Yellowing bruise" in CLASSIFICATION_PROMPT

    def test_prompt_contains_view_classification(self) -> None:
        assert "front:" in CLASSIFICATION_PROMPT
        assert "45deg:" in CLASSIFICATION_PROMPT
        assert "side:" in CLASSIFICATION_PROMPT
        assert "back:" in CLASSIFICATION_PROMPT
