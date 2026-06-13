"""G3 板级纹类近景对比区：含真皱纹类项目的板，板尾追加近景 before/after 对比行。

定位（2026-06-11 审核标准 v1 G3）：纹类项目（川字纹/额纹/法令纹）在角度行
全身/半身尺度下沟纹不可读（曾玲莉 川字纹近景缺失不可读 = 标定根因），
板尾追加一行近景对比让"打了什么改善哪里"可读。

纪律：
- 范围 = 真皱纹类 atlas key（川字/额纹/法令纹）。泪沟/卧蚕是容量填充非纹，
  刻意不进集（反臆造，与 G1 EXTRA_GATE_KEYWORDS 同纪律）。
- 源 = front 槽**原始源图**（非 AI 增强后图）：faithful-zoom L-139/L-140 ——
  真实像素全分辨率 + 古典 clarity（arm A 产品化路径，真 gate 91.67% PASS），
  零烧钱零 API。
- fail-open：无纹类命中 / 无 front 槽 / mask、裁剪、增强任何一步失败 →
  返回空/None，板照常出。近景是增益不是门槛，永不挡板。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 真皱纹类 region（facial_region_atlas key 子集；alias 由 extract_regions 归一：
# 川字纹/眉间→川字，抬头纹/额头→额纹）。
WRINKLE_REGION_KEYS: tuple[str, ...] = ("川字", "额纹", "法令纹")

# atlas key → 板面展示名
REGION_DISPLAY = {"川字": "川字纹", "额纹": "额纹", "法令纹": "法令纹"}

# 近景 cell 宽高比基准 = render_brand_clean 角度行 cell（scale=1 时 516x624），
# 同比例可让渲染端高度公式零改动。
CELL_ASPECT: tuple[int, int] = (516, 624)

# 宽带纹类横版 cell（owner 拍板 2026-06-11）：法令纹/额纹 mask 是横跨宽带
# （郭璟琳法令纹 bbox 1968x576，w/h=3.4），竖版 cell 下 expand_to_aspect 必然
# 把高顶到全脸（四档 pad 探针零差别，pad 非杠杆）→ 横版近景条（鼻到下巴/额头
# 局部带）才与正面行视觉区分。川字是小中央区，竖版已验可读，保持现状。
CELL_ASPECT_WIDE: tuple[int, int] = (800, 516)
WIDE_REGION_KEYS: tuple[str, ...] = ("法令纹", "额纹")

# 窄中央纹近景带（owner 拍板 2026-06-14）：川字类收紧到 800×420（占源高 ~29%），
# 有效孤立治疗区与其他部位。比 WIDE 更扁，配合下移对中（见 NARROW_CENTER_SHIFT_FRAC）。
CELL_ASPECT_NARROW: tuple[int, int] = (800, 420)
NARROW_STRIP_KEYS: tuple[str, ...] = ("川字",)
# 川字 mask 椭圆含上中线点 9 → 质心偏额头（探针实测 bbox 中心在眉间之上）；
# 裁剪中心向下偏 = 占crop高的比例，把眉间真川字从底边拉回画面（探针 variant C 验证 0.10 framing 正确）
NARROW_CENTER_SHIFT_FRAC = 0.10

# bbox 外扩比例：纹是细长区域，留足上下文（额头/眉/鼻周）近景才可定位部位
CROP_PAD_FRAC = 0.35


def wrinkle_regions(treatment_text: str) -> list[str]:
    """treatment 串 → 命中的真皱纹类 region key（atlas 定义序，稳定输出）。

    解析失败 fail-open 返回空列表（无近景区，不挡板）。
    """
    try:
        from backend.services.facial_region_atlas import extract_regions

        return [r for r in extract_regions(treatment_text or "") if r in WRINKLE_REGION_KEYS]
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("closeup wrinkle_regions 解析失败（fail-open 跳过）: %s", exc)
        return []


def section_label(regions: list[str]) -> str:
    return "、".join(REGION_DISPLAY.get(r, r) for r in regions)


def cell_aspect_for(regions: list[str]) -> tuple[int, int]:
    """命中宽带纹类（法令纹/额纹）→ 横版 cell；纯川字 → 竖版现状。

    混合命中（如 川字+法令纹）也走横版：mask 是各 region 的 union，
    含宽带区时 bbox 必然横宽。
    """
    if any(r in WIDE_REGION_KEYS for r in regions):
        return CELL_ASPECT_WIDE
    if any(r in NARROW_STRIP_KEYS for r in regions):
        return CELL_ASPECT_NARROW
    return CELL_ASPECT


def center_shift_for(regions: list[str]) -> float:
    """近景裁剪中心下移比例（占 crop 高）。宽版优先不偏；川字类下移到眉间真纹。"""
    if any(r in WIDE_REGION_KEYS for r in regions):
        return 0.0
    if any(r in NARROW_STRIP_KEYS for r in regions):
        return NARROW_CENTER_SHIFT_FRAC
    return 0.0


def expand_to_aspect(
    bbox: tuple[int, int, int, int],
    image_size: tuple[int, int],
    aspect: tuple[int, int] = CELL_ASPECT,
    center_shift_frac: float = 0.0,
) -> tuple[int, int, int, int]:
    """把 bbox 外扩成目标宽高比并 clamp 进图界（保中心、不变形）。

    超界时收缩另一边保比例，再平移中心进界——输出恒为合法 crop box。
    center_shift_frac>0 把裁剪中心按 crop 高的该比例向下偏（消 mask 质心偏高，见川字）。
    """
    left, top, right, bottom = bbox
    img_w, img_h = image_size
    if right <= left or bottom <= top:
        raise ValueError(f"bbox 退化: {bbox}")
    target = aspect[0] / aspect[1]

    w = float(right - left)
    h = float(bottom - top)
    if w / h < target:
        w = h * target  # 太窄 → 扩宽
    else:
        h = w / target  # 太扁 → 扩高

    # clamp 尺寸进图界，保比例收缩
    if w > img_w:
        w = float(img_w)
        h = w / target
    if h > img_h:
        h = float(img_h)
        w = h * target

    cx = (left + right) / 2.0
    cy = (top + bottom) / 2.0 + h * center_shift_frac
    new_left = min(max(0.0, cx - w / 2.0), img_w - w)
    new_top = min(max(0.0, cy - h / 2.0), img_h - h)
    return (
        int(round(new_left)),
        int(round(new_top)),
        int(round(new_left + w)),
        int(round(new_top + h)),
    )


def build_closeup_assets(
    before_path: str | Path,
    after_path: str | Path,
    regions: list[str],
    work_dir: str | Path,
) -> dict | None:
    """front 槽 before/after 源图 → 各自 clarity 增强 + 纹类区近景裁剪。

    每侧管线（faithful-zoom arm A 产品化路径，零烧钱）：
    1. ``unsharp_focal_enhance(preset="clarity")`` 全图保真增强
       （K-1 契约：失败返回原图，裁剪照常）
    2. ``generate_focus_mask`` 源图 region mask → ``_focal_crop_bbox`` 像素 bbox
    3. bbox 扩到 cell 宽高比 → 裁剪存 PNG

    返回渲染端 ``closeup_section`` dict；任何失败返回 None（fail-open 不挡板）。
    """
    try:
        from PIL import Image, ImageOps

        from backend.ai_generation_adapter import _focal_crop_bbox
        from backend.services.classical_enhance import unsharp_focal_enhance
        from backend.services.focal_mask_generator import generate_focus_mask

        work_dir = Path(work_dir)
        aspect = cell_aspect_for(regions)
        shift = center_shift_for(regions)
        out: dict[str, str] = {}
        for side, src in (("before", Path(before_path)), ("after", Path(after_path))):
            side_dir = work_dir / side
            side_dir.mkdir(parents=True, exist_ok=True)
            enhanced = unsharp_focal_enhance(
                src, focus_targets=list(regions), output_dir=side_dir, preset="clarity"
            )
            mask = generate_focus_mask(
                src, list(regions), output_path=side_dir / "closeup_mask.png"
            )
            bbox = _focal_crop_bbox(mask, pad_frac=CROP_PAD_FRAC)
            with Image.open(enhanced) as img:
                # L-145：EXIF 朝向先归一再裁剪（增强产物 PNG 无 EXIF = no-op；
                # K-1 回退原 JPEG 时归一到与 mask 同一坐标系）
                img = ImageOps.exif_transpose(img)
                crop_box = expand_to_aspect(tuple(bbox), img.size, aspect=aspect, center_shift_frac=shift)
                crop = img.convert("RGB").crop(crop_box)
            crop_path = side_dir / "closeup.png"
            crop.save(crop_path)
            out[side] = str(crop_path)
        return {
            "regions": list(regions),
            "label": section_label(regions),
            "cell_aspect": list(aspect),
            "before_path": out["before"],
            "after_path": out["after"],
        }
    except Exception as exc:  # noqa: BLE001 — fail-open，近景永不挡板
        logger.warning("closeup 近景构建失败（fail-open 跳过近景区）: %s", exc)
        return None


def build_for_manifest(
    manifest: dict,
    treatment_text: str,
    work_dir: str | Path,
) -> dict | None:
    """manifest + treatment → closeup_section dict（或 None）。

    取第一个有 front 槽的 group 的 before/after **原始源图路径**
    （selected_slots 的 path 即源图；AI 增强产物在 enhancement 字段，刻意不用）。
    """
    regions = wrinkle_regions(treatment_text)
    if not regions:
        return None
    try:
        front_sel = next(
            (
                g["selected_slots"]["front"]
                for g in (manifest.get("groups") or [])
                if isinstance(g, dict) and (g.get("selected_slots") or {}).get("front")
            ),
            None,
        )
        if not front_sel:
            logger.info("closeup: 命中纹类 %s 但无 front 槽，跳过近景区", regions)
            return None
        return build_closeup_assets(
            front_sel["before"]["path"],
            front_sel["after"]["path"],
            regions,
            work_dir,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("closeup build_for_manifest 失败（fail-open 跳过）: %s", exc)
        return None
