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
        "left_idx": [358, 429, 279, 331, 294, 327, 291, 269, 267],
        "right_idx": [129, 209, 49, 102, 64, 98, 61, 39, 37],
        "shape": "polyline",
        "rationale": "鼻翼外缘斜向下到口角外侧的褶皱，起点贴鼻翼终点在嘴角",
        "source": "鼻翼=官方NOSE; 嘴角=官方LIPS; 中段路径=社区图",
        "confidence": "inferred",
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
        "left_idx": [454, 356, 389, 345, 323, 366, 352],
        "right_idx": [234, 127, 162, 116, 93, 137, 123],
        "shape": "polyline",
        "rationale": "面部最外侧骨性突起(颧弓)，在face_oval侧轮廓中段，234/454最外缘",
        "source": "外缘=官方FACE_OVAL; 内侧填充=社区",
        "confidence": "high",
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
    "cheek": "面颊", "malar": "苹果肌",
    "nasolabial": "法令纹",
    "chin": "下巴", "jaw": "下颌线", "jawline": "下颌线",
    "nose": "鼻尖", "zygomatic": "颧骨", "cheekbone": "颧骨",
}


def resolve_region_key(target: str) -> str | None:
    """把一个 focus_target（中/英、可能含修饰）解析到 atlas 标准键。"""
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
    "FACEMESH_ANCHORS", "FACIAL_REGION_ATLAS", "REGION_ALIASES",
    "resolve_region_key", "region_landmark_groups", "region_shape",
]
