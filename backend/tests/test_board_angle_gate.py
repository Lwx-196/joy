"""G1 板级角度覆盖 gate 单测。

fixture 全部取自 2026-06-11 32 板全量审核波的真实术式目录名：
6 个 B 类 REJECT 必须拦截（其中 陈英凯/欧美吟 judge clean 漏检 = gate 存在的理由），
PASS 板不得误伤，fail-open 边界不误杀。
"""

from __future__ import annotations

import pytest

from backend.services import board_angle_gate as gate


FRONT_ONLY = {"front"}
OBLIQUE_ONLY = {"oblique"}
ALL_VIEWS = {"front", "oblique", "side"}


# === 本波 6 个 B 类 REJECT：板上实有角度不满足项目必需角度 → 必须 held ===
B_CLASS_REJECTS = [
    # (术式目录名, 板上实有角度, 必须报缺的部位)
    ("2025.3.19耳基底", FRONT_ONLY, "耳基底"),                      # 陈英凯，judge clean 漏检
    ("2025.6.18耳基底+颞部填充", FRONT_ONLY, "耳基底"),              # 欧美吟，judge clean 漏检
    ("2025.12.3玻尿酸下巴+太阳穴", FRONT_ONLY, "下巴"),              # 吕碧英
    ("2025.12.26乔雅登丰颜隆鼻+鼻基底", FRONT_ONLY, "鼻背"),         # 蓝凤端
    ("2026.1.7唇、法令纹、川字纹填充", OBLIQUE_ONLY, "唇"),           # 江佳慧（缺正面）
    ("2025.9.10玻尿酸隆鼻", FRONT_ONLY, "鼻背"),                    # 小玉姐
]


@pytest.mark.parametrize("treatment,available,expected_region", B_CLASS_REJECTS)
def test_b_class_rejects_held(treatment, available, expected_region):
    result = gate.evaluate_angle_coverage(treatment, available)
    assert result["verdict"] == gate.VERDICT_HELD
    assert result["fail_open"] is False
    missing_regions = {m["region"] for m in result["missing"]}
    assert expected_region in missing_regions


def test_b_class_pass_when_required_view_present():
    """同样的项目，板上补齐必需角度后必须放行。"""
    for treatment, _available, _region in B_CLASS_REJECTS:
        result = gate.evaluate_angle_coverage(treatment, ALL_VIEWS)
        assert result["verdict"] == gate.VERDICT_PASS, treatment
        assert result["missing"] == []


# === 本波 PASS 板：gate 不得误伤 ===
PASS_BOARDS = [
    # (术式目录名, 板上实有角度) —— 角度按审核已确认覆盖
    ("2026.2.10玻尿酸注射下巴", {"front", "side"}),                  # 刘亦卿
    ("2025.11.11乔雅登丰颜1下巴，普丽妍+海魅2额结节、面颊、太阳穴",
     ALL_VIEWS),                                                    # 胡志超
    ("2026.4.1弗缦1.0注射泪沟", FRONT_ONLY),                        # 郭若煊（泪沟只需正面）
]


@pytest.mark.parametrize("treatment,available", PASS_BOARDS)
def test_pass_boards_not_blocked(treatment, available):
    result = gate.evaluate_angle_coverage(treatment, available)
    assert result["verdict"] == gate.VERDICT_PASS
    assert result["missing"] == []


def test_front_required_regions_held_without_front():
    """纹类/浅表精修类只有 45°/侧面 → held（江佳慧模式）。"""
    result = gate.evaluate_angle_coverage(
        "2025.10.29熊猫针1支注射川字纹", {"oblique", "side"})
    assert result["verdict"] == gate.VERDICT_HELD
    assert {m["region"] for m in result["missing"]} == {"川字"}


def test_mixed_requirements_each_checked():
    """同板混合 front + profile 要求：逐项核对，只报真缺的。"""
    treatment = "保妥适（下颌缘）、1支盈致丰鼻基底、法令纹、1支弗缦注射泪沟"
    # 全角度 → pass
    assert gate.evaluate_angle_coverage(treatment, ALL_VIEWS)["verdict"] == gate.VERDICT_PASS
    # 仅正面 → 下颌线/鼻基底 报缺，泪沟/法令纹不报
    result = gate.evaluate_angle_coverage(treatment, FRONT_ONLY)
    assert result["verdict"] == gate.VERDICT_HELD
    missing = {m["region"] for m in result["missing"]}
    assert missing == {"下颌线", "鼻基底"}


def test_extra_keyword_temple_alias():
    """「颞部」走 atlas alias → 太阳穴 → 必须 45°/侧面。"""
    result = gate.evaluate_angle_coverage("颞部填充", FRONT_ONLY)
    assert result["verdict"] == gate.VERDICT_HELD
    assert {m["region"] for m in result["missing"]} == {"太阳穴"}


def test_empty_available_views_with_gated_region_held():
    """manifest 异常（0 slot 入选）但项目有角度要求 → held 不出板。"""
    result = gate.evaluate_angle_coverage("2025.9.10玻尿酸隆鼻", set())
    assert result["verdict"] == gate.VERDICT_HELD


# === fail-open 边界：解析不出登记部位 → 放行不误杀 ===
FAIL_OPEN_CASES = [
    "",                                  # 空
    None,                                # None
    "2025.7.18直角肩瘦肩针",              # 身体部位，非面部 atlas
    "2026.1.1嗨体颈纹",                   # 颈纹不在 gate 表
    "2025.5.5苹果肌填充",                 # 苹果肌正面可读，标准 v1 未设要求 → 不进表
]


@pytest.mark.parametrize("treatment", FAIL_OPEN_CASES)
def test_fail_open_pass(treatment):
    result = gate.evaluate_angle_coverage(treatment, FRONT_ONLY)
    assert result["verdict"] == gate.VERDICT_PASS
    assert result["fail_open"] is True
    assert result["reason"] == "no_gated_region"


def test_ear_base_typo_variant():
    """库内真实错别字「耳基地」（林惠贞 2026.3.31 目录）也必须命中耳基底要求。"""
    result = gate.evaluate_angle_coverage("塑公主4支注射耳基地术前", FRONT_ONLY)
    assert result["verdict"] == gate.VERDICT_HELD
    assert {m["region"] for m in result["missing"]} == {"耳基底"}


def test_required_views_for_treatment_dedup():
    """「下颌缘」补充关键词与 atlas 键「下颌线」共存时不重复。"""
    req = gate.required_views_for_treatment("下颌缘+下颌线提升")
    assert list(req).count("下颌线") == 1
    assert req["下颌线"] == gate.PROFILE_VIEWS
