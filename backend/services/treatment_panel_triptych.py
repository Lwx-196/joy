"""三联拼图编排：覆盖挑图 → 按角度分板 → 每板 AI 线稿标注 → 正面|45°|侧面 拼一张.

管线 C 下游半段。把 case_material_coverage 的"按部位最佳角度"接进 treatment_zone_panel：
- 按部位**实际可用角度**（chosen.view，已含降级）分到 front / oblique / profile 板。
- 每板挑该角度一张代表照 → AI 线稿 → 在线稿上检 landmark → 标该板的部位（斜位只标近侧）。
- profile 板 = Stage B 2D 轮廓（当前占位）。
- 三板等高拼接 + CJK 角度表头。
"""
from __future__ import annotations

import os

import cv2
import numpy as np

from backend.services import case_material_coverage as cov
from backend.services import facial_region_atlas as atlas
from backend.services import treatment_zone_panel as tzp

_VIEW_CN = {"front": "正面", "oblique": "45°斜", "profile": "侧面"}
_PANEL_ORDER = ["front", "oblique", "profile"]


def _panel_for(rc: cov.RegionCoverage) -> str:
    """该部位标到哪块板：用实际选中照的角度（已含降级）；no-face→侧面(2D)。"""
    if rc.chosen and rc.chosen.has_face and rc.chosen.view in ("front", "oblique", "profile"):
        return rc.chosen.view
    return atlas.VIEW_PROFILE  # unknown/no-face → 侧面 2D


def _rep_photo(photos: list[cov.PhotoView], view: str) -> cov.PhotoView | None:
    cands = [p for p in photos if p.has_face and p.view == view]
    if not cands:
        return None
    return cov._best_for_view(cands, view)


def _header(width: int, text: str, h: int = 70) -> np.ndarray:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (width, h), (245, 245, 245))
    d = ImageDraw.Draw(img)
    f = tzp._load_font(40)
    tb = d.textbbox((0, 0), text, font=f)
    d.text(((width - (tb[2] - tb[0])) // 2, (h - (tb[3] - tb[1])) // 2 - tb[1]),
           text, font=f, fill=(40, 40, 40))
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def _placeholder(width: int, height: int, text: str) -> np.ndarray:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (width, height), (252, 252, 252))
    d = ImageDraw.Draw(img)
    f = tzp._load_font(34)
    for i, line in enumerate(text.split("\n")):
        tb = d.textbbox((0, 0), line, font=f)
        d.text(((width - (tb[2] - tb[0])) // 2, height // 2 - 60 + i * 50),
               line, font=f, fill=(150, 150, 150))
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def _fit(img: np.ndarray, target_h: int, target_w: int) -> np.ndarray:
    """缩放到目标高，居中 letterbox 到目标宽（白边）。"""
    h, w = img.shape[:2]
    nw = min(int(w * target_h / h), target_w)
    img = cv2.resize(img, (nw, target_h), interpolation=cv2.INTER_AREA)
    canvas = np.full((target_h, target_w, 3), 255, np.uint8)
    x = (target_w - nw) // 2
    canvas[:, x:x + nw] = img
    return canvas


def build_triptych(case_dir: str, model_path: str, providers, *,
                   focus_text: str | None = None, patient: str | None = None) -> tuple[np.ndarray, cov.CaseCoverage]:
    """端到端：覆盖挑图 → 分板 → AI 线稿标注 → 三联拼图。返回 (BGR, CaseCoverage)。"""
    focus = focus_text or os.path.basename(case_dir.rstrip("/"))
    patient = patient or os.path.basename(os.path.dirname(case_dir.rstrip("/")))
    cc = cov.analyze_case(case_dir, model_path, focus_text=focus)

    buckets: dict[str, list[str]] = {v: [] for v in _PANEL_ORDER}
    for rc in cc.regions:
        if rc.status == cov.STATUS_MISSING:
            continue
        buckets[_panel_for(rc)].append(rc.region)

    panels: list[tuple[str, np.ndarray | None, list[str]]] = []
    for view in _PANEL_ORDER:
        regions = buckets[view]
        if not regions:
            continue
        if view == atlas.VIEW_PROFILE:
            panels.append(("profile", None, regions))  # Stage B 占位
            continue
        photo = _rep_photo(cc.photos, view)
        if photo is None:
            continue
        img = cv2.imread(photo.path)
        substrate, _name = tzp.lineart_ai(img, providers)
        pts = tzp.facemesh_landmarks(substrate, model_path)
        if pts is None:
            continue
        title = f"{patient} 术前"
        panel = tzp.render_panel(substrate, pts, regions, substrate_bgr=substrate,
                                 title=title, near_side_only=(view == "oblique"))
        panels.append((view, panel, regions))

    if not panels:
        raise RuntimeError("no panel rendered (no usable material)")

    target_h = max((p.shape[0] for _v, p, _r in panels if p is not None), default=900)
    target_w = max((int(p.shape[1] * target_h / p.shape[0]) for _v, p, _r in panels
                    if p is not None), default=target_h)

    cols = []
    for view, panel, regions in panels:
        if panel is None:
            body = _placeholder(target_w, target_h,
                                f"侧面 2D 轮廓标注\n(Stage B 待建)\n{' '.join(regions)}")
        else:
            body = _fit(panel, target_h, target_w)
        head = _header(target_w, f"{_VIEW_CN[view]}  ·  {' '.join(regions)}")
        cols.append(np.vstack([head, body]))

    gap = np.full((cols[0].shape[0], 8, 3), 220, np.uint8)
    out = cols[0]
    for c in cols[1:]:
        out = np.hstack([out, gap, c])
    return out, cc


__all__ = ["build_triptych"]
