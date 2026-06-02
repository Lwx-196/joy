"""术式品牌 → 项目类型 → 部位 → 循证效果 映射表 + 效果 prompt 库.

把 Phase 0 验证草稿（procedure_mapping.draft.json，case45 双场景 judge 全过）固化为生产模块，
作 anchored-simulation 产品线 Phase 1 的数据地基（prompt 库 + Phase 3 judge 共用）。

最高纪律（见 ~/.claude/plans/anchored-simulation.md §0.0；用户连拦两次「全脸综合变美」）：
- 反臆造：L1 品牌→项目 **仅录人工权威条目**（用户 + 循证库），禁 LLM 推断成分/项目。
  每条记 source + confidence。（反例：探查时一个 agent 把「海魅」瞎填成 Radiesse/PLLA。）
- fail-closed：未知品牌 → 不猜 → ``resolve_brand`` 返回 None，``parse_procedures`` 置
  ``needs_human_review``，调用方须人工核对，不得静默绑定。
- 精准对应：做几项只强化几项。``parse_procedures`` 只产出明确命中的术式/部位；
  未做的部位走 ``do_not_touch``，绝不无中生有。
- 强度非默认保守（修正②）：默认档 = 对标真实术后效果的「可见自然」，上护栏 = 不命中过度失真清单。

循证依据：effect-evidence-library.md §1 填充 / §2 除皱提升。部位键对齐
``facial_region_atlas.FACIAL_REGION_ATLAS``（SSoT）。
"""

from __future__ import annotations

from typing import Any

from backend.services import facial_region_atlas as atlas

# --- 项目类型（成分族）---
PROJECT_HA_FILLER = "HA_filler"
PROJECT_BOTOX = "botulinum_toxin"
PROJECT_BIOSTIMULATOR = "biostimulator"  # PDLLA/PLLA 胶原刺激剂（艾维岚/童颜针），无即刻体积、渐进
# 胶原蛋白填充剂（弗缦/薇旖美/柯芮琦/妮凯丽/双美，I/III 型胶原或重组人源化胶原）：即刻有体积
# （注射即填充）+ 渐进刺激自体胶原再生。机制异于 HA（材质/维持/不致 Tyndall）与 PLLA 生物刺激
# 剂（那个无即刻体积）。泪沟尤偏胶原——薄眼皮下不像 HA 透蓝灰（Tyndall）。
PROJECT_COLLAGEN_FILLER = "collagen_filler"
PROJECT_TYPES: frozenset[str] = frozenset(
    {PROJECT_HA_FILLER, PROJECT_BOTOX, PROJECT_BIOSTIMULATOR, PROJECT_COLLAGEN_FILLER}
)

# === 机制语境（injection-effect-standards.md §0.1）===
# 三类机制的术后视觉性质 + 时间锚点完全不同，决定"术后稳定态"怎么画。compose_effect_prompt
# 按 case 实际含的机制注入对应语境，让模拟用对的时间锚点与视觉语言。
_MECHANISM_CONTEXT: dict[str, str] = {
    PROJECT_HA_FILLER: (
        "玻尿酸(HA)：即刻有真实体积，效果=治疗区局部体积/轮廓塑形；模拟对标术后约 2 周消肿"
        "稳定态（体积比即刻略收敛），保毛孔真实质感与自然红润健康气色，不磨皮不漂白。"
    ),
    PROJECT_BOTOX: (
        "肉毒：神经调节、无体积、不改肤质；动态纹软化仅在做表情时显现，静止中性正脸可不明显"
        "（不强求可见变化）；减弱非消除，保留自然表情动度，不磨皮、不僵脸。"
    ),
    PROJECT_BIOSTIMULATOR: (
        "胶原刺激剂(PDLLA/PLLA，如艾维岚/童颜针)：无即刻体积，效果渐进；模拟对标术后 3-6 月"
        "成熟态 = 整体紧致 / 全局饱满 / 肤质改善的自然年轻化，绝非即刻局部隆起。"
    ),
    PROJECT_COLLAGEN_FILLER: (
        "胶原蛋白填充剂(I/III 型胶原或重组人源化胶原，如弗缦/薇旖美/柯芮琦/妮凯丽)：即刻有真实"
        "体积（注射即填充）并渐进刺激自体胶原；模拟对标术后约 2 周消肿稳定态的局部体积/轮廓改善"
        "（胶原不像 HA 强亲水吸水，肿胀通常更轻、薄皮下不透蓝灰 Tyndall），保毛孔真实质感与自然"
        "红润健康气色，不磨皮不漂白。"
    ),
}

