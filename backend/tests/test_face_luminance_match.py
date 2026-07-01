"""_match_face_luminance 双向肤色对齐单测（亮度 gain + 色度 LAB a/b）。

背景（2026-06-10 验证集偏白根因）：
- 赵12.23：AI 增强把人脸漂白（enh skin 160-184 vs 术前 128-133）——旧逻辑只提亮
  不压暗，偏白无解。
- 黄婧：源级 skin 已持平，但旧全人物 mask（max(RGB)>20）被黑毛衣稀释 + before/after
  衣物占比差 → 误判术后偏暗 → 1.3× boost 把脸打白。
亮度修复：统计改肤色像素（luma>60 且 R>B+10），gain 双向 [0.75, 1.3]，±4% deadband。

色度（2026-06-14 defect③(a)）：术前/术后源图灯光色温差 → 肤区 LAB a/b 均值偏移。
把术后肤区 a/b 对齐术前，单向 clamp ±8 LAB 单位，deadband ±2。色度只动 a/b 不碰
L——故亮度方向测试改测 LAB L（与色度解耦），色度方向另立专测。
"""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

from backend.scripts.render_ai_enhanced_boards import _match_face_luminance, _repair_midface_cyan_cast


def _cell(face_rgb, sweater_rgb=(25, 25, 25), size=200, face_box=(60, 30, 140, 110),
          sweater_box=(20, 120, 180, 200)) -> Image.Image:
    """黑底合成 cell：face_box 是暖肤色块（80×80=6400px ≥ 500），sweater_box 是衣物块。"""
    img = Image.new("RGB", (size, size), (0, 0, 0))
    fx0, fy0, fx1, fy1 = face_box
    img.paste(Image.new("RGB", (fx1 - fx0, fy1 - fy0), face_rgb), (fx0, fy0))
    sx0, sy0, sx1, sy1 = sweater_box
    img.paste(Image.new("RGB", (sx1 - sx0, sy1 - sy0), sweater_rgb), (sx0, sy0))
    return img


def _face_mean(img: Image.Image, face_box=(60, 30, 140, 110)) -> float:
    arr = np.array(img, dtype=np.float64)
    fx0, fy0, fx1, fy1 = face_box
    return float(arr[fy0:fy1, fx0:fx1].mean())


def _face_lab(img: Image.Image, face_box=(60, 30, 140, 110)) -> tuple[float, float, float]:
    """face_box 区域 LAB (L, a, b) 均值（cv2 8-bit LAB）。L 用于解耦亮度断言。"""
    arr = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2LAB).astype(np.float64)
    fx0, fy0, fx1, fy1 = face_box
    f = arr[fy0:fy1, fx0:fx1]
    return float(f[..., 0].mean()), float(f[..., 1].mean()), float(f[..., 2].mean())


def _gain_only_lab_l(tgt: Image.Image, gain: float) -> float:
    """纯亮度 gain（不含色度）后的 face LAB L——亮度方向断言的金标准。"""
    arr = np.clip(np.array(tgt, dtype=np.float64) * gain, 0, 255).astype(np.uint8)
    return _face_lab(Image.fromarray(arr))[0]


def _front_face_with_cool_midface() -> Image.Image:
    img = Image.new("RGB", (320, 420), (0, 0, 0))
    img.paste(Image.new("RGB", (190, 250), (152, 123, 104)), (65, 55))
    img.paste(Image.new("RGB", (76, 88), (148, 123, 113)), (122, 108))
    return img


def _region_lab_b(img: Image.Image, box: tuple[int, int, int, int]) -> float:
    arr = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2LAB).astype(np.float64)
    x0, y0, x1, y1 = box
    return float(arr[y0:y1, x0:x1, 2].mean())


def test_overbright_after_pulled_down():
    """核心回归：术后脸比术前白一档（赵12.23 形态）→ 压暗回拉。旧逻辑原样放行。"""
    ref = _cell(face_rgb=(150, 110, 90))     # 术前 skin mean ≈ 116.7
    tgt = _cell(face_rgb=(200, 165, 135))    # 术后 skin mean ≈ 166.7（漂白）

    out = _match_face_luminance(ref, tgt)

    assert _face_mean(out) < _face_mean(tgt), "偏白术后必须被压暗"
    # 亮度方向（解耦色度）：LAB L == 纯 gain 0.75 的结果（gain=116.7/166.7≈0.70→clamp 0.75）
    assert abs(_face_lab(out)[0] - _gain_only_lab_l(tgt, 0.75)) < 2.0
    # 黑底保持纯黑
    assert np.array(out)[0:10, 0:10].max() == 0


def test_dark_after_still_boosted_capped():
    """原方向保留：术后偏暗 → 提亮，gain 上限 1.3。"""
    ref = _cell(face_rgb=(180, 140, 110))    # skin mean ≈ 143.3
    tgt = _cell(face_rgb=(110, 80, 65))      # skin mean ≈ 85（偏暗）

    out = _match_face_luminance(ref, tgt)

    assert _face_mean(out) > _face_mean(tgt), "偏暗术后必须被提亮"
    # 亮度方向（解耦色度）：LAB L == 纯 gain 1.3 的结果（gain=143.3/85≈1.69→cap 1.3）
    assert abs(_face_lab(out)[0] - _gain_only_lab_l(tgt, 1.3)) < 2.0


