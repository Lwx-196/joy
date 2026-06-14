#!/usr/bin/env python3
"""渲染 AI 增强版正式品牌板（全分辨率管线）。

管线（Phase 6 重构后）：
1. build_manifest() 拿到 after 源图路径（原始分辨率，如 3024×4032）
2. _enhance_manifest_sources() 在原图分辨率做 rembg + AI 增强 → 存盘 → 更新 manifest
3. render_from_manifest() 读增强后的全分辨率图做对齐/缩放到 516×624 → 组板
4. slot_transform 只做 rembg 纯黑底清理（不再在 cell 级做 AI）

消除旧管线「先缩到 516×624 再增强 = 低分辨率 = 颗粒」的问题。

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

from PIL import Image, ImageFilter

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

SKILL_ROOT = Path.home() / "Desktop" / "飞书Claude" / "skills" / "case-layout-board" / "scripts"
DEFAULT_CASES_ROOT = Path.home() / "Desktop" / "案例生成器" / "incoming" / "无创案例库" / "无创注射案例库"

# v2（2026-06-12，defect③(c) owner 拍板 a+b+c）：曾玲莉 front 增强坐实局部重绘色块
# （低频 diff>25 占 9.5% 像素遍布全脸）→ 新增禁局部重绘/重着色/肤色漂移条款。
# 注意：prompt 文本进 _ai_cache_key，改文本 = 换 key = 全量重烧，勿随意微调措辞。
ENHANCE_PROMPT_V2 = (
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
    "- Keep the OVERALL skin tone and lighting IDENTICAL to the source photo — "
    "no tone shift, no local color patches\n"
    "- Apply all adjustments GLOBALLY and uniformly across the frame; NEVER locally "
    "repaint, re-render, or recolor any region of the face or skin\n"
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
    "ai_studio": "t52_vlm_judge.local.env",
    "rsta": "rsta_image.local.env",
    "tuzi": "tuzi_image.local.env",
    "flashapi": "flashapi_image.local.env",
    "77code": "77code_image.local.env",
    "vertex": "t52_vlm_judge.local.env",
}

PROVIDER_PREFIX_REMAP = {
    "rsta": "PANEL_IMG_RSTA",
    "flashapi": "PANEL_IMG_FLASHAPI",
    "77code": "PANEL_IMG_77CODE",
}

AI_STUDIO_KEY_REMAP = {
    "CASE_WORKBENCH_VLM_JUDGE_API_KEY": "GOOGLE_GENAI_API_KEY",
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
        if name == "ai_studio":
            for k, v in raw.items():
                mapped = AI_STUDIO_KEY_REMAP.get(k)
                if mapped:
                    merged[mapped] = v
            continue
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


def _ai_cache_key(png_bytes: bytes, prompt: str, size_sig: str = "") -> str:
    h = hashlib.sha256()
    h.update(png_bytes)
    h.update(b"\x00")
    h.update(prompt.encode("utf-8"))
    if size_sig:
        # 4K 接入（owner 2026-06-11 拍板）：请求分辨率配置掺进 key，否则旧低清缓存
        # 恒命中、新 size 参数永不生效。空 sig = 与历史 key 完全一致（零重烧）。
        h.update(b"\x00")
        h.update(size_sig.encode("utf-8"))
    return h.hexdigest()


def _chain_size_sig(providers) -> str:
    """请求分辨率签名（掺进 AI cache key）：取链中首个配置了 sizes 的 provider。

    注意：sig 表达的是「请求意图」——若链首 hi-res provider 失败 fallback 到无 size 配置
    的备选腿，低清结果会缓存在 hi-res key 下（与今日混合分辨率缓存语义一致，不另区分）。
    """
    for p in providers:
        sizes = getattr(p, "sizes", ()) or ()
        if sizes:
            return f"{','.join(sizes)}@{getattr(p, 'quality', '') or 'default'}"
    return ""


# gpt-image-2 原生尺寸约束（2026-04 规格，联网核实）：最长边 ≤3840、宽高均 16 的倍数、
# 宽高比 ≤3:1、总像素 655,360–8,294,400。>2560×1440 官方标 experimental。
GPT_IMAGE2_MAX_EDGE = 3840
GPT_IMAGE2_MAX_PIXELS = 8_294_400
GPT_IMAGE2_MIN_PIXELS = 655_360
GPT_IMAGE2_MAX_AR = 3.0
_ADAPTIVE_FALLBACK_SIZE = "2560x2560"


def _adaptive_4k_enabled() -> bool:
    """自适应 4K 开关（owner 翻开才生效）。默认 off = 旧 pad_to_square + 固定 size 行为字节一致。"""
    return os.environ.get("CASE_WORKBENCH_ADAPTIVE_4K", "").strip().lower() in ("1", "true", "yes")


def _adaptive_target_size(w: int, h: int) -> str:
    """按源图比例取「贴 4K 预算上限」的合法 gpt-image-2 尺寸（同比例，不强制方形）。

    横版源得横版 size、竖版得竖版——根治「固定竖版 size 把横版拉伸」。退化输入回退安全方形。
    """
    import math
    if w <= 0 or h <= 0:
        return _ADAPTIVE_FALLBACK_SIZE
    # 1) 钳宽高比到 [1/3, 3]
    if w / h > GPT_IMAGE2_MAX_AR:
        h = round(w / GPT_IMAGE2_MAX_AR)
    elif h / w > GPT_IMAGE2_MAX_AR:
        w = round(h / GPT_IMAGE2_MAX_AR)
    # 2) 放大到「最长边=3840」或「像素=预算」的较紧约束
    scale = min(GPT_IMAGE2_MAX_EDGE / max(w, h),
                math.sqrt(GPT_IMAGE2_MAX_PIXELS / (w * h)))
    tw = max(16, int(round(w * scale / 16)) * 16)
    th = max(16, int(round(h * scale / 16)) * 16)
    # 3) 16 取整可能越界 → 收边长再收预算
    tw = min(tw, GPT_IMAGE2_MAX_EDGE)
    th = min(th, GPT_IMAGE2_MAX_EDGE)
    while tw * th > GPT_IMAGE2_MAX_PIXELS:
        if tw >= th:
            tw -= 16
        else:
            th -= 16
    # 4) 极小源图低于像素下限 → 回退安全方形
    if tw < 16 or th < 16 or tw * th < GPT_IMAGE2_MIN_PIXELS:
        return _ADAPTIVE_FALLBACK_SIZE
    return f"{tw}x{th}"


def _restore_to_original(enh: Image.Image, orig_size: tuple[int, int]) -> Image.Image:
    """自适应路径复原：模型返回比例与请求基本一致，cover 缩放 + 居中裁回原始尺寸，绝不拉伸。"""
    ow, oh = orig_size
    ew, eh = enh.size
    if (ew, eh) == (ow, oh):
        return enh
    scale = max(ow / ew, oh / eh)
    rw, rh = max(ow, round(ew * scale)), max(oh, round(eh * scale))
    enh = enh.resize((rw, rh), Image.LANCZOS)
    left, top = (rw - ow) // 2, (rh - oh) // 2
    return enh.crop((left, top, left + ow, top + oh))


def _pad_to_square(img: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """把非正方形图 pad 成正方形（居中，模糊背景填充）。

    gpt-image-2 会把输出强制为正方形，如果输入非正方形，内容会被缩放嵌入导致
    人物变小。pad 成正方形后输入输出尺寸一致，避免内容缩放。

    填充改进（owner 2026-06-12 拍板 d，根治构图漂移）：纯黑 letterbox 会被模型
    当"非内容"吃掉并重新构图——曾玲莉 front rsta 2560²+V2 两次重烧均人物放大
    +提亮（dL +19~27）实锤；6-12 批量 57 槽中横版 18 槽同机制暴露。改用源图
    cover 缩放 + 强高斯模糊填充，模型视为自然背景延续、保持原构图。pad 区在
    _restore_content_geometry 裁回时丢弃，不进最终像素。
    ⚠️ 填充字节变 = _ai_cache_key 变：非方形源图旧缓存全部失效（方形不受影响），
    重烧需 owner 算账拍板后执行。

    返回 (padded_img, crop_box)，crop_box 用于增强后裁回原始尺寸。
    """
    w, h = img.size
    if w == h:
        return img, (0, 0, w, h)
    side = max(w, h)
    # 模糊底在 256² 小图上构造再放大（大半径模糊的降采样近似，快一个数量级）：
    # cover 缩放到 256² → 高斯模糊 → 放大到 side²
    bg_s = 256
    scale = bg_s / min(w, h)
    sw, sh = max(bg_s, round(w * scale)), max(bg_s, round(h * scale))
    small = img.resize((sw, sh), Image.BILINEAR)
    left, top = (sw - bg_s) // 2, (sh - bg_s) // 2
    small = small.crop((left, top, left + bg_s, top + bg_s))
    small = small.filter(ImageFilter.GaussianBlur(radius=8))
    padded = small.resize((side, side), Image.BILINEAR).convert("RGB")
    offset_x = (side - w) // 2
    offset_y = (side - h) // 2
    padded.paste(img, (offset_x, offset_y))
    crop_box = (offset_x, offset_y, offset_x + w, offset_y + h)
    return padded, crop_box


def _restore_content_geometry(enh: Image.Image, sq: int,
                              crop_box: tuple[int, int, int, int],
                              slot: str) -> Image.Image:
    """把 AI 返回图复原到 crop_box 内容几何，绝不改变内容纵横比。

    pad 成正方形送审后，provider 有两种返回形态（2026-06-10 批量实锤）：
    - 正方形：视为保留 pad 的原构图 → resize 回 sq² 后裁 crop_box
    - 非正方形：模型丢弃 pad 按内容比例出图（tuzi/flashapi 均出现）→
      视为 crop_box 区域的内容，等比 cover 缩放 + 居中裁。
      此前盲 resize((sq, sq)) 会把竖图内容横向拉宽 25-37%。
    """
    w, h = enh.size
    if w == h:
        if (w, h) != (sq, sq):
            enh = enh.resize((sq, sq), Image.LANCZOS)
        return enh.crop(crop_box)

    cw = crop_box[2] - crop_box[0]
    ch = crop_box[3] - crop_box[1]
    scale = max(cw / w, ch / h)
    rw = max(cw, round(w * scale))
    rh = max(ch, round(h * scale))
    logger.info("  [geometry] %s: 返回 %dx%d 非正方形 → 等比复原 %dx%d → 居中裁 %dx%d",
                slot, w, h, rw, rh, cw, ch)
    enh = enh.resize((rw, rh), Image.LANCZOS)
    left = (rw - cw) // 2
    top = (rh - ch) // 2
    return enh.crop((left, top, left + cw, top + ch))


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
    """保留面积 ≥ 最大连通域 1% 的所有组件，去掉微小背景残岛。

    侧面角度鼻尖等身体部位可能与主体断裂形成独立岛，面积远超 1% 阈值会保留。
    98% 守卫：最大域已占前景 ≥98% 视为干净 → 原样返回，不改 mask 字节。
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
    largest_area = int(areas.max())
    min_area = max(1, int(largest_area * 0.01))
    keep = np.zeros(labels.shape, dtype=bool)
    for i in range(1, num):
        if stats[i, cv2.CC_STAT_AREA] >= min_area:
            keep |= (labels == i)
    return keep


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