# --- 强度档（修正②：默认 natural = 对标真实术后，非保守；上下两档备用）---
STRENGTH_SUBTLE = "subtle"
STRENGTH_NATURAL = "natural"
STRENGTH_STRONG = "strong"
STRENGTHS: tuple[str, ...] = (STRENGTH_SUBTLE, STRENGTH_NATURAL, STRENGTH_STRONG)

_STRENGTH_LANG: dict[str, str] = {
    STRENGTH_SUBTLE: "效果克制、细微可辨即可",
    STRENGTH_NATURAL: "效果清晰可见但自然，对标该术式真实可达的术后稳定效果（不保守、不夸张）",
    STRENGTH_STRONG: "效果明显且方向明确、一眼可辨（达到该术式真实可达的较强一档）；宁可偏强也不要保守到看不出变化，但严守下方红线不得过度失真",
}

# === 正脸/侧脸 可见性 gate（injection-effect-standards.md §0.2）===
# 部位效果的主可见视角：frontal=正脸即可见 / profile=侧脸才是主战场（正脸几乎不可见）
# / expression=仅做表情时显（静止中性脸不可见）。喂正脸 case 照片时，profile/expression
# 部位若强推完整效果即失真（鼻背变直/颏前突在正脸推不出、肉毒动态纹静态无变化）——这是
# 之前正脸鼻/下巴模拟失败的根因（FDA/共识坐实，详见 injection-effect-standards.md）。
REGION_EFFECT_VIEW: dict[str, str] = {
    "鼻背": "profile",
    "鼻基底": "profile",
    "下巴": "profile",
    "川字": "expression",
    "额纹": "expression",
    "鱼尾纹": "expression",
}

# profile 部位在正脸图上只能推的「正脸可见」部分（替代完整 do_right，避免强推侧脸效果）。
FRONTAL_REFRAME: dict[str, str] = {
    "鼻背": "正脸只做鼻梁中线高光更对称、更连续，两侧鼻背美学线对称；鼻背变直/驼峰填平/鼻尖抬升属侧脸效果，正脸不强推、不增宽鼻梁",
    "鼻基底": "正脸只做鼻基底区轻微支撑、鼻周过渡自然；鼻背变直/鼻尖抬升属侧脸效果，正脸不强推",
    "下巴": "正脸只做下庭比例略拉长、下颌缘到颏部过渡略清晰；颏前突（E-line）属侧脸效果，正脸不强推前突",
}

# expression 部位（肉毒动态纹）在静止中性正脸的诚实备注。
_EXPRESSION_FRONTAL_CAVEAT = "动态纹仅做表情时显现，静止中性正脸可不明显，不强求可见变化"


