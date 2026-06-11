"""prepare_closeup_group 单测：G3 近景 cell 尺寸由 section["cell_aspect"] 决定。

纪律（owner 拍板 2026-06-11 横版近景 cell）：
- cell_aspect 合法 → cell 渲染尺寸 = (scale(w), scale(h))（宽带纹类横版 800x516）
- cell_aspect 缺失/非法/无 scale_fn → fail-open 回退角度行尺寸 size（旧 manifest 字节不变）
- closeup_section 缺失/损坏 → None，板照常出
"""
from PIL import Image

from scripts.render_brand_clean import prepare_closeup_group

ANGLE_SIZE = (516, 624)


def _make_section(tmp_path, **extra):
    paths = {}
    for side in ("before", "after"):
        p = tmp_path / f"{side}_closeup.png"
        Image.new("RGB", (1600, 1032), (180, 150, 130)).save(p)
        paths[f"{side}_path"] = str(p)
    return {"regions": ["法令纹"], "label": "法令纹", **paths, **extra}


def test_cell_aspect_horizontal_scales(tmp_path):
    section = _make_section(tmp_path, cell_aspect=[800, 516])
    group = prepare_closeup_group({"closeup_section": section}, ANGLE_SIZE, lambda v: int(v * 1.0))
    assert group is not None
    slot = group["slots"][0]
    assert slot["before"].size == (800, 516)
    assert slot["after"].size == (800, 516)


def test_cell_aspect_scaled_by_scale_fn(tmp_path):
    section = _make_section(tmp_path, cell_aspect=[800, 516])
    group = prepare_closeup_group({"closeup_section": section}, ANGLE_SIZE, lambda v: int(v * 2))
    assert group["slots"][0]["before"].size == (1600, 1032)


def test_missing_cell_aspect_falls_back_to_angle_size(tmp_path):
    # 旧 manifest（无 cell_aspect 字段）回退角度行尺寸 —— 字节不变保证
    section = _make_section(tmp_path)
    group = prepare_closeup_group({"closeup_section": section}, ANGLE_SIZE, lambda v: int(v * 1.0))
    assert group["slots"][0]["before"].size == ANGLE_SIZE


def test_invalid_cell_aspect_falls_back(tmp_path):
    for bad in ("800x516", [800], [0, 516], [-800, 516], None):
        section = _make_section(tmp_path, cell_aspect=bad)
        group = prepare_closeup_group({"closeup_section": section}, ANGLE_SIZE, lambda v: int(v * 1.0))
        assert group["slots"][0]["before"].size == ANGLE_SIZE, f"cell_aspect={bad!r} 未回退"


def test_no_scale_fn_falls_back(tmp_path):
    # 兼容旧调用方（不传 scale_fn）
    section = _make_section(tmp_path, cell_aspect=[800, 516])
    group = prepare_closeup_group({"closeup_section": section}, ANGLE_SIZE)
    assert group["slots"][0]["before"].size == ANGLE_SIZE


def test_no_section_returns_none():
    assert prepare_closeup_group({}, ANGLE_SIZE, lambda v: v) is None


def test_broken_paths_fail_open(tmp_path):
    section = {
        "regions": ["法令纹"],
        "label": "法令纹",
        "cell_aspect": [800, 516],
        "before_path": str(tmp_path / "nope_b.png"),
        "after_path": str(tmp_path / "nope_a.png"),
    }
    assert prepare_closeup_group({"closeup_section": section}, ANGLE_SIZE, lambda v: v) is None
