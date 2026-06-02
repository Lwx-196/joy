"""Effect-projection delivery lane — case selection + evidence-anchored effect_pairs.

anchored-sim Phase 4 生产 selector。两层职责，明确分开：

1. ``resolve_effect_pairs`` — 把品牌标注的 case 文件夹名映射成 evidence-anchored
   ``(project, region)`` pairs（反臆造 fail-closed：只保留有 registered effect_row
   的部位，无循证依据的 drop）。**从 calibration harness 物理迁移而来**，builder 反向
   import 它（避免两份漂移）。calibration 用它测全部效果类型（含鼻/颏侧脸），**不套
   下面的 scope gate**。

2. ``scope_gate`` / ``select_effect_eligible`` — 上线 scope 过滤，**仅 effect 投影交付
   lane 用**。只放行 owner greenlight 的正脸清晰类型（泪沟/苹果肌/唇/法令纹/卧蚕）；
   侧脸主导（鼻/颏）、纯动态纹（川字/额/鱼尾）、其它已注册但未greenlight（太阳穴/咬肌）
   全部 skip + 记明确原因（fail-closed 透明，不静默丢）。
"""
from __future__ import annotations

from typing import Any

from backend.services import procedure_region_mappings as prm

# 上线 scope（owner 2026-06-02 拍板）：正脸清晰类型。侧脸主导（鼻背/鼻基底/下巴）+
# 纯动态纹（川字/额纹/鱼尾纹）+ 其它已注册但未greenlight（太阳穴/咬肌）暂不上线。
LAUNCH_SCOPE_REGIONS: frozenset[str] = frozenset({"泪沟", "苹果肌", "唇", "法令纹", "卧蚕"})


def resolve_effect_pairs(
    case_name: str,
) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    """case 文件夹名 → evidence-anchored ``(project, region)`` pairs（反臆造 fail-closed）。

    ``parse_procedures`` 把品牌标注的文件夹名映射成结构化术式；只保留有 registered
    ``effect_row`` 的 ``(project, region)`` pair——无循证依据的部位 drop（绝不编造效果）。
    返回 ``(pairs, parsed)``，``parsed`` 含 ``needs_human_review`` / ``unknown_segments``
    供调用方透明上报。
    """
    parsed = prm.parse_procedures(case_name)
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for proc in parsed.get("procedures", []):
        project = str(proc.get("project") or "").strip()
        if not project:
            continue
        for region in proc.get("regions", []):
            key = (project, str(region))
            if key in seen:
                continue
            if prm.effect_row(project, str(region)) is not None:
                pairs.append(key)
                seen.add(key)
    return pairs, parsed


def scope_skip_reason(region: str) -> str | None:
    """None = 在上线 scope 内（eligible）；否则返回明确 skip 原因。"""
    if region in LAUNCH_SCOPE_REGIONS:
        return None
    view = prm.REGION_EFFECT_VIEW.get(region)
    if view == "profile":
        return f"profile_only:{region}（侧脸主战场，正脸价值有限，上线不投）"
    if view == "expression":
        return f"expression_only:{region}（肉毒动态纹，静态正脸≈无变化，上线不投）"
    return f"out_of_launch_scope:{region}（未在 owner greenlight 的正脸清晰类型）"


def scope_gate(
    pairs: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[str]]:
    """把 effect_pairs 按上线 scope 过滤；返回 ``(in_scope_pairs, skip_reasons)``。"""
    in_scope: list[tuple[str, str]] = []
    skipped: list[str] = []
    for project, region in pairs:
        reason = scope_skip_reason(region)
        if reason is None:
            in_scope.append((project, region))
        else:
            skipped.append(reason)
    return in_scope, skipped


def select_effect_eligible(case_names: list[str]) -> list[dict[str, Any]]:
    """对一组 case 名做 effect-eligibility 判定（lane discover 用）。

    每 case 返回 dict：``case_name`` / ``effect_pairs``（in-scope）/ ``eligible``（bool）/
    ``skip_reasons``（fail-closed 透明，不静默）/ ``parsed``。``eligible`` 仅当有 in-scope
    pair 才 True。
    """
    results: list[dict[str, Any]] = []
    for name in case_names:
        pairs, parsed = resolve_effect_pairs(name)
        in_scope, scope_skips = scope_gate(pairs)
        reasons: list[str] = list(scope_skips)
        if not pairs:
            reasons.append(
                "no_evidence_anchored_pairs "
                f"(needs_human_review={parsed.get('needs_human_review')})"
            )
        results.append(
            {
                "case_name": name,
                "effect_pairs": in_scope,
                "eligible": bool(in_scope),
                "skip_reasons": reasons,
                "parsed": parsed,
            }
        )
    return results