# === L1：品牌 → 项目类型 + 时间锚（人工权威，反臆造）===
# time_anchor 键：即刻 / 消肿|起效 / 稳定代表态|峰值 / 维持 —— 用于场景化 prompt 的时间语境。
BRAND_TO_PROJECT: dict[str, dict[str, Any]] = {
    "海魅": {
        "project": PROJECT_HA_FILLER,
        "project_cn": "玻尿酸填充",
        "ingredient": "玻尿酸(HA)",
        "time_anchor": {
            "即刻": "偏满偏肿（HA 亲水吸水 + 注入体积已在 + 针孔泛红）",
            "消肿": "1-2 周",
            "稳定代表态": "3-4 周，体积比即刻略收敛",
            "维持": "HA 约 6-12 月",
        },
        "source": "user_authoritative + effect-evidence-library §0.1/§1",
        "confidence": "high",
    },
    "衡力": {
        "project": PROJECT_BOTOX,
        "project_cn": "肉毒除皱",
        "ingredient": "A 型肉毒毒素",
        "time_anchor": {
            "即刻": "零变化（仅针孔泛红）",
            "起效": "1-3 天",
            "峰值": "1-2 周",
            "维持": "上面部纹 3-4 月 / 眉间 4-6 月",
            "范式": "neuromodulation 减弱非麻痹，保表情",
        },
        "source": "user_authoritative + effect-evidence-library §0.1/§2",
        "confidence": "high",
    },
    # === 胶原蛋白填充剂（机制 = PROJECT_COLLAGEN_FILLER，即刻体积 + 渐进再生）===
    # 修正记录（2026-06-02，web 权威核查）：弗缦/柯芮琦/薇旖美/妮凯丽 原被 owner 6-01/6-02
    # 误标为 HA（「泪沟 HA 品牌」批次），实为胶原蛋白填充剂——已逐个 NMPA/权威源核实推翻。
    # 临床上泪沟尤偏胶原正因其不致 Tyndall 蓝灰（见 EFFECT_ROWS 泪沟 avoid 首条）。时间锚为
    # 机制级（所有胶原填充共享），inline 与 HA 风格一致。粒径差异不入 time_anchor。
    "弗缦": {
        "project": PROJECT_COLLAGEN_FILLER,
        "project_cn": "胶原蛋白填充",
        "ingredient": "牛胶原蛋白(I+III型，原名肤美达)",
        "time_anchor": {
            "即刻": "即刻有体积（注射即填充 + 针孔泛红；胶原不强亲水吸水，肿胀通常较 HA 轻）",
            "消肿": "数天-1 周",
            "稳定代表态": "约 2 周稳定，体积接近注射量",
            "维持": "约 6-12 月，期间渐进刺激自体胶原再生",
        },
        "source": "NMPA 医用胶原充填剂(械三) + web 核查 2026-06-02（推翻 owner 6-01 误标 HA）",
        "confidence": "high",
    },
    # 盈致：owner 6-02 权威确认 = 乔雅登(Juvederm/艾尔建)旗下玻尿酸（HA）。与同批被误标的
    # 柯芮琦/薇旖美/妮凯丽（实为胶原）不同——盈致确为 HA。乔雅登是 HA 龙头，分类自洽。
    "盈致": {
        "project": PROJECT_HA_FILLER,
        "project_cn": "玻尿酸填充",
        "ingredient": "玻尿酸(HA，乔雅登/Juvederm 旗下)",
        "time_anchor": {
            "即刻": "偏满偏肿（HA 亲水吸水 + 注入体积已在 + 针孔泛红）",
            "消肿": "1-2 周",
            "稳定代表态": "3-4 周，体积比即刻略收敛",
            "维持": "HA 约 6-12 月",
        },
        "source": "user_authoritative (owner confirmed 2026-06-02: 盈致=乔雅登旗下玻尿酸 HA)",
        "confidence": "high",
    },
    "妮凯丽": {
        "project": PROJECT_COLLAGEN_FILLER,
        "project_cn": "胶原蛋白填充",
        "ingredient": "胶原蛋白(I/III型，牛跟腱提取，日本进口械三)",
        "time_anchor": {
            "即刻": "即刻有体积（注射即填充 + 针孔泛红；胶原不强亲水吸水，肿胀通常较 HA 轻）",
            "消肿": "数天-1 周",
            "稳定代表态": "约 2 周稳定，体积接近注射量",
            "维持": "约 6-12 月，期间渐进刺激自体胶原再生",
        },
        "source": "web 核查 2026-06-02（推翻 owner 6-02 误标 HA；同批均证实胶原）",
        "confidence": "high",
    },
    "柯芮琦": {
        "project": PROJECT_COLLAGEN_FILLER,
        "project_cn": "胶原蛋白填充",
        "ingredient": "牛胶原蛋白(85%I+15%III，浙江珂瑞康；库内亦写作「珂芮绮」)",
        "time_anchor": {
            "即刻": "即刻有体积（注射即填充 + 针孔泛红；胶原不强亲水吸水，肿胀通常较 HA 轻）",
            "消肿": "数天-1 周",
            "稳定代表态": "约 2 周稳定，体积接近注射量",
            "维持": "约 6-12 月，期间渐进刺激自体胶原再生",
        },
        "source": "NMPA 注射用面部胶原蛋白植入剂 2025 + web 核查 2026-06-02（推翻 owner 6-02 误标 HA）",
        "confidence": "high",
    },
    "薇旖美": {
        "project": PROJECT_COLLAGEN_FILLER,
        "project_cn": "胶原蛋白填充",
        "ingredient": "重组III型人源化胶原蛋白(锦波)",
        "time_anchor": {
            "即刻": "即刻有体积（注射即填充 + 针孔泛红；胶原不强亲水吸水，肿胀通常较 HA 轻）",
            "消肿": "数天-1 周",
            "稳定代表态": "约 2 周稳定，体积接近注射量",
            "维持": "约 6-12 月，期间渐进刺激自体胶原再生",
        },
        "source": "NMPA 国械注准20213130488 + web 核查 2026-06-02（推翻 owner 6-02 误标 HA）",
        "confidence": "high",
    },
    # generic「玻尿酸」（owner 可选）：substring 匹配，命中文件夹名含「玻尿酸」的无品牌 case。
    "玻尿酸": {
        "project": PROJECT_HA_FILLER,
        "project_cn": "玻尿酸填充",
        "ingredient": "玻尿酸(HA)",
        "time_anchor": {
            "即刻": "偏满偏肿（HA 亲水吸水 + 注入体积已在 + 针孔泛红）",
            "消肿": "1-2 周",
            "稳定代表态": "3-4 周，体积比即刻略收敛",
            "维持": "HA 约 6-12 月",
        },
        "source": "user_authoritative (owner handoff 2026-06-02: generic 玻尿酸 = HA filler)",
        "confidence": "high",
    },
}


