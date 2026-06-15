"""parse_case_meta 单测：treatment 目录名带客户名前缀时的标题解析。

背景（2026-06-10 验证集）：旧 DATE_RE 锚定开头，「林方如2026.4.1盈致…」
不匹配 → date fallback 成整串目录名（日期 chip 溢出画布）+ project 残留
客户名前缀（与板头客户名重复）。
"""
from pathlib import Path

from scripts.render_brand_clean import parse_case_meta


def test_plain_date_prefix_unchanged():
    meta = parse_case_meta(Path("/cases/刘亦卿/2026.2.10玻尿酸注射下巴"))
    assert meta == {
        "date": "2026.2.10",
        "customer_name": "刘亦卿",
        "project": "玻尿酸注射下巴",
    }


def test_customer_name_prefix_extracts_date_and_strips_name():
    # 客户名前缀剥离 + 尾部 phase 标记「术前」剥离（2026-06-15 修复）
    meta = parse_case_meta(Path("/cases/林方如/林方如2026.4.1盈致1支下巴，颏肌释放术前"))
    assert meta["date"] == "2026.4.1"
    assert meta["customer_name"] == "林方如"
    assert meta["project"] == "盈致1支下巴，颏肌释放"


def test_two_digit_year():
    meta = parse_case_meta(Path("/cases/赵建芬/25.6.4嗨体填泪沟，唇，口角溶脂"))
    assert meta["date"] == "25.6.4"
    assert meta["project"] == "嗨体填泪沟，唇，口角溶脂"


def test_no_date_falls_back_to_case_name():
    meta = parse_case_meta(Path("/cases/王某/隆鼻修复"))
    assert meta["date"] == "隆鼻修复"
    assert meta["project"] == "隆鼻修复"


def test_decimal_token_not_mistaken_for_date():
    # 弗缦1.0 等版本号不含三段数字，不应误判
    meta = parse_case_meta(Path("/cases/黄丽玲/2025.12.10弗缦1.0+0.5注射泪沟"))
    assert meta["date"] == "2025.12.10"
    assert meta["project"] == "弗缦1.0+0.5注射泪沟"


# --- 尾部 phase 标记（术前/术后/术中）剥离（2026-06-15 修复，陈小玲板头 bug）---
# 源图文件夹常以 phase 标记结尾（数据组织痕迹），泄漏进 project 会让板头标题挂单相
# 「隆鼻术前」误导（板面本就同时展示术前+术后）。13/114 真实 treatment 目录中招。


def test_phase_suffix_pre_stripped():
    # 陈小玲：板头从「菲林普利 1组 ▸ 隆鼻术前」修为「隆鼻」
    meta = parse_case_meta(Path("/cases/陈小玲/2026.4.1菲林普利 1组注射隆鼻术前"))
    assert meta["project"] == "菲林普利 1组注射隆鼻"


def test_phase_suffix_post_stripped():
    meta = parse_case_meta(Path("/cases/林惠贞/2026.3.31塑公主4支注射耳基底术后"))
    assert meta["project"] == "塑公主4支注射耳基底"


def test_phase_suffix_space_separated_stripped():
    # 空格分隔的 phase 标记也剥（阮静怡）
    meta = parse_case_meta(Path("/cases/阮静怡/2025.10.29瘦肩、反重力提升 术后"))
    assert meta["project"] == "瘦肩、反重力提升"


def test_phase_suffix_combined_pre_post_stripped():
    # 「术前术后」叠加整体剥（袁霞）
    meta = parse_case_meta(
        Path("/cases/袁霞/2025.9.23普丽妍颞区，弗缦泪沟，熊猫针口角，保妥适除皱术前术后")
    )
    assert meta["project"] == "普丽妍颞区，弗缦泪沟，熊猫针口角，保妥适除皱"


def test_phase_marker_in_middle_not_stripped():
    # 「术后修复」中段 phase 词是合法项目名，不在结尾不剥
    meta = parse_case_meta(Path("/cases/某/2026.5.1眼周术后修复"))
    assert meta["project"] == "眼周术后修复"


def test_bare_surgery_word_not_stripped():
    # 「术」结尾但非术前/后/中，不误伤
    meta = parse_case_meta(Path("/cases/某/2026.6.1双眼皮术"))
    assert meta["project"] == "双眼皮术"


def test_phase_only_falls_back_to_original():
    # 极端：去掉 phase 后为空 → fail-safe 保留原值，不返回空串
    meta = parse_case_meta(Path("/cases/某/2026.2.10术前"))
    assert meta["project"] == "术前"
