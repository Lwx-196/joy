"""自适应 4K 尺寸 + size_override 单测（纯函数，无网络）。

背景：gpt-image-2 原生支持非方形 4K（2026-04 规格，联网核实）——最长边 ≤3840、
宽高均 16 的倍数、宽高比 ≤3:1、总像素 655,360–8,294,400。旧管线 _pad_to_square 固定
方形是 workaround；自适应路径按源图比例取贴 4K 预算的合法尺寸，横版不再被竖版 size 拉伸。
"""
from __future__ import annotations

import pytest

from PIL import Image

from backend.scripts.render_ai_enhanced_boards import _adaptive_target_size, _restore_to_original
from backend.services import image_providers as ip

MAX_EDGE = 3840
MAX_PX = 8_294_400
MIN_PX = 655_360


def _parse(s: str) -> tuple[int, int]:
    w, h = s.split("x")
    return int(w), int(h)


def _assert_valid(s: str):
    w, h = _parse(s)
    assert w % 16 == 0 and h % 16 == 0, f"{s} 非 16 倍数"
    assert max(w, h) <= MAX_EDGE, f"{s} 最长边超 3840"
    assert w * h <= MAX_PX, f"{s} 像素 {w*h} 超预算"
    assert w * h >= MIN_PX, f"{s} 像素 {w*h} 低于下限"
    ar = max(w, h) / min(w, h)
    assert ar <= 3.0 + 1e-6, f"{s} 宽高比超 3:1"


@pytest.mark.parametrize("w,h", [
    (3024, 4032),   # 3:4 竖版人像（典型 iPhone 源）
    (4032, 3024),   # 4:3 横版
    (1000, 1000),   # 方形
    (768, 1376),    # 小竖版（9:16-ish）
    (2160, 3840),   # 已是 4K 竖版
    (5712, 4284),   # 大横版 4:3
    (1280, 853),    # 小横版 3:2
])
def test_target_size_always_valid(w, h):
    s = _adaptive_target_size(w, h)
    _assert_valid(s)


def test_portrait_stays_portrait():
    tw, th = _parse(_adaptive_target_size(3024, 4032))
    assert th > tw, "竖版源应得竖版 size"


def test_landscape_stays_landscape():
    tw, th = _parse(_adaptive_target_size(4032, 3024))
    assert tw > th, "横版源应得横版 size"


def test_aspect_preserved_within_2pct():
    """目标比例与源比例偏差 <2%（16 取整 + 预算收缩的容差内）。"""
    for w, h in [(3024, 4032), (4032, 3024), (5712, 4284)]:
        tw, th = _parse(_adaptive_target_size(w, h))
        assert abs((tw / th) - (w / h)) / (w / h) < 0.02


def test_square_maxes_budget():
    """方形源应取 2880²（=8.29MP 贴满 4K 预算）。"""
    assert _adaptive_target_size(1500, 1500) == "2880x2880"


def test_extreme_aspect_clamped_to_3to1():
    s = _adaptive_target_size(6000, 1000)  # 6:1 超限
    _assert_valid(s)  # 内部含 ≤3:1 断言


def test_degenerate_input_falls_back():
    _assert_valid(_adaptive_target_size(0, 0))
    _assert_valid(_adaptive_target_size(-5, 100))


# ---- size_override 透传（_size_rungs 纯函数）----

def _prov(sizes=(), quality=""):
    return ip.ImageProvider(name="t", base_url="https://x/v1", api_key="k",
                            model="gpt-image-2", sizes=sizes, quality=quality)


def test_size_rungs_default_unchanged():
    """无 override：行为与历史一致（provider.sizes + 末档空 dict）。"""
    rungs = ip._size_rungs(_prov(sizes=("2560x2560",), quality="high"))
    assert rungs == [{"size": "2560x2560", "quality": "high"}, {}]


def test_size_rungs_override_replaces_provider_sizes():
    """有 override：忽略 provider.sizes，用 override（带 quality）+ 末档空 dict。"""
    rungs = ip._size_rungs(_prov(sizes=("2560x2560",), quality="high"),
                           size_override="2496x3312")
    assert rungs == [{"size": "2496x3312", "quality": "high"}, {}]


def test_size_rungs_override_no_quality():
    rungs = ip._size_rungs(_prov(sizes=(), quality=""), size_override="2880x2880")
    assert rungs == [{"size": "2880x2880"}, {}]


# ---- _restore_to_original：cover 裁回原尺寸，绝不拉伸 ----

def test_restore_identity_when_same_size():
    im = Image.new("RGB", (800, 1000), (10, 20, 30))
    out = _restore_to_original(im, (800, 1000))
    assert out.size == (800, 1000)


def test_restore_always_returns_original_size():
    """模型返回比例/尺寸略有出入，复原后必须严格=源尺寸（下游 manifest 依赖）。"""
    orig = (3024, 4032)
    for enh_size in [(2496, 3312), (2160, 3840), (2500, 3300), (3024, 4032), (1000, 1500)]:
        out = _restore_to_original(Image.new("RGB", enh_size), orig)
        assert out.size == orig, f"{enh_size} 复原后 {out.size} != {orig}"


def test_restore_no_horizontal_stretch():
    """竖条图案经 cover 裁回不应横向变宽（拉伸 bug 的回归锁）。"""
    enh = Image.new("RGB", (2496, 3312), (0, 0, 0))
    sw = round(2496 * 0.25)
    enh.paste(Image.new("RGB", (sw, 3312), (255, 255, 255)), ((2496 - sw) // 2, 0))
    out = _restore_to_original(enh, (3024, 4032))
    # 中间行白条占比应≈0.25（cover 裁是等比，不拉伸）
    import numpy as np
    row = np.array(out)[out.height // 2, :, 0]
    white_frac = (row > 200).sum() / out.width
    assert 0.18 < white_frac < 0.32, f"白条占比 {white_frac:.3f} 偏离 0.25=被拉伸"
