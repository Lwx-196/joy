"""G3 板级近景对比区：单部位/同区集中的项目，板尾追加近景 before/after 对比行。

定位（2026-06-11 审核标准 v1 G3 → 2026-06-14 全部位延展 + 覆盖率 gate）：
局部小变化（川字纹/泪沟/卧蚕/苹果肌/下巴…）在角度行全身/半身尺度下不可读
（曾玲莉 川字纹近景缺失不可读 = 标定根因），板尾追加一行近景对比让
"打了什么改善哪里"可读。

近景 gate（owner 拍板 2026-06-14，覆盖率自动判）：
- **单部位 / 同区相邻**（川字；或 泪沟+卧蚕 都在眼下）→ 近景能放大孤立该处 → 出。
- **多部位且分散**（眉弓+鼻+下巴，上中下都有）→ 要同时看到 = 几乎整张脸，
  而正面行本来就是整张脸 → 近景冗余 → 跳过。
- 判据 = 治疗区 union 纵向高度占脸比 ≤ COMPACT_HEIGHT_MAX（纯算术，零图像处理）。

纪律：
- 源 = front 槽**原始源图**（非 AI 增强后图）：faithful-zoom L-139/L-140 ——
  真实像素全分辨率 + 古典 clarity（arm A 产品化路径，真 gate 91.67% PASS），零烧钱零 API。
- 鼻类正面刻意不入合格集（owner 2026-06-14）：正面鼻近景 = 竖条含全脸、孤立性弱，
  鼻背线轮廓斜/侧才显（atlas 同标），留 oblique/profile 另设计。
- fail-open：gate 不过 / 无 front 槽 / mask、裁剪、增强任何一步失败 →
  返回空/None，板照常出。近景是增益不是门槛，永不挡板。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 近景 cell 宽高比（owner 拍板 2026-06-11 / 2026-06-14） ──
# 基准 = render_brand_clean 角度行 cell（scale=1 时 516x624），同比例让渲染端高度公式零改动。
CELL_ASPECT: tuple[int, int] = (516, 624)          # 竖版基线（兜底）
CELL_ASPECT_WIDE: tuple[int, int] = (800, 516)     # 法令纹/额纹 横带
# 横向近景统一用宽 800（与 WIDE/NARROW 同宽，board 1920 下双格留足边距 s(110)；
# 900 宽实测仅剩 s(5) 边距贴边）。各部位只用高度调 framing 比例（probe 标定）。
CELL_ASPECT_NARROW: tuple[int, int] = (800, 420)   # 川字 窄中央带 r=1.90（owner 06-14 拍板）
CELL_ASPECT_UNDEREYE: tuple[int, int] = (800, 392) # 泪沟/卧蚕/眼袋 眼下带 r≈2.04（probe 900×440）
CELL_ASPECT_MIDFACE: tuple[int, int] = (800, 444)  # 苹果肌/面颊 中脸带 r≈1.80（probe 900×500）
CELL_ASPECT_LOWER: tuple[int, int] = (800, 570)    # 下巴/下颌线/唇 下脸带 r≈1.40（probe 700×500）

# 川字 mask 椭圆含上中线点 9 → 质心偏额头（探针实测 bbox 中心在眉间之上）；
# 裁剪中心向下偏 = 占 crop 高的比例，把眉间真川字从底边拉回画面（探针 variant C 验 0.10 正确）。
NARROW_CENTER_SHIFT_FRAC = 0.10

# 部位 → (近景 cell aspect, 裁剪中心下移比例 frac)。owner 2026-06-14 全部位延展。
# aspect 由各部位 probe 标定（/tmp/verify_g3_region_crop.py）：眼下/苹果肌=横带，下巴=下脸横带。
# 下巴/下颌线 mask 偏低延入颈 → 负 shift（上移）对中下巴。鼻类刻意不入（正面不做）。
REGION_CLOSEUP_LAYOUT: dict[str, tuple[tuple[int, int], float]] = {
    "川字": (CELL_ASPECT_NARROW, NARROW_CENTER_SHIFT_FRAC),
    "额纹": (CELL_ASPECT_WIDE, 0.0),
    "法令纹": (CELL_ASPECT_WIDE, 0.0),
    # 负 shift = 上移裁剪中心 = 头顶留白（owner 06-14：眼下/中脸带原本头顶顶格 → 居中）。
    "泪沟": (CELL_ASPECT_UNDEREYE, -0.03),
    "卧蚕": (CELL_ASPECT_UNDEREYE, -0.03),
    "眼袋": (CELL_ASPECT_UNDEREYE, -0.03),
    "苹果肌": (CELL_ASPECT_MIDFACE, -0.08),
    "面颊": (CELL_ASPECT_MIDFACE, -0.08),
    "下巴": (CELL_ASPECT_LOWER, -0.05),
    "下颌线": (CELL_ASPECT_LOWER, -0.05),
    "唇": (CELL_ASPECT_LOWER, 0.0),
}

# 出特写的合格部位集 = 有精确 _FOCAL_REGIONS 定位 + 正面可读 + 非鼻类。
CLOSEUP_ELIGIBLE_FRONT: frozenset[str] = frozenset(REGION_CLOSEUP_LAYOUT)

# aspect 解析优先序：紧凑 union 内若跨组，取最宽带的一档（expand_to_aspect 恒含 bbox，
# 选宽带只决定追加上下文方向，不会切掉任何合格区）。
# 苹果肌/面颊 排在 泪沟/卧蚕 之前：泪沟+苹果肌 同区时 union 由中脸带（更高/更低）主导，
# 取中脸 aspect+headroom 才容得下并居中（owner 06-14 陈艺琼 居中）。
_ASPECT_PRIORITY: tuple[str, ...] = (
    "法令纹", "额纹", "苹果肌", "面颊", "川字", "泪沟", "卧蚕", "眼袋",
    "下巴", "下颌线", "唇",
)

# 近景 gate 阈值（owner 拍板 2026-06-14，覆盖率自动判，边界靠真实案例标定）：
# 可定位治疗区 union 纵向高度 ≤ 此值（占脸高）= 紧凑单区 → 出特写；超过 = 多部位铺开 → 跳过。
COMPACT_HEIGHT_MAX = 0.42

# atlas key → 板面展示名
REGION_DISPLAY = {
    "川字": "川字纹", "额纹": "额纹", "法令纹": "法令纹",
    "泪沟": "泪沟", "卧蚕": "卧蚕", "眼袋": "眼袋",
    "苹果肌": "苹果肌", "面颊": "面颊",
    "下巴": "下巴", "下颌线": "下颌线", "唇": "唇",
}

# bbox 外扩比例：留足上下文（额头/眉/鼻周）近景才可定位部位
CROP_PAD_FRAC = 0.35


def closeup_plan_regions(treatment_text: str) -> list[str]:
    """治疗串 → 出近景特写的 region 列表（已过紧凑 gate）。空 = 不出特写。

    gate（owner 2026-06-14，覆盖率自动判）：
      1. 解析全部治疗区；取可 _FOCAL_REGIONS 定位的子集量 union 纵向高度。
      2. union 高度 > COMPACT_HEIGHT_MAX → 多部位铺开（≈整脸）→ 跳过，正面行已覆盖。
      3. 紧凑 → 取其中正面合格部位（CLOSEUP_ELIGIBLE_FRONT）作为特写内容；
         纯鼻类等无合格部位 → 空（不出特写）。
    解析失败/无治疗区一律 fail-open 返回空（近景是增益非门槛）。
    """
    try:
        from backend.services.facial_region_atlas import extract_regions

        all_regions = list(extract_regions(treatment_text or ""))
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("closeup 治疗区解析失败（fail-open 跳过）: %s", exc)
        return []
    if not all_regions:
        return []
    try:
        from backend.services.focal_mask_generator import union_region_height

        union_h = union_region_height(all_regions)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("closeup union 高度计算失败（fail-open 跳过）: %s", exc)
        return []
    if union_h is None:
        return []  # 无可定位治疗区
    if union_h > COMPACT_HEIGHT_MAX:
        logger.info(
            "closeup gate: 治疗区分散 union_h=%.2f > %.2f（多部位，正面已覆盖），跳过近景",
            union_h, COMPACT_HEIGHT_MAX,
        )
        return []
    eligible = [r for r in all_regions if r in CLOSEUP_ELIGIBLE_FRONT]
    if not eligible:
        logger.info("closeup gate: 紧凑但无正面合格部位（如纯鼻类），跳过近景")
    return eligible


def section_label(regions: list[str]) -> str:
    return "、".join(REGION_DISPLAY.get(r, r) for r in regions)


def _dominant_layout(regions: list[str]) -> tuple[tuple[int, int], float]:
    """紧凑 union 内取最宽带部位的 (aspect, shift)；无命中回退竖版基线。"""
    for r in _ASPECT_PRIORITY:
        if r in regions:
            return REGION_CLOSEUP_LAYOUT[r]
    return (CELL_ASPECT, 0.0)


def cell_aspect_for(regions: list[str]) -> tuple[int, int]:
    """命中部位 → 近景 cell aspect（REGION_CLOSEUP_LAYOUT，按 _ASPECT_PRIORITY 取主导）。"""
    return _dominant_layout(regions)[0]


def center_shift_for(regions: list[str]) -> float:
    """近景裁剪中心下移比例（占 crop 高，负=上移）。按主导部位取。"""
    return _dominant_layout(regions)[1]


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


def _build_focus_mask(
    norm_src: Path,
    regions: list[str],
    face_bbox: tuple[int, int, int, int] | None,
    use_face: bool,
    side_dir: Path,
) -> Path:
    """近景 region mask（统一输出 ``side_dir/closeup_mask.png``）。

    分流（③ 修复核心）：
    - 有真实 landmark 的部位（眉间/颏/唇/额 → ``precise_region_for``）用
      ``generate_precise_mask`` 按 MediaPipe landmark 锚定解剖位置（川字精准居中眉间，
      无需 shift 猜测）。
    - 无 landmark 的部位（泪沟/卧蚕/眼袋/苹果肌/面颊/法令纹）用
      ``generate_focus_mask`` 相对**真实人脸 bbox** 放置粗椭圆。
    - 一组里两类都有 → 各出一张 union（np.maximum），裁剪框恒含两者。

    ``use_face=False``（任一侧未检到脸）→ 全部走整图相对粗椭圆（face_bbox=None，
    等价旧行为，保 before/after 同口径）。precise 失败 → 该组整体退 coarse。
    """
    from PIL import Image

    from backend.services.focal_mask_generator import generate_focus_mask
    from backend.services.precise_face_mask import generate_precise_mask, precise_region_for

    out_path = side_dir / "closeup_mask.png"
    if not use_face:
        return generate_focus_mask(norm_src, list(regions), output_path=out_path, face_bbox=None)

    precise = [r for r in regions if precise_region_for(r)]
    coarse = [r for r in regions if not precise_region_for(r)]
    layers: list[Path] = []
    if precise:
        try:
            layers.append(generate_precise_mask(norm_src, precise, side_dir / "precise_mask.png"))
        except Exception as exc:  # noqa: BLE001 — precise 失败该组整体退 coarse face_bbox
            logger.info("closeup precise mask 失败，%s 退 coarse: %s", precise, exc)
            coarse = list(regions)
    if coarse:
        layers.append(
            generate_focus_mask(
                norm_src, coarse, output_path=side_dir / "coarse_mask.png", face_bbox=face_bbox
            )
        )

    if len(layers) == 1:
        with Image.open(layers[0]) as im:
            im.convert("L").save(out_path, format="PNG", optimize=True)
        return out_path

    import numpy as np

    union = None
    for m in layers:
        with Image.open(m) as im:
            arr = np.array(im.convert("L"))
        union = arr if union is None else np.maximum(union, arr)
    Image.fromarray(union, mode="L").save(out_path, format="PNG", optimize=True)
    return out_path


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
    2. ``_build_focus_mask`` 源图 region mask（精确 landmark / 真实人脸 bbox 锚定）
       → ``_focal_crop_bbox`` 像素 bbox
    3. bbox 扩到 cell 宽高比 → 裁剪存 PNG

    ③ 修复（before/after 对齐）：region mask 锚定**真实人脸解剖**（有 landmark 的
    部位用 MediaPipe landmark，其余相对真实人脸 bbox）而非整图，使术前/术后即便人脸
    在画面中位置/尺度不同，近景也按解剖对齐（根治川字术后偏上）。landmark 锚定的部位
    天然居中、shift 置 0（不再靠固定 shift 猜）；纯 coarse 部位保留标定 shift 留头顶白。
    两侧都检到人脸才启用对齐；任一侧检测失败 → 两侧都回退整图相对放置
    （保持 before/after 同口径，等价旧行为，绝不半对齐）。

    返回渲染端 ``closeup_section`` dict；任何失败返回 None（fail-open 不挡板）。
    """
    try:
        from PIL import Image, ImageOps

        from backend.ai_generation_adapter import _focal_crop_bbox
        from backend.services.classical_enhance import unsharp_focal_enhance
        from backend.services.precise_face_mask import detect_face_bbox, precise_region_for

        work_dir = Path(work_dir)
        aspect = cell_aspect_for(regions)
        sides = (("before", Path(before_path)), ("after", Path(after_path)))

        # ── Pass 1：EXIF 朝向先归一一次 + 检测真实人脸 bbox ──
        # L-145：generate_focus_mask 不做 exif_transpose（按 raw 像素放椭圆），而 unsharp 内部
        # 把输出归一到 display → 二者坐标系不一致：对 EXIF 旋转的手机竖拍源 mask bbox 会落到
        # 旋转前坐标 → 裁错。先归一一次喂 enhance+mask+detect 三方同坐标系。
        # orientation=1 源（如曾玲莉）归一=no-op，字节不变。detect_face_bbox 用 cv2.imread
        # 读 raw（不应用 EXIF），故必须喂已归一的 norm_src。
        norm: dict[str, Path] = {}
        faces: dict[str, tuple[int, int, int, int] | None] = {}
        for side, src in sides:
            side_dir = work_dir / side
            side_dir.mkdir(parents=True, exist_ok=True)
            norm_src = side_dir / "src_exif_norm.png"
            with Image.open(src) as _im:
                ImageOps.exif_transpose(_im).convert("RGB").save(norm_src, format="PNG")
            norm[side] = norm_src
            faces[side] = detect_face_bbox(norm_src)

        use_face = all(faces[s] is not None for s, _ in sides)
        if not use_face:
            logger.info(
                "closeup: 人脸 bbox 缺失（before=%s after=%s），回退整图相对放置（两侧同口径）",
                faces["before"], faces["after"],
            )

        # landmark 锚定的部位天然居中 → shift 置 0；纯 coarse（无 landmark）才用标定 shift
        # 留头顶白（owner ② 居中）。两侧同一 shift，保 before/after 同口径。
        landmark_anchored = use_face and any(precise_region_for(r) for r in regions)
        shift = 0.0 if landmark_anchored else center_shift_for(regions)

        # ── Pass 2：clarity 增强 + 解剖锚定 region mask + 裁剪 ──
        out: dict[str, str] = {}
        for side, _src in sides:
            side_dir = work_dir / side
            norm_src = norm[side]
            face_bbox = faces[side] if use_face else None
            enhanced = unsharp_focal_enhance(
                norm_src, focus_targets=list(regions), output_dir=side_dir, preset="clarity"
            )
            mask = _build_focus_mask(norm_src, list(regions), face_bbox, use_face, side_dir)
            bbox = _focal_crop_bbox(mask, pad_frac=CROP_PAD_FRAC)
            with Image.open(enhanced) as img:
                # norm_src/enhanced 均 display 朝向；exif_transpose 兜底（PNG 无 EXIF = no-op）
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
    regions = closeup_plan_regions(treatment_text)
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
