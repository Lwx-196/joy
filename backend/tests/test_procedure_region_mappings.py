"""Tests for procedure_region_mappings — 术式品牌→项目→部位→循证效果 映射表 + 效果 prompt 库.

Promotes the Phase 0 validated draft (procedure_mapping.draft.json) to production.
Encodes the disciplines from ~/.claude/plans/anchored-simulation.md §0.0:
- 反臆造 + fail-closed (unknown brand never guessed)
- 精准对应 (only treated regions surface; do_not_touch never enhanced)
- 强度非默认保守 (visible-natural default with over-done guardrail)
"""

from __future__ import annotations

from backend.services import procedure_region_mappings as prm


# --- L1 品牌→项目 解析（反臆造 + fail-closed）---

def test_known_brands_resolve_with_provenance():
    ha = prm.resolve_brand("海魅")
    assert ha is not None
    assert ha["project"] == prm.PROJECT_HA_FILLER
    assert "玻尿酸" in ha["ingredient"]
    assert ha["source"] and ha["confidence"] == "high"
    assert "即刻" in ha["time_anchor"] and "稳定代表态" in ha["time_anchor"]
    botox = prm.resolve_brand("衡力")
    assert botox is not None and botox["project"] == prm.PROJECT_BOTOX
    assert "肉毒" in botox["ingredient"]


def test_unknown_brand_fails_closed():
    # 反臆造：未知品牌不猜成分/项目 → None（调用方须标人工核对）
    assert prm.resolve_brand("童颜针X") is None
    assert prm.resolve_brand("") is None
    assert prm.resolve_brand("   ") is None


def test_every_brand_entry_has_provenance():
    assert prm.BRAND_TO_PROJECT, "must seed authoritative brands"
    for brand, spec in prm.BRAND_TO_PROJECT.items():
        assert spec["project"] in prm.PROJECT_TYPES, brand
        assert spec.get("project_cn"), brand
        assert spec.get("ingredient"), brand
        assert spec.get("source"), f"{brand} missing source (反臆造)"
        assert spec.get("confidence") in {"high", "inferred"}, brand
        assert isinstance(spec.get("time_anchor"), dict) and spec["time_anchor"], brand


# --- L3 (项目,部位)→循证效果行 ---

def test_effect_rows_for_case45_projects():
    lip = prm.effect_row(prm.PROJECT_HA_FILLER, "唇")
    assert lip and lip["do_right"] and lip["guardrail"]
    assert isinstance(lip["avoid"], list) and lip["avoid"]
    chin = prm.effect_row(prm.PROJECT_HA_FILLER, "下巴")
    assert chin and chin["avoid"]
    # botox rows carry the honest no-photo-GT caveat (case45 fact)
    fore = prm.effect_row(prm.PROJECT_BOTOX, "额纹")
    assert fore and fore.get("ground_truth_note")
    glab = prm.effect_row(prm.PROJECT_BOTOX, "川字")
    assert glab and glab.get("ground_truth_note")


def test_ha_filler_region_rows_ported_from_library():
    # effect-evidence-library §1 行港进 EFFECT_ROWS（grounded 转录，非臆造）：泪沟=Phase 0
    # 锚点 + 库最常见部位；鼻背令「海魅注射鼻子」类 case 多解析一个 effect pair。
    for region in ("泪沟", "苹果肌", "鼻基底", "鼻背"):
        row = prm.effect_row(prm.PROJECT_HA_FILLER, region)
        assert row is not None, region
        assert row["do_right"] and isinstance(row["avoid"], list) and row["avoid"]
        assert row["guardrail"] and row["evidence"]


def test_frontal_gate_reframes_profile_regions():
    # 正脸 gate：profile 部位（下巴/鼻背）只推正脸可见部分，不强推侧脸效果（鼻背变直/颏前突）
    chin = prm.build_effect_prompt_fragment(prm.PROJECT_HA_FILLER, "下巴", view="frontal")
    assert "正脸不强推前突" in chin and "下庭" in chin
    assert "前突度增加" not in chin  # 完整 do_right 的侧脸语言被 reframe 掉
    nose = prm.build_effect_prompt_fragment(prm.PROJECT_HA_FILLER, "鼻背", view="frontal")
    assert "侧脸" in nose and "中线高光" in nose
    # 非正脸视角（profile）→ 推完整 do_right（侧脸主战场）
    chin_side = prm.build_effect_prompt_fragment(prm.PROJECT_HA_FILLER, "下巴", view="profile")
    assert "前突度增加" in chin_side
    # frontal 部位（泪沟）不受 gate 影响 → 完整 do_right
    tt = prm.build_effect_prompt_fragment(prm.PROJECT_HA_FILLER, "泪沟", view="frontal")
    assert "凹陷填平" in tt