# === L3：(项目类型, 部位) → 循证效果行 ===
# do_right=做对方向 / avoid=过度失真红线 / guardrail=量化护栏 / evidence=文献强度 /
# ground_truth_note=诚实标注无照片 GT（仅循证预测）。键中的部位对齐 atlas region key。
EFFECT_ROWS: dict[tuple[str, str], dict[str, Any]] = {
    (PROJECT_HA_FILLER, "唇"): {
        "do_right": "唇珠形成、唇红缘清晰、丘比特弓保留、垂直唇高适度增加、自然丰盈",
        "avoid": ["香肠唇/鸭嘴", "球状僵硬", "唇缘消失", "口角下垂", "人中堆量", "侧面过度前突"],
        "guardrail": "上下唇比 1:1~1:1.6 区间（族裔差异，无单一值）；保唇可动；保持唇闭合自然",
        "evidence": "偏好研究 N=570 + 专家共识",
    },
    (PROJECT_HA_FILLER, "下巴"): {
        "do_right": "前突度增加、下庭比例改善、侧面接近 E-line、下颌缘略顺",
        "avoid": ["巫婆下巴(过尖前突)", "桌山方块感", "表面纤维化/鹅卵石"],
        "guardrail": "E-line 上唇后≈4mm/下唇后≈2mm（族裔差异大）；颏突约平面后 3mm；正脸下巴轮廓自然",
        "evidence": "系统综述 N=2738 + RCT（强）",
    },
    # 以下 4 行 = effect-evidence-library §1（填充类）已有但此前未港的权威行（逐字转录，
    # 键对齐 atlas region key；鼻背/鼻基底 共享库「鼻基底/鼻」行）。泪沟=Phase 0 锚点 + 案例库
    # 最常见部位；鼻背令 海魅注射鼻子 的 case 多解析一个 effect pair。
    (PROJECT_HA_FILLER, "泪沟"): {
        "do_right": "凹陷填平、眼下阴影变淡、睑-颊平滑过渡",
        "avoid": ["Tyndall 蓝灰", "眼下持续浮肿/眼袋", "sunset eyes", "松鼠脸"],
        "guardrail": "填平到齐平不填出凸度；薄皮肤勿透蓝；单侧仅约 0.45mL",
        "evidence": "共识（Anido 2021）+ 回顾性 N=155",
    },
    (PROJECT_HA_FILLER, "苹果肌"): {
        "do_right": "颧高点抬升（外上象限）、侧面 Ogee 曲线恢复、法令纹间接变浅",
        "avoid": ["pillow face/飞碟脸", "花栗鼠颊", "整脸均匀鼓起", "微笑异常前突"],
        "guardrail": "高点在外上（Hinderer 线交点）非鼻侧；定向投影非弥散膨胀",
        "evidence": "共识综述（Hinderer 线既定标准）",
    },
    (PROJECT_HA_FILLER, "鼻基底"): {
        "do_right": "鼻背视觉变直、鼻尖适度抬升、鼻基底支撑",
        "avoid": ["Avatar nose（鼻背增宽、鼻颊界限消失）", "鼻尖过度抬升", "鼻梁过厚"],
        "guardrail": "保守微调；正面不增宽；用量本就最小（均值 0.8mL）；血管高危区，模拟应保守",
        "evidence": "开放标签 N=52 + 综述",
    },
    (PROJECT_HA_FILLER, "鼻背"): {
        "do_right": "鼻背明显拉高变挺、沿鼻梁中线延伸出一条连续清晰的高光带、鼻尖适度抬升、鼻基底支撑（正面不增宽）",
        "avoid": ["Avatar nose（鼻背增宽、鼻颊界限消失）", "鼻尖过度抬升", "鼻梁过厚"],
        "guardrail": "保守微调；正面不增宽；用量本就最小（均值 0.8mL）；血管高危区，模拟应保守",
        "evidence": "开放标签 N=52 + 综述",
    },
    # 以下 4 行 = injection-effect-standards.md 厂商级新增（卧蚕/太阳穴/法令纹 HA + 咬肌 肉毒）。
    (PROJECT_HA_FILLER, "卧蚕"): {
        # ⚠️ 与泪沟方向相反：卧蚕是「塑造饱满」，不是「填平」。
        "do_right": "紧贴下睫毛线正下方塑造细窄、柔和、连续的横向饱满隆起（卧蚕），微笑时随眼轮匝肌自然鼓起更明显、眼神亲和年轻；静态低调自然——是塑造饱满，不是填平凹陷",
        "avoid": ["腊肠卧蚕（粗圆僵硬）", "做成显老的眼袋（更大更靠下、发暗下垂）", "整片浮肿/泡眼", "静态就过度膨出（应笑时显、静时收）"],
        "guardrail": "睑前深层皮下、紧贴睫毛线的窄带；小颗粒低黏弹软胶；极少量、宁窄勿宽（精确 mL 个体化，无共识值）",
        "evidence": "Life(Basel) 2025 charming roll 技术综述（中）",
    },
    (PROJECT_HA_FILLER, "太阳穴"): {
        "do_right": "填平颞凹、上面部从骨感/凹陷/憔悴变饱满流畅、发际-眉尾-颧弓-额头过渡顺滑，改善太阳穴塌陷显老/显凶",
        "avoid": ["颞区鼓出/膨隆不自然", "表浅注射结节/可见隆起", "左右不对称"],
        "guardrail": "骨膜上深层、保守；⚠️ 颞区血管高危（颞深↔眼动脉吻合，误注致失明/坏死），模拟必须保守",
        "evidence": "FDA Voluma XC 颞部指征 2024 + 颞区血管 meta（强）",
    },
    (PROJECT_HA_FILLER, "法令纹"): {
        "do_right": "鼻唇沟变浅但不完全消失（保留自然鼻唇过渡）；优先靠苹果肌从上方支撑间接变浅，沟本身仅轻度填充精修",
        "avoid": ["填到沟完全消失（法令是正常解剖，全消显假）", "鼻旁/颊隆起成嵴", "孤立猛填忽略苹果肌缺失", "pillow face 局部膨胀"],
        "guardrail": "朝目标区间靠一档、非全消；优先苹果肌地基（间接），直接填仅做剩余精修；深度分层",
        "evidence": "MD Codes NL1-3 + cheek-first 原则 PMC8012343 + Voluma 间接减轻法令（强）",
    },
    (PROJECT_BOTOX, "额纹"): {
        "do_right": "静止/抬眉横纹变浅减少、额头平顺、保留抬眉动度",
        "avoid": ["frozen 额头", "纹 100% 消失如磨皮", "眉毛下垂", "上睑沉重"],
        "guardrail": "减弱非消除；保额头自然皮肤纹理；眉位不下压",
        "evidence": "RCT + 共识（强）",
        "ground_truth_note": "case45 术后即刻照=零变化，botox 效果无照片 GT，纯循证预测",
    },
    (PROJECT_BOTOX, "川字"): {
        "do_right": "眉间竖纹变浅/消失、眉间舒展、眉形对称",
        "avoid": ["Mephisto/Spock 八字挑眉", "上睑下垂", "眉形怪异"],
        "guardrail": "眉间放松平顺；不抬外侧眉；保眉自然形",
        "evidence": "多 RCT + Cochrane（极强）",
        "ground_truth_note": "无照片 GT，纯循证预测",
    },
    (PROJECT_BOTOX, "咬肌"): {
        # 瘦脸针：肉毒里唯一在静态正脸有明显宽度变化者（靠继发萎缩，起效最慢）。
        "do_right": "下面部（下颌角/bigonial 宽度）变窄、方/国字脸→V/瓜子/卵圆脸轮廓柔化、咬肌膨隆消退；中面颊饱满度保留、笑容对称自然",
        "avoid": ["矛盾性鼓包（咬牙时咬肌前部异常鼓出）", "面颊凹陷/sunken cheek（剂量过大显疲惫非瘦）", "太阳穴下塌", "笑容不对称/歪嘴"],
        "guardrail": "只动下面部宽度，不动中面容积/太阳穴；笑容对称+咀嚼保留；非越瘦越好，过度=凹陷（萎缩约 8-35%）",
        "evidence": "前瞻+3D 研究 PMC4230655/PMC12512921（强，off-label 但大量同行评审）",
        "ground_truth_note": "肉毒咬肌即刻零变化，靠继发萎缩、可见轮廓峰值约 12 周（起效最慢）；无照片 GT 纯循证预测",
    },
}


