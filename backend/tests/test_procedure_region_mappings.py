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
    assert prm.resolve_brand("某XYZ未知针剂") is None
    assert prm.resolve_brand("") is None
    assert prm.resolve_brand("   ") is None


def test_ha_filler_brands_registered():
    # 真 HA：盈致（=乔雅登 Endyne）/ 玻尿酸 generic。柯芮琦/薇旖美/妮凯丽 经 6-02 核查
    # 推翻为胶原（见下方胶原测试）。
    for brand in ("盈致", "玻尿酸"):
        spec = prm.resolve_brand(brand)
        assert spec is not None, brand
        assert spec["project"] == prm.PROJECT_HA_FILLER, brand
        assert "玻尿酸" in spec["ingredient"], brand
        assert "稳定代表态" in spec["time_anchor"], brand
    # generic「玻尿酸」substring 命中无品牌的玻尿酸 case。
    assert prm.resolve_brand("玻尿酸注射") is not None
    # generic「胶原」substring 命中含胶原的无品牌 case → collagen_filler（6-02 普查新增）。
    assert prm.resolve_brand("胶原")["project"] == prm.PROJECT_COLLAGEN_FILLER


def test_collagen_filler_brands_reclassified():
    # 2026-06-02 web 权威核查推翻 owner 误标 HA：弗缦/妮凯丽/柯芮琦/薇旖美 实为胶原蛋白填充剂
    # （即刻体积 + 渐进再生，机制异于 HA）。
    for brand in ("弗缦", "妮凯丽", "柯芮琦", "薇旖美"):
        spec = prm.resolve_brand(brand)
        assert spec is not None, brand
        assert spec["project"] == prm.PROJECT_COLLAGEN_FILLER, brand
        assert "胶原" in spec["ingredient"], brand
        assert "玻尿酸" not in spec["ingredient"], brand   # 不再误标 HA
        assert spec["confidence"] == "high", brand
        assert ("核查" in spec["source"]) or ("NMPA" in spec["source"]), brand
    # 盈致：owner 6-02 权威确认 = 乔雅登旗下玻尿酸（HA，非胶原）→ high confidence HA。
    ying = prm.resolve_brand("盈致")
    assert ying["project"] == prm.PROJECT_HA_FILLER and ying["confidence"] == "high"
    assert "乔雅登" in ying["ingredient"]


def test_census_batch_2026_06_02():
    # 2026-06-02 NMPA 权威普查批次：案例库 fail-closed 真品牌按机制收录（全 NMPA-cited）。
    by_mech = {
        prm.PROJECT_HA_FILLER: ("乔雅登", "朔颜", "缇颜", "娇兰", "嗨体", "海媚", "塑公主", "熊猫针"),
        prm.PROJECT_COLLAGEN_FILLER: ("珂芮绮", "肤莱美", "肤柔美", "肤丽美", "肤力原"),
        prm.PROJECT_CAHA: ("菲林", "云镜", "云境"),               # CaHA hybrid（即刻体积+刺激）
        prm.PROJECT_PMMA: ("爱贝芙",),                            # PMMA 永久
        prm.PROJECT_PCL: ("伊妍仕", "少女针"),                     # PCL 少女针（即刻体积+长效，proactive）
        prm.PROJECT_BIOSTIMULATOR: ("童颜针", "普丽妍", "塑妍萃"),  # 纯 PLLA（无即刻体积）
        prm.PROJECT_BOTOX: ("保妥适", "吉适", "吉士"),
    }
    for mech, brands in by_mech.items():
        for b in brands:
            spec = prm.resolve_brand(b)
            assert spec is not None and spec["project"] == mech, b
    # substring 家族命中
    assert prm.resolve_brand("乔雅登丰颜")["project"] == prm.PROJECT_HA_FILLER
    assert prm.resolve_brand("普丽妍T")["project"] == prm.PROJECT_BIOSTIMULATOR  # T=同产品
    # 别名指回已注册同一产品（同 project）
    assert prm.resolve_brand("海媚")["project"] == prm.resolve_brand("海魅")["project"]
    assert prm.resolve_brand("珂芮绮")["project"] == prm.resolve_brand("柯芮琦")["project"]
    # 黑金=飞顿黑金超光子仪器（光子设备非注射）→ 故意不注册 → fail-closed
    assert prm.resolve_brand("黑金") is None


