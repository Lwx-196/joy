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
