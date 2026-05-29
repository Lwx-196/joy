"""Tests for treatment_zone_panel — geometry layer (pure) + multi-region extraction.

Landmark detection / line-art / PIL compositing are IO and not unit-tested here;
the geometry layer takes a synthetic (478,2) point array so it runs without mediapipe.
"""
from __future__ import annotations

import pytest

# 重 CV 依赖（numpy/cv2）按设计不在 backend venv（线稿/合成走 skill 子进程或本地 dev）。
# CI backend venv 无这些 → 优雅跳过；本地 dev 环境照跑。
pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from backend.services import facial_region_atlas as atlas  # noqa: E402
from backend.services import treatment_zone_panel as tzp  # noqa: E402


def _synthetic_pts(n: int = 478) -> np.ndarray:
    # deterministic non-collinear spread (grid) so fitEllipse has rank-2 input
    xs = (np.arange(n) % 30) * 17.0 + 50.0
    ys = (np.arange(n) // 30) * 19.0 + 40.0
    return np.stack([xs, ys], axis=1).astype(np.float32)


def test_extract_regions_multi():
    # multi-procedure folder names → all matching regions (not just first)
    assert atlas.extract_regions("玻尿酸注射面颊，下巴") == ["面颊", "下巴"]
    got = atlas.extract_regions("保妥适350U下颌线、颈阔肌咬肌")
    assert "下颌线" in got and "咬肌" in got
    assert atlas.extract_regions("童颜针全脸") == []
    # dedupe: a region named twice appears once
    assert atlas.extract_regions("泪沟，泪沟填充") == ["泪沟"]


def test_geometry_ellipse_region_two_fills():
    shapes = tzp.region_geometry("颧骨", _synthetic_pts())
    assert len(shapes) == 2  # symmetric L/R
    assert all(s.kind == "fill" for s in shapes)
    assert all(len(s.points) >= 6 for s in shapes)


def test_geometry_polyline_is_stroke_with_width():
    shapes = tzp.region_geometry("下颌线", _synthetic_pts())
    assert len(shapes) == 2
    assert all(s.kind == "stroke" and s.width > 0 for s in shapes)


def test_geometry_ribbon_is_stroke():
    shapes = tzp.region_geometry("泪沟", _synthetic_pts())
    assert len(shapes) == 2
    assert all(s.kind == "stroke" for s in shapes)


def test_geometry_polygon_region_fill():
    shapes = tzp.region_geometry("面颊", _synthetic_pts())
    assert len(shapes) == 2
    assert all(s.kind == "fill" for s in shapes)


def test_geometry_midline_single_group():
    shapes = tzp.region_geometry("川字", _synthetic_pts())
    assert len(shapes) == 1


def test_geometry_unknown_region_empty():
    assert tzp.region_geometry("不存在区", _synthetic_pts()) == []


def test_geometry_points_are_int_and_in_bounds():
    pts = _synthetic_pts()
    for region in ("泪沟", "颧骨", "下颌线", "面颊", "川字"):
        for s in tzp.region_geometry(region, pts):
            assert s.points.dtype == np.int32
            assert s.points.ndim == 2 and s.points.shape[1] == 2
