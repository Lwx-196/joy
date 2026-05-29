"""Facial region atlas — 医美治疗部位 → MediaPipe FaceMesh(478) 关键点映射知识库.

固定知识库（owner 2026-05-29 要求）：把医美注射治疗部位精确锚定到
MediaPipe FaceLandmarker(Tasks API, refine_landmarks=True, 478 点) 关键点，
让任何要标注的部位都能精确定位，不再 ad-hoc 猜椭圆。

索引来源 + 置信度（见 delivery/facial-region-atlas.md 全文 + 来源 URL）：
- "high"     = 索引直接出自官方 face_mesh_connections.py（FACE_OVAL/LIPS/EYE/EYEBROW/NOSE/IRIS）。
- "inferred" = 据拓扑/社区图（Sander de Snaijer 等）推断，颊内部/眶下区点稀疏，**需实测校准**。

左右约定（关键坑）：``left_*`` / ``right_*`` = **受试者解剖侧**（patient's own side）。
正脸非镜像照里，受试者 right = 图像左侧。医美"左泪沟/右法令纹"按患者本人解剖侧对齐。
"""

from __future__ import annotations

from typing import Any

# --- 官方 connections 锚点（HIGH，出自 mediapipe face_mesh_connections.py）---
FACEMESH_ANCHORS: dict[str, Any] = {
    "face_oval": [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397,
                  365, 379, 378, 400, 377, 152, 148, 176, 149, 150, 136, 172, 58,
                  132, 93, 234, 127, 162, 21, 54, 103, 67, 109],
    "lips_all": [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 185, 40, 39, 37,
                 0, 267, 269, 270, 409, 78, 95, 88, 178, 87, 14, 317, 402, 318, 324,
                 308, 191, 80, 81, 82, 13, 312, 311, 310, 415],
    "left_eye": [263, 249, 390, 373, 374, 380, 381, 382, 362, 466, 388, 387, 386,
                 385, 384, 398],
    "right_eye": [33, 7, 163, 144, 145, 153, 154, 155, 133, 246, 161, 160, 159, 158,
                  157, 173],
    "left_eyebrow": [276, 283, 282, 295, 285, 300, 293, 334, 296, 336],
    "right_eyebrow": [46, 53, 52, 65, 55, 70, 63, 105, 66, 107],
    "nose": [168, 6, 197, 195, 5, 4, 1, 19, 94, 2, 98, 97, 326, 327, 294, 278, 344,
             440, 275, 45, 220, 115, 48, 64],
    "left_iris": [474, 475, 476, 477],
    "left_iris_center": 473,
    "right_iris": [469, 470, 471, 472],
    "right_iris_center": 468,
}