# === 身份/真实性铁律（效果 prompt 的不变前缀；改的是治疗区，保的是同一人）===
IDENTITY_LOCKS: tuple[str, ...] = (
    "保持同一人：脸型/骨架/眼型/鼻/耳/发际/肤色/毛孔/痣/痘印等永久特征 100% 不变，"
    "看起来像同一机位拍的原始照片。",
    "只在下列治疗部位内做改变，其余区域（含背景/服装/头发/未做的部位）严禁改动，"
    "仅允许治疗区边缘自然羽化过渡。",
    "不磨皮、不美白、不换脸、不改年龄感；保留真实皮肤纹理与永久瑕疵（痘印是永久特征要留）。",
    "保持原姿态、构图、镜头视角、光照不变。",
)


def resolve_brand(brand: str) -> dict[str, Any] | None:
    """品牌 → 项目映射（反臆造 + fail-closed）。

    仅命中人工权威条目才返回（exact 或品牌名作为子串）；未知品牌返回 None，
    调用方须标人工核对，**绝不**推断成分/项目。
    """
    key = (brand or "").strip()
    if not key:
        return None
    if key in BRAND_TO_PROJECT:
        return dict(BRAND_TO_PROJECT[key])
    for known, spec in BRAND_TO_PROJECT.items():
        if known in key:
            return dict(spec)
    return None


