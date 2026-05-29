"""Treatment-zone annotation panel — 患者真实脸 → cv2 线稿 + atlas 治疗区精准标注.

Phase 1（plan anchored-focal-annotation）。owner 决策 2026-05-29：
  底图 = cv2 边缘线稿（0 付费，不烧 img2img）；交付 = 独立 panel 图（不动 case-layout-board）。

管线：
  术前照 → cv2 线稿化 → FaceLandmarker(478) → 每个 focus_target 区按 atlas 形状
  (ellipse/polygon/polyline/ribbon) 算像素几何 → 半透明色块 + CJK 标签 → 独立 panel。

几何层（region_geometry/_*）是纯函数，可脱离 mediapipe 单测；landmark 检测与线稿是 IO 层。
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from backend.services import facial_region_atlas as atlas

# --- CJK 字体回退（与 case-layout-board 同源）---
_FONT_PATHS = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
]

# 区域配色（BGR→后续转 RGB），克制的医学示意色
_REGION_COLORS: dict[str, tuple[int, int, int]] = {
    "泪沟": (90, 200, 90), "卧蚕": (120, 210, 120), "眼袋": (70, 170, 70),
    "法令纹": (70, 130, 240), "苹果肌": (40, 170, 250), "面颊": (200, 120, 255),
    "颧骨": (40, 150, 255), "下颌线": (190, 90, 160), "下巴": (235, 165, 55),
    "鼻基底": (180, 180, 80), "鼻翼": (160, 200, 90), "鼻尖": (200, 200, 70),
    "唇": (120, 100, 240), "咬肌": (90, 210, 90), "川字": (220, 100, 220),
    "太阳穴": (230, 200, 60),
}
_DEFAULT_COLOR = (120, 180, 240)


@dataclass(frozen=True)
class RegionShape:
    """一个治疗区的一笔可绘几何（对称区有左右两笔）。"""
    region: str
    kind: str            # "fill" (椭圆/多边形闭合填充) | "stroke" (折线/带状描边)
    points: np.ndarray   # (N,2) int32 像素坐标（fill=多边形顶点; stroke=有序路径）
    width: int = 0       # stroke 描边宽（fill 忽略）
    label_anchor: tuple[int, int] = (0, 0)


# --------------------------- 几何层（纯函数）---------------------------

def _ellipse_poly(pts: np.ndarray) -> np.ndarray:
    """点组 → 椭圆多边形顶点。≥5 点用 fitEllipse，否则 bbox 内切椭圆。"""
    pts = pts.astype(np.float32)
    if len(pts) >= 5:
        (cx, cy), (w, h), ang = cv2.fitEllipse(pts)
        axes = (max(w / 2, 2.0), max(h / 2, 2.0))
        poly = cv2.ellipse2Poly((int(cx), int(cy)), (int(axes[0]), int(axes[1])),
                                int(ang), 0, 360, 12)
        return poly.astype(np.int32)
    x0, y0 = pts.min(0)
    x1, y1 = pts.max(0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    ax, ay = max((x1 - x0) / 2, 3.0), max((y1 - y0) / 2, 3.0)
    poly = cv2.ellipse2Poly((int(cx), int(cy)), (int(ax), int(ay)), 0, 0, 360, 12)
    return poly.astype(np.int32)


def _hull_poly(pts: np.ndarray) -> np.ndarray:
    return cv2.convexHull(pts.astype(np.int32)).reshape(-1, 2)


def _face_scale(all_pts: np.ndarray) -> float:
    """脸宽（face_oval 极值）作为描边宽/标签字号的尺度基准。"""
    xs = all_pts[:, 0]
    return float(xs.max() - xs.min())


def region_geometry(region_key: str, pts: np.ndarray) -> list[RegionShape]:
    """把一个 atlas 区 + 该脸 478 landmark → 可绘 RegionShape 列表（左右各一笔）。

    pts: (478,2) 像素坐标。返回 [] 表示该区无定义。
    """
    spec = atlas.FACIAL_REGION_ATLAS.get(region_key)
    if not spec:
        return []
    shape = spec.get("shape", "ellipse")
    groups = atlas.region_landmark_groups(region_key)
    scale = _face_scale(pts)
    shapes: list[RegionShape] = []
    for idxs in groups:
        idxs = [i for i in idxs if 0 <= i < len(pts)]
        if len(idxs) < 2:
            continue
        gp = pts[idxs]
        if shape in ("ellipse",):
            poly = _ellipse_poly(gp)
            anchor = poly.mean(0).astype(int)
            shapes.append(RegionShape(region_key, "fill", poly, 0, tuple(anchor)))
        elif shape in ("polygon",):
            poly = _hull_poly(gp)
            anchor = poly.mean(0).astype(int)
            shapes.append(RegionShape(region_key, "fill", poly, 0, tuple(anchor)))
        elif shape in ("polyline",):
            w = max(3, int(scale * 0.011))
            path = gp.astype(np.int32)
            anchor = path.mean(0).astype(int)
            shapes.append(RegionShape(region_key, "stroke", path, w, tuple(anchor)))
        elif shape in ("ribbon",):
            # 窄弧带：沿下睑缘点序向下偏移半带宽，描细带（不吃眼球）
            w = max(3, int(scale * 0.018))
            path = gp.astype(np.float32)
            path[:, 1] += w * 0.6
            path = path.astype(np.int32)
            anchor = path.mean(0).astype(int)
            shapes.append(RegionShape(region_key, "stroke", path, w, tuple(anchor)))
    return shapes


# --------------------------- IO 层 ---------------------------

def lineart(image_bgr: np.ndarray) -> np.ndarray:
    """真实照 → 干净线稿（白底深线，医学示意图风）。

    pyrMeanShift 抹平肤色斑点保留结构边 → Canny 抽干净轮廓 → 反相成白底深线。
    比 pencilSketch 干净得多（无肤色 grain/阴影脏块）。大图先降采样跑 meanshift 提速。
    退回 bilateral+adaptiveThreshold。
    """
    try:
        h, w = image_bgr.shape[:2]
        # meanshift 在大图很慢 → 限制到长边 ~1400 跑，再放回
        scale = 1400.0 / max(h, w) if max(h, w) > 1400 else 1.0
        small = cv2.resize(image_bgr, (int(w * scale), int(h * scale)),
                           interpolation=cv2.INTER_AREA) if scale < 1.0 else image_bgr
        sm = cv2.pyrMeanShiftFiltering(small, sp=18, sr=40)
        g = cv2.cvtColor(sm, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(g, 40, 110)
        edges = cv2.dilate(edges, np.ones((2, 2), np.uint8))
        if scale < 1.0:
            edges = cv2.resize(edges, (w, h), interpolation=cv2.INTER_NEAREST)
        return cv2.cvtColor(255 - edges, cv2.COLOR_GRAY2BGR)
    except Exception:
        g = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        g = cv2.bilateralFilter(g, 9, 75, 75)
        edges = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                      cv2.THRESH_BINARY, 9, 5)
        return cv2.cvtColor(edges, cv2.COLOR_GRAY2BGR)


_LINEART_PROMPT = (
    "Redraw this portrait as a clean black-and-white line drawing for a medical "
    "aesthetics treatment chart. Pure white background. Thin, even black contour "
    "lines only. Preserve the EXACT same face shape, proportions, feature positions, "
    "head angle and hairstyle as the photo. No shading, no hatching, no grey fill, "
    "no color, no text. Minimal clean medical-illustration line art."
)


def lineart_ai(image_bgr: np.ndarray, providers, *, max_edge: int = 1536,
               prompt: str | None = None) -> tuple[np.ndarray, str]:
    """AI 线稿（img2img）：缩到长边 max_edge → provider 链生图 → 返回 (线稿 BGR, provider 名)。

    providers: image_providers.ImageProvider 列表（resolve_chain 产出）。
    """
    from backend.services import image_providers as ip

    h, w = image_bgr.shape[:2]
    s = max_edge / max(h, w)
    src = (cv2.resize(image_bgr, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
           if s < 1.0 else image_bgr)
    ok, buf = cv2.imencode(".jpg", src, [cv2.IMWRITE_JPEG_QUALITY, 88])
    img_bytes, name = ip.generate_with_fallback(providers, buf.tobytes(),
                                                prompt or _LINEART_PROMPT)
    arr = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        raise RuntimeError(f"provider {name} returned undecodable image bytes")
    return arr, name


def facemesh_landmarks(image_bgr: np.ndarray, model_path: str) -> np.ndarray | None:
    """FaceLandmarker(478) → (478,2) 像素坐标；无脸返回 None。lazy import mediapipe。"""
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    base = mp_python.BaseOptions(model_asset_path=model_path)
    opts = vision.FaceLandmarkerOptions(base_options=base, num_faces=1)
    lm = vision.FaceLandmarker.create_from_options(opts)
    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
    if not res.face_landmarks:
        return None
    h, w = image_bgr.shape[:2]
    return np.array([[p.x * w, p.y * h] for p in res.face_landmarks[0]], dtype=np.float32)


# --------------------------- 合成层（PIL，CJK）---------------------------

def _load_font(size: int):
    from PIL import ImageFont
    for p in _FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _face_crop_box(pts: np.ndarray, w: int, h: int, pad: float = 0.45) -> tuple[int, int, int, int]:
    """脸 landmark bbox + padding（给标签留白），夹到图像边界。"""
    x0, y0 = pts.min(0)
    x1, y1 = pts.max(0)
    fw, fh = x1 - x0, y1 - y0
    px, py = fw * pad, fh * pad
    return (max(0, int(x0 - px)), max(0, int(y0 - py * 1.1)),
            min(w, int(x1 + px)), min(h, int(y1 + py)))


def _near_side_x(pts: np.ndarray) -> tuple[float, float] | None:
    """斜位近侧判定。返回 (midline_x, near_cheek_x)；正面(近似对称)返回 None。

    近侧 = 离鼻中线更远的那侧颊（透视下展得更开 = 朝镜头）。
    """
    nose_x = float(pts[4, 0])
    lc, rc = float(pts[454, 0]), float(pts[234, 0])   # patient-left / patient-right 颊极值
    dl, dr = abs(nose_x - lc), abs(nose_x - rc)
    if max(dl, dr) < 1e-6:
        return None
    # 近似对称（正面）→ 不过滤
    if abs(dl - dr) / max(dl + dr, 1e-6) < 0.12:
        return None
    near_cheek_x = lc if dl > dr else rc
    return nose_x, near_cheek_x


def _keep_near_side(shapes: list[RegionShape], near: tuple[float, float]) -> list[RegionShape]:
    """斜位过滤：保留近侧 + 近中线的笔；丢远侧（透视压缩）。"""
    midline_x, near_cheek_x = near
    near_sign = 1.0 if near_cheek_x >= midline_x else -1.0
    span = abs(near_cheek_x - midline_x) or 1.0
    out = []
    for sh in shapes:
        dx = sh.label_anchor[0] - midline_x
        if abs(dx) < span * 0.25 or (dx * near_sign) > 0:  # 近中线 或 在近侧
            out.append(sh)
    return out or shapes  # 全被滤掉则不过滤（兜底）


def render_panel(image_bgr: np.ndarray, pts: np.ndarray, region_keys: list[str],
                 *, alpha: float = 0.38, title: str | None = None,
                 crop_to_face: bool = True, substrate_bgr: np.ndarray | None = None,
                 near_side_only: bool = False) -> np.ndarray:
    """线稿底图 + 各区半透明色块 + CJK 标签 → panel（返回 BGR）。

    substrate_bgr: 已备好的线稿底图（AI 线稿路径用）。None 时本地 cv2 lineart(image_bgr)。
                   注意：用 AI 线稿时 pts 必须在该线稿上检测（保证标注与线稿几何对齐）。
    crop_to_face: 独立 panel 默认裁到脸+留白，让脸填满画面（源照常框很松/带杂物）。
    near_side_only: 斜位用——双侧区只标朝镜头的近侧（远侧透视压缩/遮挡）。正面自动不过滤。
    接 board 时传 False（board 自己做对齐裁切）。
    """
    from PIL import Image, ImageDraw

    base = substrate_bgr if substrate_bgr is not None else lineart(image_bgr)
    h, w = base.shape[:2]
    scale = _face_scale(pts)
    near = _near_side_x(pts) if near_side_only else None
    pil = Image.fromarray(cv2.cvtColor(base, cv2.COLOR_BGR2RGB)).convert("RGBA")
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    a = int(255 * alpha)
    # one label per region (both L/R zones colored, single label on the highest zone)
    label_jobs: list[tuple[tuple[int, int], str, tuple[int, int, int]]] = []
    for region in region_keys:
        b, g, r = _REGION_COLORS.get(region, _DEFAULT_COLOR)
        rgb = (r, g, b)
        shapes = region_geometry(region, pts)
        if near is not None:
            shapes = _keep_near_side(shapes, near)
        for sh in shapes:
            poly = [tuple(p) for p in sh.points]
            if sh.kind == "fill":
                od.polygon(poly, fill=(r, g, b, a), outline=(r, g, b, 255))
            else:  # stroke
                od.line(poly, fill=(r, g, b, min(255, a + 40)), width=sh.width,
                        joint="curve")
        if shapes:
            # label anchor = the zone whose centroid is highest (smallest y), keeps
            # labels off the busy lower face when a region is bilateral
            top = min(shapes, key=lambda s: s.label_anchor[1])
            label_jobs.append((top.label_anchor, region, rgb))

    out = Image.alpha_composite(pil, overlay)
    od2 = ImageDraw.Draw(out)
    font = _load_font(max(16, int(scale * 0.055)))
    placed: list[tuple[int, int, int, int]] = []
    for (ax, ay), region, rgb in label_jobs:
        ax, ay = _avoid(ax, ay, placed, font, region, od2, w, h)
        _draw_label(od2, (ax, ay), region, rgb, font, placed)

    if crop_to_face:
        out = out.crop(_face_crop_box(pts, w, h))

    if title:
        od3 = ImageDraw.Draw(out)
        tfont = _load_font(max(22, int(scale * 0.07)))
        od3.text((18, 14), title, font=tfont, fill=(30, 30, 30, 255))

    rgb_arr = np.array(out.convert("RGB"))
    return cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2BGR)


def _text_size(draw, text, font) -> tuple[int, int]:
    x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=font)
    return x1 - x0, y1 - y0


def _avoid(ax, ay, placed, font, text, draw, w, h) -> tuple[int, int]:
    """简单去重叠：若标签盒与已放置重叠，下移直到不撞或越界。"""
    tw, th = _text_size(draw, text, font)
    pad = 6
    for _ in range(8):
        box = (ax - tw // 2 - pad, ay - th // 2 - pad, ax + tw // 2 + pad, ay + th // 2 + pad)
        if all(not _overlap(box, p) for p in placed):
            break
        ay += th + 8
    ax = int(min(max(ax, tw // 2 + pad), w - tw // 2 - pad))
    ay = int(min(max(ay, th // 2 + pad), h - th // 2 - pad))
    return ax, ay


def _overlap(a, b) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _draw_label(draw, anchor, text, rgb, font, placed) -> None:
    tw, th = _text_size(draw, text, font)
    ax, ay = anchor
    pad = 6
    box = (ax - tw // 2 - pad, ay - th // 2 - pad, ax + tw // 2 + pad, ay + th // 2 + pad)
    draw.rounded_rectangle(box, radius=6, fill=(255, 255, 255, 235), outline=rgb + (255,),
                           width=2)
    draw.text((ax - tw // 2, ay - th // 2), text, font=font, fill=rgb + (255,))
    placed.append(box)


def panel_for_targets(image_bgr: np.ndarray, focus_text: str, model_path: str,
                      *, title: str | None = None, lineart_mode: str = "cv2",
                      providers=None, crop_to_face: bool = True) -> tuple[np.ndarray | None, list[str]]:
    """端到端：术式描述 → 抽取全部区 → 线稿底图 → 检测 landmark → 渲染 panel。

    lineart_mode: "cv2"（本地 0 付费）| "ai"（img2img，需 providers 链）。
    AI 路径在**线稿本身**检测 landmark（标注与线稿几何对齐，Phase 0 验证可检）；
    cv2 路径在原照检测（线稿与原照同尺寸同坐标）。
    返回 (panel_bgr|None, region_keys)。无脸检出 → (None, regions)。
    """
    regions = atlas.extract_regions(focus_text)
    if lineart_mode == "ai":
        if not providers:
            raise ValueError("lineart_mode='ai' requires a non-empty providers chain")
        substrate, _name = lineart_ai(image_bgr, providers)
        pts = facemesh_landmarks(substrate, model_path)
        if pts is None:
            return None, regions
        panel = render_panel(substrate, pts, regions, title=title,
                             crop_to_face=crop_to_face, substrate_bgr=substrate)
        return panel, regions
    pts = facemesh_landmarks(image_bgr, model_path)
    if pts is None:
        return None, regions
    return render_panel(image_bgr, pts, regions, title=title, crop_to_face=crop_to_face), regions


__all__ = [
    "RegionShape", "region_geometry", "lineart", "lineart_ai", "facemesh_landmarks",
    "render_panel", "panel_for_targets",
]
