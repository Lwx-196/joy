"""七维度智能裁切 — 治疗部位驱动的对齐参数计算.

根据治疗部位大小、角度数量、部位位置、多部位覆盖等维度，
动态调整人脸对齐参数（eye_distance_ratio / eye_center_y），
替代固定 0.31/0.37 一刀切。

维度：
1. 角度数量（manifest 中实际可用的角度 slot 数）
2. 部位大小（大/中/小）
3. 部位位置（上/中上/中/下 → eye_center Y 偏移）
4. 源图距离（人脸占源图比例过小 → 裁切级别升一级）
5. 多部位覆盖（联合包围框 → 取最大尺寸分级）
6. 角度匹配（正面看眶周/鼻，侧面看鼻背线/下颌线）
7. 差异幅度（预留迭代）
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 1. 部位大小分级
# ---------------------------------------------------------------------------
SIZE_LARGE = "large"
SIZE_MEDIUM = "medium"
SIZE_SMALL = "small"

REGION_SIZES: dict[str, str] = {
    "面颊": SIZE_LARGE, "下颌线": SIZE_LARGE, "咬肌": SIZE_LARGE,
    "泪沟": SIZE_MEDIUM, "法令纹": SIZE_MEDIUM, "苹果肌": SIZE_MEDIUM,
    "颧骨": SIZE_MEDIUM, "太阳穴": SIZE_MEDIUM, "鼻背": SIZE_MEDIUM,
    "下巴": SIZE_MEDIUM, "额纹": SIZE_MEDIUM,
    "卧蚕": SIZE_SMALL, "眼袋": SIZE_SMALL, "鼻基底": SIZE_SMALL,
    "鼻翼": SIZE_SMALL, "鼻尖": SIZE_SMALL, "唇": SIZE_SMALL,
    "川字": SIZE_SMALL,
}

_SIZE_PRIORITY = {SIZE_LARGE: 0, SIZE_MEDIUM: 1, SIZE_SMALL: 2}

# ---------------------------------------------------------------------------
# 2. 裁切级别 → 对齐比例
# ---------------------------------------------------------------------------
LEVEL_PANORAMIC = "panoramic"
LEVEL_STANDARD = "standard"
LEVEL_MEDIUM = "medium"
LEVEL_CLOSEUP = "closeup"
LEVEL_TIGHT = "tight"

_LEVEL_RATIOS: dict[str, dict[str, float]] = {
    LEVEL_PANORAMIC: {"eye_distance_ratio": 0.30, "base_eye_center_y": 0.37},
    LEVEL_STANDARD:  {"eye_distance_ratio": 0.36, "base_eye_center_y": 0.37},
    LEVEL_MEDIUM:    {"eye_distance_ratio": 0.44, "base_eye_center_y": 0.37},
    LEVEL_CLOSEUP:   {"eye_distance_ratio": 0.54, "base_eye_center_y": 0.37},
    LEVEL_TIGHT:     {"eye_distance_ratio": 0.64, "base_eye_center_y": 0.37},
}

# ---------------------------------------------------------------------------
# 3. 部位垂直位置 → eye_center Y 偏移量
# ---------------------------------------------------------------------------
_REGION_VPOS: dict[str, str] = {
    "额纹": "upper", "川字": "upper",
    "泪沟": "mid_upper", "卧蚕": "mid_upper", "眼袋": "mid_upper",
    "太阳穴": "mid_upper",
    "苹果肌": "mid", "法令纹": "mid", "鼻背": "mid", "颧骨": "mid",
    "面颊": "mid",
    "唇": "lower", "下巴": "lower", "下颌线": "lower", "咬肌": "lower",
    "鼻基底": "lower", "鼻翼": "lower", "鼻尖": "lower",
}

_VPOS_OFFSETS: dict[str, float] = {
    "upper": 0.06,
    "mid_upper": 0.02,
    "mid": 0.0,
    "lower": -0.05,
}

# ---------------------------------------------------------------------------
# 4. 策略矩阵 (angle_count × region_size) → 每个槽位的裁切级别
# ---------------------------------------------------------------------------
_STRATEGY: dict[int, dict[str, list[str]]] = {
    1: {
        SIZE_LARGE:  [LEVEL_STANDARD],
        SIZE_MEDIUM: [LEVEL_MEDIUM],
        SIZE_SMALL:  [LEVEL_CLOSEUP],
    },
    2: {
        SIZE_LARGE:  [LEVEL_STANDARD, LEVEL_MEDIUM],
        SIZE_MEDIUM: [LEVEL_MEDIUM, LEVEL_CLOSEUP],
        SIZE_SMALL:  [LEVEL_MEDIUM, LEVEL_TIGHT],
    },
    3: {
        SIZE_LARGE:  [LEVEL_STANDARD, LEVEL_MEDIUM, LEVEL_CLOSEUP],
        SIZE_MEDIUM: [LEVEL_MEDIUM, LEVEL_CLOSEUP, LEVEL_TIGHT],
        SIZE_SMALL:  [LEVEL_MEDIUM, LEVEL_CLOSEUP, LEVEL_TIGHT],
    },
}

# ---------------------------------------------------------------------------
# 5. 关键词 → atlas 标准键（轻量内联版，不依赖外部 atlas 模块）
# ---------------------------------------------------------------------------
_ALIASES: dict[str, str] = {
    "面颊凹陷": "面颊", "脸颊": "面颊", "面中": "苹果肌", "面中支撑": "苹果肌",
    "印第安纹": "泪沟", "法令": "法令纹",
    "山根": "鼻背", "鼻梁": "鼻背", "鼻子": "鼻背",
    "隆鼻": "鼻背", "下颌缘": "下颌线", "下颌": "下颌线", "轮廓线": "下颌线",
    "轮廓": "下颌线", "川字纹": "川字", "眉间": "川字", "抬头纹": "额纹",
    "额头": "额纹", "抬头": "额纹", "颞": "太阳穴", "颞部": "太阳穴",
    "瘦脸": "咬肌", "口周": "唇", "嘴": "唇", "丰唇": "唇",
}


def _resolve(area: str) -> str | None:
    """把 focus area 名映射到 atlas 标准键。"""
    if area in REGION_SIZES:
        return area
    if area in _ALIASES:
        return _ALIASES[area]
    for key in REGION_SIZES:
        if key in area:
            return key
    for alias, key in _ALIASES.items():
        if alias in area:
            return key
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_region_size(region_keys: list[str]) -> str:
    """多部位取最小尺寸分级（需要最大放大倍率的部位决定裁切）。"""
    if not region_keys:
        return SIZE_MEDIUM
    sizes = [REGION_SIZES.get(k, SIZE_MEDIUM) for k in region_keys]
    return max(sizes, key=lambda s: _SIZE_PRIORITY[s])


def compute_eye_center_y_offset(region_keys: list[str]) -> float:
    """按部位垂直位置计算 eye_center Y 偏移（维度 3）。

    多部位时按权重（主部位优先）加权平均。
    """
    if not region_keys:
        return 0.0
    offsets = [_VPOS_OFFSETS[_REGION_VPOS.get(k, "mid")] for k in region_keys]
    if len(offsets) == 1:
        return offsets[0]
    total = sum(offsets[i] * (len(offsets) - i) for i in range(len(offsets)))
    weights = sum(len(offsets) - i for i in range(len(offsets)))
    return total / weights if weights else 0.0


def crop_levels_for_slots(angle_count: int, region_size: str) -> list[str]:
    """查策略矩阵，返回每个槽位的裁切级别列表（维度 1+2）。"""
    clamped = max(1, min(3, angle_count))
    row = _STRATEGY.get(clamped, _STRATEGY[3])
    return list(row.get(region_size, row[SIZE_MEDIUM]))


def compute_smart_alignment(
    cell_size: tuple[int, int],
    focus_targets: list[dict] | None,
    slot_index: int,
    angle_count: int,
    source_face_ratio: float | None = None,
) -> tuple[float, float, float, dict]:
    """计算指定槽位的智能对齐参数。

    Parameters
    ----------
    cell_size : (width, height) 渲染单元格像素尺寸
    focus_targets : parse_focus_targets() 输出（可空→全降级到标准）
    slot_index : 当前槽位在渲染序列中的索引（0=最高优先角度）
    angle_count : 本次渲染的总角度数
    source_face_ratio : 源图中人脸占画面比（0~1），None 则跳过维度 4

    Returns
    -------
    (target_eye_distance, eye_center_x, eye_center_y, debug_info)
    """
    width, height = cell_size

    if not focus_targets:
        return (
            width * 0.31,
            width / 2,
            height * 0.37,
            {"mode": "fallback", "level": LEVEL_STANDARD},
        )

    region_keys = []
    for t in focus_targets:
        resolved = _resolve(t.get("area", ""))
        if resolved:
            region_keys.append(resolved)

    region_size = classify_region_size(region_keys)
    levels = crop_levels_for_slots(angle_count, region_size)
    level = levels[min(slot_index, len(levels) - 1)]

    # 维度 4：源图距离适配 — 人脸占源图比 < 30% 认为远距离拍摄，裁切升一级
    if source_face_ratio is not None and source_face_ratio < 0.30:
        upgrade = {
            LEVEL_PANORAMIC: LEVEL_STANDARD,
            LEVEL_STANDARD: LEVEL_MEDIUM,
            LEVEL_MEDIUM: LEVEL_CLOSEUP,
            LEVEL_CLOSEUP: LEVEL_TIGHT,
            LEVEL_TIGHT: LEVEL_TIGHT,
        }
        level = upgrade[level]

    ratios = _LEVEL_RATIOS[level]
    eye_distance_ratio = ratios["eye_distance_ratio"]
    eye_center_y_ratio = ratios["base_eye_center_y"]

    y_offset = compute_eye_center_y_offset(region_keys)
    eye_center_y_ratio += y_offset

    eye_distance_ratio = max(0.22, min(0.70, eye_distance_ratio))
    eye_center_y_ratio = max(0.25, min(0.50, eye_center_y_ratio))

    return (
        width * eye_distance_ratio,
        width / 2,
        height * eye_center_y_ratio,
        {
            "mode": "smart",
            "regions": region_keys,
            "region_size": region_size,
            "level": level,
            "eye_distance_ratio": round(eye_distance_ratio, 3),
            "eye_center_y_ratio": round(eye_center_y_ratio, 3),
            "y_offset": round(y_offset, 3),
            "source_face_ratio": source_face_ratio,
        },
    )