def test_deadband_noop():
    """±4% 内不动：返回原对象，零字节变更。"""
    ref = _cell(face_rgb=(150, 110, 90))
    tgt = _cell(face_rgb=(153, 112, 92))     # 差 ~2%

    out = _match_face_luminance(ref, tgt)

    assert out is tgt


def test_clothing_fraction_does_not_skew_gain():
    """黄婧形态回归：同一张脸、衣物占比不同 → 不得触发 boost。

    旧全人物 mask：tgt 衣物块更大 → 全人物均值更低 → 误判偏暗 → 1.3× 漂白脸。
    新肤色 mask 只看脸 → 双方持平 → no-op。
    """
    face = (160, 120, 100)
    ref = _cell(face_rgb=face, sweater_box=(20, 120, 180, 160))   # 衣物小
    tgt = _cell(face_rgb=face, sweater_box=(20, 120, 180, 200))   # 衣物大

    out = _match_face_luminance(ref, tgt)

    assert out is tgt, "脸持平时衣物占比差不得触发增益"


def test_person_fallback_when_no_skin():
    """无肤色像素（灰调人物）→ 双方一致回退全人物 mask，方向逻辑不变。"""
    # 中性灰人物：R≈B 不满足肤色条件
    ref = _cell(face_rgb=(140, 140, 140), sweater_rgb=(120, 120, 120))
    tgt = _cell(face_rgb=(90, 90, 90), sweater_rgb=(80, 80, 80))

    out = _match_face_luminance(ref, tgt)

    assert _face_mean(out) > _face_mean(tgt), "回退路径仍应提亮偏暗术后"


# ── 色度（chroma）专测：2026-06-14 defect③(a) ──────────────────────────────
# 设计：ref/tgt 肤区 RGB 均值取等（亮度 gain 落 deadband），仅 hue 不同 → 隔离色度。


def test_chroma_pulled_toward_ref():
    """术后 hue 偏冷 → 肤区 a/b 向术前收敛（亮度持平不动）。"""
    ref = _cell(face_rgb=(150, 110, 90))     # 偏暖：a/b 较高
    tgt = _cell(face_rgb=(130, 120, 100))    # 同均值(116.7) 偏中性：a/b 较低

    out = _match_face_luminance(ref, tgt)

    _, ra, rb = _face_lab(ref)
    _, ta, tb = _face_lab(tgt)
    _, oa, ob = _face_lab(out)
    assert abs(oa - ra) < abs(ta - ra), "a 通道必须向术前收敛"
    assert abs(ob - rb) < abs(tb - rb), "b 通道必须向术前收敛"
    assert np.array(out)[0:10, 0:10].max() == 0   # 黑底保持


def test_chroma_does_not_cool_naturally_warm_after():
    """#420 回归：术后已有正常暖肤色时，不得为了贴近术前而降红/降黄拉灰偏青。"""
    ref = _cell(face_rgb=(130, 120, 100))    # 同均值，较中性
    tgt = _cell(face_rgb=(150, 110, 90))     # 同均值，术后更暖、更有血色

    out = _match_face_luminance(ref, tgt)

    _, ta, tb = _face_lab(tgt)
    _, oa, ob = _face_lab(out)
    assert oa >= ta - 1.0, "自然暖调术后不得被明显降 a 通道"
    assert ob >= tb - 1.0, "自然暖调术后不得被明显降 b 通道"
    assert np.array(out)[0:10, 0:10].max() == 0


def test_midface_cyan_cast_repaired_for_front_only():
    """#420 回归：front 术后面中局部偏青/偏灰时，只回暖面中肤色，不动黑底。"""
    img = _front_face_with_cool_midface()
    mid_box = (126, 120, 194, 170)
    before_b = _region_lab_b(img, mid_box)

    out = _repair_midface_cyan_cast(img, slot="front")
    after_b = _region_lab_b(out, mid_box)

    assert after_b > before_b + 0.8
    assert np.array(out)[0:20, 0:20].max() == 0


def test_midface_cyan_cast_repair_ignores_non_front_slot():
    img = _front_face_with_cool_midface()

    out = _repair_midface_cyan_cast(img, slot="side")

    assert out is img


def test_chroma_shift_clamped():
    """极端 hue 差 → 位移被 clamp 到 ±8，不得抹平到术前。"""
    from backend.scripts.render_ai_enhanced_boards import _LUM_CHROMA_MAX_SHIFT

    ref = _cell(face_rgb=(210, 95, 70))      # 极饱和暖：a 远高
    tgt = _cell(face_rgb=(140, 122, 108))    # 近中性，均值接近(gain 落 deadband)

    out = _match_face_luminance(ref, tgt)

    _, ra, _ = _face_lab(ref)
    _, ta, _ = _face_lab(tgt)
    _, oa, _ = _face_lab(out)
    moved = oa - ta
    assert moved > 0, "必须朝术前(更红)方向移动"
    assert moved <= _LUM_CHROMA_MAX_SHIFT + 1.5, "位移必须被 clamp 住（含取整余量）"
    assert oa < ra - 5, "clamp 后仍远未抹平到术前（证明 clamp 真生效）"


def test_chroma_deadband_noop_identity():
    """肤区均值相等 + hue 仅微差(<2 LAB) → 亮度与色度双 deadband，返回原对象。"""
    ref = _cell(face_rgb=(150, 110, 90))
    tgt = _cell(face_rgb=(151, 110, 89))     # 同均值(116.7, gain=1.0)，da=0/db=-1 双 deadband

    out = _match_face_luminance(ref, tgt)

    assert out is tgt, "双 deadband 必须零字节返回原对象"
