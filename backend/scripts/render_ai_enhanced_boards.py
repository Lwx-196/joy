#!/usr/bin/env python3
"""渲染 AI 增强版正式品牌板（正确管线）。

正确流程：
1. build_manifest() + render_from_manifest() 正常走对齐/背景/缩放 → 产出 516×624 术后 cell
2. after_transform 钩子在 cell 贴上画布前拦截 → 送 gpt-image-2 AI 增强
3. 增强后的图直接贴回品牌板 — 不重复经过对齐/缩放管线

这解决了旧版把 AI 增强图再过一遍渲染管线导致变形偏色的问题。

Usage:
    python3 backend/scripts/render_ai_enhanced_boards.py \
        --cases-root ~/Desktop/案例生成器/incoming/无创案例库/无创注射案例库 \
        --output-dir /tmp/ai-enhance-boards \
        --brand fumei \
        [--provider-order tuzi,flashapi] \
        [--customers 黄靖榕,陈艺琼] \
        [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import logging
import os
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path

from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

SKILL_ROOT = Path.home() / "Desktop" / "飞书Claude" / "skills" / "case-layout-board" / "scripts"
DEFAULT_CASES_ROOT = Path.home() / "Desktop" / "案例生成器" / "incoming" / "无创案例库" / "无创注射案例库"

ENHANCE_PROMPT_V1 = (
    "CRITICAL: Preserve patient identity exactly. The output must look like a REAL photograph "
    "of the SAME PERSON, not an AI-generated portrait.\n\n"
    "Task: Subtle quality enhancement for this post-treatment clinical photo.\n"
    "- Gently improve lighting uniformity and reduce harsh shadows on face\n"
    "- Preserve ALL original skin texture, pores, freckles, blemishes, moles\n"
    "- Preserve exact eye shape, facial structure, bone structure, lip shape\n"
    "- NO smoothing, NO over-brightening, NO skin whitening, NO plastic look\n"
    "- NO changes to hair, clothing, accessories, background\n"
    "- Maintain iPhone-native photo realism — the result should look like "
    "a better-lit version of the same photo, not an AI render\n"
    "- Keep natural skin redness, blood color undertones, pore visibility\n"
    "- Only adjust: subtle fill-light on shadow side, minor color temperature normalization\n"
    "Output a photograph indistinguishable from a real clinical photo taken with better lighting."
)

# env 文件搜索路径（sibling worktree 里的 tasks/）
_SIBLING_TASKS_DIRS = [
    Path.home() / "Desktop" / "案例生成器" / "case-workbench-effect-calibration" / "tasks",
    Path.home() / "Desktop" / "案例生成器" / "case-workbench-prod" / "tasks",
    Path.home() / "Desktop" / "案例生成器" / "case-workbench-async-simulate" / "tasks",
    Path.home() / "Desktop" / "案例生成器" / "case-workbench" / "tasks",
]

PROVIDER_ENV_FILES = {
    "tuzi": "tuzi_image.local.env",
    "flashapi": "flashapi_image.local.env",
    "77code": "77code_image.local.env",
    "vertex": "t54_vertex_adc.local.env",  # Vertex ADC 出图兜底（gemini-3-pro-image）
}

PROVIDER_PREFIX_REMAP = {
    "flashapi": "PANEL_IMG_FLASHAPI",
    "77code": "PANEL_IMG_77CODE",
}

# provider 名归一化：用户口语 "vertex adc" / "vertex-adc" 都映射到 registry key "vertex"
PROVIDER_NAME_ALIASES = {
    "vertex-adc": "vertex",
    "vertex_adc": "vertex",
    "vertexadc": "vertex",
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {name}: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _find_env_file(filename: str) -> Path | None:
    for d in _SIBLING_TASKS_DIRS:
        p = d / filename
        if p.is_file():
            return p
    return None


def _load_all_provider_envs(provider_order: list[str]) -> dict[str, str]:
    """加载所有需要的 provider env 到一个合并 dict。

    tuzi 用原始 TUZI_IMAGE_PRIMARY_* 前缀；
    flashapi/77code 需重映射到 PANEL_IMG_<NAME>_* 前缀让 image_providers 识别。
    """
    merged: dict[str, str] = {}
    for name in provider_order:
        env_filename = PROVIDER_ENV_FILES.get(name)
        if not env_filename:
            continue
        env_path = _find_env_file(env_filename)
        if not env_path:
            logger.warning("env file not found for %s: %s", name, env_filename)
            continue
        raw = _load_env_from_file(env_path)
        remap_prefix = PROVIDER_PREFIX_REMAP.get(name)
        if remap_prefix:
            for k, v in raw.items():
                if k.startswith("TUZI_IMAGE_PRIMARY_"):
                    new_key = k.replace("TUZI_IMAGE_PRIMARY_", f"{remap_prefix}_", 1)
                    merged[new_key] = v
        else:
            merged.update(raw)

    if len(provider_order) > 1:
        merged["PANEL_IMAGE_PROVIDERS"] = ",".join(provider_order)

    return merged


def _load_env_from_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def _pil_to_png_bytes(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _bytes_to_pil(data: bytes) -> Image.Image:
    return Image.open(BytesIO(data)).convert("RGB")


# 内容寻址缓存：key = sha256(送 AI 的 PNG 字节 + prompt)。只缓存成功结果，
# 失败不落盘 → 重跑只补失败的 cell，不重烧已成功的（应对 provider 过载）。
AI_CACHE_DIR = Path.home() / ".cache" / "case-workbench-ai-enhance-boards"


def _ai_cache_key(png_bytes: bytes, prompt: str) -> str:
    h = hashlib.sha256()
    h.update(png_bytes)
    h.update(b"\x00")
    h.update(prompt.encode("utf-8"))
    return h.hexdigest()


def _pad_to_square(img: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """把非正方形图 pad 成正方形（居中，黑色填充）。

    gpt-image-2 会把输出强制为正方形，如果输入非正方形，内容会被缩放嵌入导致
    人物变小。pad 成正方形后输入输出尺寸一致，避免内容缩放。

    返回 (padded_img, crop_box)，crop_box 用于增强后裁回原始尺寸。
    """
    w, h = img.size
    if w == h:
        return img, (0, 0, w, h)
    side = max(w, h)
    padded = Image.new("RGB", (side, side), (0, 0, 0))
    offset_x = (side - w) // 2
    offset_y = (side - h) // 2
    padded.paste(img, (offset_x, offset_y))
    crop_box = (offset_x, offset_y, offset_x + w, offset_y + h)
    return padded, crop_box


def _cell_content_coverage(img: Image.Image) -> float:
    """估算 cell 中照片内容覆盖率（vs 对齐填充背景）。

    对齐算法在源图缩放后填不满 cell 时，会用估算的背景色填充。
    这里通过检测边缘主色调 + 全图匹配来估算填充区域占比。

    返回 content_ratio (0~1)，越高表示照片填得越满。
    """
    import numpy as np

    arr = np.array(img.convert("RGB"))
    h, w = arr.shape[:2]

    border_pixels = np.concatenate([
        arr[:3, :].reshape(-1, 3),
        arr[-3:, :].reshape(-1, 3),
        arr[:, :3].reshape(-1, 3),
        arr[:, -3:].reshape(-1, 3),
    ])
    bg_color = np.median(border_pixels, axis=0).astype(int)

    diff = np.abs(arr.astype(int) - bg_color)
    bg_mask = np.all(diff < 25, axis=2)
    return 1.0 - (bg_mask.sum() / (h * w))


def _log_cell_coverage(before_img: Image.Image, after_img: Image.Image, slot: str) -> None:
    """仅记录术前/术后 cell 覆盖率（advisory），不做任何拒绝。

    一致性标准 = **治疗部位的可比性**，不是图片大小/缩放严格一致
    （owner 2026-06-04 明确纠正，见 ~/.claude/memory/feedback_board_consistency_principle.md；
    当天胡志超「图偏小但可接受」判 PASS）。所以"人偏小 / 术前术后大小不一致"
    绝不单独判 FAIL。覆盖率只作参考日志，真正的 FAIL（部位遮挡 / 角度太差 / 不同人 /
    分割伪影）交由 owner 看板人工裁决，或在分类/选片环节（case_layout_pick）砍掉坏角度。
    """
    b_cov = _cell_content_coverage(before_img)
    a_cov = _cell_content_coverage(after_img)
    logger.info("  [cov] %s: before=%.0f%% after=%.0f%% diff=%.0fpp (advisory, 不影响出图)",
                slot, b_cov * 100, a_cov * 100, abs(b_cov - a_cov) * 100)


def _largest_component(mask):
    """只保留最大连通域，去掉断开的背景残留岛（深色背景被误判为前景的情况）。

    98% 守卫：最大域已占前景 ≥98% 视为干净 → 原样返回，不改 mask 字节，
    从而保住其他客户已验证板的缓存命中（只有真有 bleed 的 mask 才被清理）。
    """
    import cv2
    import numpy as np

    m = (mask.astype(np.uint8) > 0).astype(np.uint8)
    num, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    if num <= 2:
        return mask  # 背景 + 至多 1 个前景域，无可清理
    areas = stats[1:, cv2.CC_STAT_AREA]
    total = int(areas.sum())
    if total == 0 or int(areas.max()) >= 0.98 * total:
        return mask
    largest_idx = 1 + int(np.argmax(areas))
    return labels == largest_idx


def _strip_blue_background(arr, mask):
    """从前景 mask 剔除「蓝主导 + 偏暗」像素 = 影棚蓝背景被误判为前景的部分。

    皮肤暖色 R>B、头发中性暗 R≈B 都不满足蓝主导 → 不误伤；只打蓝背景。
    干净案例 mask 里没有这种像素 → 返回原 mask（字节不变，保缓存）。
    """
    import numpy as np

    r = arr[..., 0].astype(np.int16)
    g = arr[..., 1].astype(np.int16)
    b = arr[..., 2].astype(np.int16)
    val = arr.max(axis=2)
    bluish_dark = (b > r + 10) & (b >= g) & (val < 130)
    if not bool(bluish_dark.any()):
        return mask
    return mask.astype(bool) & ~bluish_dark


_REMBG_SESSION = None


def _get_rembg_session():
    """懒加载 rembg 抠图 session（首次会下模型到 ~/.u2net/）。"""
    global _REMBG_SESSION
    if _REMBG_SESSION is None:
        from rembg import new_session
        _REMBG_SESSION = new_session(os.environ.get("CASE_REMBG_MODEL", "u2net_human_seg"))
    return _REMBG_SESSION


def _composite_on_black(pil_img: Image.Image, slot: str, case_layout) -> Image.Image:
    """人物分割 + 纯黑底还原（对齐后的 cell，不依赖源图背景颜色）。

    原始案例是深色影棚背景拍摄。这里抠出人物前景，把背景统一还原成
    纯黑 (0,0,0)，消除影棚背景的不均匀打光 / 阴影 / 光斑，得到干净一致的底。

    默认 MediaPipe + 蓝底剔除 + 最大连通域（对干净影棚底完美，已验 3 板靠它产出）。
    CASE_SEG_METHOD=rembg 时改用 rembg 人像 matte（对复杂背景更稳，但缕发在纯黑上偏散乱，
    且会改变 mask 字节使缓存失效）——留作硬背景源图的可选实验路径。
    """
    import numpy as np

    rgb = pil_img.convert("RGB")
    arr = np.array(rgb)
    alpha = None

    if os.environ.get("CASE_SEG_METHOD", "mediapipe").lower() == "rembg":
        try:
            from rembg import remove
            mask = remove(rgb, session=_get_rembg_session(), only_mask=True, post_process_mask=True)
            alpha = np.asarray(mask.convert("L"), dtype=np.float64) / 255.0
        except Exception as exc:  # noqa: BLE001 — rembg 失败回退 MediaPipe
            logger.warning("  rembg 抠图失败，回退 MediaPipe: %s", exc)

    if alpha is None:
        foreground_mask = case_layout.build_foreground_mask_for_face(arr, slot)
        foreground_mask = _strip_blue_background(arr, foreground_mask)
        foreground_mask = _largest_component(foreground_mask)
        foreground_keep = case_layout.dilated_foreground_mask(foreground_mask, padding_px=4)
        alpha = case_layout.soft_alpha_from_foreground(foreground_keep, feather_px=7)

    alpha = _strip_lower_white_clothing(arr, alpha)  # 精准剔除下部白色衣领/内衬光斑
    alpha = _fade_bottom_to_black(alpha)  # 底部渐隐，消除硬截断线
    filled = np.clip(arr.astype(np.float64) * alpha[..., None], 0, 255).astype(np.uint8)
    return Image.fromarray(filled)


def _strip_lower_white_clothing(arr, alpha, lower_from: float = 0.6):
    """下部（颈以下）剔除亮白低饱和像素 = 白色衣领/内衬光斑，不动肤色颈部。

    白领亮(val>195)且低饱和(sat<50)，颈部肤色饱和度更高 → 不误伤。羽化避免硬洞。
    只在确有白领的案例触发（黄阿红/郭若煊等深领案例 lower 区无亮白像素 → 原样返回）。
    """
    import cv2
    import numpy as np

    h = int(arr.shape[0])
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    val = hsv[..., 2].astype(np.int16)
    sat = hsv[..., 1].astype(np.int16)
    band = np.zeros(alpha.shape, dtype=bool)
    band[int(h * lower_from):] = True
    white = band & (val > 195) & (sat < 50)
    if not bool(white.any()):
        return alpha
    white_soft = cv2.GaussianBlur(white.astype(np.float64) * 255.0, (0, 0), sigmaX=3.0, sigmaY=3.0) / 255.0
    return alpha * np.clip(1.0 - white_soft, 0.0, 1.0)


def _fade_bottom_to_black(alpha, keep_frac: float = 0.72, black_frac: float = 0.83):
    """底部 alpha 渐隐到黑：彻底消除源图截断边缘的白色衣领/内衬光斑 + 明显硬截断线。

    人脸眼距对齐后人脸恒在上中部，颈以下（白色内衬/衣领光斑 + 硬截断）在 ~72% 以下。
    `keep_frac` 以上全保留（脸 + 上颈），`keep_frac`~`black_frac` 线性渐隐，`black_frac`
    以下纯黑——人物自然从黑色浮现，不留悬浮白块和硬截断线（肖像标准做法）。
    白领亮、单纯渐隐压不住（85% 处仍有 ~0.8 alpha），故 black_frac 以下直接归零。
    """
    import numpy as np

    h = int(alpha.shape[0])
    keep = max(0, min(h - 1, int(h * keep_frac)))
    black = max(keep + 1, min(h, int(h * black_frac)))
    ramp = np.ones(h, dtype=np.float64)
    if black > keep:
        ramp[keep:black] = np.linspace(1.0, 0.0, black - keep)
    ramp[black:] = 0.0
    return alpha * ramp[:, None]


def _enable_manual_pairing(case_layout) -> None:
    """让 AI-enhance lane 走人工配对逻辑：姿态门只警告不排除（绕过 auto 模式的姿态差排除）。

    对应 skill 里 `manual_pair` 分支的行为（case_layout_board.py:2019 姿态差大也保留该角度），
    也对应 owner 用 organize/pick 工具人工配对的工作流。单一明确的术前/术后对，姿态差超阈值
    也保留，交看板人工复核——一致性标准 = 治疗部位可比性（6-04），不是头部姿态严格一致。
    `CASE_AIENHANCE_POSE_GATE=strict` 可恢复严格 auto 排除。
    """
    if os.environ.get("CASE_AIENHANCE_POSE_GATE", "manual").lower() == "strict":
        return
    orig = case_layout.pose_delta_within_threshold

    def _manual_pass(slot, pose_delta):
        if not orig(slot, pose_delta):
            logger.info("  [pose] %s 姿态差超阈值(weighted=%.1f) → manual-pair 放行，交看板复核",
                        slot, float(pose_delta.get("weighted", -1)))
        return True

    case_layout.pose_delta_within_threshold = _manual_pass


def _apply_treatment_lock(after_img: Image.Image, enhanced_img: Image.Image,
                          focus_targets: list[str], slot: str, stats: dict) -> Image.Image:
    """gemini 出图后把效果只锁回治疗区，mask 外字节级保原图（matted after cell）。

    复用 effect 线已验证的 _apply_effect_mask_anchor（generate_focus_mask 治疗区椭圆 →
    向内羽化消缝 → Image.composite(ai 内, 原图 外)）。泛红/磨皮/漂发型/增殖痣/白领全在
    治疗区外 → 物理消除。失败安全：任何异常返回 enhanced_img（不阻断交付）。
    """
    if not focus_targets:
        logger.info("  [lock] %s: 无可识别治疗区 → 跳过锁定（保 gemini raw）", slot)
        return enhanced_img
    try:
        from backend import ai_generation_adapter as adp

        with tempfile.TemporaryDirectory(prefix="board-lock-") as td:
            tdp = Path(td)
            orig_p, ai_p, out_p = tdp / "after_orig.png", tdp / "after_ai.png", tdp / "after_locked.png"
            after_img.save(orig_p, format="PNG")
            enhanced_img.save(ai_p, format="PNG")
            locked_path = adp._apply_effect_mask_anchor(
                original_path=orig_p, ai_output_path=ai_p,
                focus_targets=focus_targets, output_path=out_p,
            )
            locked = Image.open(locked_path).convert("RGB")
            locked.load()  # tempdir 删除前读入内存
        logger.info("  [lock] %s: 治疗区 %s 锁定完成", slot, focus_targets)
        stats["locked"] = stats.get("locked", 0) + 1
        return locked
    except Exception as exc:  # noqa: BLE001 — 锁定失败不阻断，退回 raw
        logger.warning("  [lock] %s: 锁定失败(%s) → 返回 gemini raw", slot, exc)
        return enhanced_img


def _make_slot_transform(case_layout, providers, prompt: str, stats: dict, *,
                         dry_run: bool = False, use_cache: bool = True,
                         focus_targets: list[str] | None = None,
                         mask_lock: bool = False):
    """返回 slot_transform 回调，供 render_from_manifest 使用。

    回调签名: (before_img, after_img, slot) -> (before_img, after_img)
    流程：
    1. 术前 + 术后都做人物分割 + 纯黑底还原
    2. QA 校验术前/术后一致性
    3. 术后送 AI 增强（gpt-image-2）
    """
    from backend.services.image_providers import generate_with_fallback

    focus_targets = focus_targets or []

    def transform(before_img: Image.Image, after_img: Image.Image, slot: str
                  ) -> tuple[Image.Image, Image.Image]:
        stats["total"] += 1

        before_img = _composite_on_black(before_img, slot, case_layout)
        after_img = _composite_on_black(after_img, slot, case_layout)

        # advisory only：覆盖率仅记录，绝不因"人偏小/大小不一致"跳过 AI（部位可比性标准）
        _log_cell_coverage(before_img, after_img, slot)

        if dry_run:
            logger.info("  [DRY-RUN] %s: 跳过 AI 增强 (%dx%d)", slot, *after_img.size)
            stats["skipped"] += 1
            return before_img, after_img

        original_size = after_img.size
        logger.info("  [AI] %s: 送增强 (%dx%d) ...", slot, *original_size)
        t0 = time.time()

        try:
            padded_img, crop_box = _pad_to_square(after_img)
            png_bytes = _pil_to_png_bytes(padded_img)
            sq_side = padded_img.size[0]
            cache_path = AI_CACHE_DIR / f"{_ai_cache_key(png_bytes, prompt)}.png"

            if use_cache and cache_path.is_file():
                enhanced_raw = cache_path.read_bytes()
                provider_name = "cache"
            else:
                enhanced_raw, provider_name = generate_with_fallback(
                    providers, png_bytes, prompt, mime="image/png",
                )
                AI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(enhanced_raw)

            enhanced_img = _bytes_to_pil(enhanced_raw)
            if enhanced_img.size != (sq_side, sq_side):
                enhanced_img = enhanced_img.resize((sq_side, sq_side), Image.LANCZOS)
            enhanced_img = enhanced_img.crop(crop_box)
            elapsed = time.time() - t0
            logger.info("  [AI] %s: OK via %s (%.1fs)", slot, provider_name, elapsed)
            stats["ok"] += 1
            if mask_lock:
                enhanced_img = _apply_treatment_lock(
                    after_img, enhanced_img, focus_targets, slot, stats)
            return before_img, enhanced_img
        except Exception as exc:
            elapsed = time.time() - t0
            logger.error("  [AI] %s: FAILED (%.1fs): %s", slot, elapsed, exc)
            stats["failed"] += 1
            return before_img, after_img

    return transform


# 治疗区 → 循证效果短语（喂 native 强化器的 focus，planner 会细化为具体编辑步骤）。
_REGION_EFFECT = {
    "泪沟": "泪沟填充后更平整、黑眼圈减轻",
    "苹果肌": "苹果肌饱满度提升、轮廓更顺",
    "法令纹": "法令纹变浅、过渡更自然",
    "鼻基底": "鼻基底支撑提升、面中更立体",
    "下巴": "下巴线条更流畅、下面部更协调",
    "面颊": "面颊凹陷改善、轮廓更饱满",
    "太阳穴": "太阳穴凹陷填充、上面部更顺",
    "额头": "额部轮廓更饱满平整",
    "额纹": "额纹变浅、肤面更平整",
}


def _native_focus_targets(case_layout, prm, treatment: str):
    """从治疗名推导 native 强化器 focus_targets（parse_focus_targets 格式）。"""
    regions = prm.parse_procedures(treatment).get("all_regions", [])
    tokens = [f"{r}:{_REGION_EFFECT.get(r, r + '区域改善、过渡自然')}" for r in regions]
    return case_layout.parse_focus_targets(tokens)


# 背景暗像素地板阈值：max(RGB) < 此值 → 纯黑（灭 rembg 误留的暗背景缝；远低于最暗五官眉≈125）。
_BLACK_FLOOR = 50


def _rembg_composite_on_black(pil_img: Image.Image) -> Image.Image:
    """rembg 语义抠图 → 纯黑底；不做 _fade_bottom_to_black 截断 / _strip_lower_white_clothing 剔除。

    根因（2026-06-06 owner 揪出）：_composite_on_black 的那两个去白领 hack 会截断下颌/颈、
    且 MediaPipe 前景 mask 留脏边——污染人物。rembg 人像分割完整保人物（含肩/领自然过渡），
    边缘干净、无截断，对 studio 深底/灰底都稳。
    """
    import numpy as np
    from rembg import remove

    rgb = pil_img.convert("RGB")
    mask = remove(rgb, session=_get_rembg_session(), only_mask=True, post_process_mask=True)
    m = np.asarray(mask.convert("L"), dtype=np.float64)
    # 保 rembg 软边（消锯齿），但用最大连通域把背景残岛/漏光软边压到 0 → 背景纯黑。
    kept = _largest_component(m > 110)
    kept = np.asarray(kept if isinstance(kept, np.ndarray) else (m > 110), dtype=bool)
    alpha = (m / 255.0) * kept
    arr = np.asarray(rgb, dtype=np.float64)
    out = np.clip(arr * alpha[..., None], 0, 255).astype(np.uint8)
    # 背景纯黑兜底①：rembg 会把 studio 暗背景布误判前景(mask=255)，留下 ~8-17 的暗块与
    # 纯黑(0)交界成「拼接缝」（2026-06-06 owner 揪出）。暗背景(≤17)与最暗五官(眉≈125)鸿沟
    # 极大 → 全图暗像素地板：max(RGB)<_BLACK_FLOOR 的像素归 0，统一灭平背景，绝不伤五官/肤质。
    out[out.max(axis=2) < _BLACK_FLOOR] = 0
    # 背景纯黑兜底②：背景布被光照到的「亮折边」>地板阈值会留成竖线残痕；地板清平暗背景后
    # 它成了被黑色隔开的孤立岛 → 取最大非黑连通域只留人物，灭折边残线（无 98% 守卫）。
    import cv2
    nonblack = (out.max(axis=2) > 0).astype(np.uint8)
    num, labels, stats, _ = cv2.connectedComponentsWithStats(nonblack, connectivity=8)
    if num > 2:
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        out[labels != largest] = 0
    return Image.fromarray(out)


def _make_matte_black_transform(case_layout, stats: dict):
    """matte + 纯黑底 slot_transform；native-enhance 用（AI 已在 apply_after_enhancements 做完，这里只抠黑底）。

    用 rembg 干净抠图（_rembg_composite_on_black），不走 _composite_on_black 的截断/白领 hack。
    """
    def transform(before_img: Image.Image, after_img: Image.Image, slot: str
                  ) -> tuple[Image.Image, Image.Image]:
        stats["total"] += 1
        before_img = _rembg_composite_on_black(before_img)
        after_img = _rembg_composite_on_black(after_img)
        stats["ok"] += 1
        return before_img, after_img
    return transform


def main() -> None:
    parser = argparse.ArgumentParser(description="渲染 AI 增强版品牌板（正确管线）")
    parser.add_argument("--cases-root", type=Path, default=DEFAULT_CASES_ROOT)
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/ai-enhance-boards"))
    parser.add_argument("--brand", default="fumei")
    parser.add_argument("--provider-order", default="flashapi,tuzi,vertex",
                        help="逗号分隔的 provider 优先级（默认: flashapi,tuzi,vertex；"
                             "vertex=Vertex ADC gemini-3-pro-image 兜底）")
    parser.add_argument("--customers", default="",
                        help="逗号分隔的客户名筛选（空=全部）")
    parser.add_argument("--dry-run", action="store_true",
                        help="跳过 AI 增强，只出标准品牌板（验证渲染管线）")
    parser.add_argument("--no-cache", action="store_true",
                        help="禁用 AI 结果缓存，强制重新增强（默认启用内容寻址缓存）")
    parser.add_argument("--mask-lock", action="store_true",
                        help="治疗区硬锁：gemini 出图后只锁回治疗区（parse_procedures 推导 all_regions），"
                             "mask 外字节级保原图——灭泛红/磨皮/漂发型/增殖痣")
    parser.add_argument("--native-enhance", action="store_true",
                        help="owner 管线：渲染器原生 focus-scoped 局部增强"
                             "(gpt-image-2 忠实 + 姿态锁 + 稳定回退) → matte 纯黑底；替代 gemini bolt-on")
    parser.add_argument("--enhance-model", default="gemini-3-pro-image-preview",
                        help="原生增强模型（默认 gemini-3-pro-image-preview 主力，需 t54 vertex ADC 在线，"
                             "4 案例验证；gpt-image-2=忠实零 vertex 依赖备选，gemini 失败时单角度退未增强原图）")
    parser.add_argument("--enhance-direction", default="heal", choices=["strict", "heal"],
                        help="增强方向：heal(默认)=恢复预览定向 prompt（身份锁不变 + 往恢复良好理想化，"
                             "4 案例验证一致安全）；strict=旧版忠实严格 prompt（只许极轻、偏保守）")
    args = parser.parse_args()

    if args.native_enhance:
        # 原生强化器是 node subprocess（继承 os.environ）：注入图像 creds + 顶掉写死的 gemini-4k 死模型。
        _enhance_env_files = ["tuzi_image.local.env", "flashapi_image.local.env"]
        if args.enhance_model.startswith("gemini"):
            # gemini 走 relay 的 vertex 成员（6-05「Vertex 404」是 -4k 死模型 id 而非鉴权问题；
            # 基础款 gemini-3-pro-image-preview 可达）。t54 ADC 若存在一并注入兜底。
            _enhance_env_files.append("t54_vertex_adc.local.env")
        for _env_fn in _enhance_env_files:
            _p = _find_env_file(_env_fn)
            if _p:
                os.environ.update(_load_env_from_file(_p))
        os.environ["CASE_LAYOUT_ENHANCE_MODEL"] = args.enhance_model
        os.environ["CASE_LAYOUT_ENHANCE_DIRECTION"] = args.enhance_direction
        print(f"  [native] enhance_model={args.enhance_model} direction={args.enhance_direction}")

    case_layout = _load_module("case_layout_board", SKILL_ROOT / "case_layout_board.py")
    _enable_manual_pairing(case_layout)  # 姿态门只警告不排除（人工配对逻辑，部位可比性标准）
    render_mod = _load_module("render_brand_clean", SKILL_ROOT / "render_brand_clean.py")
    if args.native_enhance:
        # 关掉渲染器 6-05 新增的「保护区对齐」：对 jawline/法令纹/鼻背 类治疗它会为框住治疗区把
        # 人脸缩成小块 + 留方框（2026-06-06 泛化暴露，郭若煊泪沟不命中关键词才侥幸满脸）。默认满脸
        # 对齐才是 owner 要的框法；focus_targets 仍用于增强的局部计划，不受影响。
        render_mod.collect_protection_targets = lambda *a, **k: []

    provider_order = [PROVIDER_NAME_ALIASES.get(x.strip(), x.strip())
                      for x in args.provider_order.split(",") if x.strip()]
    env = _load_all_provider_envs(provider_order)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from backend.services.image_providers import resolve_chain
    from backend.services import procedure_region_mappings as prm
    providers = resolve_chain(env, explicit=provider_order)
    logger.info("就绪 providers: %s", [p.name for p in providers])
    if not providers and not args.dry_run and not args.native_enhance:
        logger.error("没有就绪的 image provider，退出")
        sys.exit(1)

    brand = case_layout.resolve_brand(args.brand)
    customer_filter = set(x.strip() for x in args.customers.split(",") if x.strip()) if args.customers else None

    cases_root = args.cases_root
    case_dirs = sorted([
        d for d in cases_root.iterdir()
        if d.is_dir() and not d.name.startswith(".")
        and (customer_filter is None or d.name in customer_filter)
    ])

    logger.info("共 %d 个案例目录%s", len(case_dirs),
                f"（筛选: {customer_filter}）" if customer_filter else "")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for case_dir in case_dirs:
        customer = case_dir.name
        treatments = sorted([
            t for t in case_dir.iterdir()
            if t.is_dir() and not t.name.startswith(".")
        ])

        if not treatments:
            logger.info("SKIP %s — 无子目录", customer)
            continue

        for treatment_dir in treatments:
            treatment = treatment_dir.name
            print(f"\n{'=' * 50}")
            print(f"  {customer} / {treatment}")

            native_focus = _native_focus_targets(case_layout, prm, treatment) if args.native_enhance else None
            try:
                manifest = case_layout.build_manifest(
                    treatment_dir, brand, "tri-compare",
                    focus_targets=native_focus, semantic_judge_mode="off",
                )
            except Exception as exc:
                logger.warning("  build_manifest 失败: %s", exc)
                results.append({
                    "customer": customer, "treatment": treatment,
                    "status": "MANIFEST_FAILED", "error": str(exc)[:200],
                })
                continue

            stats = {"total": 0, "ok": 0, "failed": 0, "skipped": 0, "locked": 0}
            if args.native_enhance:
                print(f"  [native] focus = {[f['area'] for f in (native_focus or [])]}")
                if manifest.get("status") == "ok":
                    manifest["enhance_model"] = args.enhance_model
                    inspect_root = args.output_dir / ".native-enhance" / customer
                    try:
                        manifest = case_layout.apply_after_enhancements(
                            manifest, inspect_root, list(case_layout.ANGLE_SLOTS))
                        enh = manifest.get("enhancement", {})
                        print(f"  [native] 增强 {enh.get('generated_count')}/{len(case_layout.ANGLE_SLOTS)} "
                              f"fallback={enh.get('fallback_count')}")
                    except Exception as exc:
                        logger.error("  apply_after_enhancements 失败: %s", exc)
                else:
                    logger.warning("  manifest 非 ok（%s），跳过原生增强", manifest.get("status"))
                transform_fn = _make_matte_black_transform(case_layout, stats)
            else:
                focus_targets = prm.parse_procedures(treatment).get("all_regions", []) if args.mask_lock else []
                if args.mask_lock:
                    print(f"  [lock] 治疗区 focus_targets = {focus_targets}")
                transform_fn = _make_slot_transform(
                    case_layout, providers, ENHANCE_PROMPT_V1, stats,
                    dry_run=args.dry_run, use_cache=not args.no_cache,
                    focus_targets=focus_targets, mask_lock=args.mask_lock,
                )

            out_path = args.output_dir / f"{customer}_{treatment}_ai_enhanced.jpg"
            try:
                render_mod.render_from_manifest(
                    manifest, out_path, slot_transform=transform_fn,
                )
                status = "OK" if stats["failed"] == 0 else "PARTIAL"
                print(f"  ✅ {out_path} (增强 {stats['ok']}/{stats['total']})")
                results.append({
                    "customer": customer, "treatment": treatment,
                    "status": status, "board": str(out_path), **stats,
                })
            except Exception as exc:
                logger.error("  ❌ 渲染失败: %s", exc)
                results.append({
                    "customer": customer, "treatment": treatment,
                    "status": "RENDER_FAILED", "error": str(exc)[:200],
                })

    print(f"\n{'=' * 50}")
    ok = sum(1 for r in results if r["status"] in ("OK", "PARTIAL"))
    total = len(results)
    print(f"  完成: {ok}/{total}")

    out_manifest = args.output_dir / "boards_manifest.json"
    with open(out_manifest, "w", encoding="utf-8") as f:
        json.dump({"boards": results, "provider_order": provider_order}, f, ensure_ascii=False, indent=2)

    if ok > 0:
        print(f"\n  品牌板目录: {args.output_dir}")
        print(f"  manifest: {out_manifest}")


if __name__ == "__main__":
    main()
