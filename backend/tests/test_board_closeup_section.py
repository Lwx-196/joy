"""G3 近景对比区单测：覆盖率 gate + 触发解析 + per-region 布局 + fail-open + 构建管线。

2026-06-14 全部位延展 + 覆盖率 gate（owner 拍板）后的契约：
- closeup_plan_regions = 触发判定（单部位/同区紧凑出；多部位分散跳；鼻类正面不做）
- cell_aspect_for / center_shift_for 走 REGION_CLOSEUP_LAYOUT 表
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from backend.services import board_closeup_section as closeup


# ---------- closeup_plan_regions：覆盖率 gate + 触发 ----------


def test_plan_chuanzi_single_compact():
    # 单纹类 → 紧凑 → 出
    assert closeup.closeup_plan_regions("保妥适20U川字纹") == ["川字"]


def test_plan_leigou_single_now_triggers():
    # 泪沟 单做（容量类，旧逻辑刻意排除）→ 现在单部位紧凑 → 出
    assert closeup.closeup_plan_regions("弗缦1.0注射泪沟") == ["泪沟"]


def test_plan_xiaba_single_now_triggers():
    # 下巴 单做（旧逻辑非纹类不触发）→ 现在出
    assert closeup.closeup_plan_regions("玻尿酸注射下巴") == ["下巴"]


def test_plan_colocated_undereye_both():
    # 泪沟+卧蚕 都在眼下 → union 仍紧凑 → 两者都进
    got = closeup.closeup_plan_regions("弗缦注射泪沟、玻尿酸卧蚕")
    assert set(got) == {"泪沟", "卧蚕"}


def test_plan_scattered_multi_region_skips():
    # 泪沟(眼下) + 下巴(下脸) 纵向铺开 → 多部位 → 跳过（正面已覆盖）
    assert closeup.closeup_plan_regions("弗缦注射泪沟、玻尿酸注射下巴") == []


def test_plan_nose_only_skipped_front():
    # 纯鼻类：鼻背可定位（紧凑）但不入正面合格集 → 不出
    assert closeup.closeup_plan_regions("海魅骨性1支注射鼻子") == []


def test_plan_nose_plus_chin_scattered_skips():
    # 鼻+下巴（许楚楚式多部位分散）→ union 铺满 → 跳过
    assert closeup.closeup_plan_regions("越致1支注射鼻子下巴") == []


def test_plan_empty_and_none_safe():
    assert closeup.closeup_plan_regions("") == []
    assert closeup.closeup_plan_regions(None) == []  # type: ignore[arg-type]


def test_plan_no_locatable_region_returns_empty():
    # 无任何可定位治疗区（纯非局部词）→ []
    assert closeup.closeup_plan_regions("水光针全脸补水") == []


def test_plan_pingguoji_single_triggers():
    assert closeup.closeup_plan_regions("盈致1支注射苹果肌") == ["苹果肌"]


def test_section_label_display_names():
    assert closeup.section_label(["川字"]) == "川字纹"
    assert closeup.section_label(["泪沟", "卧蚕"]) == "泪沟、卧蚕"


# ---------- cell_aspect_for / center_shift_for（REGION_CLOSEUP_LAYOUT） ----------


def test_cell_aspect_wide_for_faling_and_ewen():
    assert closeup.cell_aspect_for(["法令纹"]) == closeup.CELL_ASPECT_WIDE
    assert closeup.cell_aspect_for(["额纹"]) == closeup.CELL_ASPECT_WIDE


def test_cell_aspect_narrow_for_chuanzi():
    # owner 06-14 拍板：川字 = 800×420 窄带
    assert closeup.cell_aspect_for(["川字"]) == closeup.CELL_ASPECT_NARROW


def test_cell_aspect_undereye_for_leigou_woocan():
    assert closeup.cell_aspect_for(["泪沟"]) == closeup.CELL_ASPECT_UNDEREYE
    assert closeup.cell_aspect_for(["卧蚕"]) == closeup.CELL_ASPECT_UNDEREYE


def test_cell_aspect_midface_for_pingguoji():
    assert closeup.cell_aspect_for(["苹果肌"]) == closeup.CELL_ASPECT_MIDFACE


def test_cell_aspect_lower_for_xiaba():
    assert closeup.cell_aspect_for(["下巴"]) == closeup.CELL_ASPECT_LOWER


def test_cell_aspect_mixed_priority_widest_band():
    # 跨组取最宽带（法令纹优先），保证 expand_to_aspect 恒含 union bbox
    assert closeup.cell_aspect_for(["川字", "法令纹"]) == closeup.CELL_ASPECT_WIDE


def test_cell_aspect_empty_defaults_vertical():
    assert closeup.cell_aspect_for([]) == closeup.CELL_ASPECT


def test_center_shift_chuanzi_down():
    assert closeup.center_shift_for(["川字"]) == closeup.NARROW_CENTER_SHIFT_FRAC


def test_center_shift_xiaba_up():
    # 下巴 mask 偏低延入颈 → 负 shift（上移对中下巴）
    assert closeup.center_shift_for(["下巴"]) < 0


def test_center_shift_undereye_headroom():
    # 眼下带轻微上移留头顶白（owner 06-14 居中）
    assert closeup.center_shift_for(["泪沟"]) < 0


def test_center_shift_midface_more_headroom():
    # 苹果肌/面颊 头顶留白更多（更高/更低带，陈艺琼 居中）
    assert closeup.center_shift_for(["苹果肌"]) <= -0.08


def test_pingguoji_dominates_undereye_for_aspect_and_shift():
    # 泪沟+苹果肌 同区 → 中脸带主导（容得下 + 居中）
    assert closeup.cell_aspect_for(["泪沟", "苹果肌"]) == closeup.CELL_ASPECT_MIDFACE
    assert closeup.center_shift_for(["泪沟", "苹果肌"]) <= -0.08


# ---------- expand_to_aspect ----------


def _aspect(box):
    left, top, right, bottom = box
    return (right - left) / (bottom - top)


TARGET = closeup.CELL_ASPECT[0] / closeup.CELL_ASPECT[1]


def test_expand_to_aspect_widens_narrow_bbox():
    box = closeup.expand_to_aspect((900, 400, 1100, 1400), (4000, 5000))
    assert abs(_aspect(box) - TARGET) < 0.01
    assert box[0] <= 900 and box[2] >= 1100


def test_expand_to_aspect_heightens_flat_bbox():
    box = closeup.expand_to_aspect((500, 1000, 2500, 1400), (4000, 5000))
    assert abs(_aspect(box) - TARGET) < 0.01
    assert box[1] <= 1000 and box[3] >= 1400


def test_expand_to_aspect_clamps_inside_image():
    box = closeup.expand_to_aspect((0, 0, 100, 1000), (1200, 1500))
    left, top, right, bottom = box
    assert left >= 0 and top >= 0 and right <= 1200 and bottom <= 1500
    assert abs(_aspect(box) - TARGET) < 0.01


def test_expand_to_aspect_bbox_larger_than_image_shrinks():
    box = closeup.expand_to_aspect((0, 0, 5000, 5000), (1000, 800))
    left, top, right, bottom = box
    assert left >= 0 and top >= 0 and right <= 1000 and bottom <= 800
    assert abs(_aspect(box) - TARGET) < 0.02


def test_expand_to_aspect_degenerate_raises():
    with pytest.raises(ValueError):
        closeup.expand_to_aspect((100, 100, 100, 200), (1000, 1000))


def test_expand_to_aspect_wide_keeps_band_local():
    wide = closeup.CELL_ASPECT_WIDE
    box = closeup.expand_to_aspect((984, 1024, 2952, 1600), (3936, 2624), aspect=wide)
    left, top, right, bottom = box
    assert abs(_aspect(box) - wide[0] / wide[1]) < 0.01
    assert (bottom - top) < 2624 * 0.6  # 不再渲成全脸高


def test_expand_to_aspect_negative_shift_moves_up():
    # center_shift_frac < 0 把裁剪中心上移（下巴对中）
    base = closeup.expand_to_aspect((1000, 3000, 1400, 3400), (4000, 5000))
    up = closeup.expand_to_aspect(
        (1000, 3000, 1400, 3400), (4000, 5000), center_shift_frac=-0.1
    )
    assert up[1] < base[1]  # 顶边更高


# ---------- build_closeup_assets（重依赖 monkeypatch） ----------


def _patch_pipeline(monkeypatch, tmp_path, bbox=(100, 100, 300, 500)):
    """unsharp_focal_enhance=identity / mask=占位 PNG / bbox=固定值。"""
    import backend.ai_generation_adapter as adapter
    from backend.services import classical_enhance, focal_mask_generator

    monkeypatch.setattr(classical_enhance, "unsharp_focal_enhance", lambda src, **kw: src)

    def _fake_mask(image_path, focus_targets, *, output_path=None, **kw):
        mask = Image.new("L", (10, 10), 255)
        mask.save(output_path)
        return output_path

    monkeypatch.setattr(focal_mask_generator, "generate_focus_mask", _fake_mask)
    monkeypatch.setattr(adapter, "_focal_crop_bbox", lambda mask_path, pad_frac=0.15: bbox)


def _make_src(tmp_path, name, size=(1200, 1600)):
    p = tmp_path / name
    Image.new("RGB", size, (180, 150, 130)).save(p)
    return p


def test_build_closeup_assets_chuanzi_narrow(monkeypatch, tmp_path):
    _patch_pipeline(monkeypatch, tmp_path)
    before = _make_src(tmp_path, "before.jpg")
    after = _make_src(tmp_path, "after.jpg")

    got = closeup.build_closeup_assets(before, after, ["川字"], tmp_path / "work")

    assert got is not None
    assert got["regions"] == ["川字"]
    assert got["label"] == "川字纹"
    assert got["cell_aspect"] == list(closeup.CELL_ASPECT_NARROW)  # 川字 = 800×420
    narrow_target = closeup.CELL_ASPECT_NARROW[0] / closeup.CELL_ASPECT_NARROW[1]
    for side in ("before_path", "after_path"):
        crop_path = Path(got[side])
        assert crop_path.is_file()
        with Image.open(crop_path) as img:
            assert abs((img.width / img.height) - narrow_target) < 0.02


def test_build_closeup_assets_wide_region_horizontal_crop(monkeypatch, tmp_path):
    _patch_pipeline(monkeypatch, tmp_path, bbox=(100, 600, 1100, 900))
    before = _make_src(tmp_path, "before.jpg")
    after = _make_src(tmp_path, "after.jpg")

    got = closeup.build_closeup_assets(before, after, ["法令纹"], tmp_path / "work")

    assert got is not None
    assert got["cell_aspect"] == list(closeup.CELL_ASPECT_WIDE)
    wide_target = closeup.CELL_ASPECT_WIDE[0] / closeup.CELL_ASPECT_WIDE[1]
    for side in ("before_path", "after_path"):
        with Image.open(got[side]) as img:
            assert abs((img.width / img.height) - wide_target) < 0.02


def test_build_closeup_assets_undereye_region(monkeypatch, tmp_path):
    # 新增部位：泪沟 → 眼下带 aspect
    _patch_pipeline(monkeypatch, tmp_path, bbox=(100, 400, 1100, 700))
    before = _make_src(tmp_path, "before.jpg")
    after = _make_src(tmp_path, "after.jpg")

    got = closeup.build_closeup_assets(before, after, ["泪沟"], tmp_path / "work")

    assert got is not None
    assert got["cell_aspect"] == list(closeup.CELL_ASPECT_UNDEREYE)


def test_build_closeup_assets_missing_source_fail_open(monkeypatch, tmp_path):
    _patch_pipeline(monkeypatch, tmp_path)
    got = closeup.build_closeup_assets(
        tmp_path / "nope_b.jpg", tmp_path / "nope_a.jpg", ["川字"], tmp_path / "work"
    )
    assert got is None


def test_build_closeup_assets_bbox_failure_fail_open(monkeypatch, tmp_path):
    import backend.ai_generation_adapter as adapter

    _patch_pipeline(monkeypatch, tmp_path)

    def _boom(mask_path, pad_frac=0.15):
        raise RuntimeError("mask 全黑")

    monkeypatch.setattr(adapter, "_focal_crop_bbox", _boom)
    before = _make_src(tmp_path, "b.jpg")
    after = _make_src(tmp_path, "a.jpg")
    assert closeup.build_closeup_assets(before, after, ["额纹"], tmp_path / "w") is None


# ---------- build_for_manifest ----------


def _manifest_with_front(before, after):
    return {
        "groups": [
            {
                "name": "g1",
                "selected_slots": {
                    "front": {"before": {"path": str(before)}, "after": {"path": str(after)}}
                },
            }
        ]
    }


def test_build_for_manifest_scattered_returns_none(tmp_path):
    # 多部位分散 → gate 跳过 → None
    m = _manifest_with_front(tmp_path / "b.jpg", tmp_path / "a.jpg")
    assert closeup.build_for_manifest(m, "弗缦注射泪沟、玻尿酸注射下巴", tmp_path) is None


def test_build_for_manifest_no_front_slot_returns_none(tmp_path):
    m = {"groups": [{"name": "g1", "selected_slots": {"side": {}}}]}
    assert closeup.build_for_manifest(m, "保妥适20U川字纹", tmp_path) is None


def test_build_for_manifest_single_region_happy_path(monkeypatch, tmp_path):
    _patch_pipeline(monkeypatch, tmp_path)
    before = _make_src(tmp_path, "b.jpg")
    after = _make_src(tmp_path, "a.jpg")
    m = _manifest_with_front(before, after)

    # 下巴 单做现在出近景（旧逻辑非纹类返回 None）
    got = closeup.build_for_manifest(m, "玻尿酸注射下巴", tmp_path / "work")

    assert got is not None and got["regions"] == ["下巴"]


def test_build_for_manifest_empty_groups_fail_open(tmp_path):
    assert closeup.build_for_manifest({"groups": []}, "川字纹", tmp_path) is None
    assert closeup.build_for_manifest({}, "川字纹", tmp_path) is None