def _guided_filter(guide, src, radius, eps):
    """He et al. guided filter — edge-aware alpha refinement using image structure."""
    import cv2
    ksize = (2 * radius + 1, 2 * radius + 1)
    mean_g = cv2.boxFilter(guide, -1, ksize)
    mean_s = cv2.boxFilter(src, -1, ksize)
    cov_gs = cv2.boxFilter(guide * src, -1, ksize) - mean_g * mean_s
    var_g = cv2.boxFilter(guide * guide, -1, ksize) - mean_g * mean_g
    a = cov_gs / (var_g + eps)
    b = mean_s - a * mean_g
    return cv2.boxFilter(a, -1, ksize) * guide + cv2.boxFilter(b, -1, ksize)


def _get_rembg_session():
    """懒加载 rembg 抠图 session（首次会下模型到 ~/.u2net/）。"""
    global _REMBG_SESSION
    if _REMBG_SESSION is None:
        from rembg import new_session
        _REMBG_SESSION = new_session(os.environ.get("CASE_REMBG_MODEL", "birefnet-portrait"))
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
        foreground_keep = case_layout.dilated_foreground_mask(foreground_mask, padding_px=12)
        alpha = case_layout.soft_alpha_from_foreground(foreground_keep, feather_px=10)

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


def _log_manifest_pose_info(manifest: dict) -> None:
    """从 manifest 提取并打印每个角度的姿态配对信息（含被拒绝的角度及原因）。"""
    groups = manifest.get("groups", [])
    for group in groups:
        selected = group.get("selected_slots", {})
        rejections = group.get("rejection_reasons", [])
        for slot in ("front", "oblique", "side"):
            if slot in selected:
                pd = selected[slot].get("pose_delta", {})
                logger.info("  [pose] %s: yaw=%.1f pitch=%.1f weighted=%.1f → PASS",
                            slot, pd.get("yaw", 0), pd.get("pitch", 0), pd.get("weighted", 0))
            else:
                pose_rej = [r for r in rejections
                            if r.get("slot") == slot and r.get("reason") == "pose_delta_exceeded"]
                if pose_rej:
                    logger.warning("  [pose] %s: 姿态差超阈值 → 已排除 (%s)", slot, pose_rej[0].get("detail", ""))
                else:
                    no_cand = [r for r in rejections if r.get("slot") == slot]
                    if no_cand:
                        logger.info("  [pose] %s: 排除 (%s)", slot, no_cand[0].get("reason", "unknown"))


def _hybrid_pose_revalidate(manifest: dict, case_layout) -> dict:
    """用 pose_backend hybrid 模式二次校验 manifest 中选中的配对，侧脸比 FaceMesh 更准。

    超阈值的 slot 从 manifest 中移除（降级 template）。fail-open：pose_backend 不可用时跳过。
    """
    try:
        from backend.services.pose_backend import pose_backend_mode
        mode = pose_backend_mode()
        if mode == "facemesh":
            return manifest
        logger.info("  [pose-hybrid] 二次校验 mode=%s", mode)
    except ImportError:
        return manifest

    try:
        if mode == "hybrid":
            from backend.services.pose_backend import HybridPoseBackend
            backend = HybridPoseBackend()
        elif mode == "sixdrep":
            from backend.services.pose_backend import SixDRepNetBackend
            backend = SixDRepNetBackend()
        else:
            return manifest
    except Exception as exc:
        logger.warning("  [pose-hybrid] backend 初始化失败(%s) → 跳过二次校验", exc)
        return manifest

    from PIL import Image
    groups = manifest.get("groups", [])
    for group in groups:
        selected = group.get("selected_slots", {})
        to_remove = []
        for slot, info in selected.items():
            before_path = info.get("before", {}).get("path")
            after_path = info.get("after", {}).get("path")
            if not before_path or not after_path:
                continue
            try:
                before_pose = backend.estimate(Image.open(before_path))
                after_pose = backend.estimate(Image.open(after_path))
                if before_pose.yaw is None or after_pose.yaw is None:
                    continue
                delta = case_layout.compute_pose_delta(
                    {"yaw": before_pose.yaw, "pitch": before_pose.pitch or 0, "roll": before_pose.roll or 0},
                    {"yaw": after_pose.yaw, "pitch": after_pose.pitch or 0, "roll": after_pose.roll or 0},
                )
                if not case_layout.pose_delta_within_threshold(slot, delta):
                    logger.warning("  [pose-hybrid] %s: 二次校验不通过 (yaw=%.1f weighted=%.1f) → 移除",
                                   slot, delta["yaw"], delta["weighted"])
                    to_remove.append(slot)
                else:
                    logger.info("  [pose-hybrid] %s: 二次校验 PASS (yaw=%.1f weighted=%.1f)",
                                slot, delta["yaw"], delta["weighted"])
            except Exception as exc:
                logger.warning("  [pose-hybrid] %s: 估计失败(%s) → 保留原判", slot, exc)
        for slot in to_remove:
            del selected[slot]
            group.setdefault("rejection_reasons", []).append({
                "group_name": group.get("group_name", ""),
                "slot": slot, "phase": None,
                "reason": "hybrid_pose_revalidation",
                "detail": f"{slot} hybrid pose 二次校验姿态差超阈值",
            })
    return manifest


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


