"""_restore_content_geometry 单测。

背景（2026-06-10 验证集批量实锤）：pad 成正方形送 AI 后，tuzi/flashapi 都可能
丢弃 pad 按内容比例返回非正方形图（1071×1469 / 1086×1448 / 1122×1402），
旧逻辑盲 resize((sq, sq)) 把竖图内容横向拉宽 25-37%（林方如 side、许3.31 side、
骆萍 front+oblique 四槽变形根因）。
"""
from __future__ import annotations

from PIL import Image

from backend.scripts.render_ai_enhanced_boards import (
    _pad_to_square,
    _restore_content_geometry,
)


def _vstripe(w: int, h: int, stripe_frac: float = 0.25) -> Image.Image:
    """黑底白竖条（居中，宽 = w*stripe_frac，贯穿全高）。用条宽占比测内容是否被拉伸。"""
    img = Image.new("RGB", (w, h), (0, 0, 0))
    sw = round(w * stripe_frac)
    img.paste(Image.new("RGB", (sw, h), (255, 255, 255)), ((w - sw) // 2, 0))
    return img


def _stripe_width_frac(img: Image.Image) -> float:
    """中线行白像素占比。"""
    row = list(img.convert("L").crop((0, img.height // 2, img.width, img.height // 2 + 1)).getdata())
    return sum(1 for v in row if v > 128) / len(row)


def test_square_return_keeps_legacy_path():
    src = _vstripe(300, 400)
    padded, crop_box = _pad_to_square(src)
    sq = padded.size[0]
    # 模拟 provider 保留 pad 构图、缩到 1254² 返回
    ret = padded.resize((1254, 1254), Image.LANCZOS)

    out = _restore_content_geometry(ret, sq, crop_box, "front")

    assert out.size == (300, 400)
    assert abs(_stripe_width_frac(out) - 0.25) < 0.03


def test_nonsquare_return_not_stretched():
    """核心回归：3:4 非方形返回（模型去 pad）→ 条宽占比不变；旧逻辑会拉到 ≈0.33。"""
    src = _vstripe(300, 400)
    _, crop_box = _pad_to_square(src)
    # 模拟模型丢弃 pad、按内容 3:4 比例返回（等比，无畸变）
    ret = src.resize((150, 200), Image.LANCZOS)

    out = _restore_content_geometry(ret, 400, crop_box, "side")

    assert out.size == (300, 400)
    frac = _stripe_width_frac(out)
    assert abs(frac - 0.25) < 0.03, f"内容被拉伸: stripe_frac={frac:.3f}"
    # 旧盲拉伸路径的特征值（0.25 * 4/3 ≈ 0.333）必须不出现
    assert frac < 0.30


def test_nonsquare_ar_mismatch_cover_crops_without_distortion():
    """4:5 返回（AR≠内容 3:4，如骆萍 oblique 1122×1402）→ 等比 cover + 居中裁，无畸变。"""
    src = _vstripe(300, 400)
    _, crop_box = _pad_to_square(src)
    # 模拟模型按 4:5 画布返回但内容等比（用 cover 裁切构造，不引入畸变）
    scaled = src.resize((160, 213), Image.LANCZOS)  # 等比 ×0.5333
    ret = scaled.crop((0, 6, 160, 206))  # 160×200 = 4:5

    out = _restore_content_geometry(ret, 400, crop_box, "oblique")

    assert out.size == (300, 400)
    # 条的绝对宽 / 图高 比例应保持（等比复原后高度对齐 → 条宽占比不变）
    assert abs(_stripe_width_frac(out) - 0.25) < 0.04


def test_landscape_source_nonsquare_return():
    """横图源（如胡志超 3936×2624）pad 上下；非方形返回同样等比复原。"""
    src = _vstripe(400, 300)
    _, crop_box = _pad_to_square(src)
    ret = src.resize((200, 150), Image.LANCZOS)

    out = _restore_content_geometry(ret, 400, crop_box, "front")

    assert out.size == (400, 300)
    assert abs(_stripe_width_frac(out) - 0.25) < 0.03


# ---- letterbox 填充改进（2026-06-12 owner 拍板 d：纯黑→模糊背景，根治构图漂移） ----


def test_pad_fill_is_blurred_content_not_black():
    """pad 区 = 模糊背景延续（非纯黑），内容区像素逐字节不变。"""
    src = Image.new("RGB", (400, 300), (200, 120, 80))
    padded, crop_box = _pad_to_square(src)

    assert padded.size == (400, 400)
    # 内容区像素逐字节不变（送 AI 的内容保真）
    assert padded.crop(crop_box).tobytes() == src.tobytes()
    # pad 区（上条带）不再是纯黑，且色调接近内容（模糊延续）
    top_band = padded.crop((0, 0, 400, crop_box[1]))
    px = list(top_band.getdata())
    assert not any(p == (0, 0, 0) for p in px), "pad 区出现纯黑像素 = 回退旧行为"
    avg = tuple(sum(c[i] for c in px) / len(px) for i in range(3))
    assert abs(avg[0] - 200) < 30 and abs(avg[1] - 120) < 30 and abs(avg[2] - 80) < 30, (
        f"pad 区色调偏离内容: avg={avg}")


def test_pad_fill_deterministic():
    """同输入 → pad 后字节逐位相同（_ai_cache_key 稳定性前提）。"""
    src = _vstripe(300, 400)
    p1, _ = _pad_to_square(src)
    p2, _ = _pad_to_square(src)
    assert p1.tobytes() == p2.tobytes()


def test_square_input_passthrough_zero_rebake():
    """方形输入原样返回（字节不变 = cache key 不变 = 零重烧）。"""
    src = _vstripe(400, 400)
    padded, crop_box = _pad_to_square(src)
    assert padded is src
    assert crop_box == (0, 0, 400, 400)