# shape: ellipse | polygon(凸包) | polyline(折线 buffer) | ribbon(窄弧带)
FACIAL_REGION_ATLAS: dict[str, dict[str, Any]] = {
    "泪沟": {
        "left_idx": [362, 382, 381, 380, 374, 373, 340, 346, 347, 348, 329, 277],
        "right_idx": [133, 155, 154, 153, 145, 144, 111, 117, 118, 119, 100, 47],
        "shape": "ribbon",
        "rationale": "眶下缘内侧凹陷沟，位于下睑缘行与中颊隆起之间的过渡窄带",
        "source": "下睑缘=官方EYE; 颊侧界=Sander de Snaijer 社区图",
        "confidence": "inferred",
    },
    "卧蚕": {
        "left_idx": [263, 249, 390, 373, 374, 380, 381, 382, 362],
        "right_idx": [33, 7, 163, 144, 145, 153, 154, 155, 133],
        "shape": "ribbon",
        "rationale": "紧贴睫毛下缘的眼轮匝肌睑部肌肉条，比泪沟高且更贴眼",
        "source": "官方 FACEMESH_LEFT/RIGHT_EYE 下半弧",
        "confidence": "high",
    },
    "眼袋": {
        "left_idx": [374, 373, 380, 346, 347, 348, 330],
        "right_idx": [145, 144, 153, 117, 118, 119, 101],
        "shape": "ellipse",
        "rationale": "眶隔脂肪膨出，范围比卧蚕大比泪沟低，占下睑缘到眶下缘",
        "source": "睑缘=官方EYE; 颊点=社区图",
        "confidence": "inferred",
    },
    "法令纹": {
        # 校准 2026-05-29（Phase 1，8 真实正脸叠点）：旧索引含上唇点(269/267/39/37)导致
        # 折线在嘴角回钩 + 路径贴鼻不贴沟。改为 ala→沿沟→口角外下，沿可见折线走向。
        "left_idx": [358, 423, 426, 322, 410, 287],
        "right_idx": [129, 203, 206, 92, 186, 57],
        "shape": "polyline",
        "rationale": "鼻翼外缘斜向下到口角外侧的褶皱，起点贴鼻翼终点在口角外下方",
        "source": "ala=官方NOSE; 口角外=官方LIPS邻接; 沟中段=社区图(校准实测)",
        "confidence": "calibrated",
    },
    "苹果肌": {
        "left_idx": [352, 280, 425, 427, 411, 376, 345, 346, 347],
        "right_idx": [123, 50, 205, 207, 187, 147, 116, 117, 118],
        "shape": "ellipse",
        "rationale": "颧大肌前方眶下区软组织饱满隆起(malar fat pad)，微笑最高点",
        "source": "Sander de Snaijer 颊部表面点图(社区)",
        "confidence": "inferred",
    },
    "面颊": {
        "left_idx": [345, 346, 347, 348, 329, 280, 425, 426, 427, 411, 352, 376, 433, 416],
        "right_idx": [116, 117, 118, 119, 100, 50, 205, 206, 207, 187, 123, 147, 213, 192],
        "shape": "polygon",
        "rationale": "覆盖上颌-颧骨前方大片软组织，外界face_oval内界鼻唇沟",
        "source": "社区表面点图 + 官方 FACE_OVAL 外边界",
        "confidence": "inferred",
    },
    "颧骨": {
        # 重做 2026-05-29（Phase 1 owner 钦定）：旧索引沿 face_oval 侧轮廓=颧弓(arch)，已弃。
        # 改为正面骨性突起椭圆——刻意取上外侧高位点(117/118/119/100/101)，比 苹果肌(下内侧
        # 脂肪垫 50/205/207)更高更外，叠点验证两区不再同位。眶下隆起的硬骨突起。
        "left_idx": [345, 346, 347, 348, 329, 330, 280, 266],
        "right_idx": [116, 117, 118, 119, 100, 101, 50, 36],
        "shape": "ellipse",
        "rationale": "颧骨体正面骨性突起，眶下外侧高位隆起，高于并外于苹果肌脂肪垫",
        "source": "Sander de Snaijer 颊部表面点图(社区，校准实测)",
        "confidence": "calibrated",
    },
    "咬肌": {
        # 新增 2026-05-29（Phase 1，真实 4 例）：咬肌肥大瘦脸针主战场，覆盖下颌支/角的肌肉块。
        "left_idx": [288, 397, 365, 379, 394, 430, 434, 367, 364],
        "right_idx": [58, 172, 136, 150, 169, 210, 214, 138, 135],
        "shape": "ellipse",
        "rationale": "下颌角/下颌支表面的咀嚼肌隆起，从耳前(58/288)到下颌角(150/379)前方肌腹",
        "source": "下颌缘=官方FACE_OVAL下半弧; 肌腹内界=社区(校准实测)",
        "confidence": "calibrated",
    },
    "川字": {
        # 新增 2026-05-29（Phase 1，真实 4 例）：眉间纵纹(glabellar lines)，除皱针。midline 单区。
        "idx": [9, 8, 168, 107, 336, 55, 285, 66, 296],
        "center_idx": 8,
        "shape": "ellipse",
        "rationale": "两眉头之间眉间区的纵向皱纹带，中线9/8/168 + 眉头107/336/55/285围成",
        "source": "全部=官方 EYEBROW 内端 + 鼻根NOSE(校准实测)",
        "confidence": "calibrated",
    },
    "太阳穴": {
        # 新增 2026-05-29（Phase 1，真实 4 例）：颞部凹陷填充。眉尾外侧、发际线内的颞窝。
        "left_idx": [251, 284, 298, 276, 300, 383, 368, 389],
        "right_idx": [21, 54, 68, 46, 70, 156, 139, 162],
        "shape": "ellipse",
        "rationale": "眉尾外侧的颞窝凹陷，上界发际(21/54)、内界眉尾(46/70)、外下界颧弓起点",
        "source": "颞线=官方FACE_OVAL上端 + 眉尾EYEBROW(校准实测)",
        "confidence": "calibrated",
    },
    "下颌线": {
        "left_idx": [152, 377, 400, 378, 379, 365, 397, 288],
        "right_idx": [172, 136, 150, 149, 176, 148, 152],
        "shape": "polyline",
        "rationale": "下颌骨下缘，从下巴152沿两侧上行到下颌角，全落在face_oval下半弧",
        "source": "官方 FACEMESH_FACE_OVAL",
        "confidence": "high",
    },
    "下巴": {
        "idx": [152, 148, 176, 377, 400, 378, 149, 150, 379, 175, 199, 200, 18, 83, 313],
        "shape": "ellipse",
        "rationale": "颏部下颌正中隆起，最低点152向上到下唇下方",
        "source": "152/148/176/377/400/378=官方FACE_OVAL; 颏中线=社区",
        "confidence": "high",
    },
    "鼻基底": {
        "center_idx": [2, 94, 19, 1, 4],
        "left_idx": [327, 326, 294, 278, 344, 440],
        "right_idx": [98, 97, 64, 48, 115, 220],
        "shape": "ellipse",
        "rationale": "鼻底与上唇交界横向带(鼻翼脚alar base + 鼻小柱基部)",
        "source": "全部=官方 FACEMESH_NOSE",
        "confidence": "high",
    },
    "鼻翼": {
        "left_idx": [327, 294, 278, 344, 440, 439],
        "right_idx": [98, 64, 48, 115, 220, 219],
        "shape": "polygon",
        "rationale": "鼻孔外侧弧形软骨翼，最外缘约48/278，下缘98/327",
        "source": "主点=官方NOSE; 219/439=社区",
        "confidence": "high",
    },
    "鼻尖": {
        "idx": [4, 1, 19, 45, 275, 5],
        "center_idx": 4,
        "shape": "ellipse",
        "rationale": "鼻最前突点，MediaPipe中 4=鼻尖(1是其上方鼻梁下端，勿混淆)",
        "source": "官方 FACEMESH_NOSE; 多源确认 4=tip",
        "confidence": "high",
    },
    "鼻背": {
        "idx": [168, 6, 197, 195, 5],
        "center_idx": 197,
        "shape": "polyline",
        "rationale": "鼻梁中线 radix(168鼻根)→dorsum(6→197→195)→supratip(5)；隆鼻/山根垫高=正面高光带变、侧面鼻背线轮廓",
        "source": "官方 FACEMESH_NOSE 中线；168=鼻根/5=鼻尖上端(2026-05-30 真实正脸+40°斜叠点校准)",
        "confidence": "calibrated",
    },
    "唇": {
        "upper_idx": [61, 185, 40, 39, 37, 0, 267, 269, 270, 409, 291, 78, 80, 81, 82,
                      13, 312, 311, 310, 415, 308],
        "lower_idx": [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 78, 95, 88,
                      178, 87, 14, 317, 402, 318, 324, 308],
        "center_upper": 13,
        "center_lower": 14,
        "shape": "polygon",
        "rationale": "唇红区由外圈(vermilion border 61→291)与内圈(口裂78→308)围成",
        "source": "全部=官方 FACEMESH_LIPS",
        "confidence": "high",
    },
}

