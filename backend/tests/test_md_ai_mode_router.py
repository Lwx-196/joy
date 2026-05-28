"""Unit tests for md_ai_mode_router — Phase 1 of 4-mode dispatch."""

from __future__ import annotations

import json

import pytest

from backend.services.md_ai_mode_router import (
    EnhancementMode,
    RouteDecision,
    resolve_mode,
)


# ---------------------------------------------------------------------------
# Signal #1: meta_json.enhancement_mode (operator override) wins everything
# ---------------------------------------------------------------------------

class TestSignal1MetaJsonOverride:

    @pytest.mark.parametrize("mode_value,expected", [
        ("polish", EnhancementMode.POLISH),
        ("archive", EnhancementMode.ARCHIVE),
        ("focal", EnhancementMode.FOCAL),
        ("composite", EnhancementMode.COMPOSITE),
        ("POLISH", EnhancementMode.POLISH),  # case-insensitive
        ("  archive  ", EnhancementMode.ARCHIVE),  # whitespace
    ])
    def test_explicit_mode_returns_mode(self, mode_value: str, expected: EnhancementMode):
        decision = resolve_mode(
            render_job_meta_json=json.dumps({"enhancement_mode": mode_value}),
            brand="md_ai",
            focus_targets=["chin"],  # signal 4 would say focal, but signal 1 wins
        )
        assert decision.mode == expected
        assert decision.reason == "meta_json.enhancement_mode"

    def test_invalid_mode_value_falls_through(self):
        decision = resolve_mode(
            render_job_meta_json=json.dumps({"enhancement_mode": "garbage_mode"}),
            brand="md_ai",
        )
        # Should NOT return REJECTED — fall through to signal #5 brand default
        assert decision.mode == EnhancementMode.POLISH
        assert decision.reason == "brand_default"

    def test_malformed_meta_json_falls_through(self):
        decision = resolve_mode(
            render_job_meta_json="{not valid json",
            brand="md_ai",
        )
        assert decision.mode == EnhancementMode.POLISH
        assert decision.reason == "brand_default"

    def test_rejected_value_in_meta_is_ignored(self):
        """REJECTED is internal sentinel — operator can't request it via meta."""
        decision = resolve_mode(
            render_job_meta_json=json.dumps({"enhancement_mode": "rejected"}),
            brand="md_ai",
        )
        assert decision.mode == EnhancementMode.POLISH
        assert decision.reason == "brand_default"


# ---------------------------------------------------------------------------
# Signal #2: case tags
# ---------------------------------------------------------------------------

class TestSignal2Tags:

    def test_clinical_archive_tag_string(self):
        decision = resolve_mode(
            case_tags_json=json.dumps(["clinical_archive"]),
            brand="md_ai",  # would default to polish via signal 5
        )
        assert decision.mode == EnhancementMode.ARCHIVE
        assert decision.reason == "tags_json.clinical_archive"

    def test_composite_required_tag(self):
        decision = resolve_mode(
            case_tags_json=json.dumps(["composite_required"]),
            brand="md_ai",
        )
        assert decision.mode == EnhancementMode.COMPOSITE

    def test_focal_enhance_tag(self):
        decision = resolve_mode(
            case_tags_json=json.dumps(["focal_enhance"]),
            brand="md_ai",
        )
        assert decision.mode == EnhancementMode.FOCAL

    def test_dict_tag_with_mode_key(self):
        decision = resolve_mode(
            case_tags_json=json.dumps([{"mode": "archive"}]),
            brand="md_ai",
        )
        assert decision.mode == EnhancementMode.ARCHIVE
        assert decision.reason == "tags_json.mode"

    def test_mixed_tags_first_marker_wins(self):
        decision = resolve_mode(
            case_tags_json=json.dumps(["focal_enhance", "clinical_archive"]),
            brand="md_ai",
        )
        # Iteration order from list; focal_enhance is first
        assert decision.mode == EnhancementMode.FOCAL

    def test_case_insensitive_tag(self):
        decision = resolve_mode(
            case_tags_json=json.dumps(["CLINICAL_ARCHIVE"]),
            brand="md_ai",
        )
        assert decision.mode == EnhancementMode.ARCHIVE


# ---------------------------------------------------------------------------
# Signal #3: template-implied composite
# ---------------------------------------------------------------------------

class TestSignal3Template:

    @pytest.mark.parametrize("template", ["tri-compare", "composite", "before-after-pair"])
    def test_composite_template_returns_composite(self, template: str):
        decision = resolve_mode(
            template=template,
            brand="md_ai",  # would default to polish
            focus_targets=["chin"],  # signal 4 would say focal — but signal 3 wins
        )
        assert decision.mode == EnhancementMode.COMPOSITE
        assert decision.reason == "template"

    def test_unknown_template_falls_through(self):
        decision = resolve_mode(
            template="unknown_template",
            brand="md_ai",
        )
        assert decision.mode == EnhancementMode.POLISH  # brand default
        assert decision.reason == "brand_default"


