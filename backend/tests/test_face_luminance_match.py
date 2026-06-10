"""_match_face_luminance 双向肤色对齐单测。

背景（2026-06-10 验证集偏白根因）：
- 赵12.23：AI 增强把人脸漂白（enh skin 160-184 vs 术前 128-133）——旧逻辑只提亮
  不压暗，偏白无解。
- 黄婧：源级 skin 已持平，但旧全人物 mask（max(RGB)>20）被黑毛衣稀释 + before/after
  衣物占比差 → 误判术后偏暗 → 1.3× boost 把脸打白。
修复：亮度统计改肤色像素（luma>60 且 R>B+10），gain 双向 [0.75, 1.3]，±4% deadband。
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from backend.scripts.render_ai_enhanced_boards import _match_face_luminance


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


def test_overbright_after_pulled_down():
    """核心回归：术后脸比术前白一档（赵12.23 形态）→ 压暗回拉。旧逻辑原样放行。"""
    ref = _cell(face_rgb=(150, 110, 90))     # 术前 skin mean ≈ 116.7
    tgt = _cell(face_rgb=(200, 165, 135))    # 术后 skin mean ≈ 166.7（漂白）

    out = _match_face_luminance(ref, tgt)

    before, after = _face_mean(tgt), _face_mean(out)
    assert after < before, "偏白术后必须被压暗"
    # gain = 116.7/166.7 ≈ 0.70 → clamp 0.75 → face ≈ 166.7*0.75 = 125
    assert abs(after - before * 0.75) < 2.0
    # 黑底保持纯黑
    assert np.array(out)[0:10, 0:10].max() == 0


def test_dark_after_still_boosted_capped():
    """原方向保留：术后偏暗 → 提亮，gain 上限 1.3。"""
    ref = _cell(face_rgb=(180, 140, 110))    # skin mean ≈ 143.3
    tgt = _cell(face_rgb=(110, 80, 65))      # skin mean ≈ 85（偏暗）

    out = _match_face_luminance(ref, tgt)

    before, after = _face_mean(tgt), _face_mean(out)
    assert after > before, "偏暗术后必须被提亮"
    assert abs(after - before * 1.3) < 2.0   # gain = 143.3/85 ≈ 1.69 → cap 1.3


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
