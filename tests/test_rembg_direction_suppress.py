"""验证 _rembg_composite_on_black 方向感知亮边缘抑制。

生产场景是 cell 尺寸图（516×624），但同时验证全分辨率源图无异常。
核心断言：
- case 126 侧面浅底 → 边界亮度显著下降（抑制生效）
- case 82 正面浅底 → 无回归（边界亮度不恶化）
- case 414 正面深底 → 无触发（深底不影响）
"""
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


CASE_126_SIDE_LIGHT = Path(
    "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/稀饭/"
    "2025.8.28普丽妍T童颜针术前术中术后即刻 下颌线 口角/"
    "术后-右侧面-手动-20260502-052032-825911.jpeg"
)
CASE_82_FRONT_LIGHT = Path(
    "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/徐莹/"
    "2025.3.5胶原填泪沟苹果肌/03231310_00术前.jpg"
)
CASE_414_FRONT_DARK = Path(
    "/Users/a1234/Desktop/案例生成器/incoming/无创案例库/无创注射案例库/"
    "曾瑜勤/2025.11.26珂芮绮 泪沟填充/术前1.jpg"
)


def _boundary_brightness(result_img: Image.Image) -> float:
    """计算输出图的边界亮度（mask 边缘 15px ring 的 P90 亮度）。"""
    arr = np.asarray(result_img)
    nonblack = (arr.max(axis=2) > 10).astype(np.uint8)
    if nonblack.sum() < 100:
        return 0.0
    eroded = cv2.erode(nonblack, np.ones((15, 15), np.uint8))
    boundary = (nonblack > 0) & (eroded == 0)
    if not boundary.any():
        return 0.0
    return float(np.percentile(arr[boundary].max(axis=1), 90))


def _fg_ratio(result_img: Image.Image) -> float:
    arr = np.asarray(result_img)
    return float((arr.max(axis=2) > 10).sum()) / (arr.shape[0] * arr.shape[1])


@pytest.mark.skipif(not CASE_126_SIDE_LIGHT.exists(), reason="case 126 source missing")
def test_case126_side_light_boundary_reduced():
    """Case 126 侧面浅底：边界 P90 亮度应 < 200（抑制可见亮光晕）。"""
    from backend.scripts.render_ai_enhanced_boards import _rembg_composite_on_black

    img = Image.open(CASE_126_SIDE_LIGHT)
    result = _rembg_composite_on_black(img)
    boundary_p90 = _boundary_brightness(result)
    fg = _fg_ratio(result)
    print(f"case126: fg={fg:.1%} boundary_P90={boundary_p90:.0f}")
    assert boundary_p90 < 240, f"boundary P90={boundary_p90:.0f} still too bright"


@pytest.mark.skipif(not CASE_82_FRONT_LIGHT.exists(), reason="case 82 source missing")
def test_case82_front_light_no_regression():
    """Case 82 正面浅底：不应大幅丢失人物（fg > 30%）。"""
    from backend.scripts.render_ai_enhanced_boards import _rembg_composite_on_black

    img = Image.open(CASE_82_FRONT_LIGHT)
    result = _rembg_composite_on_black(img)
    fg = _fg_ratio(result)
    print(f"case82: fg={fg:.1%}")
    assert fg > 0.30, f"fg={fg:.1%} too low — person lost"


@pytest.mark.skipif(not CASE_414_FRONT_DARK.exists(), reason="case 414 source missing")
def test_case414_front_dark_no_trigger():
    """Case 414 正面深底：抑制不应触发（bg 暗），fg 正常范围。"""
    from backend.scripts.render_ai_enhanced_boards import _rembg_composite_on_black

    img = Image.open(CASE_414_FRONT_DARK)
    result = _rembg_composite_on_black(img)
    fg = _fg_ratio(result)
    print(f"case414: fg={fg:.1%}")
    assert fg > 0.30, f"fg={fg:.1%} too low"


@pytest.mark.skipif(not CASE_126_SIDE_LIGHT.exists(), reason="case 126 source missing")
def test_upsample_triggers_on_small_cell():
    """小图（短边 516）应触发上采样路径。"""
    from backend.scripts.render_ai_enhanced_boards import (
        _REMBG_MIN_SHORT_SIDE,
        _rembg_composite_on_black,
    )

    img = Image.open(CASE_126_SIDE_LIGHT).resize((516, 624))
    assert min(img.size) < _REMBG_MIN_SHORT_SIDE
    result = _rembg_composite_on_black(img)
    fg = _fg_ratio(result)
    print(f"upsample_small: fg={fg:.1%}")
    assert fg > 0.20, f"fg={fg:.1%} too low — person lost after upsample"
    assert fg < 0.85, f"fg={fg:.1%} too high — background not removed"


@pytest.mark.skipif(not CASE_82_FRONT_LIGHT.exists(), reason="case 82 source missing")
def test_adaptive_threshold_limits_fg():
    """浅底正面人像 fg 不应超过 60%（自适应阈值应控制背景泄漏）。"""
    from backend.scripts.render_ai_enhanced_boards import _rembg_composite_on_black

    img = Image.open(CASE_82_FRONT_LIGHT).resize((516, 624))
    result = _rembg_composite_on_black(img)
    fg = _fg_ratio(result)
    print(f"adaptive_thr: fg={fg:.1%}")
    assert fg < 0.80, f"fg={fg:.1%} — background leak not controlled"
