"""Tests for facial_region_atlas — the FaceMesh region knowledge base (pure logic)."""

from __future__ import annotations

from backend.services import facial_region_atlas as atlas


def test_all_indices_in_478_range():
    # Every landmark index must be a valid FaceLandmarker(refine) index 0..477.
    bad = []
    for region, spec in atlas.FACIAL_REGION_ATLAS.items():
        for key in ("idx", "left_idx", "right_idx", "center_idx"):
            v = spec.get(key)
            idxs = v if isinstance(v, list) else ([v] if isinstance(v, int) else [])
            for i in idxs:
                if not (0 <= i <= 477):
                    bad.append((region, key, i))
    assert not bad, f"out-of-range indices: {bad}"
    for name, group in atlas.FACEMESH_ANCHORS.items():
        idxs = group if isinstance(group, list) else [group]
        assert all(0 <= i <= 477 for i in idxs), f"anchor {name} out of range"


def test_every_region_has_shape_and_groups():
    for region in atlas.FACIAL_REGION_ATLAS:
        assert atlas.region_shape(region) in {"ellipse", "polygon", "polyline", "ribbon"}
        assert atlas.region_landmark_groups(region), f"{region} has no groups"


def test_symmetric_regions_have_two_groups():
    # 泪沟/法令纹/苹果肌 etc. are left+right → 2 groups.
    assert len(atlas.region_landmark_groups("泪沟")) == 2
    assert len(atlas.region_landmark_groups("法令纹")) == 2
    # 下巴/鼻尖 are centered → 1 group.
    assert len(atlas.region_landmark_groups("下巴")) == 1
    assert len(atlas.region_landmark_groups("鼻尖")) == 1


def test_resolve_region_key():
    assert atlas.resolve_region_key("泪沟") == "泪沟"
    assert atlas.resolve_region_key("tear_trough") == "泪沟"
    assert atlas.resolve_region_key("丰唇") == "唇"
    # substring from a procedure folder name
    assert atlas.resolve_region_key("玻尿酸注射面颊，下巴") == "面颊"
    assert atlas.resolve_region_key("童颜针全脸") is None


def test_phase1_new_regions_present_and_resolve():
    # 4 regions added/redone in Phase 1 (real-case keywords 咬肌/川字/太阳穴 + 颧骨 redo).
    for region in ("咬肌", "川字", "太阳穴", "颧骨"):
        assert region in atlas.FACIAL_REGION_ATLAS, region
        assert atlas.region_landmark_groups(region), f"{region} has no groups"
    # resolve from procedure substrings (single-region strings; multi-region dirs
    # need a separate all-matches extractor — see panel design)
    assert atlas.resolve_region_key("瘦脸针咬肌注射") == "咬肌"
    assert atlas.resolve_region_key("骨性1支注射川字纹") == "川字"
    assert atlas.resolve_region_key("太阳穴填充") == "太阳穴"
    # 川字 is a midline single-group zone
    assert len(atlas.region_landmark_groups("川字")) == 1
    # 颧骨 redone to an ellipse, symmetric L/R
    assert atlas.region_shape("颧骨") == "ellipse"
    assert len(atlas.region_landmark_groups("颧骨")) == 2


# high = 官方 connections 索引; inferred = 社区图待校准;
# calibrated = Phase 1 真实正脸叠点校准过; uncalibrated-unused = 真实语料 0 例、暂不重做
_VALID_CONFIDENCE = {"high", "inferred", "calibrated", "uncalibrated-unused"}


def test_region_views_and_effects_consistent():
    valid_views = {atlas.VIEW_FRONT, atlas.VIEW_OBLIQUE, atlas.VIEW_PROFILE}
    valid_sig = {atlas.SIG_HIGHLIGHT, atlas.SIG_SHADOW, atlas.SIG_OGEE,
                 atlas.SIG_LINE, atlas.SIG_WIDTH, atlas.SIG_VOLUME}
    # every atlas region has a view list and an effect signal
    for region in atlas.FACIAL_REGION_ATLAS:
        views = atlas.region_views(region)
        assert views and all(v in valid_views for v in views), region
        assert atlas.region_effect(region) in valid_sig, region
    # literature-grounded routing (knowledge base): projection→front-first,
    # ogee/contour→oblique-first, hollow→shadow signal
    assert atlas.region_views("鼻尖")[0] == atlas.VIEW_FRONT      # 高光 frontal
    assert atlas.region_views("下巴")[0] == atlas.VIEW_FRONT      # 高光 frontal
    assert atlas.region_views("太阳穴")[0] == atlas.VIEW_OBLIQUE  # 颞凹陷正面占比小
    assert atlas.region_views("下颌线")[0] == atlas.VIEW_OBLIQUE  # ogee/轮廓
    assert atlas.region_views("咬肌")[0] == atlas.VIEW_FRONT      # 瘦脸宽度正面
    assert atlas.region_effect("泪沟") == atlas.SIG_SHADOW
    assert atlas.region_effect("鼻尖") == atlas.SIG_HIGHLIGHT
    assert atlas.region_effect("面颊") == atlas.SIG_OGEE


def test_confidence_values_valid():
    for region, spec in atlas.FACIAL_REGION_ATLAS.items():
        assert spec.get("confidence") in _VALID_CONFIDENCE, region
        assert spec.get("source"), f"{region} missing source"
        assert spec.get("rationale"), f"{region} missing rationale"