def _fidelity_check(raw_img: Image.Image, enhanced_img: Image.Image,
                    slot: str, stats: dict) -> bool:
    """保真度探针（advisory only）：记录指标，不拒绝。

    vertex gemini 天然 smoothing（hf ratio 0.39-0.43），owner 已看过认可效果。
    像素级 fidelity 改为 advisory 日志，真正质量门交给 VLM 审核。
    """
    import numpy as np

    try:
        from backend.services.fidelity_probes import compute_fidelity_probes, prescreen_verdict
    except ImportError:
        return True

    try:
        with tempfile.TemporaryDirectory(prefix="fidelity-") as td:
            tdp = Path(td)
            raw_p, enh_p, mask_p = tdp / "raw.png", tdp / "enh.png", tdp / "mask.png"
            raw_img.save(raw_p, format="PNG")
            enhanced_img.save(enh_p, format="PNG")
            gray = np.asarray(raw_img.convert("L"))
            Image.fromarray(((gray > 10).astype(np.uint8) * 255)).save(mask_p, format="PNG")
            probes = compute_fidelity_probes(raw_p, enh_p, mask_p)
            verdict = prescreen_verdict(probes)
        if not verdict["passed"]:
            logger.info("  [fidelity] %s: ADVISORY — %s (不拒绝，等 VLM 审核)",
                        slot, "; ".join(verdict["reasons"]))
            stats["fidelity_advisory"] = stats.get("fidelity_advisory", 0) + 1
        else:
            logger.info("  [fidelity] %s: PASS (hf=%.2f luma=%.1f bg_delta=%.1f)",
                        slot, probes["hf_energy_ratio"], probes["luma_signed_shift"],
                        probes["out_mask_mean_abs_delta"])
        return True
    except Exception as exc:
        logger.warning("  [fidelity] %s: 探针异常(%s) → 放行", slot, exc)
        return True


def _classical_fallback(after_img: Image.Image, focus_targets: list[str],
                        slot: str, stats: dict) -> Image.Image | None:
    """AI 增强失败或保真度拒绝时，尝试零 AI 古典锐化增强（clarity preset）。

    成功返回增强后的 PIL Image；失败返回 None（调用方继续用未增强原图）。
    K-1 契约：classical_enhance 本身失败安全——返回原路径不抛异常。
    """
    try:
        from backend.services.classical_enhance import unsharp_focal_enhance
    except ImportError:
        logger.info("  [classical] %s: classical_enhance 不可用，跳过 fallback", slot)
        return None

    try:
        with tempfile.TemporaryDirectory(prefix="classical-fb-") as td:
            tdp = Path(td)
            src_p = tdp / "after_for_classical.png"
            after_img.save(src_p, format="PNG")
            out_p = unsharp_focal_enhance(
                src_p, focus_targets=focus_targets,
                output_dir=tdp / "classical_out", preset="clarity",
            )
            if out_p == src_p:
                logger.info("  [classical] %s: classical 返回原图（无增强效果）", slot)
                return None
            result = Image.open(out_p).convert("RGB")
            result.load()
        logger.info("  [classical] %s: classical clarity fallback 成功", slot)
        stats["classical_fallback"] = stats.get("classical_fallback", 0) + 1
        return result
    except Exception as exc:
        logger.warning("  [classical] %s: classical fallback 失败(%s)", slot, exc)
        return None


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
    size_sig = _chain_size_sig(providers)

    def transform(before_img: Image.Image, after_img: Image.Image, slot: str
                  ) -> tuple[Image.Image, Image.Image]:
        stats["total"] += 1

        before_img = _rembg_composite_on_black(before_img)
        after_img = _rembg_composite_on_black(after_img)

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
            cache_path = AI_CACHE_DIR / f"{_ai_cache_key(png_bytes, prompt, size_sig)}.png"

            if use_cache and cache_path.is_file():
                enhanced_raw = cache_path.read_bytes()
                provider_name = "cache"
            else:
                enhanced_raw, provider_name = generate_with_fallback(
                    providers, png_bytes, prompt, mime="image/png",
                )
                AI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(enhanced_raw)

            enhanced_img = _restore_content_geometry(
                _bytes_to_pil(enhanced_raw), sq_side, crop_box, slot)
            elapsed = time.time() - t0
            logger.info("  [AI] %s: OK via %s (%.1fs)", slot, provider_name, elapsed)

            fidelity_ok = _fidelity_check(after_img, enhanced_img, slot, stats)
            if not fidelity_ok:
                classical = _classical_fallback(after_img, focus_targets, slot, stats)
                if classical is not None:
                    return before_img, classical
                return before_img, after_img

            stats["ok"] += 1
            if mask_lock:
                enhanced_img = _apply_treatment_lock(
                    after_img, enhanced_img, focus_targets, slot, stats)
            return before_img, enhanced_img
        except Exception as exc:
            elapsed = time.time() - t0
            logger.error("  [AI] %s: FAILED (%.1fs): %s", slot, elapsed, exc)
            stats["failed"] += 1
            classical = _classical_fallback(after_img, focus_targets, slot, stats)
            if classical is not None:
                return before_img, classical
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
    """从治疗名推导 native 强化器 focus_targets（parse_focus_targets 格式）。

    用 atlas 增强：
    - 区域解析走 prm.parse_procedures（内部已用 atlas.extract_regions）
    - 每个区域附加 atlas 元数据（optimal_views / effect_signal / zone）供下游角度过滤
    """
    from backend.services import facial_region_atlas as atlas

    regions = prm.parse_procedures(treatment).get("all_regions", [])
    tokens = [f"{r}:{_REGION_EFFECT.get(r, r + '区域改善、过渡自然')}" for r in regions]
    targets = case_layout.parse_focus_targets(tokens)
    for t in targets:
        key = atlas.resolve_region_key(t.get("area", ""))
        if key:
            t["optimal_views"] = atlas.region_views(key)
            t["effect_signal"] = atlas.region_effect(key)
            t["zone"] = atlas.region_zone(key)
    return targets


# 背景暗像素地板阈值：max(RGB) < 此值 → 纯黑（灭 rembg 误留的暗背景缝）。
# 仅在 eroded person mask 外生效——安全区内保留所有暗五官（瞳孔/虹膜/鼻孔/深色发根）。
_BLACK_FLOOR = 50
_REMBG_MIN_SHORT_SIDE = 1024
_REMBG_FG_LEAK_THRESHOLD = 0.75


def _audit_eye_preservation(orig_arr, out_arr, kept_mask):
    """对比输入/输出眼部区域亮度，检测瞳孔/虹膜是否被异常涂黑（仅 warning，不阻塞）。"""
    import numpy as np

    rows = np.where(kept_mask.any(axis=1))[0]
    cols = np.where(kept_mask.any(axis=0))[0]
    if len(rows) == 0 or len(cols) == 0:
        return
    y0, y1 = int(rows[0]), int(rows[-1])
    x0, x1 = int(cols[0]), int(cols[-1])
    h, w = y1 - y0, x1 - x0
    if h < 40 or w < 40:
        return
    ey0 = y0 + int(h * 0.22)
    ey1 = y0 + int(h * 0.45)
    ex0 = x0 + int(w * 0.1)
    ex1 = x0 + int(w * 0.9)
    eye_mask = kept_mask[ey0:ey1, ex0:ex1]
    if not eye_mask.any():
        return
    orig_eye = orig_arr[ey0:ey1, ex0:ex1].astype(np.float64)
    out_eye = out_arr[ey0:ey1, ex0:ex1].astype(np.float64)
    orig_mean = float(orig_eye[eye_mask].mean())
    out_mean = float(out_eye[eye_mask].mean())
    if orig_mean > 20 and out_mean < orig_mean * 0.6:
        logger.warning(
            "  [pupil-audit] 眼部区域亮度显著下降 (%.1f → %.1f, -%.0f%%)，"
            "可能瞳孔/虹膜被误杀",
            orig_mean, out_mean, (1 - out_mean / orig_mean) * 100,
        )


