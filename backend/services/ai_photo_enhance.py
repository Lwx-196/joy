"""AI 术后照片增强服务 — gpt-image-2 via image_providers cascade.

复用 image_providers.resolve_chain + generate_with_fallback，
prompt v1 定稿（2026-06-05 POC 验证，owner 选定）。

用途：术后临床照片的光影微调（补光 + 色温归一），保留原始皮肤纹理和面部结构。
禁区：不做面部结构改动、不磨皮、不美白、不改五官。

调用方式：
    from backend.services.ai_photo_enhance import enhance_after_photo
    result_bytes, provider_name = enhance_after_photo(image_path, env)
"""
from __future__ import annotations

import logging
from pathlib import Path

from .image_providers import (
    generate_with_fallback,
    load_env_file,
    resolve_chain,
)

logger = logging.getLogger(__name__)

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

MAX_LONG_EDGE = 1024


def prepare_image(image_path: Path, *, max_long_edge: int = MAX_LONG_EDGE) -> bytes:
    """读取图片 → EXIF 矫正 → 缩放到 max_long_edge → PNG bytes。"""
    from io import BytesIO

    import cv2
    import numpy as np
    from PIL import Image, ImageOps

    pil = ImageOps.exif_transpose(Image.open(image_path))
    img = np.array(pil.convert("RGB"))

    h, w = img.shape[:2]
    scale = min(max_long_edge / max(h, w), 1.0)
    if scale < 1.0:
        new_w, new_h = int(w * scale), int(h * scale)
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)

    buf = BytesIO()
    Image.fromarray(img).save(buf, format="PNG")
    return buf.getvalue()


def enhance_after_photo(
    image_path: Path,
    env: dict[str, str] | None = None,
    *,
    prompt: str = ENHANCE_PROMPT_V1,
    max_long_edge: int = MAX_LONG_EDGE,
    provider_order: list[str] | None = None,
) -> tuple[bytes, str]:
    """增强单张术后照片，返回 (增强图 bytes, provider 名)。

    全失败抛 RuntimeError（由 generate_with_fallback 抛出）。
    """
    if env is None:
        env = load_env_file()

    providers = resolve_chain(env, explicit=provider_order)
    if not providers:
        raise RuntimeError(
            "no ready image provider — check env: "
            "TUZI_IMAGE_PRIMARY_* / PANEL_IMG_* / PANEL_IMAGE_PROVIDERS"
        )

    image_bytes = prepare_image(image_path, max_long_edge=max_long_edge)
    logger.info(
        "ai_photo_enhance: %s → %dKB, %d providers",
        image_path.name, len(image_bytes) // 1024, len(providers),
    )

    result_bytes, provider_name = generate_with_fallback(
        providers, image_bytes, prompt, mime="image/png",
    )
    logger.info("ai_photo_enhance: OK via %s", provider_name)
    return result_bytes, provider_name


__all__ = [
    "ENHANCE_PROMPT_V1",
    "MAX_LONG_EDGE",
    "prepare_image",
    "enhance_after_photo",
]
