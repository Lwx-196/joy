from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip("mediapipe")


CASE45_PREOP = Path(
    "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/康巧佳/"
    "2025.10.29衡力20抬头、川字、海魅1.0ml注射唇、下巴/术前1.JPG"
)


def _precise_face_mask_module():
    try:
        return importlib.import_module("backend.services.precise_face_mask")
    except ModuleNotFoundError as exc:
        pytest.fail(f"backend.services.precise_face_mask is missing: {exc}")


def _coverage_pct(mask_path: Path, *, threshold: int = 16) -> float:
    with Image.open(mask_path) as mask:
        mask_l = mask.convert("L")
        pixels = mask_l.tobytes()
    covered = sum(px > threshold for px in pixels)
    return 100.0 * covered / len(pixels)


def test_module_does_not_import_mediapipe_at_top_level():
    precise_face_mask = _precise_face_mask_module()
    tree = ast.parse(Path(precise_face_mask.__file__).read_text())
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(alias.name != "mediapipe" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module is None or not node.module.startswith("mediapipe")


def test_unknown_region_key_raises_clear_error(tmp_path: Path):
    precise_face_mask = _precise_face_mask_module()
    with pytest.raises(ValueError, match="unknown precise face mask region"):
        precise_face_mask.generate_precise_mask(
            Path("unused.jpg"),
            ["unknown_target_xyz"],
            tmp_path / "mask.png",
        )


def test_precise_region_for_maps_landmark_and_coarse_keys():
    # closeup 分流 SSoT：有 landmark 的部位返回 precise region 名，其余 None（走 coarse）。
    precise_face_mask = _precise_face_mask_module()
    pr = precise_face_mask.precise_region_for
    assert pr("川字") == "glabella"
    assert pr("下巴") == "chin"
    assert pr("下颌线") == "chin"
    assert pr("唇") == "lips"
    assert pr("额纹") == "forehead"
    # 无精确 landmark 的 closeup 合格部位 → None（face_bbox 相对粗椭圆）
    assert pr("泪沟") is None
    assert pr("苹果肌") is None
    assert pr("法令纹") is None
    assert pr("unknown_xyz") is None


def test_detect_face_bbox_unreadable_returns_none(tmp_path: Path):
    # fail-open：读不出图 → None（近景永不挡板）
    precise_face_mask = _precise_face_mask_module()
    assert precise_face_mask.detect_face_bbox(tmp_path / "does_not_exist.png") is None


_REAL_FACE_CANDIDATES = [
    CASE45_PREOP,
    Path(
        "/Users/a1234/Desktop/案例生成器/incoming/无创案例库/无创注射案例库/"
        "曾玲莉/2025.10.29熊猫针1支+海魅云境骨性1支注射川字纹/术前1.JPG"
    ),
]


def test_detect_face_bbox_real_image_within_bounds(tmp_path: Path):
    src = next((p for p in _REAL_FACE_CANDIDATES if p.exists()), None)
    if src is None:
        pytest.skip("no real face smoke image available")
    from PIL import ImageOps

    precise_face_mask = _precise_face_mask_module()
    norm = tmp_path / "norm.png"
    with Image.open(src) as im:
        disp = ImageOps.exif_transpose(im).convert("RGB")
        disp.save(norm)
        w, h = disp.size
    bbox = precise_face_mask.detect_face_bbox(norm)
    assert bbox is not None
    left, top, right, bottom = bbox
    assert 0 <= left < right <= w and 0 <= top < bottom <= h
    # 人脸占画面合理比例（非退化点、非整图）
    assert 0.05 * w < (right - left) < w
    assert 0.05 * h < (bottom - top) < h


def test_case45_four_region_union_matches_phase0_coverage(tmp_path: Path):
    if not CASE45_PREOP.exists():
        pytest.skip(f"real smoke image unavailable: {CASE45_PREOP}")

    precise_face_mask = _precise_face_mask_module()
    out = tmp_path / "mask.png"
    result = precise_face_mask.generate_precise_mask(
        CASE45_PREOP,
        ["唇", "下巴", "川字", "额纹"],
        out,
    )

    assert result == out
    assert result.is_file()
    with Image.open(result) as mask:
        assert mask.mode == "L"
        assert mask.size[0] > 0
        assert mask.size[1] > 0

    coverage = _coverage_pct(result)
    assert coverage == pytest.approx(7.9, abs=0.9)