def test_immediate_volume_regenerative_fill_reuse():
    # 即刻体积型再生（CaHA/PCL/PMMA）在深层结构填充区（苹果肌/法令纹，Radiesse/少女针经典）复用
    # HA 视觉行 → 可发货；薄层浅区（泪沟/唇/卧蚕）它们一般不用 → 不复用 None。
    for proj in (prm.PROJECT_CAHA, prm.PROJECT_PCL, prm.PROJECT_PMMA):
        for region in ("苹果肌", "法令纹"):
            assert prm.effect_row(proj, region) == prm.effect_row(prm.PROJECT_HA_FILLER, region), (proj, region)
        for region in ("泪沟", "唇", "卧蚕"):
            assert prm.effect_row(proj, region) is None, (proj, region)


def test_plla_biostimulator_no_fill_rows_global_effect():
    # 循证 injection-effect-standards §2 铁律：PLLA 纯生物刺激剂术后稳定态是全局渐进紧致/饱满/
    # 肤质，**绝不能画成即刻局部体积爆出** → per-region 填充行恒 None（不复用 HA），效果走机制语境。
    for region in ("泪沟", "苹果肌", "法令纹", "下颌线", "全脸"):
        assert prm.effect_row(prm.PROJECT_BIOSTIMULATOR, region) is None, region
    # 各机制语境就位，compose 注入对应语境，绝不臆造成 HA
    bio = prm.compose_effect_prompt([(prm.PROJECT_BIOSTIMULATOR, "苹果肌")])
    caha = prm.compose_effect_prompt([(prm.PROJECT_CAHA, "苹果肌")])
    pcl = prm.compose_effect_prompt([(prm.PROJECT_PCL, "下巴")])
    pmma = prm.compose_effect_prompt([(prm.PROJECT_PMMA, "下巴")])
    assert "机制语境：胶原刺激剂" in bio
    assert "机制语境：羟基磷灰石(CaHA" in caha and "颧高点抬升" in caha  # CaHA 苹果肌 复用 HA 片段
    assert "机制语境：聚己内酯(PCL" in pcl
    assert "机制语境：PMMA" in pmma
    for p in (bio, caha, pcl, pmma):
        assert "机制语境：玻尿酸(HA)" not in p


def test_collagen_reuses_ha_fill_effect_rows():
    # 胶原即刻体积 → 软组织填充区复用 HA 视觉行（单部位术后视觉与 HA 一致）。
    for region in ("泪沟", "苹果肌", "唇", "法令纹", "卧蚕"):
        col = prm.effect_row(prm.PROJECT_COLLAGEN_FILLER, region)
        ha = prm.effect_row(prm.PROJECT_HA_FILLER, region)
        assert col is not None and col == ha, region
    # 结构性支撑区（鼻背/鼻基底/下巴）胶原一般不用 → 不复用，fail-closed（不编造效果）。
    for region in ("鼻背", "鼻基底", "下巴"):
        assert prm.effect_row(prm.PROJECT_COLLAGEN_FILLER, region) is None, region


def test_collagen_mechanism_context_injected():
    # 胶原 case → 注入胶原机制语境（即刻体积 + 再生 + 不致 Tyndall），不是 HA 语境。
    prompt = prm.compose_effect_prompt([(prm.PROJECT_COLLAGEN_FILLER, "泪沟")])
    assert "机制语境：胶原蛋白填充剂" in prompt
    assert "机制语境：玻尿酸(HA)" not in prompt
    # 仍带泪沟视觉方向（复用 HA 行）+ 身份铁律
    assert "凹陷填平" in prompt and ("身份" in prompt or "同一" in prompt)


def test_collagen_case_parses_to_collagen_project():
    # 真实库案例名（弗缦泪沟）→ 解析绑定 COLLAGEN，仍命中泪沟（eligible 不回归）。
    p = prm.parse_procedures("2026.4.1弗缦1.0注射泪沟")
    assert not p["needs_human_review"], p
    proc = {pr["brand"]: pr for pr in p["procedures"]}
    assert proc["弗缦"]["project"] == prm.PROJECT_COLLAGEN_FILLER
    assert "泪沟" in proc["弗缦"]["regions"]


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


