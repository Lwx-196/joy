"""parse_title_b 单测：标题结构化方案 B（owner 拍板 2026-06-11）。

纪律：展示级纯文本重排——每个字都来自原串，不增不减不猜医学绑定；
解析失败 fail-open 返回 None，调用方回退原串单行。
锚定用例 = A/B 实物渲板拍板时的真实输出（郭璟琳/胡志超/陈艺琼/蓝凤端）。
"""
from scripts.render_brand_clean import parse_title_b


def test_canonical_multi_material_three_lines():
    # 蓝凤端范例串（todo 方案 B 候选原例）
    lines = parse_title_b("丰颜2支注射隆鼻，苹果肌、质颜1支注射鼻基底、衡力50U川字鱼尾抬头纹")
    assert lines == [
        "丰颜 2支 ▸ 隆鼻 · 苹果肌",
        "质颜 1支 ▸ 鼻基底",
        "衡力 50U ▸ 川字鱼尾抬头纹",
    ]


def test_bracket_region_list_protected_and_unwrapped():
    # 郭璟琳：括号内顿号不切分，整段括号剥外层（拍板实物第一行）
    lines = parse_title_b(
        "保妥适250U（面部皱纹、下颌缘、颈阔肌、腮腺）、1支盈致+1支丰鼻基底、法令纹、1支弗曼注射泪沟",
        customer="郭璟琳",
    )
    assert lines is not None
    assert lines[0] == "保妥适 250U ▸ 面部皱纹 · 下颌缘 · 颈阔肌 · 腮腺"
    assert len(lines) == 3


def test_no_material_word_falls_back_none():
    # 全程无材料词 → fail-open 回退原串单行（吴玉婷）
    assert parse_title_b("丰唇") is None


def test_empty_and_blank_fall_back_none():
    assert parse_title_b("") is None
    assert parse_title_b("   ") is None


def test_embedded_date_stripped():
    # 林惠贞：多治疗合并目录名嵌第二个日期，结构化行不得残留日期
    lines = parse_title_b("盈致2支注射面颊，娇兰注射唇2026.3.31塑公主4支注射耳基地术前")
    assert lines is not None
    assert all("2026" not in ln and "3.31" not in ln for ln in lines)


def test_customer_name_embedded_is_stripped():
    lines = parse_title_b("丰颜2支注射隆鼻林方如苹果肌", customer="林方如")
    assert lines == ["丰颜 2支 ▸ 隆鼻 · 苹果肌"]


def test_fullwidth_plus_splits_groups():
    # 许楚楚 2026.3.31：全角 ➕ 顶层切分
    lines = parse_title_b("缇颜1支眉弓 越致1支➕海魅骨性1支 注射鼻子下巴")
    assert lines is not None
    assert any(ln.startswith("缇颜 1支") for ln in lines)
    assert any(ln.startswith("越致 1支") for ln in lines)


def test_leading_material_less_segment_keeps_own_line():
    # 首段无材料词 → 独立组保留原样行，不并入后续材料组
    lines = parse_title_b("丰唇、质颜1支注射鼻基底")
    assert lines is not None
    assert lines[0] == "丰唇"
    assert lines[1] == "质颜 1支 ▸ 鼻基底"


def test_material_only_group_no_arrow():
    # 胡志超「普丽妍」孤组：无剂量无部位 → 仅材料词行，无 ▸
    lines = parse_title_b("乔雅登丰颜1下巴，普丽妍+海魅2额结节、面颊、太阳穴")
    assert lines is not None
    assert "普丽妍" in lines
    assert all("▸" not in ln for ln in lines if ln == "普丽妍")
