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
    meta = parse_case_meta(Path("/cases/林方如/林方如2026.4.1盈致1支下巴，颏肌释放术前"))
    assert meta["date"] == "2026.4.1"
    assert meta["customer_name"] == "林方如"
    assert meta["project"] == "盈致1支下巴，颏肌释放术前"


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
