"""Regression tests for real formal-render background cleanup samples."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


REAL_JOB49_BEFORE = Path(
    "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/康巧佳/"
    "2025.10.29衡力20抬头、川字、海魅1.0ml注射唇、下巴/术前3.JPG"
)
REAL_JOB49_AFTER = Path(
    "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/康巧佳/"
    "2025.10.29衡力20抬头、川字、海魅1.0ml注射唇、下巴/术后3.JPG"
)
RENDER_SCRIPT = Path("/Users/a1234/Desktop/飞书Claude/skills/case-layout-board/scripts/render_brand_clean.py")
FACE_ALIGN_SCRIPT = Path("/Users/a1234/Desktop/飞书Claude/scripts/face_align_compare.py")
REAL_CASE114_SIDE = Path(
    "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/黄宝凤/"
    "2025.6.26/术前/IMG_2605.JPG"
)


@pytest.mark.skipif(
    not (REAL_JOB49_BEFORE.exists() and REAL_JOB49_AFTER.exists() and RENDER_SCRIPT.exists()),
    reason="真实 job #49 源图或 renderer 不在当前机器，无法验证真实抠图质量",
)
def test_protected_alignment_preserves_dark_real_background_without_white_mask_leak():
    code = f"""
import importlib.util
import json
from pathlib import Path
import numpy as np

spec = importlib.util.spec_from_file_location("render_brand_clean", {str(RENDER_SCRIPT)!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

before_img, _after_img = mod.render_aligned_pair(
    {str(REAL_JOB49_BEFORE)!r},
    [{str(REAL_JOB49_AFTER)!r}],
    (516, 624),
    "oblique",
    allow_direction_mismatch=True,
    protection_targets=["jawline", "mouth_corner"],
    render_plan_records=[],
)
arr = np.asarray(before_img.convert("RGB"))
white = (arr[:, :, 0] > 238) & (arr[:, :, 1] > 238) & (arr[:, :, 2] > 238)
h, w = white.shape
central = white[int(h * 0.08):int(h * 0.94), int(w * 0.20):int(w * 0.90)]
print(json.dumps({{
    "white_ratio_all": float(white.mean()),
    "white_ratio_central": float(central.mean()),
}}, ensure_ascii=False))
"""
    proc = subprocess.run(["python3", "-c", code], check=True, text=True, capture_output=True, timeout=60)
    metrics = json.loads(proc.stdout.strip().splitlines()[-1])

    assert metrics["white_ratio_central"] < 0.03
    assert metrics["white_ratio_all"] < 0.08


@pytest.mark.skipif(
    not (REAL_CASE114_SIDE.exists() and FACE_ALIGN_SCRIPT.exists()),
    reason="真实 case 114 侧面源图或 face_align_compare.py 不在当前机器，无法验证 EXIF 方向",
)
def test_auto_orient_does_not_double_rotate_exif_oriented_real_side_photo():
    code = f"""
import importlib.util
import json
import cv2
from PIL import Image, ImageOps

spec = importlib.util.spec_from_file_location("face_align_compare", {str(FACE_ALIGN_SCRIPT)!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

path = {str(REAL_CASE114_SIDE)!r}
raw = cv2.imread(path)
oriented = mod.auto_orient(raw, path)
display = ImageOps.exif_transpose(Image.open(path))
print(json.dumps({{
    "cv2_shape": list(raw.shape[:2]),
    "oriented_shape": list(oriented.shape[:2]),
    "display_size": list(display.size),
}}, ensure_ascii=False))
"""
    proc = subprocess.run(["python3", "-c", code], check=True, text=True, capture_output=True, timeout=30)
    metrics = json.loads(proc.stdout.strip().splitlines()[-1])
    display_w, display_h = metrics["display_size"]

    assert metrics["oriented_shape"] == [display_h, display_w]
