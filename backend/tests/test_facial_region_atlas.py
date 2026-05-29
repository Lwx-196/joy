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


def test_confidence_values_valid():
    for region, spec in atlas.FACIAL_REGION_ATLAS.items():
        assert spec.get("confidence") in {"high", "inferred"}, region
        assert spec.get("source"), f"{region} missing source"
        assert spec.get("rationale"), f"{region} missing rationale"
