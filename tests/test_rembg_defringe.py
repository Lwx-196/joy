"""验证 _alpha_defringe：matting 反解边缘背景去污染 + alpha erosion（owner 6-11 白边/原色 halo 实证）。

纯函数合成测试（不跑 rembg 模型）：
- 亮背景：边缘 ring 反解后前景色回归真值（白边消）
- 暗背景：字节恒等（per-channel min 保证只除亮污染、永不提亮 → 存量深底板零回归）
- 人物内部与背景像素字节不变（只动边界 ring）
- 无背景可参考 / 无边界可修：原样返回
- alpha erosion：低 alpha 最外圈归零，其余保留
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.scripts.render_ai_enhanced_boards import _alpha_defringe  # noqa: E402

SKIN = np.array([182.0, 142.0, 118.0])
BG_BRIGHT = np.array([224.0, 228.0, 232.0])  # 影棚浅灰白（偏蓝），白边场景
BG_DARK = np.array([18.0, 18.0, 20.0])       # 影棚深底，存量主流场景


def _make_scene(bg_color, h=240, w=240, r_person=70, ramp_px=4):
    """人物=中心圆盘（前景色 SKIN），边缘 ramp_px 线性半透明 ring，背景=bg_color。

    源图像素按真实混合 C = aF + (1-a)B 合成——与相机在人物边缘的混色同模型；
    alpha 取理想 matte（=真值 a），即 rembg 完美抠图时的输入。
    """
    yy, xx = np.mgrid[0:h, 0:w]
    dist = np.sqrt((yy - h / 2) ** 2 + (xx - w / 2) ** 2)
    a_true = np.clip((r_person - dist) / ramp_px + 1.0, 0.0, 1.0)
    arr = a_true[..., None] * SKIN + (1.0 - a_true[..., None]) * bg_color
    return arr.astype(np.float64), a_true.astype(np.float64)


def test_bright_bg_fringe_unmixed():
    """亮背景：边缘 ring（0.25<a<0.9）反解后颜色应回归 SKIN（白边/偏色消除）。"""
    arr, alpha = _make_scene(BG_BRIGHT)
    fixed, _ = _alpha_defringe(arr, alpha)

    band = (alpha > 0.25) & (alpha < 0.9)
    assert band.any()
    err_after = np.abs(fixed[band] - SKIN).max()
    err_before = np.abs(arr[band] - SKIN).max()
    # 修前混了亮背景偏差巨大（>40），修后应基本回归前景真值
    assert err_before > 40, f"场景构造失效 err_before={err_before:.1f}"
    assert err_after < 12, f"反解未回归前景色 err_after={err_after:.1f}"


def test_bright_bg_composite_brightness_drops():
    """亮背景：黑底合成后的边缘 ring 亮度应显著低于修前（halo 实际消失）。"""
    arr, alpha = _make_scene(BG_BRIGHT)
    fixed, alpha_fixed = _alpha_defringe(arr, alpha)

    band = (alpha > 0.15) & (alpha < 0.7)
    naive = (arr * alpha[..., None])[band].max(axis=1)
    cleaned = (fixed * alpha_fixed[..., None])[band].max(axis=1)
    assert float(np.percentile(naive - cleaned, 50)) > 5, "边缘亮度无可见下降"
    assert float((cleaned - naive).max()) <= 1e-6, "出现提亮（违反 per-channel min 保证）"


def test_dark_bg_identity():
    """暗背景：F=(C-(1-a)B)/a ≥ C，per-channel min 取回原值 → 颜色字节恒等。"""
    arr, alpha = _make_scene(BG_DARK)
    fixed, _ = _alpha_defringe(arr, alpha)
    assert np.array_equal(fixed, arr), "深底场景颜色被改动（应恒等）"


def test_interior_and_background_untouched():
    """人物内部（实心区）与背景像素必须字节不变——只动边界 ring。"""
    import cv2

    arr, alpha = _make_scene(BG_BRIGHT)
    fixed, alpha_fixed = _alpha_defringe(arr, alpha, edge_r=5)

    support = (alpha > 0.0).astype(np.uint8)
    interior = cv2.erode(support, np.ones((5, 5), np.uint8)) > 0
    assert np.array_equal(fixed[interior], arr[interior]), "人物内部被改动"
    bg = alpha < 0.02
    assert np.array_equal(fixed[bg], arr[bg]), "背景像素被改动"
    assert np.array_equal(alpha_fixed[interior], alpha[interior]), "内部 alpha 被改动"


def test_no_background_returns_unchanged():
    """全前景（无背景像素可参考）：原样返回。"""
    arr = np.full((60, 60, 3), 150.0)
    alpha = np.ones((60, 60))
    fixed, alpha_fixed = _alpha_defringe(arr, alpha)
    assert np.array_equal(fixed, arr)
    assert np.array_equal(alpha_fixed, alpha)


def test_alpha_erosion_zeroes_low_tail():
    """alpha erosion：< 阈值的最外圈归零，≥ 阈值保留原值。"""
    arr, alpha = _make_scene(BG_BRIGHT)
    _, alpha_fixed = _alpha_defringe(arr, alpha, erode_alpha_below=0.12)

    low = (alpha > 0.0) & (alpha < 0.12)
    high = alpha >= 0.12
    assert low.any()
    assert float(alpha_fixed[low].max()) == 0.0, "低 alpha 尾巴未归零"
    assert np.array_equal(alpha_fixed[high], alpha[high]), "正常 alpha 被误动"


def test_gradient_bg_local_field():
    """背景有打光梯度（左亮右暗，恒亮于前景）：局部背景色场应分别正确反解两侧边缘。"""
    h = w = 240
    grad = np.linspace(245.0, 200.0, w)[None, :, None] * np.ones((h, 1, 3))
    yy, xx = np.mgrid[0:h, 0:w]
    dist = np.sqrt((yy - h / 2) ** 2 + (xx - w / 2) ** 2)
    a_true = np.clip((70 - dist) / 4.0 + 1.0, 0.0, 1.0)
    arr = a_true[..., None] * SKIN + (1.0 - a_true[..., None]) * grad

    fixed, _ = _alpha_defringe(arr.astype(np.float64), a_true.astype(np.float64))
    band = (a_true > 0.3) & (a_true < 0.85)
    err = np.abs(fixed[band] - SKIN)
    assert float(err.max()) < 20, f"梯度背景反解误差过大 max={err.max():.1f}"
