"""P0.2-A — source_selection 产 crop_touches_frame warning 时附完整匹配维度。

让下游 pre_render_gate / accepted_warnings 可对 slot+code+selected_files / message_contains
做精确匹配，结束 5/16 18 case 卡死。

Warning 对象必须含 5 个必填 key: code / slot / message_contains / selected_files / source。
"""
from __future__ import annotations

from backend import source_selection


def _crop_dict_with_touch(image_path: str | None = None) -> dict:
    item = {"crop_touches_frame": True, "crop_margin": 0.01}
    if image_path is not None:
        item["image_path"] = image_path
    return item


def test_crop_component_front_block_carries_matching_dimensions() -> None:
    """front view + crop_touches_frame → status=block + 5 必填匹配维度 key 全填。"""
    before = _crop_dict_with_touch("source/术前-正面.jpg")
    after = _crop_dict_with_touch("source/术后-正面.jpg")

    result = source_selection.crop_component("front", before, after)

    assert result["status"] == "block"
    assert result["code"] == "crop_touches_frame"
    # P0.2-A: 5 必填匹配维度
    for key in ("slot", "message_contains", "selected_files", "source"):
        assert key in result, f"crop_component output missing matching dim: {key}"
    assert result["slot"] == "front"
    assert result["source"] == "source_selection"
    assert "裁切" in result["message_contains"] or "贴边" in result["message_contains"]
    assert isinstance(result["selected_files"], list)
    assert "source/术前-正面.jpg" in result["selected_files"]
    assert "source/术后-正面.jpg" in result["selected_files"]


def test_crop_component_side_review_also_carries_dimensions() -> None:
    """side view + crop_touches_frame → status=review，仍带 5 维度便于 accepted match。"""
    before = _crop_dict_with_touch("a.jpg")
    after = _crop_dict_with_touch("b.jpg")

    result = source_selection.crop_component("side", before, after)

    assert result["status"] == "review"
    assert result["code"] == "crop_touches_frame"
    assert result["slot"] == "side"
    assert result["source"] == "source_selection"
    assert result["selected_files"] == ["a.jpg", "b.jpg"]
    assert "message_contains" in result


def test_crop_component_ok_no_warning_no_dimensions() -> None:
    """主体未触边 → status=ok 不带 code，也不应带匹配维度（避免误匹配）。"""
    before = {"crop_touches_frame": False, "crop_margin": 0.10}
    after = {"crop_touches_frame": False, "crop_margin": 0.10}

    result = source_selection.crop_component("front", before, after)

    assert result["status"] == "ok"
    assert "code" not in result
    # OK 路径不带匹配维度（status=ok 不进 accepted_warnings 系统）
    assert "selected_files" not in result


def test_crop_component_missing_image_path_falls_back_to_role_labels() -> None:
    """before/after 没有 image_path → selected_files 退化到 role 标签，仍可匹配。"""
    before = _crop_dict_with_touch(None)  # no image_path
    after = _crop_dict_with_touch(None)

    result = source_selection.crop_component("front", before, after)

    assert result["status"] == "block"
    assert isinstance(result["selected_files"], list)
    # 必须非空，至少给出 role 标签
    assert result["selected_files"], "selected_files must not be empty when warning fires"


def test_crop_component_partial_touch_only_includes_touched_role_files() -> None:
    """只有 before 触边 → selected_files 只含 before 的 path（不污染 after）。"""
    before = _crop_dict_with_touch("before.jpg")
    after = {"crop_touches_frame": False, "crop_margin": 0.1, "image_path": "after.jpg"}

    result = source_selection.crop_component("front", before, after)

    assert result["status"] == "block"
    assert result["selected_files"] == ["before.jpg"]
    assert "after.jpg" not in result["selected_files"]