# 胶原蛋白填充剂的单部位视觉效果复用 HA 填充行：即刻体积的软组织填充区，术后稳定态的单部位
# 视觉结果与 HA 一致（材质差异体现在机制语境/维持时长，不改 do_right/avoid/guardrail）。仅限
# 胶原临床实际用于填充的软组织区（= 正脸填充类）；结构性支撑区（鼻背/鼻基底/下巴）胶原一般
# 不用 → 不 fallback，保持 fail-closed（不编造胶原在那些部位的效果）。
_COLLAGEN_REUSES_HA_REGION: frozenset[str] = frozenset(
    {"泪沟", "苹果肌", "唇", "法令纹", "卧蚕"}
)


def effect_row(project: str, region_key: str) -> dict[str, Any] | None:
    """(项目类型, 部位) → 循证效果行；未登记返回 None（不得编造效果语言）。

    胶原蛋白填充剂（即刻体积）在软组织填充区（``_COLLAGEN_REUSES_HA_REGION``）复用 HA 填充
    视觉行——单部位术后稳定态视觉与 HA 一致，材质差异由机制语境承载。结构性区不复用 → None。
    """
    row = EFFECT_ROWS.get((project, region_key))
    if (
        row is None
        and project == PROJECT_COLLAGEN_FILLER
        and region_key in _COLLAGEN_REUSES_HA_REGION
    ):
        row = EFFECT_ROWS.get((PROJECT_HA_FILLER, region_key))
    return dict(row) if row is not None else None