# 术式文件名 / 焦点关键词同义词 → atlas 标准键（focal 关键词集的超集）
REGION_ALIASES: dict[str, str] = {
    "tear_trough": "泪沟", "undereye": "泪沟",
    "lip": "唇", "lips": "唇", "嘴": "唇", "丰唇": "唇",
    "cheek": "面颊", "脸颊": "面颊", "malar": "苹果肌",
    "nasolabial": "法令纹",
    "chin": "下巴", "jaw": "下颌线", "jawline": "下颌线",
    "nose": "鼻尖", "zygomatic": "颧骨", "cheekbone": "颧骨",
    "隆鼻": "鼻背", "山根": "鼻背", "鼻梁": "鼻背", "鼻子": "鼻背",
    "nose_bridge": "鼻背", "dorsum": "鼻背", "radix": "鼻背",
    "masseter": "咬肌", "瘦脸": "咬肌",
    "川字纹": "川字", "眉间": "川字", "glabella": "川字", "frown": "川字",
    "temple": "太阳穴", "颞": "太阳穴", "颞部": "太阳穴",
}


# 部位 → 可观察角度（按优先级；首个可用的角度即标注所在板）。
# 原则：中线/眶周扁平特征→正面看全；突度类→看正面**高光区变化**也能辨（owner 2026-05-30：
# 山根/鼻背垫高后正面高光带变、下巴前突正面高光可辨）；凹陷/轮廓流畅度类→斜/侧才显
# （太阳穴填充、下颌线流畅在正面投影占比极小）。侧面板留作突度类的轮廓确认。
VIEW_FRONT, VIEW_OBLIQUE, VIEW_PROFILE = "front", "oblique", "profile"
REGION_VIEWS: dict[str, list[str]] = {
    "川字": [VIEW_FRONT],
    "泪沟": [VIEW_FRONT, VIEW_OBLIQUE],      # 眶下阴影正面(侧光)，斜看辅助 (A.S.S.E.S.S.)
    "卧蚕": [VIEW_FRONT],
    "眼袋": [VIEW_FRONT],
    "唇": [VIEW_FRONT],
    "鼻基底": [VIEW_FRONT],
    "鼻翼": [VIEW_FRONT],
    "鼻尖": [VIEW_FRONT, VIEW_PROFILE],      # 山根/鼻背垫高→正面高光带变化可辨；侧面看鼻背线
    "鼻背": [VIEW_FRONT, VIEW_OBLIQUE, VIEW_PROFILE],  # 正面高光带(owner钦定oracle)→斜看鼻梁脊→侧面鼻背线(silhouette,Layer2)
    "下巴": [VIEW_FRONT, VIEW_PROFILE],      # 前突→正面高光可辨 + 宽度；侧面看前突轮廓
    "苹果肌": [VIEW_FRONT, VIEW_OBLIQUE],    # 位置正面、顶点高光/饱满斜看(亚洲取内侧)
    "法令纹": [VIEW_FRONT, VIEW_OBLIQUE],    # 沟阴影正面、沟深斜看
    "颧骨": [VIEW_OBLIQUE, VIEW_FRONT],      # ogee 突度斜看，正面高光辅助
    "面颊": [VIEW_OBLIQUE, VIEW_FRONT],      # ogee S 曲线只有斜看
    "太阳穴": [VIEW_OBLIQUE, VIEW_PROFILE],  # 颞凹陷正面占比极小 (A.S.S.E.S.S. lateral+oblique)
    "下颌线": [VIEW_OBLIQUE, VIEW_PROFILE],  # 流畅度/轮廓正面占比极小
    "咬肌": [VIEW_FRONT, VIEW_OBLIQUE],      # 方→V 宽度正面为主(瘦脸)，轮廓斜看辅助
}