def _alpha_defringe(arr_orig, alpha, *, edge_r: int | None = None,
                    erode_alpha_below: float = 0.12):
    """Matting 反解边缘去污染（defringe）+ alpha erosion（owner 6-11 实证 6 板白边/原色 halo）。

    根因：rembg 边缘半透明像素的源色 C = αF + (1-α)B；黑底合成 C·α 把亮背景 B 的
    (1-α)·B 份额留在输出 → 浅底案例人物边缘出现亮圈/原背景色 halo，与黑底形成两图层
    割裂感（_guided_filter 只精修 alpha 形状，不去边缘色污染）。

    修法（只动边缘带，人物实心内部与背景像素字节不变）：
    - defringe：边缘带 ∩ α∈[0.2, 0.995) 内按 F = (C - (1-α)·B) / α 精确反解前景色。
      B 取真背景像素大核归一化卷积的局部色场（影棚底有打光梯度，全局均值不够准），
      窗口内无背景回退全局背景均值。α < 0.2 不反解（除法噪声放大）——该尾巴由
      erosion + 下游 _BLACK_FLOOR 地板清理兜住（out ≤ 0.2·255 ≈ 51 ≈ 地板阈值）。
    - per-channel min 保证：F 只在「背景比前景亮」的通道生效（去亮污染），永不提亮
      —— 深底（B≈0）时 F≥C 恒取原值，存量深底板颜色字节恒等，零回归。
    - alpha erosion：α < erode_alpha_below 的最外圈估计不可靠，直接归零。
    - 边缘带 = support − erode(support, edge_r)，edge_r 取宽（默认 ≥17）以盖住
      guided-filter 后的整个半透明 ramp；深处内部纹理的 α 轻微波动点不受影响。

    Returns: (arr_fixed, alpha_fixed)；无背景可参考 / 无边界可修时原样返回输入。
    """
    import cv2
    import numpy as np

    h, w = alpha.shape
    support = (alpha > 0.0).astype(np.uint8)
    bg_mask = alpha < 0.02
    if not bg_mask.any() or not support.any():
        return arr_orig, alpha

    if edge_r is None:
        edge_r = max(17, min(h, w) // 50)
    interior = cv2.erode(support, np.ones((edge_r, edge_r), np.uint8)) > 0
    band = (support > 0) & ~interior & (alpha >= 0.2) & (alpha < 0.995)
    if not band.any():
        return arr_orig, alpha

    # 真背景局部色场：归一化卷积（boxFilter 加权平均），窗口内无背景像素回退全局均值
    k = max(31, (min(h, w) // 20) | 1)
    bgf = bg_mask.astype(np.float32)
    den = cv2.boxFilter(bgf, -1, (k, k), normalize=True)
    num = cv2.boxFilter(arr_orig.astype(np.float32) * bgf[..., None], -1,
                        (k, k), normalize=True)
    bg_global = arr_orig[bg_mask].mean(axis=0)
    bg_field = np.where(den[..., None] > 1e-3,
                        num / np.maximum(den[..., None], np.float32(1e-6)),
                        bg_global.astype(np.float32))

    a_band = alpha[band][:, None]
    c_band = arr_orig[band]
    f_band = (c_band - (1.0 - a_band) * bg_field[band]) / a_band
    f_band = np.minimum(np.clip(f_band, 0.0, 255.0), c_band)  # 只除亮污染，永不提亮

    arr_fixed = arr_orig.copy()
    arr_fixed[band] = f_band
    alpha_fixed = np.where(alpha < erode_alpha_below, 0.0, alpha)
    changed = int((np.abs(f_band - c_band).max(axis=1) > 1.0).sum())
    if changed:
        logger.info("  [defringe] band=%dpx changed=%dpx edge_r=%d erode<%.2f",
                    int(band.sum()), changed, edge_r, erode_alpha_below)
    return arr_fixed, alpha_fixed


def _blue_spill_suppress(arr, alpha, *, spill_r: int | None = None,
                         tol: float = 8.0):
    """边缘带蓝 spill 中和（2026-06-14 owner 选 A，临床蓝手术铺巾染发缘）。

    根因（cell repro harness 探针实锤，vs 旧 handoff hypothesis 修正）：蓝铺巾的
    环境光把人物发缘染上 teal tint，这些像素 α 中位≈1.0（较实心发缘，非低 α 漏网
    细发）、源色本就偏蓝（B≫R）。_alpha_defringe 用「背景色」反解 F=(C-(1-α)B)/α
    治的是半透明边混入的背景污染；但此处蓝是前景自带 tint，且本地背景恰是暗中性区
    （bg_field B-R≈0）→ reverse-solve 几乎不动蓝 → teal 撕裂带残留。

    修法（只压蓝、永不提亮 → 深底零回归）：
    - 只作用于「边缘带」= support − erode(support, spill_r)；实心内部与背景字节不变。
    - 参照「本地 interior 干净前景色场」fg_field（interior 像素色归一化卷积向外蔓延）：
      navy 上衣内部本就蓝 → 其边缘 fg_spill 高 → excess≈0 不动（保护衣服 / 任何
      合法蓝色边缘）；发/肤内部中性 → 发缘异常蓝被识别并压回本地前景蓝量。
    - blue spill 度量 = B − max(R,G)；excess = max(spill − max(fg_spill,0) − tol, 0)；
      out_B = B − excess。只减蓝通道 → out ≤ in 逐通道 → 合成后只会变暗，深底
      （边缘 spill≈0）excess≈0 字节恒等。
    - 局限（已知，归 owner scope）：离边 >spill_r 的深层实心蓝发（整片侧发被铺巾
      染色，源照灯光问题）不在边缘带内，本函数不强治——de-blue 实心前景会误伤
      navy 衣，超出 edge-fringe 清理范畴。

    Returns: arr_fixed（band 蓝被压的副本）；无边界 / 无参照可用时原样返回输入。
    """
    import cv2
    import numpy as np

    h, w = alpha.shape
    support = (alpha > 0.0).astype(np.uint8)
    if not support.any():
        return arr
    if spill_r is None:
        spill_r = max(24, min(h, w) // 22)
    interior = cv2.erode(support, np.ones((spill_r, spill_r), np.uint8)) > 0
    band = (support > 0) & ~interior
    if not band.any() or not interior.any():
        return arr

    # 本地 interior 干净前景色场：归一化卷积向外蔓延盖住 band
    k = max(31, (min(h, w) // 15) | 1)
    intf = interior.astype(np.float32)
    den = cv2.boxFilter(intf, -1, (k, k), normalize=True)
    num = cv2.boxFilter(arr.astype(np.float32) * intf[..., None], -1,
                        (k, k), normalize=True)
    fg_field = num / np.maximum(den[..., None], np.float32(1e-6))

    bm = band & (den > 1e-3)
    if not bm.any():
        return arr
    c = arr[bm].astype(np.float64)
    f = fg_field[bm].astype(np.float64)
    spill = c[:, 2] - np.maximum(c[:, 0], c[:, 1])        # 边缘像素蓝超量
    fg_spill = f[:, 2] - np.maximum(f[:, 0], f[:, 1])     # 本地干净前景自带蓝量
    excess = np.maximum(spill - np.maximum(fg_spill, 0.0) - tol, 0.0)

    arr_fixed = arr.copy()
    nb = arr_fixed[bm]
    nb[:, 2] = np.clip(c[:, 2] - excess, 0.0, 255.0)
    arr_fixed[bm] = nb
    changed = int((excess > 1.0).sum())
    if changed:
        logger.info("  [blue-spill] band=%dpx changed=%dpx spill_r=%d tol=%.0f maxΔB=%.0f",
                    int(bm.sum()), changed, spill_r, tol, float(excess.max()))
    return arr_fixed


def _rembg_composite_on_black(pil_img: Image.Image) -> Image.Image:
    """rembg 语义抠图 → 纯黑底；不做 _fade_bottom_to_black 截断 / _strip_lower_white_clothing 剔除。

    根因（2026-06-06 owner 揪出）：_composite_on_black 的那两个去白领 hack 会截断下颌/颈、
    且 MediaPipe 前景 mask 留脏边——污染人物。rembg 人像分割完整保人物（含肩/领自然过渡），
    边缘干净、无截断，对 studio 深底/灰底都稳。

    边缘 defringe + alpha erosion 见 _alpha_defringe（合成前最后一步，2026-06-11）。
    """
    import cv2
    import numpy as np
    from PIL import ImageOps
    from rembg import remove

    rgb = ImageOps.exif_transpose(pil_img).convert("RGB")
    w_orig, h_orig = rgb.size
    arr_orig = np.asarray(rgb, dtype=np.float64)

    short_side = min(h_orig, w_orig)
    if short_side < _REMBG_MIN_SHORT_SIDE:
        scale = _REMBG_MIN_SHORT_SIDE / short_side
        rgb_hi = rgb.resize((round(w_orig * scale), round(h_orig * scale)), Image.LANCZOS)
        logger.info("  [rembg-upsample] %dx%d → %dx%d", w_orig, h_orig, *rgb_hi.size)
    else:
        rgb_hi = rgb
        scale = 1.0

    session = _get_rembg_session()
    mask_hi = remove(rgb_hi, session=session, only_mask=True, post_process_mask=True)
    if scale > 1.0:
        mask = mask_hi.resize((w_orig, h_orig), Image.LANCZOS)
    else:
        mask = mask_hi
    m = np.asarray(mask.convert("L"), dtype=np.float64)

    kept = _largest_component(m > 110)
    kept = np.asarray(kept if isinstance(kept, np.ndarray) else (m > 110), dtype=bool)
    fg_ratio = float(kept.sum()) / kept.size

    if fg_ratio > _REMBG_FG_LEAK_THRESHOLD:
        mask_raw_hi = remove(rgb_hi, session=session, only_mask=True, post_process_mask=False)
        if scale > 1.0:
            mask_raw = mask_raw_hi.resize((w_orig, h_orig), Image.LANCZOS)
        else:
            mask_raw = mask_raw_hi
        m_raw = np.asarray(mask_raw.convert("L"), dtype=np.float64)
        chosen_thr = 200
        for thr in (140, 170, 200):
            if float((m_raw > thr).sum()) / m_raw.size < _REMBG_FG_LEAK_THRESHOLD:
                chosen_thr = thr
                break
        kept = _largest_component(m_raw > chosen_thr)
        kept = np.asarray(kept if isinstance(kept, np.ndarray) else (m_raw > chosen_thr), dtype=bool)
        alpha = (m_raw / 255.0) * kept
        logger.info(
            "  [rembg-bg-leak] fg@110=%.0f%% → raw thr=%d fg=%.0f%%",
            fg_ratio * 100, chosen_thr, float(kept.sum()) / kept.size * 100,
        )
    else:
        alpha = (m / 255.0) * kept
    # Guided-filter alpha refinement: snap mask edges to natural image boundaries,
    # eliminating jagged binary thresholding artifacts on nose/chin/hair edges.
    guide_gray = cv2.cvtColor(np.asarray(rgb, dtype=np.uint8), cv2.COLOR_RGB2GRAY)
    alpha_refined = _guided_filter(
        guide_gray.astype(np.float32) / 255.0,
        alpha.astype(np.float32),
        radius=6, eps=1e-4,
    )
    alpha = np.clip(alpha_refined, 0, 1).astype(np.float64)
    dilate_r = 5
    possible_fg = cv2.dilate(kept.astype(np.uint8),
                             np.ones((dilate_r, dilate_r), np.uint8)) > 0
    alpha[~possible_fg] = 0
    # Light-background bright-fringe suppression for off-center subjects.
    # Skipped when upsample was applied — higher-res mask is already clean enough.
    h_img, w_img = kept.shape
    fg_ratio = float(kept.sum()) / kept.size
    bg_region = ~kept
    if scale <= 1.0 and fg_ratio > 0.45 and bg_region.any():
        bg_brightness = float(arr_orig.max(axis=2)[bg_region].mean())
        if bg_brightness > 180:
            xs_fg = np.where(kept)[1]
            centroid_x = float(xs_fg.mean())
            centroid_offset = (centroid_x - w_img / 2.0) / w_img
            if abs(centroid_offset) > 0.05:
                orig_brightness = arr_orig.max(axis=2)
                bright_thr_base = bg_brightness - 80
                fringe_r = max(20, min(h_img, w_img) // 15)
                fringe_safe = cv2.erode(
                    kept.astype(np.uint8),
                    np.ones((fringe_r, fringe_r), np.uint8),
                ) > 0
                killed = 0
                if centroid_offset > 0:
                    cx_int = int(centroid_x)
                    bg_cols = np.arange(0, cx_int, dtype=np.float64)
                    if len(bg_cols) > 0:
                        dist_ratio = 1.0 - bg_cols / centroid_x
                        col_thr = 255.0 - dist_ratio * (255.0 - bright_thr_base)
                        kill_zone = (
                            kept[:, :cx_int]
                            & (orig_brightness[:, :cx_int] > col_thr[np.newaxis, :])
                            & ~fringe_safe[:, :cx_int]
                        )
                        alpha[:, :cx_int][kill_zone] = 0
                        killed = int(kill_zone.sum())
                else:
                    cx_int = int(centroid_x)
                    bg_cols = np.arange(cx_int, w_img, dtype=np.float64)
                    if len(bg_cols) > 0:
                        dist_ratio = (bg_cols - centroid_x) / (w_img - centroid_x)
                        col_thr = 255.0 - dist_ratio * (255.0 - bright_thr_base)
                        kill_zone = (
                            kept[:, cx_int:]
                            & (orig_brightness[:, cx_int:] > col_thr[np.newaxis, :])
                            & ~fringe_safe[:, cx_int:]
                        )
                        alpha[:, cx_int:][kill_zone] = 0
                        killed = int(kill_zone.sum())
                if killed > 0:
                    kept = alpha > 0
                    logger.info(
                        "  [rembg-fringe] bg_L=%.0f offset=%.1f%% killed=%d safe_r=%d",
                        bg_brightness, centroid_offset * 100, killed, fringe_r,
                    )
    # Defringe + alpha erosion（owner 6-11 实证白边/原色 halo）：黑底合成前反解边缘背景污染
    arr_fg, alpha = _alpha_defringe(arr_orig, alpha)
    # 蓝铺巾 spill 中和（2026-06-14 owner 选 A）：临床蓝手术铺巾环境光把发缘染蓝，
    # defringe 用背景色反解治不了前景自带蓝 tint → 边缘带按本地干净前景参照压蓝。
    arr_fg = _blue_spill_suppress(arr_fg, alpha)
    out = np.clip(arr_fg * alpha[..., None], 0, 255).astype(np.uint8)
    # 背景纯黑兜底①：erode person mask 建「安全区」——安全区内（瞳孔/虹膜/鼻孔/深色发根）
    # 保留所有暗像素，只在安全区外做地板清零灭 rembg 误留的暗背景缝。
    erode_r = max(10, out.shape[0] // 60)
    person_safe = cv2.erode(kept.astype(np.uint8),
                            np.ones((erode_r, erode_r), np.uint8)) > 0
    out[(out.max(axis=2) < _BLACK_FLOOR) & ~person_safe] = 0
    # 背景纯黑兜底②：背景布被光照到的「亮折边」>地板阈值会留成竖线残痕；地板清平暗背景后
    # 它成了被黑色隔开的孤立岛 → 取最大非黑连通域只留人物，灭折边残线（无 98% 守卫）。
    nonblack = (out.max(axis=2) > 0).astype(np.uint8)
    num, labels, stats, _ = cv2.connectedComponentsWithStats(nonblack, connectivity=8)
    if num > 2:
        areas = stats[1:, cv2.CC_STAT_AREA]
        largest_area = int(areas.max())
        min_area = max(1, int(largest_area * 0.01))
        for i in range(1, num):
            if stats[i, cv2.CC_STAT_AREA] < min_area:
                out[labels == i] = 0
    _audit_eye_preservation(arr_orig, out, kept)
    return Image.fromarray(out)


# 肤色亮度对齐参数（2026-06-10 偏白根因标定，FLAG=黄婧/赵12.23 vs ctrl 8 案例）
_LUM_SKIN_MIN_PIXELS = 500   # 肤色像素低于此回退全人物 mask（双方一致用同一种 mask）
_LUM_GAIN_MIN = 0.75         # 压暗下限：赵12.23 最重槽实测 gain 0.764
_LUM_GAIN_MAX = 1.3          # 提亮上限：沿用原值（AI+rembg 黑底压暗补偿）
_LUM_GAIN_DEADBAND = 0.04    # ±4% 内不动，避免无意义微调
# 肤色色度对齐参数（2026-06-14 defect③(a)：术前/术后源图灯光色温差 → LAB a/b 偏移）
_LUM_CHROMA_MAX_SHIFT = 8.0  # 单向最大位移（cv2 8-bit LAB a/b，1:1 真 CIELAB），避免抹平合理术前/术后差
_LUM_CHROMA_DEADBAND = 2.0   # |Δa|,|Δb| 均 < 2 不动，避免无意义微调


def _skin_mask(arr) -> "object":
    """肤色像素 mask：luma>60 且 R>B+10。

    黑/白衣物（中性 R≈B）、头发（暗）、黑底全排除 → 亮度统计直击人脸/颈。
    全人物 mask（max(RGB)>20）会被衣物占比稀释：黄婧黑毛衣案例全人物 Δ 只 +4.9，
    人脸实际偏白被掩盖；且 before/after 衣物占比差会让 gain 系统性跑偏。
    """
    luma = arr.mean(axis=2)
    return (luma > 60) & (arr[..., 0] > arr[..., 2] + 10)


def _match_face_luminance(ref_img: Image.Image, target_img: Image.Image) -> Image.Image:
    """术后肤色双向对齐术前：亮度(luminance) gain + 色度(chroma) LAB a/b 对齐。

    亮度（2026-06-10 偏白根因修复，保持不动）：
    - 术后偏暗（AI+rembg 黑底压暗）→ 提亮，gain ≤ 1.3
    - 术后偏白 → 压暗，gain ≥ 0.75。两种实锤偏白来源：
      ① AI 增强直接漂白人脸（赵12.23：enh skin 160-184 vs 术前 128-133）
      ② 旧全人物 mask 因 before/after 衣物占比差误判术后偏暗 → 1.3× boost
        把脸打白（黄婧：源级 skin 已持平，板上仍偏白）

    色度（2026-06-14 defect③(a)）：术前/术后源图灯光色温差 → 肤区 LAB a/b 均值偏移，
    叠加局部色块放大「面部颜色不一致」感知（曾玲莉 front 实锤）。把术后肤区 a/b 均值
    对齐术前，单向 clamp ±8 LAB 单位（避免抹平合理术前/术后差），deadband ±2。色度位移
    与亮度一致地按全人物应用并随后强制黑底；fail-safe：任何异常退回纯亮度结果。

    肤色 mask 任一侧不足 500px 时双方一起回退全人物 mask（禁止混用两种口径）。
    """
    import cv2
    import numpy as np

    ref = np.array(ref_img, dtype=np.float64)
    tgt = np.array(target_img, dtype=np.float64)

    ref_skin, tgt_skin = _skin_mask(ref), _skin_mask(tgt)
    if int(ref_skin.sum()) >= _LUM_SKIN_MIN_PIXELS and int(tgt_skin.sum()) >= _LUM_SKIN_MIN_PIXELS:
        ref_mask, tgt_mask, basis = ref_skin, tgt_skin, "skin"
    else:
        ref_mask = ref.max(axis=2) > 20
        tgt_mask = tgt.max(axis=2) > 20
        basis = "person"

    if not ref_mask.any() or not tgt_mask.any():
        return target_img

    # ① 亮度 gain（RGB 均值，2026-06-10 标定，不动）
    ref_lum = float(ref[ref_mask].mean())
    tgt_lum = float(tgt[tgt_mask].mean())
    if tgt_lum < 1:
        return target_img
    gain_raw = ref_lum / tgt_lum
    gain_active = abs(gain_raw - 1.0) >= _LUM_GAIN_DEADBAND
    gain = min(max(gain_raw, _LUM_GAIN_MIN), _LUM_GAIN_MAX) if gain_active else 1.0

    # ② 色度 da/db（LAB a/b 均值对齐 ref；在 gain 之后算，方向更准）。fail-safe。
    da = db = 0.0
    try:
        gained = np.clip(tgt * gain, 0, 255).astype(np.uint8)
        ref_lab = cv2.cvtColor(np.ascontiguousarray(ref.astype(np.uint8)), cv2.COLOR_RGB2LAB).astype(np.float64)
        out_lab = cv2.cvtColor(gained, cv2.COLOR_RGB2LAB).astype(np.float64)
        da_raw = float(ref_lab[..., 1][ref_mask].mean() - out_lab[..., 1][tgt_mask].mean())
        db_raw = float(ref_lab[..., 2][ref_mask].mean() - out_lab[..., 2][tgt_mask].mean())
        if abs(da_raw) >= _LUM_CHROMA_DEADBAND or abs(db_raw) >= _LUM_CHROMA_DEADBAND:
            da = min(max(da_raw, -_LUM_CHROMA_MAX_SHIFT), _LUM_CHROMA_MAX_SHIFT)
            db = min(max(db_raw, -_LUM_CHROMA_MAX_SHIFT), _LUM_CHROMA_MAX_SHIFT)
    except Exception as exc:  # noqa: BLE001 — 色度对齐失败安全，退回纯亮度
        logger.warning("  [lum-match] %s 色度对齐异常(%s) → 仅亮度", basis, exc)

    chroma_active = (da != 0.0 or db != 0.0)
    if not gain_active and not chroma_active:
        return target_img

    person = tgt.max(axis=2) > 20
    out = np.clip(tgt * gain, 0, 255).astype(np.uint8)
    if chroma_active:
        lab = cv2.cvtColor(out, cv2.COLOR_RGB2LAB).astype(np.float64)
        lab[..., 1] = np.clip(lab[..., 1] + da, 0, 255)
        lab[..., 2] = np.clip(lab[..., 2] + db, 0, 255)
        out = cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2RGB)
    out[~person] = 0
    logger.info("  [lum-match] %s before=%.0f after=%.0f gain=%.2f da=%.1f db=%.1f",
                basis, ref_lum, tgt_lum, gain, da, db)
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
        after_img = _match_face_luminance(before_img, after_img)
        stats["ok"] += 1
        return before_img, after_img
    return transform


# cache-miss 烧钱预估（F2，前端确认卡展示）：真 4K rsta 实测单槽 ~$0.057、生图 ~150s（NOW.md 6-12）。
_EST_BURN_USD_PER_SLOT = 0.057
_EST_BURN_SEC_PER_SLOT = 150


def _slot_cache_plan(orig_img, providers: list, prompt: str):
    """单槽 cache 计划：算 cache_path + 送 AI 的 png + size_override + 复原 callable。

    **实际增强（_enhance_manifest_sources）与前端预判（_check_cache_coverage）共用此函数**，
    保证「预测命中」与「实际查找」用同一把 key，杜绝 cache-miss 护栏自己产生预测漂移。
    restore(raw_pil, slot) 封装 adaptive（按比例复原）/ 非adaptive（去方形 pad 裁回）差异。
    """
    adaptive_quality = next((getattr(p, "quality", "") for p in providers
                             if getattr(p, "quality", "")), "default")
    if _adaptive_4k_enabled():
        target_size = _adaptive_target_size(*orig_img.size)
        png = _pil_to_png_bytes(orig_img)
        cache_key = _ai_cache_key(png, prompt, f"adaptive:{target_size}@{adaptive_quality}")
        def restore(raw_pil, _slot, _osize=orig_img.size):
            return _restore_to_original(raw_pil, _osize)
        return AI_CACHE_DIR / f"{cache_key}.png", png, target_size, restore
    padded, crop_box = _pad_to_square(orig_img)
    sq = padded.size[0]
    png = _pil_to_png_bytes(padded)
    cache_key = _ai_cache_key(png, prompt, _chain_size_sig(providers))
    def restore(raw_pil, slot, _sq=sq, _cb=crop_box):
        return _restore_content_geometry(raw_pil, _sq, _cb, slot)
    return AI_CACHE_DIR / f"{cache_key}.png", png, None, restore


def _check_cache_coverage(manifest: dict, providers: list, prompt: str) -> dict:
    """零成本预判前端单案例的 cache 覆盖（不调 API、不写盘，纯 ls）：用与实际增强相同的
    _slot_cache_plan 算每个待增强 after 槽的 cache_path，统计命中/未命中。

    与 _enhance_manifest_sources 同口径跳过 error 组（那些槽不会烧，故不计 miss）。
    返回 {total_slots, miss_count, miss_slots: [slot...]}。
    """
    from PIL import ImageOps
    total = 0
    miss: list[str] = []
    for group in manifest.get("groups", []):
        if (group.get("status") or "ok") != "ok":
            continue
        for slot, selection in (group.get("selected_slots") or {}).items():
            src_path = ((selection or {}).get("after") or {}).get("path")
            if not src_path or not Path(src_path).is_file():
                continue
            total += 1
            orig_img = ImageOps.exif_transpose(Image.open(src_path)).convert("RGB")
            cache_path, _png, _so, _restore = _slot_cache_plan(orig_img, providers, prompt)
            if not cache_path.is_file():
                miss.append(slot)
    return {"total_slots": total, "miss_count": len(miss), "miss_slots": miss}


def _enhance_manifest_sources(
    manifest: dict, providers: list, prompt: str,
    stats: dict, *, enhance_dir: Path, use_cache: bool = True,
    focus_targets: list[str] | None = None, mask_lock: bool = False,
) -> dict:
    """render_from_manifest 前，对 manifest 每个 after 源图做全分辨率 AI 增强。

    消除旧管线「先缩到 516×624 再增强」的颗粒感：
    ① EXIF 校正 → ② pad_to_square → ③ AI 增强（原图直送，不做 rembg——
    全分辨率 rembg 对蓝治疗垫/器械误留严重，rembg 只在 cell 级由 slot_transform 处理）
    → ④ crop_back → ⑤ 存盘 → ⑥ 写入 manifest enhancement.enhanced_path。
    """
    from backend.services.image_providers import generate_with_fallback

    enhance_dir.mkdir(parents=True, exist_ok=True)
    ft = focus_targets or []
    if _adaptive_4k_enabled():
        logger.info("  [fullres] 自适应 4K 已启用（按源图比例动态 size，原图直送不 pad）")

    for group in manifest.get("groups", []):
        # defect① 修复（2026-06-11）：error 组跳过要 WARNING + skipped 计数，ok 组照常。
        # 旧行为是 main() 按 manifest 顶级 status 整板静默跳增强——单组 blocking 把顶级
        # status 拉成 error，连累全板零增强但板照常渲出、无任何痕迹（江佳慧空目录根因）。
        g_status = group.get("status") or "ok"
        if g_status != "ok":
            n_slots = len(group.get("selected_slots") or {})
            logger.warning(
                "  [fullres] group %s status=%s（blocking=%d）→ 跳过该组 %d 槽增强（其余组照常）",
                group.get("name", "?"), g_status,
                len(group.get("blocking_issues") or []), n_slots,
            )
            stats["skipped"] += n_slots
            continue
        for slot, selection in group.get("selected_slots", {}).items():
            after_info = selection["after"]
            src_path = after_info["path"]
            if not Path(src_path).is_file():
                logger.warning("  [fullres] %s: 源图不存在 %s", slot, src_path)
                continue

            stats["total"] += 1
            from PIL import ImageOps
            orig_img = ImageOps.exif_transpose(Image.open(src_path)).convert("RGB")
            logger.info("  [fullres] %s: 源图 %dx%d → AI 增强", slot, *orig_img.size)

            # cache_path/png/size_override/restore 与 _check_cache_coverage 同源（_slot_cache_plan）：
            # 自适应 4K=按比例动态 size 原图直送；非adaptive=方形 pad。adaptive key 掺 per-image
            # size（adaptive: 前缀，与旧方形 key 永不碰撞）。
            cache_path, png, size_override, restore = _slot_cache_plan(orig_img, providers, prompt)

            t0 = time.time()
            try:
                if use_cache and cache_path.is_file():
                    raw = cache_path.read_bytes()
                    prov = "cache"
                else:
                    raw, prov = generate_with_fallback(providers, png, prompt,
                                                       mime="image/png", size_override=size_override)
                    AI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    cache_path.write_bytes(raw)

                enh = restore(_bytes_to_pil(raw), slot)

                elapsed = time.time() - t0
                logger.info("  [fullres] %s: OK via %s (%.1fs) %dx%d",
                            slot, prov, elapsed, *enh.size)

                if mask_lock and ft:
                    enh = _apply_treatment_lock(orig_img, enh, ft, slot, stats)

                out = enhance_dir / f"{Path(src_path).stem}_{slot}_enhanced.png"
                enh.save(out, format="PNG")
                after_info.setdefault("enhancement", {})["enhanced_path"] = str(out)
                stats["ok"] += 1

            except Exception as exc:
                elapsed = time.time() - t0
                logger.error("  [fullres] %s: FAILED (%.1fs): %s", slot, elapsed, exc)
                stats["failed"] += 1

    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="渲染 AI 增强版品牌板（正确管线）")
    parser.add_argument("--cases-root", type=Path, default=DEFAULT_CASES_ROOT)
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/ai-enhance-boards"))
    parser.add_argument("--brand", default="fumei")
    parser.add_argument("--provider-order", default="rsta,tuzi,flashapi,77code",
                        help="逗号分隔的 provider 优先级（默认: rsta,tuzi,flashapi,77code，"
                             "owner 2026-06-11 拍板 rsta 升第一选择；rsta 实测 size/quality 透传 2048²）")
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
    parser.add_argument("--enhance-model", default="gemini-3-pro-image",
                        help="原生增强模型（默认 gemini-3-pro-image 走 AI Studio；"
                             "失败时单角度退未增强原图）")
    parser.add_argument("--enhance-direction", default="heal", choices=["strict", "heal"],
                        help="增强方向：heal(默认)=恢复预览定向 prompt（身份锁不变 + 往恢复良好理想化，"
                             "4 案例验证一致安全）；strict=旧版忠实严格 prompt（只许极轻、偏保守）")
    parser.add_argument("--scale", type=float, default=2.0,
                        help="板输出分辨率倍率（默认 2.0 = 3840 宽 4K；1.0 = 1920 旧版）")
    parser.add_argument("--board-qa", action="store_true",
                        help="渲染后跑 D6 板级 VLM 质量门（白边/留白/标注缺陷 → held），需 Vertex ADC 凭证")
    parser.add_argument("--no-board-qa", action="store_true",
                        help="强制关闭 D6 板级 QA。注意：board-qa 默认会因 judge env 文件存在而自动启用"
                             "（见下方 `args.board_qa or _qa_env_p`）——前端 per-click 路径用此 flag 显式关闭，"
                             "避免每次点击烧 judge 钱（render_executor.run_ai_enhanced_render 默认带）")
    parser.add_argument("--case-dir", type=Path, default=None,
                        help="单案例入口（server 集成用）：直接渲染指定的治疗目录，绕过 --cases-root 遍历。"
                             "成功后 stdout 打印 'AI_BOARD_RESULT: <board.jpg>' 供父进程解析")
    parser.add_argument("--allow-cache-miss-burn", action="store_true",
                        help="授权 cache-miss 烧 API（F2）。默认 off：--case-dir 前端路径遇 cache-miss 不烧、"
                             "打印 'AI_BOARD_CACHE_MISS: <json>' 等用户确认。用户确认后父进程带此 flag 重入即真烧。"
                             "batch（--customers）不受此门约束（args.case_dir is None 直接放行）")
    args = parser.parse_args()

    if args.native_enhance:
        # 原生强化器是 node subprocess（继承 os.environ）：注入图像 creds + 顶掉写死的 gemini-4k 死模型。
        _enhance_env_files = ["tuzi_image.local.env", "flashapi_image.local.env"]
        if args.enhance_model.startswith("gemini"):
            _enhance_env_files.append("t52_vlm_judge.local.env")
        for _env_fn in _enhance_env_files:
            _p = _find_env_file(_env_fn)
            if _p:
                os.environ.update(_load_env_from_file(_p))
        os.environ["CASE_LAYOUT_ENHANCE_MODEL"] = args.enhance_model
        os.environ["CASE_LAYOUT_ENHANCE_DIRECTION"] = args.enhance_direction
        print(f"  [native] enhance_model={args.enhance_model} direction={args.enhance_direction}")

    case_layout = _load_module("case_layout_board", SKILL_ROOT / "case_layout_board.py")
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
    from backend.services import board_angle_gate
    from backend.services import board_closeup_section
    from backend.services import board_pair_gate
    from backend.services import procedure_region_mappings as prm
    providers = resolve_chain(env, explicit=provider_order)
    logger.info("就绪 providers: %s", [p.name for p in providers])
    if not providers and not args.dry_run and not args.native_enhance:
        logger.error("没有就绪的 image provider，退出")
        sys.exit(1)

    board_qa = None
    _qa_env_p = _find_env_file("t52_vlm_judge.local.env")
    if _qa_env_p:
        os.environ.update(_load_env_from_file(_qa_env_p))
    if (args.board_qa or _qa_env_p) and not args.no_board_qa:
        try:
            from backend.services.board_delivery_qa import BoardDeliveryQA
            from backend.services.vlm_provider import VLMProvider
            import sqlite3
            vlm = VLMProvider(env=dict(os.environ))
            db_path = os.environ.get("CASE_WORKBENCH_DB_PATH",
                                     str(Path(__file__).resolve().parent.parent.parent / "case-workbench.db"))
            qa_conn = sqlite3.connect(db_path)
            board_qa = BoardDeliveryQA(vlm, qa_conn, purpose="judge")
            logger.info("[board-qa] D6 VLM 质量门已就绪 (db=%s%s)",
                        db_path, "" if args.board_qa else "，凭证自动发现")
        except Exception as exc:
            logger.warning("[board-qa] 初始化失败(%s) → 禁用板级 QA", exc)

    brand = case_layout.resolve_brand(args.brand)
    customer_filter = set(x.strip() for x in args.customers.split(",") if x.strip()) if args.customers else None

    single_treatment_name = None
    if args.case_dir is not None:
        # 单案例入口（server 集成）：--case-dir 是治疗目录，customer = 其父目录名，只渲染这一个
        case_dir_p = args.case_dir.resolve()
        case_dirs = [case_dir_p.parent]
        single_treatment_name = case_dir_p.name
    else:
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
            and (single_treatment_name is None or t.name == single_treatment_name)
        ])

        if not treatments:
            logger.info("SKIP %s — 无子目录", customer)
            continue

        for treatment_dir in treatments:
            treatment = treatment_dir.name
            # 品牌交付标题：客户 日期 项目（parse_case_meta 已剥客户名前缀/提日期）
            _meta = render_mod.parse_case_meta(treatment_dir)
            _title_date = _meta["date"] if _meta["date"] != treatment else ""
            board_title = " ".join(
                x for x in (customer, _title_date, _meta["project"]) if x)
            # 标题方案 B（owner 拍板 2026-06-11）：结构化行同步进 manifest；
            # 解析 fail-open → None（原串 title 字段不动，下游回退单行）
            try:
                board_title_lines = render_mod.parse_title_b(
                    _meta["project"], customer=customer)
            except Exception:
                board_title_lines = None
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
                    "customer": customer, "treatment": treatment, "title": board_title,
                    "status": "MANIFEST_FAILED", "error": str(exc)[:200],
                })
                continue

            _log_manifest_pose_info(manifest)
            manifest = _hybrid_pose_revalidate(manifest, case_layout)

            # G1 角度覆盖 gate（审核标准 v1 B 条）：项目部位必需角度缺失 → 板级 HELD 不出板。
            # 零烧钱结构核对，在 AI 增强/渲染之前短路；fail-open 不误杀（详见 board_angle_gate）。
            available_views = {
                slot
                for group in (manifest.get("groups") or [])
                for slot, sel in (group.get("selected_slots") or {}).items()
                if sel
            }
            angle_gate = board_angle_gate.evaluate_angle_coverage(treatment, available_views)
            if angle_gate["verdict"] == board_angle_gate.VERDICT_HELD:
                _missing_desc = "；".join(
                    f"{m['region']}需{'|'.join(m['required_any_of'])}"
                    for m in angle_gate["missing"])
                print(f"  🚫 ANGLE_GATE_HELD: {_missing_desc}（板上实有 {sorted(available_views)}）")
                if args.case_dir is not None:
                    # 单案例入口（server 集成）：machine-parseable HELD 信号，供父进程
                    # 区分「质量保留 blocked」与「渲染失败」（aligned-render-pipeline WP2）。
                    # G1 在出板前短路，无诊断板（board=None）。
                    print("AI_BOARD_HELD: " + json.dumps(
                        {"gate": "angle", "reason": _missing_desc, "board": None},
                        ensure_ascii=False))
                results.append({
                    "customer": customer, "treatment": treatment, "title": board_title,
                    "status": "ANGLE_GATE_HELD", "angle_gate": angle_gate,
                    # 区分「有素材但缺角度」vs「manifest 本身没选出槽」（avail=[] 类）
                    "manifest_status": manifest.get("status"),
                })
                continue

            stats = {"total": 0, "ok": 0, "failed": 0, "skipped": 0, "locked": 0}
            if args.native_enhance:
                print(f"  [native] focus = {[f['area'] for f in (native_focus or [])]}")
                if args.dry_run:
                    print("  [native] --dry-run：跳过 AI 增强，只出标准板（验证管线/路径/creds）")
                elif manifest.get("status") == "ok":
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
                # F2 cache-miss 烧钱护栏（仅 --case-dir 前端单案例路径，batch 不受约束）：
                # 烧 API 前零成本预判 cache 覆盖，miss 且未授权 → 不烧、发 AI_BOARD_CACHE_MISS
                # 信号 + 预估 $/耗时给前端弹确认卡。用户确认后父进程带 --allow-cache-miss-burn 重入即真烧。
                if (args.case_dir is not None and not args.dry_run
                        and not args.allow_cache_miss_burn):
                    cov = _check_cache_coverage(manifest, providers, ENHANCE_PROMPT_V2)
                    if cov["miss_count"] > 0:
                        est_cost = round(cov["miss_count"] * _EST_BURN_USD_PER_SLOT, 3)
                        est_seconds = cov["miss_count"] * _EST_BURN_SEC_PER_SLOT
                        print("AI_BOARD_CACHE_MISS: " + json.dumps({
                            "miss_slots": cov["miss_slots"],
                            "miss_count": cov["miss_count"],
                            "total_slots": cov["total_slots"],
                            "est_cost_usd": est_cost,
                            "est_seconds": est_seconds,
                            "board": None,
                        }, ensure_ascii=False))
                        print(f"  💸 CACHE_MISS_HOLD: {cov['miss_count']}/{cov['total_slots']} 槽未命中 cache"
                              f"（预估 ${est_cost} / ~{est_seconds}s）→ 不烧，等前端确认")
                        results.append({
                            "customer": customer, "treatment": treatment, "title": board_title,
                            "status": "CACHE_MISS_HELD", "cache_miss": cov,
                        })
                        continue
                if not args.dry_run:
                    # defect① 修复（2026-06-11）：manifest 顶级 status 不再整板拦增强——
                    # 单组 blocking 即拉 error，旧条件整板静默跳过但板照常渲出（江佳慧
                    # .fullres-enhance 空目录根因）。改组粒度：error 组在
                    # _enhance_manifest_sources 内 WARNING + skipped 计数，ok 组照常。
                    if manifest.get("status") != "ok":
                        logger.warning(
                            "  manifest 非 ok（%s，blocking=%d）→ 按组粒度增强，error 组跳过",
                            manifest.get("status"),
                            len(manifest.get("blocking_issues") or []),
                        )
                    enhance_dir = args.output_dir / ".fullres-enhance" / customer
                    _enhance_manifest_sources(
                        manifest, providers, ENHANCE_PROMPT_V2, stats,
                        enhance_dir=enhance_dir, use_cache=not args.no_cache,
                        focus_targets=focus_targets, mask_lock=args.mask_lock,
                    )
                else:
                    logger.info("  [DRY-RUN] 跳过全分辨率 AI 增强")
                _cell_stats = {"total": 0, "ok": 0, "failed": 0, "skipped": 0, "locked": 0}
                transform_fn = _make_matte_black_transform(case_layout, _cell_stats)

            # G3 纹类近景对比区（审核标准 v1）：含真皱纹类项目（川字/额纹/法令纹）
            # 板尾追加近景 before/after 行。源 = front 槽原始源图 + 古典 clarity
            # （faithful-zoom arm A 产品化路径，零烧钱）。fail-open 不挡板。
            if not args.dry_run:
                closeup = board_closeup_section.build_for_manifest(
                    manifest, treatment,
                    args.output_dir / ".closeup-section" / f"{customer}_{treatment}",
                )
                if closeup:
                    manifest["closeup_section"] = closeup
                    print(f"  📸 近景对比区: {closeup['label']}（front 源图 clarity 裁剪）")

            out_path = args.output_dir / f"{customer}_{treatment}_ai_enhanced.jpg"
            try:
                render_mod.render_from_manifest(
                    manifest, out_path, slot_transform=transform_fn,
                    scale=args.scale,
                )

                # G2 配对 gate（审核标准 v1 C 条灾难级兜底）：front 终格眼距比
                # 出 [0.78, 1.30] → 板级 HELD 不交付（板文件保留供诊断）。
                # 信号 = render_from_manifest 写回的 render_plan pair_eye_signal，
                # 解析零成本；fail-open 不误杀（详见 board_pair_gate）。
                pair_gate = board_pair_gate.evaluate_pair_coverage(
                    manifest.get("render_plan"))
                if pair_gate["verdict"] == board_pair_gate.VERDICT_HELD:
                    _viol_desc = "；".join(
                        f"{v['slot']} eye_ratio={v['eye_ratio']}（允许 {v['allowed']}）"
                        for v in pair_gate["violations"])
                    print(f"  🚫 PAIR_GATE_HELD: {_viol_desc}")
                    if args.case_dir is not None:
                        # G2 板已渲（out_path 在）但不交付；保留诊断板路径供前端缩略展示。
                        print("AI_BOARD_HELD: " + json.dumps(
                            {"gate": "pair", "reason": _viol_desc, "board": str(out_path)},
                            ensure_ascii=False))
                    results.append({
                        "customer": customer, "treatment": treatment, "title": board_title,
                        "status": "PAIR_GATE_HELD", "board": str(out_path),
                        "pair_gate": pair_gate, "angle_gate": angle_gate,
                    })
                    continue

                status = "OK" if stats["failed"] == 0 else "PARTIAL"
                print(f"  ✅ {out_path} (增强 {stats['ok']}/{stats['total']})")
                if args.case_dir is not None:
                    # 单案例入口：machine-parseable 标记供 server 父进程解析 board 路径
                    print(f"AI_BOARD_RESULT: {out_path}")
                qa_result = {}
                if board_qa is not None:
                    try:
                        # 带 G3 近景行的板走 v1+closeup prompt（近景行不参与配对评判，
                        # 防 judge 把特写行当配对行误判 blocker —— 郭璟琳探针 3/3 实锤）
                        v = board_qa.assess(
                            out_path,
                            has_closeup_section=bool(
                                (manifest.get("closeup_section") or {}).get("regions")),
                        )
                        qa_result = {"qa_verdict": v.verdict, "qa_defect": v.primary_defect,
                                     "qa_held": v.held}
                        _qa_icon = "🚫" if v.held else "✅"
                        print(f"  [D6-QA] {_qa_icon} {v.verdict}: {v.primary_defect or '无缺陷'}")
                    except Exception as qa_exc:
                        logger.warning("  [D6-QA] 评估失败: %s", qa_exc)
                        qa_result = {"qa_verdict": "unavailable", "qa_held": True}
                results.append({
                    "customer": customer, "treatment": treatment, "title": board_title,
                    "title_lines": board_title_lines,
                    "status": status, "board": str(out_path), **stats, **qa_result,
                    "angle_gate": angle_gate, "pair_gate": pair_gate,
                    "closeup_regions": (manifest.get("closeup_section") or {}).get("regions"),
                })
            except Exception as exc:
                logger.error("  ❌ 渲染失败: %s", exc)
                results.append({
                    "customer": customer, "treatment": treatment, "title": board_title,
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
