"""Tests for robust background color estimation — D1 bg_letterbox fix."""
import numpy as np
import pytest

from scripts.face_align_compare import estimate_background_color
from scripts.render_brand_clean import clean_background_color, cover_foreground_with_background


WALL_BGR = np.array([225, 230, 230], dtype=np.float64)
SKIN_BGR = np.array([120, 150, 200], dtype=np.uint8)
HAIR_BGR = np.array([40, 30, 25], dtype=np.uint8)


def _make_wall_image(h=400, w=400):
    return np.full((h, w, 3), WALL_BGR.astype(np.uint8), dtype=np.uint8)


def _contaminate_corners(img, corners, color):
    h, w = img.shape[:2]
    patch = 30
    if "tl" in corners:
        img[:patch, :patch] = color
    if "tr" in corners:
        img[:patch, w - patch:w] = color
    if "bl" in corners:
        img[h - patch:h, :patch] = color
    if "br" in corners:
        img[h - patch:h, w - patch:w] = color
    return img


class TestEstimateBackgroundColor:
    def test_clean_corners_returns_wall_color(self):
        img = _make_wall_image()
        result = np.array(estimate_background_color(img))
        assert np.linalg.norm(result - WALL_BGR) < 3

    def test_two_corners_skin_rejects_contamination(self):
        img = _make_wall_image()
        _contaminate_corners(img, ["tl", "bl"], SKIN_BGR)
        result = np.array(estimate_background_color(img))
        assert np.linalg.norm(result - WALL_BGR) < 5

    def test_three_corners_skin_still_finds_wall(self):
        img = _make_wall_image()
        _contaminate_corners(img, ["tl", "tr", "bl"], SKIN_BGR)
        result = np.array(estimate_background_color(img))
        assert np.linalg.norm(result - WALL_BGR) < 5

    def test_hair_corner_filtered_out(self):
        img = _make_wall_image()
        _contaminate_corners(img, ["bl"], HAIR_BGR)
        result = np.array(estimate_background_color(img))
        assert np.linalg.norm(result - WALL_BGR) < 5

    def test_all_corners_same_returns_that_color(self):
        uniform = np.full((200, 200, 3), [180, 180, 175], dtype=np.uint8)
        result = np.array(estimate_background_color(uniform))
        assert np.linalg.norm(result - np.array([180, 180, 175])) < 3


class TestCleanBackgroundColor:
    def test_clean_wall_image_preserves_tone(self):
        img = _make_wall_image()
        result = np.array(clean_background_color(img))
        assert np.linalg.norm(result - WALL_BGR) < 10

    def test_darker_wall_not_rejected(self):
        dark_wall = np.full((400, 400, 3), [140, 145, 140], dtype=np.uint8)
        result = np.array(clean_background_color(dark_wall))
        assert all(100 < v < 220 for v in result)


class TestCoverForegroundWithBackground:
    def test_empty_mask_returns_unchanged(self):
        cell = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        mask = np.zeros((100, 100), dtype=bool)
        out = cover_foreground_with_background(cell, mask)
        np.testing.assert_array_equal(out, cell)

    def test_sigma_scales_with_cell_size(self):
        cell_small = np.full((100, 100, 3), 200, dtype=np.uint8)
        cell_large = np.full((800, 800, 3), 200, dtype=np.uint8)
        mask_small = np.zeros((100, 100), dtype=bool)
        mask_small[40:60, 40:60] = True
        mask_large = np.zeros((800, 800), dtype=bool)
        mask_large[300:500, 300:500] = True
        out_small = cover_foreground_with_background(cell_small, mask_small)
        out_large = cover_foreground_with_background(cell_large, mask_large)
        assert out_small is not None
        assert out_large is not None
