"""G1 板级角度覆盖 gate：项目部位 → 板上必需展示角度 的零成本结构核对。

依据板审核标准 v1 B 条（owner 2026-06-11 口述定锚，全文见
~/.claude/memory/projects/case-workbench.md 末节）：项目部位需要的展示角度必须在板上，
缺关键角度 = 整板不合格（owner 原话「宁可不要，不合格」）。

必要性实锤（2026-06-11 32 板全量审核波）：6/32 板因 B 条否决，其中 陈英凯/欧美吟
（耳基底仅正面）VLM judge verdict=clean 完全漏检——judge 看工艺不看「项目↔角度」结构，
本 gate 是唯一拦截层。

纪律：
- 零烧钱：纯文本解析（atlas.extract_regions）+ slot 集合核对，不碰图不调 API。
- fail-open：解析不出任何登记部位 / 输入异常 → pass 不挡板（与
  procedure_region_mappings.has_immediate_visible_effect 同策略，不误杀）。
- 反臆造：映射表只录 审核标准 v1 B 条 明示的部位类；未明示部位（苹果肌/咬肌/鼻翼等）
  不进表 = 不设要求，绝不推断。
"""

from __future__ import annotations

from typing import Any

from backend.services import facial_region_atlas as atlas

VIEW_FRONT = "front"
VIEW_OBLIQUE = "oblique"
VIEW_SIDE = "side"

# any-of 语义：命中其一即满足
PROFILE_VIEWS: frozenset[str] = frozenset({VIEW_OBLIQUE, VIEW_SIDE})
FRONT_VIEWS: frozenset[str] = frozenset({VIEW_FRONT})

# === atlas 区 → 板上必需角度（审核标准 v1 B 条逐字对应）===
# 「隆鼻/下巴/鼻基底/耳基底 → 侧面或 45°；面颊/轮廓/下颌缘 → 必须有 45° 或侧面；
#   泪沟/卧蚕/川字纹/口周 → 正面可读」
REGION_REQUIRED_VIEWS: dict[str, frozenset[str]] = {
    # 突度/轮廓类：正面读不出对比 → 必须 45° 或侧面
    "鼻背": PROFILE_VIEWS,
    "鼻尖": PROFILE_VIEWS,
    "鼻基底": PROFILE_VIEWS,
    "下巴": PROFILE_VIEWS,
    "面颊": PROFILE_VIEWS,
    "颧骨": PROFILE_VIEWS,
    "下颌线": PROFILE_VIEWS,
    "太阳穴": PROFILE_VIEWS,
    # 纹类/浅表精修类：必须正面可读
    "泪沟": FRONT_VIEWS,
    "卧蚕": FRONT_VIEWS,
    "眼袋": FRONT_VIEWS,
    "川字": FRONT_VIEWS,
    "额纹": FRONT_VIEWS,
    "法令纹": FRONT_VIEWS,
    "唇": FRONT_VIEWS,
}

# === atlas 没收录、但板级 gate 必须识别的部位关键词 ===
# 耳基底无 facemesh 几何（atlas.extract_regions 抓不到）= 陈英凯/欧美吟漏检根因；
# 「下颌缘」不是 atlas 键「下颌线」的子串，substring 匹配不命中。
# 值 = (gate 展示用部位名, 必需角度)。
EXTRA_GATE_KEYWORDS: dict[str, tuple[str, frozenset[str]]] = {
    "耳基底": ("耳基底", PROFILE_VIEWS),
    "耳基地": ("耳基底", PROFILE_VIEWS),  # 库内真实错别字（林惠贞 2026.3.31 目录实证）
    "下颌缘": ("下颌线", PROFILE_VIEWS),
    "口周": ("唇", FRONT_VIEWS),
    "印堂": ("川字", FRONT_VIEWS),
    "眉弓": ("眉弓", FRONT_VIEWS),
}

VERDICT_PASS = "pass"
VERDICT_HELD = "held"


def required_views_for_treatment(treatment: str) -> dict[str, frozenset[str]]:
    """术式目录名 → {部位: 必需角度集合}（仅登记部位；空 dict = 无角度要求）。"""
    text = (treatment or "").strip()
    if not text:
        return {}
    requirements: dict[str, frozenset[str]] = {}
    for region in atlas.extract_regions(text):
        required = REGION_REQUIRED_VIEWS.get(region)
        if required:
            requirements[region] = required
    for keyword, (label, required) in EXTRA_GATE_KEYWORDS.items():
        if keyword in text:
            requirements.setdefault(label, required)
    return requirements


def evaluate_angle_coverage(treatment: str, available_views: Any) -> dict[str, Any]:
    """核对板上实有角度是否覆盖项目部位的必需角度。

    ``available_views`` = 板上有素材入选的 slot 集合（front/oblique/side，
    取自 manifest groups[].selected_slots 非空键）。

    返回 dict：
    - ``verdict``: "pass" / "held"
    - ``missing``: [{region, required_any_of, available}]（held 时非空）
    - ``required``: {部位: [必需角度]}（本板的全部角度要求，可观测性用）
    - ``fail_open``: True = 未解析出登记部位或内部异常，直接放行
    - ``reason``: fail_open / held 的简短原因
    """
    result: dict[str, Any] = {
        "verdict": VERDICT_PASS,
        "missing": [],
        "required": {},
        "fail_open": False,
        "reason": "",
    }
    try:
        requirements = required_views_for_treatment(treatment)
        if not requirements:
            result["fail_open"] = True
            result["reason"] = "no_gated_region"
            return result
        result["required"] = {label: sorted(req) for label, req in requirements.items()}
        available = {str(v) for v in (available_views or ())}
        missing = [
            {
                "region": label,
                "required_any_of": sorted(required),
                "available": sorted(available),
            }
            for label, required in requirements.items()
            if not (required & available)
        ]
        if missing:
            result["verdict"] = VERDICT_HELD
            result["missing"] = missing
            result["reason"] = "missing_required_view"
        return result
    except Exception as exc:  # noqa: BLE001 — gate 永不让板子因自身崩溃
        result["verdict"] = VERDICT_PASS
        result["fail_open"] = True
        result["reason"] = f"gate_error: {exc}"
        return result


__all__ = [
    "REGION_REQUIRED_VIEWS",
    "EXTRA_GATE_KEYWORDS",
    "PROFILE_VIEWS",
    "FRONT_VIEWS",
    "VERDICT_PASS",
    "VERDICT_HELD",
    "required_views_for_treatment",
    "evaluate_angle_coverage",
]
