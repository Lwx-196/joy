"""Mask-anchored full-frame composite — gpt-image-2 术后效果模拟的身份保护硬底线.

硬底线（anchored-simulation.md §硬约束 / demo + case45 Run A/B judge 验证）：
gpt-image-2 把**整张**脸出图后必然漂移（瘦脸/磨皮/换脸），不可直接交付。本模块用
``Image.composite`` 把 AI 效果**只锁回 mask 内的治疗区**，mask==0 的区域像素 **字节级 == 原图**
→ 身份完全保住，只有治疗区是 AI 效果。

与 ``ai_generation_adapter._composite_focal`` 互补、不重复：
- ``_composite_focal`` = ComfyUI **crop-inpaint** 路线（把 inpaint 过的小 crop 贴回 bbox）。
- 本模块 = plan 钦定的 **gpt-image-2 整帧生成** 路线（整张 AI 图 → mask 锚定锁回原图）。

纯 PIL，无 numpy → CI 安全（Pillow 在 requirements.txt）。``Image.composite(ai, original, mask)``
在 mask==0 处逐字节返回 original 像素，硬底线由原语保证；本模块再做一次 mask==0 区域的
字节级核验（``outside_exact``），作回归护栏。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 治疗区覆盖率统计阈值（与 build_mask 的 union>16 羽化判据一致）。
_MASK_COVERAGE_THRESHOLD = 16


@dataclass(frozen=True)
class CompositeResult:
    """mask 锚定 composite 的结果与不变量度量。"""

    output_path: Path
    width: int
    height: int
    changed_pct: float       # 与原图不同的像素占整帧比例（治疗效果落点）
    coverage_pct: float      # mask>阈值（治疗区）占整帧比例
    outside_exact: bool      # mask==0 区域是否字节级 == 原图（硬底线核验）


def _combined_channel_diff(diff_rgb):
    """RGB 差图 → 任一通道差的最大值(L)，避免等亮异色差被 convert('L') 抹掉。"""
    from PIL import ImageChops

    r, g, b = diff_rgb.split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b)


def mask_anchor_composite(
    original_path: str | Path,
    ai_image_path: str | Path,
    mask_path: str | Path,
    output_path: str | Path,
    *,
    strict: bool = True,
) -> CompositeResult:
    """把 AI 整帧效果按 mask 锚定锁回原图（mask 外字节级保原图）。

    参数:
      original_path: 原始术后/术前照（身份基准；像素空间须与 mask/ai 一致——
        EXIF 朝向由调用方上游统一，本模块不做 transpose 以免与 mask 错位）。
      ai_image_path: gpt-image-2 整帧输出（尺寸不符会 LANCZOS 缩放到原图尺寸）。
      mask_path: L-mode 焦点 mask，白=治疗区（可羽化）；尺寸不符会缩放到原图尺寸。
      output_path: 写出路径（PNG，sibling temp + atomic replace）。
      strict: True 时若 mask==0 区域出现像素漂移则抛 AssertionError（硬底线护栏）。

    返回 ``CompositeResult``（含 outside_exact 硬底线核验 + 覆盖/改动度量）。
    """
    from PIL import Image, ImageChops

    original_path = Path(original_path)
    output_path = Path(output_path)

    with Image.open(original_path) as o:
        original = o.convert("RGB")
    width, height = original.size

    with Image.open(ai_image_path) as a:
        ai = a.convert("RGB")
        if ai.size != (width, height):
            ai = ai.resize((width, height), Image.LANCZOS)

    with Image.open(mask_path) as m:
        mask = m.convert("L")
        if mask.size != (width, height):
            mask = mask.resize((width, height), Image.LANCZOS)

    # mask=255 → AI，mask=0 → original（逐字节），羽化带按 alpha 混合。
    out = Image.composite(ai, original, mask)

    diff = _combined_channel_diff(ImageChops.difference(out, original))

    # 硬底线核验：mask==0 区域是否字节级 == 原图。
    strict_outside = mask.point(lambda p: 255 if p == 0 else 0)
    leaked = ImageChops.multiply(diff, strict_outside)
    outside_exact = leaked.getbbox() is None
    if strict and not outside_exact:
        raise AssertionError(
            "mask-anchor breached: pixels changed outside the mask==0 region "
            "(identity floor violated)"
        )

    total = width * height
    changed_px = total - diff.histogram()[0]            # 非零差像素数
    coverage_bin = mask.point(lambda p: 255 if p > _MASK_COVERAGE_THRESHOLD else 0)
    coverage_px = coverage_bin.histogram()[255]         # 治疗区像素数
    changed_pct = round(100.0 * changed_px / total, 4)
    coverage_pct = round(100.0 * coverage_px / total, 4)

    tmp_out = output_path.with_name(f".mask-anchor-{os.getpid()}-{output_path.name}")
    out.save(tmp_out, format="PNG")
    os.replace(tmp_out, output_path)

    return CompositeResult(
        output_path=output_path,
        width=width,
        height=height,
        changed_pct=changed_pct,
        coverage_pct=coverage_pct,
        outside_exact=outside_exact,
    )


__all__ = ["CompositeResult", "mask_anchor_composite"]
