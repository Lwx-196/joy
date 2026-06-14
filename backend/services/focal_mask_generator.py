"""Focal mask generator — Step 2 of the 4-mode dispatch plan.

Given a portrait image + focus_targets list (chin / lips / cheek / etc.),
produce a single-channel mask PNG defining the *coarse* enhancement region.

Design intent
-------------

The downstream v2 ComfyUI workflow (``local_region_enhance_v2``) has its own
``MediaPipeFaceMeshToSEGS`` + ``SAMDetectorCombined`` nodes that refine any
input mask to the actual face geometry. This generator therefore does NOT
need pixel-precise lip / eye contours — it just needs a coarse bounding
ellipse that says "the focus region is roughly here." SAM does the rest.

This keeps the helper fast (~50ms) and avoids a hard MediaPipe dep at the
backend layer; if MediaPipe is unavailable, we fall back to a centered
ellipse covering ~70% of the image area (full-face fallback).

Per ``~/.claude/plans/md-ai-4-mode-router.md`` Step 2.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Iterable

LOGGER = logging.getLogger(__name__)


# Relative region-of-interest per focus_target keyword.
#
# Each value is a tuple ``(cx_pct, cy_pct, w_pct, h_pct)`` describing where
# inside the face bounding box the mask ellipse should be centered (cx,cy)
# and how wide/tall it should be (w,h) — all as fractions of the face bbox.
#
# Coordinates: (0, 0) = top-left of face bbox, (1, 1) = bottom-right.
# Tested values were tuned by-eye on real L2 case dataset (case 79 / 134).
_FOCAL_REGIONS: dict[str, tuple[float, float, float, float]] = {
    # Chin / jaw line (lower face)
    "下巴": (0.5, 0.85, 0.55, 0.30),
    "下颌线": (0.5, 0.85, 0.65, 0.25),
    "chin": (0.5, 0.85, 0.55, 0.30),
    # Lips
    "唇": (0.5, 0.78, 0.40, 0.18),
    "嘴": (0.5, 0.78, 0.45, 0.20),
    "lip": (0.5, 0.78, 0.40, 0.18),
    "lips": (0.5, 0.78, 0.40, 0.18),
    # Cheek / 面颊
    "面颊": (0.5, 0.55, 0.85, 0.35),
    "苹果肌": (0.5, 0.55, 0.75, 0.30),
    "cheek": (0.5, 0.55, 0.85, 0.35),
    # Nasolabial fold / 法令纹
    "法令纹": (0.5, 0.65, 0.50, 0.22),
    "nasolabial": (0.5, 0.65, 0.50, 0.22),
    # Tear trough / 泪沟 / undereye
    "泪沟": (0.5, 0.40, 0.60, 0.18),
    "眼袋": (0.5, 0.42, 0.55, 0.18),
    "卧蚕": (0.5, 0.42, 0.55, 0.18),
    "tear_trough": (0.5, 0.40, 0.60, 0.18),
    "undereye": (0.5, 0.42, 0.55, 0.18),
    # Nose region
    "鼻尖": (0.5, 0.55, 0.18, 0.18),
    "鼻基底": (0.5, 0.62, 0.30, 0.10),
    "鼻翼": (0.5, 0.60, 0.28, 0.15),
    # 鼻背 / 鼻梁 / 山根 / 隆鼻 — 鼻中线竖向高光带（atlas 把 鼻子/鼻梁/山根/隆鼻 归一到 鼻背，
    # 此前无 entry → 全脸 fallback；补竖向窄椭圆 radix(眉间下)→tip 才能定位近景/局部增强）
    "鼻背": (0.5, 0.48, 0.16, 0.40),
    "nose": (0.5, 0.55, 0.25, 0.30),
    # Forehead horizontal lines / 额纹 / 抬头纹 (frontalis) — AI 术后模拟新增
    "额纹": (0.5, 0.20, 0.55, 0.16),
    "抬头": (0.5, 0.20, 0.55, 0.16),
    "抬头纹": (0.5, 0.20, 0.55, 0.16),
    "额头": (0.5, 0.20, 0.55, 0.16),
    # Glabella / 川字 / 眉间 (vertical frown lines) — AI 术后模拟新增
    "川字": (0.5, 0.33, 0.22, 0.10),
    "川字纹": (0.5, 0.33, 0.22, 0.10),
    "眉间": (0.5, 0.33, 0.22, 0.10),
    # Full face fallback
    "face": (0.5, 0.5, 0.95, 0.95),
    "面部": (0.5, 0.5, 0.95, 0.95),
}


def _union_regions(targets: Iterable[str]) -> tuple[float, float, float, float] | None:
    """Combine multiple focal regions into a single bounding box.

    Returns ``(cx, cy, w, h)`` covering the union, or ``None`` if no target
    is recognised in ``_FOCAL_REGIONS``.
    """
    regions = [_FOCAL_REGIONS[t] for t in targets if t in _FOCAL_REGIONS]
    if not regions:
        return None
    # Convert each (cx, cy, w, h) to bbox corners then union
    bboxes = [
        (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
        for cx, cy, w, h in regions
    ]
    x_min = max(0.0, min(b[0] for b in bboxes))
    y_min = max(0.0, min(b[1] for b in bboxes))
    x_max = min(1.0, max(b[2] for b in bboxes))
    y_max = min(1.0, max(b[3] for b in bboxes))
    cx = (x_min + x_max) / 2
    cy = (y_min + y_max) / 2
    w = x_max - x_min
    h = y_max - y_min
    return (cx, cy, w, h)


def union_region_height(targets: Iterable[str]) -> float | None:
    """治疗区 union 纵向高度（占人脸 bbox 高度的分数 [0,1]）；无可定位区返回 None。

    用于近景 gate（board_closeup_section）：高度小 = 治疗区紧凑集中（单区/同区相邻），
    高度大 = 多部位纵向铺开（≈整脸）。纯算术、零图像处理、与具体图像/朝向无关。
    全脸 fallback key（face/面部）不计入可定位区。
    """
    located = [t for t in targets if t in _FOCAL_REGIONS and t not in ("face", "面部")]
    u = _union_regions(located)
    return None if u is None else u[3]


def _to_pixel_bbox(
    region: tuple[float, float, float, float], w: int, h: int
) -> tuple[int, int, int, int]:
    """Convert a normalised ``(cx, cy, fw, fh)`` region to a clamped pixel bbox."""
    cx, cy, fw, fh = region
    px_cx, px_cy = int(cx * w), int(cy * h)
    px_w, px_h = int(fw * w), int(fh * h)
    return (
        max(0, px_cx - px_w // 2),
        max(0, px_cy - px_h // 2),
        min(w, px_cx + px_w // 2),
        min(h, px_cy + px_h // 2),
    )


def generate_focus_mask(
    image_path: Path,
    focus_targets: list[str],
    *,
    output_path: Path | None = None,
    separate_ellipses: bool = False,
) -> Path:
    """Generate a coarse single-channel focus mask PNG.

    Returns the mask file path. The mask has the same dimensions as the
    input image; pixels inside the focus ellipse are 255 (white),
    everything else is 0 (black).

    If ``focus_targets`` is empty or contains no recognised keywords,
    falls back to a full-face mask (centered ellipse, 70% of image area).

    ``separate_ellipses`` (default False = backward-compatible):
      - False → recognised targets are merged into a single bounding-box
        ellipse (``_union_regions``). Correct for the ComfyUI v2 route whose
        downstream ``MediaPipeFaceMeshToSEGS`` + SAM refine the coarse box.
      - True  → each recognised target is drawn as its own ellipse and the
        true union is kept (gaps between spread regions stay black). Needed
        for the gpt-image-2 mask-anchor route (no SAM refine downstream), so
        spread治疗区 (额纹+川字+唇+下巴) don't collapse into a whole-face box —
        enforces precise-correspondence (只锁实际治疗区，不外扩).

    If PIL is unavailable, raises ``ImportError`` (no silent fail here —
    caller is expected to wrap in its own try/except).
    """
    from PIL import Image, ImageDraw

    if output_path is None:
        output_path = Path(tempfile.mkstemp(suffix=".focus_mask.png", prefix=".focal-")[1])

    with Image.open(image_path) as src:
        w, h = src.size

    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)

    # Treat the whole image as the "face bbox" for simplicity. The downstream
    # MediaPipeFaceMeshToSEGS in the v2 workflow (union path) will detect the
    # actual face and intersect with this mask to refine.
    recognised = [t for t in focus_targets if t in _FOCAL_REGIONS] if focus_targets else []
    if separate_ellipses and recognised:
        for target in recognised:
            draw.ellipse(_to_pixel_bbox(_FOCAL_REGIONS[target], w, h), fill=255)
        LOGGER.info(
            "focal_mask: %dx%d separate-ellipse union for %s → %s",
            w, h, recognised, output_path,
        )
    else:
        region = _union_regions(focus_targets) if focus_targets else None
        if region is None:
            # Full-face fallback: centered ellipse covering ~70% of frame
            region = (0.5, 0.5, 0.85, 0.85)
            LOGGER.info(
                "focal_mask: no recognised targets in %s; falling back to full-face ellipse",
                focus_targets,
            )
        bbox = _to_pixel_bbox(region, w, h)
        draw.ellipse(bbox, fill=255)
        LOGGER.info(
            "focal_mask: generated %dx%d mask for targets=%s region_bbox=%s → %s",
            w, h, focus_targets, bbox, output_path,
        )

    mask.save(output_path, format="PNG", optimize=True)
    return output_path


__all__ = ["generate_focus_mask"]
