"""Phase 2.1 — build_after_enhancement_prompt 新增 effect_projection 模式（保真模式 BC）.

只测 prompt 构建分支：默认 fidelity 字节行为不变；effect_projection 走循证 prompt 库。
不触生产调用方（wiring 在 2.2）。
"""

from __future__ import annotations

from backend import ai_generation_adapter as adp
from backend.services import procedure_region_mappings as prm


def test_mode_constants_exposed():
    assert adp.FIDELITY_MODE == "fidelity"
    assert adp.EFFECT_PROJECTION_MODE == "effect_projection"


def test_default_mode_is_fidelity_bc():
    # 默认 fidelity：保真语言保留，effect 专属 framing 不出现
    p = adp.build_after_enhancement_prompt(["唇"], [])
    assert "整张" in p or "整体" in p
    assert "实际做过" not in p and "无中生有" not in p


def test_fidelity_region_branch_unchanged_bc():
    p = adp.build_after_enhancement_prompt(
        ["下颌线"], [{"x": 0.2, "y": 0.3, "width": 0.4, "height": 0.2, "label": "下颌线"}]
    )
    assert "增强必须限制在框选区域内" in p
    assert "不得调亮" in p


def test_effect_projection_uses_evidence_library():
    p = adp.build_after_enhancement_prompt(
        [], [],
        mode=adp.EFFECT_PROJECTION_MODE,
        effect_pairs=[(prm.PROJECT_HA_FILLER, "唇"), (prm.PROJECT_BOTOX, "额纹")],
        do_not_touch=["苹果肌", "泪沟"],
    )
    assert "唇" in p and "额纹" in p
    assert "苹果肌" in p and "泪沟" in p          # do_not_touch surfaced
    assert "实际做过" in p or "无中生有" in p       # anti-fabrication framing
    assert "身份" in p or "同一" in p              # identity locks
    # NOT the fidelity 保真 framing
    assert "轻量局部增强" not in p and "整张" not in p


def test_effect_projection_without_pairs_falls_back_to_fidelity():
    # effect 模式但没给 effect_pairs → BC-safe 回退保真（不崩、不空）
    p = adp.build_after_enhancement_prompt(
        ["唇"], [], mode=adp.EFFECT_PROJECTION_MODE, effect_pairs=None
    )
    assert "整张" in p or "整体" in p or "框选" in p
    assert "实际做过" not in p


def test_effect_projection_strength_passthrough():
    weak = adp.build_after_enhancement_prompt(
        [], [], mode=adp.EFFECT_PROJECTION_MODE,
        effect_pairs=[(prm.PROJECT_HA_FILLER, "唇")], strength=prm.STRENGTH_SUBTLE,
    )
    strong = adp.build_after_enhancement_prompt(
        [], [], mode=adp.EFFECT_PROJECTION_MODE,
        effect_pairs=[(prm.PROJECT_HA_FILLER, "唇")], strength=prm.STRENGTH_STRONG,
    )
    assert weak and strong and weak != strong