def parse_procedures(raw: str) -> dict[str, Any]:
    """把术式目录名（如「2025.10.29衡力20抬头、川字、海魅1.0ml注射唇、下巴」）解析为结构化术式。

    算法：定位已知品牌出现位置 → 每个品牌「拥有」从它的位置到下一个品牌位置之间的文本段
    → 段内用 atlas.extract_regions 抽部位。
    纪律：
    - 只有**已知品牌**才进 ``procedures``（带 brand→project 绑定）；
    - 任何无品牌归属的部位文本（首个品牌之前的前缀 / 全程无已知品牌）进 ``unknown_segments``
      并置 ``needs_human_review``（反臆造 fail-closed，绝不给无品牌的部位猜项目）。

    返回 dict：``raw`` / ``procedures``[{brand,project,project_cn,regions,segment}] /
    ``unknown_segments``[{segment,regions}] / ``all_regions``(去重,首见序) / ``needs_human_review``。
    """
    text = (raw or "").strip()
    result: dict[str, Any] = {
        "raw": text,
        "procedures": [],
        "unknown_segments": [],
        "all_regions": [],
        "needs_human_review": False,
    }
    if not text:
        result["needs_human_review"] = True
        return result

    seen_regions: list[str] = []

    def collect(segment: str) -> list[str]:
        regs: list[str] = []
        for r in atlas.extract_regions(segment):
            if r not in regs:
                regs.append(r)
            if r not in seen_regions:
                seen_regions.append(r)
        return regs

    # 定位所有已知品牌出现位置
    hits: list[tuple[int, str]] = []
    for brand in BRAND_TO_PROJECT:
        pos = text.find(brand)
        while pos != -1:
            hits.append((pos, brand))
            pos = text.find(brand, pos + 1)
    hits.sort(key=lambda h: h[0])

    if not hits:
        regs = collect(text)
        result["unknown_segments"].append({"segment": text, "regions": regs})
        result["needs_human_review"] = True
        result["all_regions"] = list(seen_regions)
        return result

    # 首个品牌之前的前缀：若含部位但无品牌 → 无归属，标人工
    prefix = text[: hits[0][0]]
    prefix_regs = collect(prefix)
    if prefix_regs:
        result["unknown_segments"].append({"segment": prefix, "regions": prefix_regs})
        result["needs_human_review"] = True

    # 每个品牌拥有 [pos, next_pos) 文本段
    for i, (pos, brand) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        segment = text[pos:end]
        spec = BRAND_TO_PROJECT[brand]
        result["procedures"].append({
            "brand": brand,
            "project": spec["project"],
            "project_cn": spec["project_cn"],
            "regions": collect(segment),
            "segment": segment,
        })

    result["all_regions"] = list(seen_regions)
    return result