# ---------------------------------------------------------------------------
# Signal #4: focus_targets + AI-allowed brand → focal
# ---------------------------------------------------------------------------

class TestSignal4FocusTargets:

    def test_focus_targets_with_md_ai_returns_focal(self):
        decision = resolve_mode(
            focus_targets=["chin", "lips"],
            brand="md_ai",
        )
        assert decision.mode == EnhancementMode.FOCAL
        assert decision.reason == "focus_targets_with_ai_brand"

    def test_focus_targets_with_meiji_ai_returns_focal(self):
        decision = resolve_mode(
            focus_targets=["lips"],
            brand="meiji_ai",
        )
        assert decision.mode == EnhancementMode.FOCAL

    def test_focus_targets_with_non_ai_brand_falls_through(self):
        decision = resolve_mode(
            focus_targets=["chin"],
            brand="fumei",  # not in AI-allowed set
        )
        # Falls through to signal 5; fumei not in brand_default → REJECTED
        assert decision.mode == EnhancementMode.REJECTED
        assert decision.reason == "fail_closed"

    def test_empty_focus_targets_falls_through(self):
        decision = resolve_mode(
            focus_targets=[],
            brand="md_ai",
        )
        assert decision.mode == EnhancementMode.POLISH  # brand default
        assert decision.reason == "brand_default"


# ---------------------------------------------------------------------------
# Signal #5: brand defaults
# ---------------------------------------------------------------------------

class TestSignal5BrandDefault:

    @pytest.mark.parametrize("brand,expected", [
        ("md_ai", EnhancementMode.POLISH),
        ("meiji_ai", EnhancementMode.POLISH),
        ("MD_AI", EnhancementMode.POLISH),  # case-insensitive
    ])
    def test_known_brand_default(self, brand: str, expected: EnhancementMode):
        decision = resolve_mode(brand=brand)
        assert decision.mode == expected
        assert decision.reason == "brand_default"


# ---------------------------------------------------------------------------
# Signal #6: fail-closed
# ---------------------------------------------------------------------------

class TestSignal6FailClosed:

    def test_no_signals_returns_rejected(self):
        decision = resolve_mode()
        assert decision.mode == EnhancementMode.REJECTED
        assert decision.reason == "fail_closed"

    def test_unknown_brand_without_other_signals_rejected(self):
        decision = resolve_mode(brand="random_brand")
        assert decision.mode == EnhancementMode.REJECTED

    def test_fumei_brand_without_signals_rejected(self):
        """fumei is real production brand but not in AI-allowed nor brand_default."""
        decision = resolve_mode(brand="fumei")
        assert decision.mode == EnhancementMode.REJECTED

    def test_detail_includes_signal_summary(self):
        decision = resolve_mode(
            brand="unknown",
            template="unknown_template",
            focus_targets=["chin"],
        )
        assert decision.mode == EnhancementMode.REJECTED
        assert "unknown" in decision.detail
        assert "chin" in decision.detail


# ---------------------------------------------------------------------------
# Signal priority — higher signal always wins over lower
# ---------------------------------------------------------------------------

class TestSignalPriority:

    def test_meta_override_beats_tags(self):
        decision = resolve_mode(
            render_job_meta_json=json.dumps({"enhancement_mode": "archive"}),
            case_tags_json=json.dumps(["focal_enhance"]),
            brand="md_ai",
        )
        assert decision.mode == EnhancementMode.ARCHIVE
        assert decision.reason == "meta_json.enhancement_mode"

    def test_tags_beat_template(self):
        decision = resolve_mode(
            case_tags_json=json.dumps(["focal_enhance"]),
            template="tri-compare",
            brand="md_ai",
        )
        assert decision.mode == EnhancementMode.FOCAL
        assert decision.reason == "tags_json.focal_enhance"

    def test_template_beats_focus_targets(self):
        decision = resolve_mode(
            template="tri-compare",
            focus_targets=["chin"],
            brand="md_ai",
        )
        assert decision.mode == EnhancementMode.COMPOSITE
        assert decision.reason == "template"

    def test_focus_targets_beat_brand_default(self):
        decision = resolve_mode(
            focus_targets=["chin"],
            brand="md_ai",
        )
        assert decision.mode == EnhancementMode.FOCAL
        assert decision.reason == "focus_targets_with_ai_brand"


# ---------------------------------------------------------------------------
# Edge: RouteDecision is hashable & immutable
# ---------------------------------------------------------------------------

def test_route_decision_is_frozen():
    decision = resolve_mode(brand="md_ai")
    with pytest.raises((AttributeError, Exception)):
        # frozen=True dataclass refuses attribute mutation
        decision.mode = EnhancementMode.ARCHIVE  # type: ignore


def test_enhancement_mode_is_str_enum():
    """EnhancementMode inherits str → JSON-serialisable as plain string."""
    assert EnhancementMode.POLISH == "polish"
    assert json.dumps({"m": EnhancementMode.ARCHIVE.value}) == '{"m": "archive"}'
