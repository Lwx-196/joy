"""底部断图对齐单测：源图 bottom edge 高于 cell 下边线时整体下移贴框。

背景（2026-06-10 验证集）：许12.23 术后双槽 / 胡志超·许4.15 术前，cell 底部
valid_mask gap 被背景填充，rembg 黑底化后呈现人物截断边浮空 + 黑带。
规范：人像下移、截断边与下框线齐平；只 shift 有 gap 的一侧。
"""
import numpy as np

from scripts.render_brand_clean import (
    bottom_valid_gap_px,
    compute_protected_transform,
    shift_cell_bottom_edge_to_frame,
)


def _cell_with_bottom_gap(h=100, w=80, gap=12):
    cell = np.full((h, w, 3), 200, dtype=np.uint8)
    mask = np.zeros((h, w), dtype=bool)
    mask[: h - gap, :] = True
    cell[h - gap:, :, :] = 240  # 底部 padding 的背景填充色
    return cell, mask


def test_gap_measured_from_last_valid_row():
    _, mask = _cell_with_bottom_gap(gap=12)
    assert bottom_valid_gap_px(mask) == 12


def test_no_gap_is_noop():
    cell = np.full((50, 40, 3), 180, dtype=np.uint8)
    mask = np.ones((50, 40), dtype=bool)
    out_cell, out_mask, shift = shift_cell_bottom_edge_to_frame(cell, mask)
    assert shift == 0
    assert out_cell is cell
    assert out_mask is mask


def test_empty_mask_is_noop():
    cell = np.full((50, 40, 3), 180, dtype=np.uint8)
    mask = np.zeros((50, 40), dtype=bool)
    _, _, shift = shift_cell_bottom_edge_to_frame(cell, mask)
    assert shift == 0


def test_shift_brings_truncation_edge_to_frame_bottom():
    cell, mask = _cell_with_bottom_gap(h=100, gap=12)
    cell[87, :, :] = 30  # valid 区最后一行 = 人物截断边
    out_cell, out_mask, shift = shift_cell_bottom_edge_to_frame(cell, mask)
    assert shift == 12
    # 截断边行下移到 cell 最后一行，与下框线齐平
    assert (out_cell[99, :, :] == 30).all()
    # mask 底部贴框；顶部空出的 gap 行标 invalid 交背景策略填充
    assert out_mask[99, :].all()
    assert not out_mask[:12, :].any()
    assert bottom_valid_gap_px(out_mask) == 0


def test_diagonal_truncation_uses_lowest_valid_row():
    # 轻微旋转 warp 后源图底边是斜线：以最低 valid 像素所在行计 gap
    h, w = 60, 60
    mask = np.zeros((h, w), dtype=bool)
    mask[:40, :] = True
    mask[40:50, : w // 2] = True  # 左半比右半多 10 行 valid
    cell = np.full((h, w, 3), 200, dtype=np.uint8)
    _, out_mask, shift = shift_cell_bottom_edge_to_frame(cell, mask)
    assert shift == 10
    assert out_mask[59, : w // 2].all()
    assert not out_mask[59, w // 2:].any()


# ---- 保护区主路径（should_use_protected_alignment 恒 True，实际渲染全走
# render_protected_pair → compute_protected_transform）。6ed6e6b 只修了不可达的
# render_prepared_cell fallback，本组测试钉死主路径的贴框行为。----


def test_protected_transform_shifts_bottom_edge_to_frame():
    # landscape 源图缩放后矮于 portrait cell：offset 下移使源图底边贴下框线
    size = (400, 600)
    image_shape = (300, 800)  # src_h, src_w → scaled_h=300 < target_h=600
    box = (350.0, 100.0, 450.0, 200.0)
    t = compute_protected_transform(image_shape, box, size, "front", 1.0)
    _, y = t["offset"]
    assert y + 300 == 600
    assert t["bottom_edge_shift_px"] > 0
    assert t["clipped_px"]["bottom"] == 0
    # protection_cell_box 记录的是 shift 后的真实落位
    assert t["protection_cell_box"][3] == box[3] + y


def test_protected_transform_no_shift_when_image_covers_bottom():
    # 源图覆盖 cell 底边（无 gap）：不动
    size = (400, 600)
    image_shape = (900, 700)
    box = (250.0, 300.0, 450.0, 500.0)
    t = compute_protected_transform(image_shape, box, size, "front", 1.0)
    _, y = t["offset"]
    assert t["bottom_edge_shift_px"] == 0
    assert y + 900 >= 600
