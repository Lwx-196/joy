"""G2 板级配对 gate：术前/术后终格眼距比的灾难级兜底核对。

定位（2026-06-11 标定拍板）：**抓配对灾难（审核标准 v1 C 条），不复现感知级判断**。
19 板标定（11 G2 感知类 + 8 PASS 对照）实测结论：
- 感知类「脸大小/对齐」是 发型轮廓×躯干占比×裁切松紧×垂直偏移 的复合感知，
  eye_ratio/dy 阈值无法线性分离（clean 集自身含 eye_ratio 0.863 / dy +0.108，
  与 bad 集的 0.860 / -0.125 仅差 0.003-0.004，零安全边际）→ 感知类继续归
  VLM judge（本波 11 板 judge 全标 blocker，覆盖成立）。
- C 灾难 3 板信号 = 极端值且与 clean 完美分离：
  曾瑜勤 1.666 / 高雅静 0.677 / 黄靖榕 0.632（front 行实测）
  vs clean front 集 [0.863, 1.069] → 阈值 [0.78, 1.30] 双向 margin ≥0.08。
增量价值（judge 已抓本波 3/3 的前提下）：确定性（许3.31 judge 波动前科）、
judge 断线兜底（6-10 D6 judge 全断真实发生）、为 tier-2 保护区 scale 补偿
提供控制信号。

纪律：
- 信号源 = render_plan slots 的 ``pair_eye_signal``（render_brand_clean
  保护路径解析计算：源图眼距 × 终格 scale，零额外检测零烧钱）。
- 只评 front 槽（oblique/side 眼距被 yaw 透视主导，标定证实是噪音）。
- fail-open：无 render_plan / 无 front 信号 / 信号 invalid / 内部异常 → pass。
- owner 手工修过的槽（manual_preop_transform enabled）跳过：人工已修，
  解析信号不再反映板面真实，gate 不得把人工修复板永久 HELD。
"""

from __future__ import annotations

from typing import Any

# front 终格眼距比阈值（19+3 板标定，见模块 docstring）
FRONT_EYE_RATIO_MIN = 0.78
FRONT_EYE_RATIO_MAX = 1.30

VERDICT_PASS = "pass"
VERDICT_HELD = "held"


def _slot_records(render_plan: Any) -> list[dict]:
    if not isinstance(render_plan, dict):
        return []
    slots = render_plan.get("slots")
    return [r for r in slots if isinstance(r, dict)] if isinstance(slots, list) else []


def evaluate_pair_coverage(render_plan: Any) -> dict[str, Any]:
    """核对板 render_plan 的 front 槽配对眼距比是否在灾难阈值内。

    返回 dict：
    - ``verdict``: "pass" / "held"
    - ``violations``: [{slot, eye_ratio, allowed}]（held 时非空）
    - ``signals``: {slot: eye_ratio}（全部已评信号，可观测性）
    - ``fail_open``: True = 无可评信号或内部异常，直接放行
    - ``reason``: fail_open / held 的简短原因
    """
    result: dict[str, Any] = {
        "verdict": VERDICT_PASS,
        "violations": [],
        "signals": {},
        "fail_open": False,
        "reason": "",
    }
    try:
        records = _slot_records(render_plan)
        manual_slots = {
            r.get("slot")
            for r in records
            if (r.get("manual_preop_transform") or {}).get("enabled")
        }
        evaluated = False
        for rec in records:
            slot = rec.get("slot")
            if slot != "front" or slot in manual_slots:
                continue
            signal = rec.get("pair_eye_signal")
            if not isinstance(signal, dict) or not signal.get("valid"):
                continue
            ratio = signal.get("eye_ratio")
            if not isinstance(ratio, (int, float)) or ratio <= 0:
                continue
            evaluated = True
            result["signals"][slot] = ratio
            if not (FRONT_EYE_RATIO_MIN <= ratio <= FRONT_EYE_RATIO_MAX):
                result["violations"].append({
                    "slot": slot,
                    "eye_ratio": ratio,
                    "allowed": [FRONT_EYE_RATIO_MIN, FRONT_EYE_RATIO_MAX],
                })
        if not evaluated:
            result["fail_open"] = True
            result["reason"] = "no_evaluable_front_signal"
            return result
        if result["violations"]:
            result["verdict"] = VERDICT_HELD
            result["reason"] = "front_eye_ratio_out_of_range"
        return result
    except Exception as exc:  # noqa: BLE001 — gate 永不让板子因自身崩溃
        result["verdict"] = VERDICT_PASS
        result["fail_open"] = True
        result["reason"] = f"gate_error: {exc}"
        return result


__all__ = [
    "FRONT_EYE_RATIO_MIN",
    "FRONT_EYE_RATIO_MAX",
    "VERDICT_PASS",
    "VERDICT_HELD",
    "evaluate_pair_coverage",
]
