"""anchored-sim Phase 4 — effect 投影交付 lane selector.

resolve_effect_pairs（迁移自 calibration harness，反臆造 fail-closed）+ 上线 scope gate
（只放行 owner greenlight 的正脸清晰类型，profile/expression/out-of-scope skip 有原因）。
"""
from __future__ import annotations

from backend.services import effect_delivery_selector as sel
from backend.services import procedure_region_mappings as prm


# --- resolve_effect_pairs（反臆造 fail-closed）---

def test_resolve_effect_pairs_real_tear_trough():
    pairs, _parsed = sel.resolve_effect_pairs("郭若煊__2026.4.1弗缦1.0注射泪沟")
    assert (prm.PROJECT_HA_FILLER, "泪沟") in pairs


def test_resolve_effect_pairs_unknown_brand_drops_fail_closed():
    pairs, parsed = sel.resolve_effect_pairs("测试__某未知牌子注射泪沟")
    assert pairs == []                                  # 未注册品牌 → 不臆造
    assert parsed.get("needs_human_review") or parsed.get("unknown_segments")


# --- scope_gate（上线 scope）---

def test_scope_gate_keeps_launch_clear_types():
    pairs = [
        (prm.PROJECT_HA_FILLER, "泪沟"),
        (prm.PROJECT_HA_FILLER, "苹果肌"),
        (prm.PROJECT_HA_FILLER, "唇"),
        (prm.PROJECT_HA_FILLER, "法令纹"),
        (prm.PROJECT_HA_FILLER, "卧蚕"),
    ]
    in_scope, skipped = sel.scope_gate(pairs)
    assert in_scope == pairs
    assert skipped == []


def test_scope_gate_skips_profile_expression_and_out_of_scope():
    pairs = [
        (prm.PROJECT_HA_FILLER, "泪沟"),       # keep
        (prm.PROJECT_HA_FILLER, "鼻背"),       # profile
        (prm.PROJECT_HA_FILLER, "下巴"),       # profile
        (prm.PROJECT_BOTOX, "川字"),           # expression
        (prm.PROJECT_HA_FILLER, "太阳穴"),     # 已注册 frontal 但未greenlight
        (prm.PROJECT_BOTOX, "咬肌"),           # 已注册 frontal 但未greenlight（12周肉毒）
    ]
    in_scope, skipped = sel.scope_gate(pairs)
    assert in_scope == [(prm.PROJECT_HA_FILLER, "泪沟")]
    joined = " ".join(skipped)
    assert "profile_only:鼻背" in joined
    assert "profile_only:下巴" in joined
    assert "expression_only:川字" in joined
    assert "out_of_launch_scope:太阳穴" in joined
    assert "out_of_launch_scope:咬肌" in joined


# --- select_effect_eligible（lane discover）---

def test_select_effect_eligible_classifies_with_reasons():
    res = sel.select_effect_eligible(
        [
            "郭若煊__2026.4.1弗缦1.0注射泪沟",   # eligible（泪沟 in scope）
            "测试__某未知牌子注射泪沟",            # ineligible（无 pair）
        ]
    )
    by_name = {r["case_name"]: r for r in res}
    tear = by_name["郭若煊__2026.4.1弗缦1.0注射泪沟"]
    assert tear["eligible"] is True
    assert (prm.PROJECT_HA_FILLER, "泪沟") in tear["effect_pairs"]
    unknown = by_name["测试__某未知牌子注射泪沟"]
    assert unknown["eligible"] is False
    assert any("no_evidence_anchored_pairs" in r for r in unknown["skip_reasons"])


def test_scope_gate_profile_only_yields_no_in_scope():
    # 只有侧脸主导部位 → 无 in-scope pair（lane 该 case ineligible）。
    in_scope, skipped = sel.scope_gate([(prm.PROJECT_HA_FILLER, "鼻背")])
    assert in_scope == []
    assert skipped and "profile_only:鼻背" in skipped[0]