# 部位 → 治疗效果在照片上的视觉信号（驱动标注样式 + 未来 before/after eval"该看什么变化"）。
# 循证见 delivery/region-view-effect-knowledge.md（A.S.S.E.S.S. 光影原理 + ogee + 亚洲面型）。
SIG_HIGHLIGHT = "highlight"  # 垫高凸起→正面新高光带（投影类）
SIG_SHADOW = "shadow"        # 填平凹陷→阴影减少（凹陷类，侧光显影）
SIG_OGEE = "ogee"            # 颊→颌 S 形轮廓连续（45° 斜位唯一可辨）
SIG_LINE = "line"            # 动态皱纹松解
SIG_WIDTH = "width"          # 瘦脸宽度变窄（方→V）
SIG_VOLUME = "volume"        # 形态/容量
REGION_EFFECTS: dict[str, str] = {
    "川字": SIG_LINE, "泪沟": SIG_SHADOW, "卧蚕": SIG_VOLUME, "眼袋": SIG_SHADOW,
    "唇": SIG_VOLUME, "鼻基底": SIG_VOLUME, "鼻翼": SIG_VOLUME,
    "鼻尖": SIG_HIGHLIGHT, "鼻背": SIG_HIGHLIGHT, "下巴": SIG_HIGHLIGHT, "苹果肌": SIG_HIGHLIGHT,
    "法令纹": SIG_SHADOW, "颧骨": SIG_OGEE, "面颊": SIG_OGEE,
    "太阳穴": SIG_SHADOW, "下颌线": SIG_OGEE, "咬肌": SIG_WIDTH,
}


