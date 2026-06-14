"""验证 _blue_spill_suppress：边缘带蓝铺巾 spill 中和（owner 选 A，2026-06-14）。

纯函数合成测试（不跑 rembg 模型）：
- 中性发缘被蓝染（B≫R,G）：边缘带蓝被压回本地前景蓝量（teal 撕裂带消）
- 中性深底（边缘无蓝 spill）：字节恒等（深底零回归铁律）
- navy 整片前景（interior 本就蓝）：fg_spill 高 → 边缘不动（保护衣服 / 合法蓝边）
- 永不提亮：R/G 通道不变、B 只减不增（逐通道 out ≤ in）
- 无前景 / 无边界：原样返回
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.scripts.render_ai_enhanced_boards import _blue_spill_suppress  # noqa: E402

HAIR = np.array([40.0, 45.0, 42.0])      # 中性暗发（B≈R≈G）
HAIR_BLUE = np.array([40.0, 55.0, 92.0])  # 被蓝铺巾染色的发缘（B 远高）
NAVY = np.array([38.0, 40.0, 92.0])       # navy 上衣（整片本就蓝）


def _disk(h=300, w=300, r=110):
    """中心圆盘 alpha=1（外 0），不带 ramp（blue-spill 不依赖 alpha 梯度）。"""
    yy, xx = np.mgrid[0:h, 0:w]
    dist = np.sqrt((yy - h / 2) ** 2 + (xx - w / 2) ** 2)
    return dist, (dist <= r).astype(np.float64)


def _ring_scene(interior_color, band_color, r=110, band_w=10):
    """interior=interior_color，最外 band_w px=band_color。

    band_w 取窄（默认 10px）以确保蓝带 ⊂ 函数实际 band（cv2.erode 24-kernel 缩 ~12px），
    且 interior 参照保持纯净（不被蓝带污染）——否则 fg_field 含蓝 fg_spill 高、抑制退让。
    """
    dist, alpha = _disk(r=r)
    arr = np.zeros((*alpha.shape, 3), dtype=np.float64)
    fg = alpha > 0
    arr[fg] = interior_color
    band = fg & (dist > (r - band_w))
    arr[band] = band_color
    return arr, alpha, band


def test_blue_fringe_suppressed():
    """中性发缘被蓝染：被压像素 B 显著下降、残留蓝超量落到 tol 量级。

    断言对「函数实际改动的像素」（几何无关，避开 cv2.erode kernel 半径换算）。
    """
    arr, alpha, _ = _ring_scene(HAIR, HAIR_BLUE)
    out = _blue_spill_suppress(arr, alpha, spill_r=24, tol=8.0)
    changed = out[..., 2] < arr[..., 2] - 0.5
    assert changed.sum() > 200, f"被压像素过少: {int(changed.sum())}"
    db = (arr[..., 2][changed] - out[..., 2][changed])
    assert db.mean() > 20, f"边缘蓝下降不足: meanΔB={db.mean():.1f}"
    # 本地中性前景 fg_spill≈0 → 压后残留蓝超量应落到 tol 量级
    resid = out[..., 2][changed] - np.maximum(out[..., 0][changed], out[..., 1][changed])
    assert resid.mean() < 12, f"边缘残留蓝超量过高: {resid.mean():.1f}"


def test_neutral_deep_bg_byte_identical():
    """中性前景 + 中性边缘（无蓝 spill）：字节恒等（深底零回归）。"""
    arr, alpha, _ = _ring_scene(HAIR, HAIR)
    out = _blue_spill_suppress(arr, alpha, spill_r=24, tol=8.0)
    assert np.array_equal(out, arr), "中性场景被改动（深底回归）"


def test_navy_clothing_protected():
    """navy 整片前景（interior 本就蓝）：fg_spill 高 → 边缘蓝几乎不动（保护衣服）。"""
    arr, alpha, band = _ring_scene(NAVY, NAVY)
    out = _blue_spill_suppress(arr, alpha, spill_r=24, tol=8.0)
    db = (arr[band][:, 2] - out[band][:, 2])
    assert db.max() < 6, f"navy 边缘蓝被误压 maxΔB={db.max():.0f}（应保护）"


def test_never_brightens_only_blue():
    """永不提亮：R/G 不变、B 只减不增（逐通道 out ≤ in）。"""
    arr, alpha, _ = _ring_scene(HAIR, HAIR_BLUE)
    out = _blue_spill_suppress(arr, alpha, spill_r=24, tol=8.0)
    assert np.array_equal(out[..., 0], arr[..., 0]), "R 通道被改动"
    assert np.array_equal(out[..., 1], arr[..., 1]), "G 通道被改动"
    assert (out[..., 2] <= arr[..., 2] + 1e-9).all(), "B 通道出现提亮"


def test_empty_foreground_returns_input():
    """全背景（alpha=0）：原样返回。"""
    alpha = np.zeros((120, 120), dtype=np.float64)
    arr = np.full((120, 120, 3), 50.0)
    out = _blue_spill_suppress(arr, alpha, spill_r=24, tol=8.0)
    assert out is arr or np.array_equal(out, arr)


def test_no_interior_returns_input():
    """前景太小无法 erode 出 interior：原样返回（不崩）。"""
    dist, alpha = _disk(h=80, w=80, r=8)
    arr = np.full((80, 80, 3), 50.0)
    arr[alpha > 0] = HAIR_BLUE
    out = _blue_spill_suppress(arr, alpha, spill_r=24, tol=8.0)
    assert out is arr or np.array_equal(out, arr)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