def build_effect_prompt_fragment(
    project: str, region_key: str, strength: str = STRENGTH_NATURAL, view: str = "frontal"
) -> str | None:
    """单部位循证效果 prompt 片段（prompt 库核心单元）。

    组成：方向（做对）+ 强度语 + 红线（避免过度失真）+ 量化护栏 [+ 无 GT 诚实备注]。
    未登记 (项目,部位) → None（fail-closed，不编造效果语言）。

    ``view`` = 待模拟照片视角（默认 frontal 正脸）。正脸图上：profile 部位（鼻背/下巴等）
    用 ``FRONTAL_REFRAME`` 只推正脸可见部分（不强推侧脸效果）；expression 部位（动态纹）
    加静态不可见备注。见 REGION_EFFECT_VIEW / injection-effect-standards.md §0.2。
    """
    row = effect_row(project, region_key)
    if row is None:
        return None
    strength_lang = _STRENGTH_LANG.get(strength, _STRENGTH_LANG[STRENGTH_NATURAL])
    effect_view = REGION_EFFECT_VIEW.get(region_key, "frontal")
    avoid = "；".join(row["avoid"])
    if view == "frontal" and effect_view == "profile" and region_key in FRONTAL_REFRAME:
        direction = FRONTAL_REFRAME[region_key]
    else:
        direction = row["do_right"]
    parts = [
        f"【{region_key}】方向：{direction}。",
        f"强度：{strength_lang}。",
        f"红线（避免过度失真）：{avoid}。",
        f"护栏：{row['guardrail']}。",
    ]
    note = row.get("ground_truth_note")
    if note:
        parts.append(f"备注（循证预测，无照片 GT）：{note}。")
    if view == "frontal" and effect_view == "expression":
        parts.append(f"备注：{_EXPRESSION_FRONTAL_CAVEAT}。")
    return " ".join(parts)


def _pairs_from(source: Any) -> list[tuple[str, str]]:
    """从 parse_procedures 结果 或 [(project, region), ...] 列表 抽取 (项目, 部位) 对。"""
    if isinstance(source, dict) and "procedures" in source:
        pairs: list[tuple[str, str]] = []
        for proc in source["procedures"]:
            project = proc.get("project")
            for region in proc.get("regions", []):
                pairs.append((project, region))
        return pairs
    return [(p, r) for p, r in source]


def compose_effect_prompt(
    source: Any,
    *,
    strength: str = STRENGTH_NATURAL,
    do_not_touch: list[str] | None = None,
    scenario_note: str | None = None,
    view: str = "frontal",
) -> str:
    """把多部位循证片段 + 身份铁律 + do_not_touch 组装成完整效果 prompt（prompt 库输出）。

    ``source`` = parse_procedures 结果 或 [(project, region), ...]。
    ``do_not_touch`` = 未做的部位（精准对应 + 不外扩，修正③：协调但只在做了的部位内）。
    ``view`` = 待模拟照片视角（默认 frontal）：正脸图对 profile/expression 部位做 gate（见
    build_effect_prompt_fragment / REGION_EFFECT_VIEW），避免在正脸强推侧脸/表情态效果。
    """
    pairs = _pairs_from(source)
    lines: list[str] = [
        "任务：医美术后效果模拟。严格只强化以下**实际做过**的术式部位，绝不无中生有添加未做的项目。",
    ]
    # 机制语境：按 case 实际含的机制（HA/肉毒/胶原刺激剂）注入对应时间锚点+视觉性质，
    # 让模拟用对的"术后稳定态"逻辑（混合机制 case 各注一条）。
    seen_mechanisms: list[str] = []
    for project, _region in pairs:
        if project in _MECHANISM_CONTEXT and project not in seen_mechanisms:
            seen_mechanisms.append(project)
    for project in seen_mechanisms:
        lines.append(f"机制语境：{_MECHANISM_CONTEXT[project]}")
    for project, region in pairs:
        frag = build_effect_prompt_fragment(project, region, strength, view)
        if frag:
            lines.append(frag)
    if do_not_touch:
        lines.append(f"绝对不要改动（未做的项目/部位）：{'、'.join(do_not_touch)}。")
    if scenario_note:
        lines.append(scenario_note)
    lines.append("身份与真实性铁律：")
    lines.extend(f"- {lock}" for lock in IDENTITY_LOCKS)
    return "\n".join(lines)


__all__ = [
    "PROJECT_HA_FILLER", "PROJECT_BOTOX", "PROJECT_BIOSTIMULATOR",
    "PROJECT_COLLAGEN_FILLER", "PROJECT_TYPES",
    "STRENGTH_SUBTLE", "STRENGTH_NATURAL", "STRENGTH_STRONG", "STRENGTHS",
    "BRAND_TO_PROJECT", "EFFECT_ROWS", "IDENTITY_LOCKS",
    "resolve_brand", "effect_row", "parse_procedures",
    "build_effect_prompt_fragment", "compose_effect_prompt",
]