# 乔雅登三阶光影美学：光/影/灰三区（光影对比越强→脸越立体，如化妆 T 区高光+外侧阴影）。
# 光区=打高光点吸光拉伸轮廓(含填平致影凹陷)；影区=内收收缩定深邃；灰区=正脸侧脸交界/转折(↔45°斜位 ogee)。
# 见 delivery/region-view-effect-knowledge.md。
ZONE_LIGHT, ZONE_SHADOW, ZONE_TRANSITION = "light", "shadow", "transition"
REGION_ZONES: dict[str, str] = {
    "川字": ZONE_LIGHT, "泪沟": ZONE_LIGHT, "卧蚕": ZONE_LIGHT, "眼袋": ZONE_SHADOW,
    "唇": ZONE_LIGHT, "鼻基底": ZONE_LIGHT, "鼻翼": ZONE_LIGHT, "鼻尖": ZONE_LIGHT,
    "鼻背": ZONE_LIGHT,                                          # T 区高光脊
    "下巴": ZONE_LIGHT, "苹果肌": ZONE_LIGHT, "法令纹": ZONE_LIGHT,
    "颧骨": ZONE_TRANSITION,                                    # 颧凸=灰区转折
    "面颊": ZONE_SHADOW, "太阳穴": ZONE_SHADOW, "下颌线": ZONE_SHADOW, "咬肌": ZONE_SHADOW,
}

# MD Codes FCR 层级（de Maio）：Foundation 地基→Contour 轮廓→Refinement 精修。
# = owner 说的"面部轮廓 vs 精细化部位"：foundation+contour=轮廓骨架，refinement=精细化。
TIER_FOUNDATION, TIER_CONTOUR, TIER_REFINEMENT = "foundation", "contour", "refinement"
REGION_TIERS: dict[str, str] = {
    "苹果肌": TIER_FOUNDATION, "面颊": TIER_FOUNDATION,          # 中颊地基(Ck)
    "太阳穴": TIER_CONTOUR, "下巴": TIER_CONTOUR, "下颌线": TIER_CONTOUR,
    "咬肌": TIER_CONTOUR, "颧骨": TIER_CONTOUR, "鼻尖": TIER_CONTOUR,  # 轮廓/投影结构
    "鼻背": TIER_CONTOUR,                                        # 鼻梁投影=轮廓骨架
    "泪沟": TIER_REFINEMENT, "法令纹": TIER_REFINEMENT, "川字": TIER_REFINEMENT,
    "唇": TIER_REFINEMENT, "卧蚕": TIER_REFINEMENT, "眼袋": TIER_REFINEMENT,
    "鼻基底": TIER_REFINEMENT, "鼻翼": TIER_REFINEMENT,          # 精细化
}


def region_views(region_key: str) -> list[str]:
    """该部位按优先级的可观察角度列表（未登记 → 默认 [front]）。"""
    return REGION_VIEWS.get(region_key, [VIEW_FRONT])


