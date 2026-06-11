"""G3 纹类近景对比区单测：触发解析 + bbox 比例扩展 + fail-open + 构建管线（monkeypatch 重依赖）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from backend.services import board_closeup_section as closeup


# ---------- wrinkle_regions 触发解析 ----------


def test_wrinkle_regions_hits_chuanzi():
    assert closeup.wrinkle_regions("保妥适20U川字纹") == ["川字"]


def test_wrinkle_regions_alias_taitou_maps_to_ewen():
    # 抬头纹 是 额纹 的 alias；鱼尾 不在 atlas，诚实不命中
    assert closeup.wrinkle_regions("衡力50U川字鱼尾抬头纹") == ["川字", "额纹"]


def test_wrinkle_regions_faling():
    got = closeup.wrinkle_regions("缇颜3支唇，法令纹，口角、保妥适20U川字纹")
    assert set(got) == {"法令纹", "川字"}


def test_wrinkle_regions_filler_regions_excluded():
    # 泪沟/卧蚕 = 容量填充非纹，刻意不触发
    assert closeup.wrinkle_regions("弗缦1.0注射泪沟") == []
    assert closeup.wrinkle_regions("玻尿酸卧蚕 唇填充") == []


def test_wrinkle_regions_empty_and_none_safe():
    assert closeup.wrinkle_regions("") == []
    assert closeup.wrinkle_regions(None) == []  # type: ignore[arg-type]


def test_wrinkle_regions_order_stable():
    # extract_regions 保持 atlas 定义序（法令纹 在 川字 之前定义）
    a = closeup.wrinkle_regions("川字纹及法令纹")
    b = closeup.wrinkle_regions("法令纹及川字纹")
    assert a == b


def test_section_label_display_names():
    assert closeup.section_label(["川字"]) == "川字纹"
    assert closeup.section_label(["川字", "法令纹"]) == "川字纹、法令纹"


# ---------- expand_to_aspect ----------


def _aspect(box):
    left, top, right, bottom = box
    return (right - left) / (bottom - top)


TARGET = closeup.CELL_ASPECT[0] / closeup.CELL_ASPECT[1]


def test_expand_to_aspect_widens_narrow_bbox():
    box = closeup.expand_to_aspect((900, 400, 1100, 1400), (4000, 5000))
    assert abs(_aspect(box) - TARGET) < 0.01
    # 原 bbox 被包含
    assert box[0] <= 900 and box[2] >= 1100


def test_expand_to_aspect_heightens_flat_bbox():
    box = closeup.expand_to_aspect((500, 1000, 2500, 1400), (4000, 5000))
    assert abs(_aspect(box) - TARGET) < 0.01
    assert box[1] <= 1000 and box[3] >= 1400


def test_expand_to_aspect_clamps_inside_image():
    # bbox 顶在角落，扩展后必须整体平移进界
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


# ---------- build_closeup_assets（重依赖 monkeypatch） ----------


def _patch_pipeline(monkeypatch, tmp_path, bbox=(100, 100, 300, 500)):
    """unsharp_focal_enhance=identity / mask=占位 PNG / bbox=固定值。"""
    import backend.ai_generation_adapter as adapter
    from backend.services import classical_enhance, focal_mask_generator

    monkeypatch.setattr(
        classical_enhance,
        "unsharp_focal_enhance",
        lambda src, **kw: src,
    )

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


def test_build_closeup_assets_happy_path(monkeypatch, tmp_path):
    _patch_pipeline(monkeypatch, tmp_path)
    before = _make_src(tmp_path, "before.jpg")
    after = _make_src(tmp_path, "after.jpg")

    got = closeup.build_closeup_assets(before, after, ["川字"], tmp_path / "work")

    assert got is not None
    assert got["regions"] == ["川字"]
    assert got["label"] == "川字纹"
    for side in ("before_path", "after_path"):
        crop_path = Path(got[side])
        assert crop_path.is_file()
        with Image.open(crop_path) as img:
            assert abs((img.width / img.height) - TARGET) < 0.02


def test_build_closeup_assets_missing_source_fail_open(monkeypatch, tmp_path):
    _patch_pipeline(monkeypatch, tmp_path)
    got = closeup.build_closeup_assets(
        tmp_path / "nope_b.jpg", tmp_path / "nope_a.jpg", ["川字"], tmp_path / "work"
    )
    assert got is None  # 不抛，fail-open


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


def test_build_for_manifest_no_wrinkle_returns_none(tmp_path):
    m = _manifest_with_front(tmp_path / "b.jpg", tmp_path / "a.jpg")
    assert closeup.build_for_manifest(m, "玻尿酸注射下巴", tmp_path) is None


def test_build_for_manifest_no_front_slot_returns_none(tmp_path):
    m = {"groups": [{"name": "g1", "selected_slots": {"side": {}}}]}
    assert closeup.build_for_manifest(m, "保妥适20U川字纹", tmp_path) is None


def test_build_for_manifest_happy_path(monkeypatch, tmp_path):
    _patch_pipeline(monkeypatch, tmp_path)
    before = _make_src(tmp_path, "b.jpg")
    after = _make_src(tmp_path, "a.jpg")
    m = _manifest_with_front(before, after)

    got = closeup.build_for_manifest(m, "保妥适20U川字纹", tmp_path / "work")

    assert got is not None and got["regions"] == ["川字"]


def test_build_for_manifest_empty_groups_fail_open(tmp_path):
    assert closeup.build_for_manifest({"groups": []}, "川字纹", tmp_path) is None
    assert closeup.build_for_manifest({}, "川字纹", tmp_path) is None
