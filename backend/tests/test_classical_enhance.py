"""Tests for backend/services/classical_enhance.py (arm A — focal UnsharpMask).

Asserts: focal region changes, pristine background, originals untouched, and the
K-1 silent-fail contract (bad input → returns input path unchanged).
"""
from __future__ import annotations

import pytest
from PIL import Image, ImageFilter

from backend.services import classical_enhance as ce

np = pytest.importorskip("numpy")


def _portrait(path, size=(600, 800), seed=1):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0 : size[1], 0 : size[0]]
    base = 150 + 30 * np.sin(xx / 25.0) + 20 * np.cos(yy / 30.0) + rng.integers(-10, 10, size=(size[1], size[0]))
    base = np.clip(base, 0, 255).astype(np.uint8)
    rgb = np.stack(
        [np.clip(base * 1.0, 0, 255), np.clip(base * 0.78 + 30, 0, 255), np.clip(base * 0.62 + 45, 0, 255)],
        axis=-1,
    ).astype(np.uint8)
    Image.fromarray(rgb, "RGB").filter(ImageFilter.GaussianBlur(1.0)).save(path, quality=95)


def test_focal_enhance_changes_focal_keeps_background(tmp_path):
    src = tmp_path / "术后.jpg"
    _portrait(src)
    orig_bytes = src.read_bytes()

    out = ce.unsharp_focal_enhance(src, focus_targets=["泪沟"], output_dir=tmp_path / "out")
    assert out.is_file()
    assert out != src

    raw = np.asarray(Image.open(src).convert("RGB"), dtype=np.int16)
    enh = np.asarray(Image.open(out).convert("RGB").resize((raw.shape[1], raw.shape[0])), dtype=np.int16)
    diff = np.abs(enh - raw).sum(axis=-1)
    # Something changed (focal sharpened) ...
    assert diff.max() > 0
    # ... but the far corners (well outside the tear-trough ellipse) are pristine.
    assert diff[0:20, 0:20].mean() < 1.0
    assert diff[-20:, -20:].mean() < 1.0

    # Original is byte-identical (never mutated).
    assert src.read_bytes() == orig_bytes


def test_k1_silent_fail_on_bad_input(tmp_path):
    missing = tmp_path / "nope.jpg"
    out = ce.unsharp_focal_enhance(missing, focus_targets=["泪沟"], output_dir=tmp_path / "out")
    # K-1 contract: returns the input path unchanged on failure.
    assert out == missing


def test_empty_focus_falls_back_to_full_face(tmp_path):
    # No recognised keyword → mask generator falls back to a full-face ellipse;
    # enhancement still produces a valid output (no crash).
    src = tmp_path / "术后.jpg"
    _portrait(src)
    out = ce.unsharp_focal_enhance(src, focus_targets=[], output_dir=tmp_path / "out")
    assert out.is_file()