def test_effect_row_unknown_pair_is_none():
    # not seeded → None (do NOT fabricate effect language)
    assert prm.effect_row(prm.PROJECT_HA_FILLER, "太阳穴") is None
    assert prm.effect_row("__nope__", "唇") is None


# --- parse_procedures（case45 ground truth）---

def test_parse_case45_folder_name():
    raw = "2025.10.29衡力20抬头、川字、海魅1.0ml注射唇、下巴"
    p = prm.parse_procedures(raw)
    assert not p["needs_human_review"], p
    brands = {pr["brand"] for pr in p["procedures"]}
    assert brands == {"衡力", "海魅"}
    by_brand = {pr["brand"]: set(pr["regions"]) for pr in p["procedures"]}
    assert by_brand["衡力"] == {"额纹", "川字"}   # 抬头→额纹 alias (atlas)
    assert by_brand["海魅"] == {"唇", "下巴"}
    # project binding correct
    proj = {pr["brand"]: pr["project"] for pr in p["procedures"]}
    assert proj["衡力"] == prm.PROJECT_BOTOX
    assert proj["海魅"] == prm.PROJECT_HA_FILLER
    assert set(p["all_regions"]) == {"额纹", "川字", "唇", "下巴"}
    # 精准对应：未做的部位不出现
    assert "苹果肌" not in p["all_regions"]
    assert "泪沟" not in p["all_regions"]


def test_parse_unknown_brand_flags_human_review():
    # 反臆造：无已知品牌 → 不猜品牌→项目绑定 → needs_human_review
    p = prm.parse_procedures("某不知名针剂注射苹果肌")
    assert p["needs_human_review"]
    # 没有带品牌→项目绑定的 procedure
    assert all(pr["brand"] is not None for pr in p["procedures"])  # 已知品牌才进 procedures
    assert p["procedures"] == []


def test_parse_empty_or_no_region():
    p = prm.parse_procedures("")
    assert p["needs_human_review"] and p["all_regions"] == []
    p2 = prm.parse_procedures("   ")
    assert p2["needs_human_review"]


# --- 效果 prompt 库 ---

def test_strength_constants():
    # 修正②：默认非保守 = natural（可见自然，对标真实术后），上下两档备用
    assert prm.STRENGTH_NATURAL == "natural"
    assert {prm.STRENGTH_SUBTLE, prm.STRENGTH_NATURAL, prm.STRENGTH_STRONG} <= set(prm.STRENGTHS)


def test_build_effect_prompt_fragment_contains_evidence():
    frag = prm.build_effect_prompt_fragment(prm.PROJECT_HA_FILLER, "唇")
    assert "唇" in frag
    # surfaces the over-done red line (correction② upper guardrail stays)
    assert "香肠" in frag or "鸭嘴" in frag
    # surfaces the quant guardrail
    assert "1:1" in frag or "唇可动" in frag


def test_build_effect_prompt_fragment_unknown_returns_none():
    # fail-closed: no evidence row → no fabricated effect language
    assert prm.build_effect_prompt_fragment(prm.PROJECT_HA_FILLER, "太阳穴") is None


def test_build_effect_prompt_fragment_strength_modulates():
    sub = prm.build_effect_prompt_fragment(prm.PROJECT_HA_FILLER, "唇", strength=prm.STRENGTH_SUBTLE)
    strong = prm.build_effect_prompt_fragment(prm.PROJECT_HA_FILLER, "唇", strength=prm.STRENGTH_STRONG)
    assert sub and strong and sub != strong


def test_botox_fragment_carries_no_gt_caveat():
    frag = prm.build_effect_prompt_fragment(prm.PROJECT_BOTOX, "额纹")
    assert frag and ("循证" in frag or "GT" in frag)
    # botox discipline: soften not freeze, keep brow motion
    assert "抬眉" in frag or "动度" in frag or "frozen" in frag.lower()


def test_compose_effect_prompt_multi_region_and_do_not_touch():
    raw = "2025.10.29衡力20抬头、川字、海魅1.0ml注射唇、下巴"
    p = prm.parse_procedures(raw)
    prompt = prm.compose_effect_prompt(p, do_not_touch=["苹果肌", "泪沟"])
    for r in ("唇", "下巴", "额纹", "川字"):
        assert r in prompt, r
    # 精准对应 + 不外扩：do_not_touch surfaced
    assert "苹果肌" in prompt and "泪沟" in prompt
    # identity 铁律 present (保身份)
    assert "身份" in prompt or "同一" in prompt
    # 只强化做过的 (anti-fabrication framing)
    assert "无中生有" in prompt or "实际做过" in prompt


def test_compose_from_explicit_pairs():
    prompt = prm.compose_effect_prompt(
        [(prm.PROJECT_HA_FILLER, "唇"), (prm.PROJECT_HA_FILLER, "下巴")]
    )
    assert "唇" in prompt and "下巴" in prompt
