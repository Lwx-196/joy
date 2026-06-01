"""Tests for mask_anchor_composite — gpt-image-2 整帧效果锁回原图(硬底线: mask 外字节级==原图).

纯 PIL（CI 无 numpy），合成图验证 demo + case45 Run A/B 验证过的 mask 锚定不变量。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from backend.services.mask_anchor_composite import CompositeResult, mask_anchor_composite


def _solid(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> Path:
    Image.new("RGB", size, color).save(path)
    return path


def _circle_mask(path: Path, size: tuple[int, int], r: int) -> Path:
    m = Image.new("L", size, 0)
    cx, cy = size[0] // 2, size[1] // 2
    ImageDraw.Draw(m).ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    m.save(path)
    return path


def test_outside_mask_is_byte_exact(tmp_path: Path):
    orig = _solid(tmp_path / "o.png", (100, 100), (200, 50, 50))
    ai = _solid(tmp_path / "a.png", (100, 100), (50, 50, 200))
    mask = _circle_mask(tmp_path / "m.png", (100, 100), 20)
    res = mask_anchor_composite(orig, ai, mask, tmp_path / "out.png")
    assert res.outside_exact
    out = Image.open(tmp_path / "out.png").convert("RGB")
    o = Image.open(orig).convert("RGB")
    # corner (outside circle) == original byte-exact
    assert out.getpixel((2, 2)) == o.getpixel((2, 2)) == (200, 50, 50)
    # center (inside circle, mask=255) == AI
    assert out.getpixel((50, 50)) == (50, 50, 200)


def test_all_black_mask_returns_original_unchanged(tmp_path: Path):
    orig = _solid(tmp_path / "o.png", (64, 64), (123, 222, 11))
    ai = _solid(tmp_path / "a.png", (64, 64), (0, 0, 0))
    Image.new("L", (64, 64), 0).save(tmp_path / "m.png")
    res = mask_anchor_composite(orig, ai, tmp_path / "m.png", tmp_path / "out.png")
    assert res.outside_exact and res.coverage_pct == 0.0 and res.changed_pct == 0.0
    out = Image.open(tmp_path / "out.png").convert("RGB")
    assert out.tobytes() == Image.open(orig).convert("RGB").tobytes()


def test_all_white_mask_returns_ai(tmp_path: Path):
    orig = _solid(tmp_path / "o.png", (64, 64), (10, 10, 10))
    ai = _solid(tmp_path / "a.png", (64, 64), (240, 200, 100))
    Image.new("L", (64, 64), 255).save(tmp_path / "m.png")
    res = mask_anchor_composite(orig, ai, tmp_path / "m.png", tmp_path / "out.png")
    assert res.coverage_pct == 100.0
    out = Image.open(tmp_path / "out.png").convert("RGB")
    assert out.getpixel((30, 30)) == (240, 200, 100)


def test_resizes_ai_and_mask_to_original_dims(tmp_path: Path):
    orig = _solid(tmp_path / "o.png", (120, 80), (200, 50, 50))
    _solid(tmp_path / "a.png", (60, 40), (50, 50, 200))      # half-size AI
    _circle_mask(tmp_path / "m.png", (60, 40), 10)            # half-size mask
    res = mask_anchor_composite(orig, tmp_path / "a.png", tmp_path / "m.png", tmp_path / "out.png")
    assert res.width == 120 and res.height == 80 and res.outside_exact
    assert Image.open(tmp_path / "out.png").size == (120, 80)


def test_changed_pct_tracks_mask_area(tmp_path: Path):
    orig = _solid(tmp_path / "o.png", (100, 100), (200, 50, 50))
    ai = _solid(tmp_path / "a.png", (100, 100), (50, 50, 200))
    _circle_mask(tmp_path / "m.png", (100, 100), 30)         # area ~2827 / 10000 ~28%
    res = mask_anchor_composite(orig, ai, tmp_path / "m.png", tmp_path / "out.png")
    assert 20.0 < res.changed_pct < 35.0
    assert res.outside_exact


def test_detects_any_channel_change_outside(tmp_path: Path):
    # two colors with equal luminance but different chroma must still count as changed
    orig = _solid(tmp_path / "o.png", (50, 50), (100, 100, 100))
    ai = _solid(tmp_path / "a.png", (50, 50), (120, 90, 95))
    _circle_mask(tmp_path / "m.png", (50, 50), 12)
    res = mask_anchor_composite(orig, ai, tmp_path / "m.png", tmp_path / "out.png")
    assert res.changed_pct > 0.0 and res.outside_exact


def test_returns_composite_result(tmp_path: Path):
    orig = _solid(tmp_path / "o.png", (40, 40), (1, 2, 3))
    ai = _solid(tmp_path / "a.png", (40, 40), (4, 5, 6))
    _circle_mask(tmp_path / "m.png", (40, 40), 10)
    res = mask_anchor_composite(orig, ai, tmp_path / "m.png", tmp_path / "out.png")
    assert isinstance(res, CompositeResult)
    assert res.output_path == tmp_path / "out.png"