def region_effect(region_key: str) -> str:
    """该部位治疗效果的视觉信号（highlight/shadow/ogee/line/width/volume；默认 volume）。"""
    return REGION_EFFECTS.get(region_key, SIG_VOLUME)


def region_zone(region_key: str) -> str:
    """乔雅登光影灰三区归属（light/shadow/transition；默认 light）。"""
    return REGION_ZONES.get(region_key, ZONE_LIGHT)


def region_tier(region_key: str) -> str:
    """MD Codes FCR 层级（foundation/contour/refinement；默认 refinement）。"""
    return REGION_TIERS.get(region_key, TIER_REFINEMENT)


def resolve_region_key(target: str) -> str | None:
    """把一个 focus_target（中/英、可能含修饰）解析到 atlas 标准键（单个，第一匹配）。"""
    t = target.strip()
    if t in FACIAL_REGION_ATLAS:
        return t
    if t in REGION_ALIASES:
        return REGION_ALIASES[t]
    # 子串匹配（术式文件名常含整段描述）
    for key in FACIAL_REGION_ATLAS:
        if key in t:
            return key
    for alias, key in REGION_ALIASES.items():
        if alias in t:
            return key
    return None


def extract_regions(text: str) -> list[str]:
    """从一段术式描述里抽取**全部**命中的 atlas 区（多术式目录用）。

    真实 case 目录常含多术式（如「下颌线、颈阔肌咬肌」「面颊，下巴」），
    resolve_region_key 只返回第一个 → panel 会漏区。本函数返回去重后的全部命中，
    保持 atlas 定义顺序（稳定输出）。
    """
    found: list[str] = []
    seen: set[str] = set()
    for key in FACIAL_REGION_ATLAS:
        if key in text and key not in seen:
            found.append(key)
            seen.add(key)
    for alias, key in REGION_ALIASES.items():
        if alias in text and key not in seen:
            found.append(key)
            seen.add(key)
    return found


_IDX_KEYS = ("idx", "left_idx", "right_idx", "center_idx", "upper_idx", "lower_idx")


def _all_idx(spec: dict[str, Any]) -> list[int]:
    """收集一个 region spec 里所有 landmark 索引。"""
    out: list[int] = []
    for k in _IDX_KEYS:
        v = spec.get(k)
        if isinstance(v, list):
            out.extend(v)
        elif isinstance(v, int):
            out.append(v)
    return out


def region_landmark_groups(region_key: str) -> list[list[int]]:
    """返回该部位的 landmark 索引分组（对称部位左右各一组，居中部位一组）。

    用于：每组单独成形（如泪沟左右各一个 ribbon），而非全部混成一个 bbox。
    """
    spec = FACIAL_REGION_ATLAS.get(region_key)
    if not spec:
        return []
    groups: list[list[int]] = []
    for k in ("left_idx", "right_idx", "idx", "upper_idx", "lower_idx"):
        if isinstance(spec.get(k), list):
            groups.append(list(spec[k]))
    if not groups and isinstance(spec.get("center_idx"), list):
        groups.append(list(spec["center_idx"]))
    return groups


def region_shape(region_key: str) -> str:
    spec = FACIAL_REGION_ATLAS.get(region_key)
    return spec.get("shape", "ellipse") if spec else "ellipse"


__all__ = [
    "FACEMESH_ANCHORS", "FACIAL_REGION_ATLAS", "REGION_ALIASES", "REGION_VIEWS",
    "REGION_EFFECTS", "REGION_ZONES", "REGION_TIERS",
    "VIEW_FRONT", "VIEW_OBLIQUE", "VIEW_PROFILE",
    "ZONE_LIGHT", "ZONE_SHADOW", "ZONE_TRANSITION",
    "TIER_FOUNDATION", "TIER_CONTOUR", "TIER_REFINEMENT",
    "region_views", "region_effect", "region_zone", "region_tier",
    "resolve_region_key", "extract_regions", "region_landmark_groups", "region_shape",
]