def test_mechanism_context_injected_per_case():
    # 机制语境按 case 实际含的机制注入：HA-only → 只 HA 语境
    ha = prm.compose_effect_prompt([(prm.PROJECT_HA_FILLER, "泪沟")])
    assert "机制语境：玻尿酸(HA)" in ha and "机制语境：肉毒" not in ha
    # 混合（肉毒+HA）→ 两条都注入
    mixed = prm.compose_effect_prompt([(prm.PROJECT_BOTOX, "川字"), (prm.PROJECT_HA_FILLER, "唇")])
    assert "机制语境：肉毒" in mixed and "机制语境：玻尿酸(HA)" in mixed
    # 肉毒静态无变化的诚实标注（防 judge 把静态正脸无变化判失败）
    assert "静止中性正脸可不明显" in mixed


def test_new_region_rows_grounded():
    # injection-effect-standards 厂商级新增行（atlas key 都存在 → 能从 case 名解析）
    for proj, region in [
        (prm.PROJECT_HA_FILLER, "卧蚕"), (prm.PROJECT_HA_FILLER, "太阳穴"),
        (prm.PROJECT_HA_FILLER, "法令纹"), (prm.PROJECT_BOTOX, "咬肌"),
    ]:
        row = prm.effect_row(proj, region)
        assert row and row["do_right"] and isinstance(row["avoid"], list) and row["avoid"]
        assert row["guardrail"] and row["evidence"]
    # 卧蚕 = 塑造饱满（与泪沟填平方向相反，prompt 不能混）
    wocan = prm.effect_row(prm.PROJECT_HA_FILLER, "卧蚕")["do_right"]
    assert "塑造" in wocan and "饱满" in wocan and "不是填平" in wocan
    # 咬肌(瘦脸) = 肉毒，带即刻零变化/12 周时间锚点
    masseter = prm.effect_row(prm.PROJECT_BOTOX, "咬肌")
    assert masseter.get("ground_truth_note") and "12 周" in masseter["ground_truth_note"]


def test_effect_row_unknown_pair_is_none():
    # not seeded → None (do NOT fabricate effect language)
    assert prm.effect_row(prm.PROJECT_HA_FILLER, "耳") is None
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
    # fail-closed: no evidence row → no fabricated effect language（耳=未登记部位）
    assert prm.build_effect_prompt_fragment(prm.PROJECT_HA_FILLER, "耳") is None


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


# --- 即刻效果分类（has_immediate_visible_effect）---

def test_immediate_effect_constants_complete():
    assert prm.IMMEDIATE_EFFECT_PROJECTS & prm.NO_IMMEDIATE_EFFECT_PROJECTS == frozenset()
    assert prm.IMMEDIATE_EFFECT_PROJECTS | prm.NO_IMMEDIATE_EFFECT_PROJECTS <= prm.PROJECT_TYPES


def test_pure_botox_no_immediate_effect():
    has, reason = prm.has_immediate_visible_effect("2025.10.29衡力20抬头、川字")
    assert not has
    assert "botulinum_toxin" in reason


def test_pure_biostimulator_no_immediate_effect():
    has, reason = prm.has_immediate_visible_effect("2025.03.15塑妍萃法令纹")
    assert not has
    assert "biostimulator" in reason


def test_mixed_botox_ha_has_effect():
    has, _ = prm.has_immediate_visible_effect("2025.10.29衡力20抬头、川字、海魅1.0ml注射唇、下巴")
    assert has


def test_pure_filler_has_effect():
    has, _ = prm.has_immediate_visible_effect("2025.06.01海魅泪沟")
    assert has


def test_unknown_brand_fail_open():
    has, _ = prm.has_immediate_visible_effect("2025.01.01某不知名剂注射唇")
    assert has


def test_empty_case_name_fail_open():
    has, _ = prm.has_immediate_visible_effect("")
    assert has


def test_unknown_segments_with_botox_fail_open():
    # 丰颜/质颜 是乔雅登子品牌(HA)但未单独注册 → needs_human_review → fail-open 不误删
    has, _ = prm.has_immediate_visible_effect(
        "2025.12.26丰颜2支注射隆鼻，苹果肌、质颜1支注射鼻基底、衡力50U川字鱼尾抬头纹"
    )
    assert has


def test_neck_wrinkle_no_photo_value():
    # 颈纹无论用什么产品（嗨体HA/滚针），即刻照无正向对比价值
    has, reason = prm.has_immediate_visible_effect("2025.12.10颈纹（嗨体1.5+2.5）+滚针")
    assert not has
    assert "region_no_photo_value" in reason


def test_neck_wrinkle_with_other_regions_kept():
    # 颈纹 + 其他有效部位（泪沟）→ 保留
    has, _ = prm.has_immediate_visible_effect("2025.12.10嗨体颈纹+泪沟")
    assert has
