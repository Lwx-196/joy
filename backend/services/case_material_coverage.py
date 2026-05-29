"""素材角度充分度 + 按部位挑图（知识库反哺前段识别/分类/筛选）。

owner 2026-05-30：**不硬分角度——区分不出就降级**。角度判不准（no-face / 边界附近）
绝不因此误拦素材：标"不确定"→ 覆盖判定走降级（仍可用、加提示），只有**完全无可用素材**判 missing。

分两层：
- IO 层 `classify_views`（lazy mediapipe，yaw + 是否检到脸 → 软角度）。
- 纯逻辑层 `route_region` / `analyze`（吃 PhotoView 列表，可脱离 mediapipe 单测）。

角度只是"优先级线索"，不是硬门：route 先找确定匹配 → 退不确定匹配 → 退任意可用脸 →
退 no-face(走侧面2D) → 才 missing。
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from backend.services import facial_region_atlas as atlas

# 软角度边界（度）。边界 ±band 内标"不确定"，不硬判。
_FRONT_MAX = 12.0
_OBLIQUE_MAX = 48.0
_UNCERTAIN_LO = (12.0, 20.0)   # front↔oblique 模糊带
_UNCERTAIN_HI = (48.0, 62.0)   # oblique↔profile 模糊带

VIEW_UNKNOWN = "unknown"
STATUS_COVERED, STATUS_DEGRADED, STATUS_MISSING = "covered", "degraded", "missing"
_VIEW_CN = {"front": "正面", "oblique": "45°斜", "profile": "侧面", VIEW_UNKNOWN: "未知"}


@dataclass(frozen=True)
class PhotoView:
    path: str
    has_face: bool
    yaw: float | None          # None=未检到脸
    view: str                  # front|oblique|profile|unknown（best-guess）
    certain: bool              # 角度是否可靠（边界/no-face → False）


@dataclass
class RegionCoverage:
    region: str
    required_views: list[str]
    status: str                # covered|degraded|missing
    chosen: PhotoView | None
    note: str = ""


@dataclass
class CaseCoverage:
    focus: str
    regions: list[RegionCoverage]
    photos: list[PhotoView] = field(default_factory=list)


def classify_angle(yaw: float | None, has_face: bool) -> tuple[str, bool]:
    """yaw + 是否检到脸 → (best-guess view, certain)。不硬分：边界/no-face → certain=False。"""
    if not has_face or yaw is None:
        return VIEW_UNKNOWN, False           # 检不到=区分不出→降级，不强判侧面
    if yaw < _FRONT_MAX:
        return "front", True
    if _UNCERTAIN_LO[0] <= yaw < _UNCERTAIN_LO[1]:
        return "oblique", False              # front↔oblique 模糊
    if yaw < _OBLIQUE_MAX:
        return "oblique", True
    if _UNCERTAIN_HI[0] <= yaw < _UNCERTAIN_HI[1]:
        return "profile", False              # oblique↔profile 模糊
    return "profile", True


def _yaw_target(view: str) -> float:
    return {"front": 0.0, "oblique": 33.0, "profile": 75.0}.get(view, 33.0)


def _best_for_view(cands: list[PhotoView], view: str) -> PhotoView:
    """同 view 候选里挑最贴该角度的（front→最正；oblique→最近 33°；profile→最大 yaw）。"""
    tgt = _yaw_target(view)
    return min(cands, key=lambda p: abs((p.yaw if p.yaw is not None else 90.0) - tgt))


def route_region(region: str, photos: list[PhotoView]) -> RegionCoverage:
    """按部位需求 + 现有素材路由（纯函数）。绝不因角度不确定硬拒，逐级降级。"""
    required = atlas.region_views(region)
    faces = [p for p in photos if p.has_face]
    # 1) 确定匹配所需角度
    for v in required:
        cands = [p for p in faces if p.view == v and p.certain]
        if cands:
            return RegionCoverage(region, required, STATUS_COVERED,
                                  _best_for_view(cands, v), f"用{_VIEW_CN[v]}")
    # 2) 不确定匹配（边界附近，降级）
    for v in required:
        cands = [p for p in faces if p.view == v]
        if cands:
            return RegionCoverage(region, required, STATUS_DEGRADED,
                                  _best_for_view(cands, v),
                                  f"角度近边界，按{_VIEW_CN[v]}降级使用")
    # 3) 任意可用脸（角度非理想，best-effort 取最接近首选角度的）
    if faces:
        v0 = required[0]
        return RegionCoverage(region, required, STATUS_DEGRADED,
                              _best_for_view(faces, v0),
                              f"无理想角度，用现有最接近{_VIEW_CN[v0]}的素材")
    # 4) 仅 no-face（侧面无 landmark）→ 若该部位可侧面，走 2D 轮廓降级
    unknown = [p for p in photos if not p.has_face]
    if unknown and atlas.VIEW_PROFILE in required:
        return RegionCoverage(region, required, STATUS_DEGRADED, unknown[0],
                              "仅侧面无 landmark，走 2D 轮廓降级")
    # 5) 真无可用素材
    return RegionCoverage(region, required, STATUS_MISSING, None,
                          f"无可用素材，需补 {'/'.join(_VIEW_CN[v] for v in required)}")


def analyze(focus_text: str, photos: list[PhotoView]) -> CaseCoverage:
    """术式文本 → 部位 → 逐部位覆盖（纯函数）。"""
    regions = atlas.extract_regions(focus_text)
    return CaseCoverage(focus_text, [route_region(r, photos) for r in regions], photos)


# --------------------------- IO 层（lazy mediapipe）---------------------------

def classify_views(case_dir: str, model_path: str, *, prefix: str = "术前") -> list[PhotoView]:
    """跑 FaceLandmarker 分类 case 目录下所有 prefix 照片的角度。"""
    import cv2
    import mediapipe as mp
    import numpy as np
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    base = mp_python.BaseOptions(model_asset_path=model_path)
    opts = vision.FaceLandmarkerOptions(base_options=base, num_faces=1,
                                        output_facial_transformation_matrixes=True)
    det = vision.FaceLandmarker.create_from_options(opts)
    out: list[PhotoView] = []
    files = sorted(f for f in os.listdir(case_dir) if f.startswith(prefix)
                   and f.lower().endswith((".jpg", ".jpeg", ".png")))
    for f in files:
        path = os.path.join(case_dir, f)
        img = cv2.imread(path)
        if img is None:
            continue
        r = det.detect(mp.Image(image_format=mp.ImageFormat.SRGB,
                                data=cv2.cvtColor(img, cv2.COLOR_BGR2RGB)))
        has_face = bool(r.face_landmarks)
        yaw = None
        if has_face and r.facial_transformation_matrixes:
            R = np.array(r.facial_transformation_matrixes[0])[:3, :3]
            yaw = abs(math.degrees(math.atan2(-R[2, 0], math.hypot(R[0, 0], R[1, 0]))))
        view, certain = classify_angle(yaw, has_face)
        out.append(PhotoView(path, has_face, yaw, view, certain))
    return out


def analyze_case(case_dir: str, model_path: str, *, focus_text: str | None = None) -> CaseCoverage:
    """端到端：分类角度 + 路由覆盖。focus_text 缺省用目录名。"""
    focus = focus_text or os.path.basename(case_dir.rstrip("/"))
    return analyze(focus, classify_views(case_dir, model_path))


__all__ = [
    "PhotoView", "RegionCoverage", "CaseCoverage", "VIEW_UNKNOWN",
    "STATUS_COVERED", "STATUS_DEGRADED", "STATUS_MISSING",
    "classify_angle", "route_region", "analyze", "classify_views", "analyze_case",
]
