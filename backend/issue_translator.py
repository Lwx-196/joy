"""Map blocking_issues English flags to Chinese with next-step hints (P0-2).

B2 schema v2: a blocking issue is now a dict
    { "code": "<flag>", "files": ["before-1.jpg"], "severity": "block"|"warn" }
Old shape (bare string) is still accepted on read — `normalize_issue` upcasts.
The translator output adds zh + next while preserving files + severity.
"""
from __future__ import annotations

from typing import Any

ISSUE_DICT: dict[str, dict[str, str]] = {
    "face_detection_failure": {
        "zh": "图片中没有检测到清晰人脸",
        "next": "用术前 1 / 术后 1 这类标准正面图复查；剔除遮挡或角度过大的样本",
    },
    "pose_delta_exceeded": {
        "zh": "术前/术后姿态偏差超过阈值",
        "next": "重拍同角度，或在 case-layout-board 预检时显式放宽 tolerance",
    },
    "direction_mismatch": {
        "zh": "术前/术后人脸朝向不一致",
        "next": "确认两张图都是正面或同侧 45°/侧面",
    },
    "sharp_ratio_low": {
        "zh": "术前/术后清晰度差异过大",
        "next": "用同光源/同距离重拍模糊那张图",
    },
    "no_labeled_sources": {
        "zh": "目录里没有 术前/术后 命名",
        "next": "先做案例整理，按 术前-正面 / 术后-右45侧 改名",
    },
    "missing_front": {
        "zh": "缺少正面对比",
        "next": "补一张术前正面或术后正面",
    },
    "missing_oblique": {
        "zh": "缺少 45° 侧面对比",
        "next": "补 45° 侧面，或自动降级为 single-compare",
    },
    "missing_side": {
        "zh": "缺少侧面对比",
        "next": "补侧面，或自动降级为 bi-compare",
    },
    "ambiguous_candidates": {
        "zh": "候选图过多，无法自动配对",
        "next": "先收敛候选，每阶段保留 1-2 张代表图",
    },
    "body_section_visual_mismatch": {
        "zh": "身体案例正/背面视觉不匹配（前后人脸可见性不一致）",
        "next": "确认 front 都看得到正脸、back 都看不到正脸",
    },
    "screen_timeout": {
        "zh": "视觉判读超时",
        "next": "保持网络稳定后重跑；或减少单次扫描案例数",
    },
    "quality_poor": {
        "zh": "图片质量评分偏低",
        "next": "补拍清晰图或剔除该角度",
    },
    "not_case_source_directory": {
        "zh": "不是案例源照片目录",
        "next": "保留为素材归档，不进入正式出图；如需要出图，请绑定或补充真实术前/术后源目录",
    },
}

# Codes that are warnings rather than hard blockers (still surface in UI but as "warn").
# Empty for now — refine when the scanner emits warn-level codes.
WARN_CODES: set[str] = set()


def normalize_issue(item: Any) -> dict[str, Any]:
    """Coerce either the legacy bare string or the v2 object into the v2 shape.

    Legacy:   "face_detection_failure"
              -> {"code": "face_detection_failure", "files": [], "severity": "block"}
    V2:       {"code": "...", "files": [...], "severity": "..."}
              -> same, with missing keys filled in
    """
    if isinstance(item, str):
        return {
            "code": item,
            "files": [],
            "severity": "warn" if item in WARN_CODES else "block",
        }
    if isinstance(item, dict):
        code = str(item.get("code") or "")
        files_raw = item.get("files") or []
        files = [str(f) for f in files_raw if f]
        severity = item.get("severity") or ("warn" if code in WARN_CODES else "block")
        return {"code": code, "files": files, "severity": severity}
    # Anything else — silently drop. Caller filters truthy codes downstream.
    return {"code": "", "files": [], "severity": "block"}


def translate(item: Any) -> dict[str, Any]:
    """Translate a single issue (legacy string or v2 object).
    Returns: {code, zh, next, files, severity}"""
    norm = normalize_issue(item)
    code = norm["code"]
    if not code:
        return {
            "code": code,
            "zh": "未知阻塞码",
            "next": "联系维护者补充错误码字典",
            "files": norm["files"],
            "severity": norm["severity"],
        }
    entry = ISSUE_DICT.get(code)
    if entry:
        return {
            "code": code,
            "zh": entry["zh"],
            "next": entry["next"],
            "files": norm["files"],
            "severity": norm["severity"],
        }
    return {
        "code": code,
        "zh": f"未知阻塞码：{code}",
        "next": "联系维护者补充错误码字典",
        "files": norm["files"],
        "severity": norm["severity"],
    }


def translate_list(items: list[Any]) -> list[dict[str, Any]]:
    return [translate(it) for it in items]


def merge_codes(items: list[Any]) -> list[Any]:
    """Merge legacy strings + v2 objects into a unique-by-code list, preserving the
    most informative entry (object > string when same code appears twice)."""
    seen: dict[str, dict[str, Any]] = {}
    for it in items:
        norm = normalize_issue(it)
        code = norm["code"]
        if not code:
            continue
        existing = seen.get(code)
        if existing is None:
            seen[code] = norm
            continue
        # Merge files, prefer max severity (block > warn).
        merged_files = list({*existing["files"], *norm["files"]})
        sev = "block" if "block" in {existing["severity"], norm["severity"]} else "warn"
        seen[code] = {"code": code, "files": merged_files, "severity": sev}
    return list(seen.values())


def all_entries() -> list[dict[str, str]]:
    return [{"code": code, **info} for code, info in ISSUE_DICT.items()]
