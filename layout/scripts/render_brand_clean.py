#!/usr/bin/env python3
"""render_brand_clean.py

正式品牌版渲染器：
- 顶部：时间 / 客户姓名 + 操作项目（同一行）
- 中部：按 render_slots 渲染术前/术后对比
- 底部：居中放大品牌 logo
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASE_LAYOUT_PATH = Path(__file__).resolve().parent / "case_layout_board.py"
CASE_LAYOUT_SPEC = importlib.util.spec_from_file_location("case_layout_board", CASE_LAYOUT_PATH)
if CASE_LAYOUT_SPEC is None or CASE_LAYOUT_SPEC.loader is None:
    raise RuntimeError(f"无法加载 case_layout_board.py: {CASE_LAYOUT_PATH}")
CASE_LAYOUT = importlib.util.module_from_spec(CASE_LAYOUT_SPEC)
CASE_LAYOUT_SPEC.loader.exec_module(CASE_LAYOUT)

SMART_CROP_PATH = Path(__file__).resolve().parent / "smart_crop.py"
_smart_crop_spec = importlib.util.spec_from_file_location("smart_crop", SMART_CROP_PATH)
if _smart_crop_spec and _smart_crop_spec.loader:
    SMART_CROP = importlib.util.module_from_spec(_smart_crop_spec)
    _smart_crop_spec.loader.exec_module(SMART_CROP)
else:
    SMART_CROP = None

DATE_RE = re.compile(r"(\d{2,4}\.\d{1,2}\.\d{1,2})")


def parse_case_meta(case_dir: Path) -> dict:
    case_name = case_dir.name.strip()
    customer_name = case_dir.parent.name.strip()
    date = ""
    project = case_name

    # 不锚定开头：treatment 目录名可能带客户名前缀（如「林方如2026.4.1盈致…」），
    # 锚定开头会让日期解析整体 fallback → 日期 chip 被塞进整串目录名溢出画布
    match = DATE_RE.search(case_name)
    if match:
        date = match.group(1)
        project = (case_name[:match.start()] + case_name[match.end():]).strip(" _-，,")
    if customer_name and project.startswith(customer_name):
        stripped = project[len(customer_name):].strip(" _-，,")
        if stripped:
            project = stripped

    return {
        "date": date or case_name,
        "customer_name": customer_name,
        "project": project or case_name,
    }


def resolve_meta(manifest: dict) -> dict:
    meta = parse_case_meta(Path(manifest["case_dir"]))
    overrides = manifest.get("meta") or {}
    for key in ("date", "customer_name", "project"):
        if key in overrides:
            meta[key] = overrides[key]
    return meta


def detect_view_direction(image_path: str) -> str | None:
    try:
        face = CASE_LAYOUT.FACE_ALIGN.detect_face_landmarks(image_path)
    except Exception:
        try:
            face = CASE_LAYOUT.detect_profile_fallback_face(image_path)
        except Exception:
            return None
    return (face.get("view") or {}).get("direction")


def edge_connected_background_mask(candidate: np.ndarray) -> np.ndarray:
    h, w = candidate.shape[:2]
    work = candidate.astype(np.uint8).copy()
    mask = np.zeros((h + 2, w + 2), dtype=np.uint8)
    seeds = [
        (0, 0),
        (w - 1, 0),
        (0, h - 1),
        (w - 1, h - 1),
        (w // 2, 0),
        (w // 2, h - 1),
        (0, h // 2),
        (w - 1, h // 2),
    ]
    for seed in seeds:
        if work[seed[1], seed[0]] != 1:
            continue
        cv2.floodFill(work, mask, seedPoint=seed, newVal=2)
    return work == 2


def foreground_protect_mask(shape: tuple[int, int]) -> np.ndarray:
    h, w = shape
    mask = np.zeros((h, w), dtype=np.uint8)
    center = (int(w * 0.5), int(h * 0.49))
    axes = (int(w * 0.24), int(h * 0.31))
    cv2.ellipse(mask, center, axes, 0, 0, 360, 255, -1)
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=max(w, h) * 0.015, sigmaY=max(w, h) * 0.015)
    return mask > 12


def view_direction_from_face(face: dict) -> str | None:
    return (face.get("view") or {}).get("direction")


def is_profile_fallback_face(face: dict) -> bool:
    return face.get("fallback") == "profile-cascade"


PROTECTION_TARGET_KEYWORDS = {
    "jawline": ("下颌线", "下颌", "下巴", "颏", "轮廓线", "脸型", "面部轮廓"),
    "mouth_corner": ("口角", "嘴角", "口周", "法令纹"),
    "nose_bridge": ("鼻背", "鼻梁", "鼻根", "鼻尖"),
    "neck_shoulder": ("颈", "颈颏角", "肩", "直角肩", "锁骨"),
}


def collect_protection_targets(manifest: dict, meta: dict) -> list[str]:
    parts = [
        str(meta.get("project") or ""),
        str(meta.get("customer_name") or ""),
        str(manifest.get("case_dir") or ""),
    ]
    for target in manifest.get("focus_targets") or []:
        if isinstance(target, dict):
            parts.extend(str(target.get(key) or "") for key in ("part", "effect", "text", "target"))
        else:
            parts.append(str(target))
    for group in manifest.get("groups") or []:
        for slot_data in (group.get("selected_slots") or {}).values():
            for phase in ("before", "after"):
                p = slot_data.get(phase, {}).get("path") or ""
                parts.append(p)
    haystack = " ".join(parts)
    targets = [
        name
        for name, keywords in PROTECTION_TARGET_KEYWORDS.items()
        if any(keyword in haystack for keyword in keywords)
    ]
    return targets


def should_use_protected_alignment(slot: str, protection_targets: list[str]) -> bool:
    # ⚠ 恒 True：render_aligned_pair 中本函数守卫的 if 分支永远命中，其后的
    # elif（side_profile_contain）与 else（compute_pair_alignment_scales 配对缩放锁）
    # 当前不可达。else 保留为 render_protected_pair 异常 fallback 的升级路径
    # （现 fallback 走无 scale lock 的 render_prepared_cell）。若改本函数为条件判定，
    # 需让其尊重空 targets（影响面大，见 handoff-enhance-pipeline-v5 步骤 5 评估）。
    return slot in {"front", "oblique", "side"}


def clean_background_color(image: np.ndarray) -> tuple[int, int, int]:
    h, w = image.shape[:2]
    band = max(12, min(h, w) // 18)
    edge_pixels = np.concatenate(
        [
            image[:band, :, :].reshape(-1, 3),
            image[h - band:h, :, :].reshape(-1, 3),
            image[:, :band, :].reshape(-1, 3),
            image[:, w - band:w, :].reshape(-1, 3),
        ],
        axis=0,
    ).astype(np.float64)
    gray = edge_pixels.mean(axis=1)
    channel_spread = edge_pixels.max(axis=1) - edge_pixels.min(axis=1)
    brightness_floor = max(120, float(np.percentile(gray, 40)))
    wall_like = edge_pixels[(gray > brightness_floor) & (channel_spread < 40)]
    if len(wall_like) < 50:
        wall_like = edge_pixels[channel_spread < 50]
    if len(wall_like) < 50:
        wall_like = edge_pixels[gray > np.percentile(gray, 60)]
    if len(wall_like) == 0:
        sampled = np.asarray(CASE_LAYOUT.FACE_ALIGN.estimate_background_color(image), dtype=np.float64)
    else:
        sampled = np.median(wall_like, axis=0)
    clean_tone = np.asarray((238, 241, 242), dtype=np.float64)
    blended = sampled * 0.78 + clean_tone * 0.22
    return tuple(int(max(0, min(255, round(value)))) for value in blended)


def protection_box_from_face(
    face: dict,
    slot: str,
    protection_targets: list[str],
) -> tuple[float, float, float, float]:
    crop = CASE_LAYOUT.build_crop_box(face)
    width, height = face["size"]
    x1 = float(crop["x1"])
    y1 = float(crop["y1"])
    x2 = float(crop["x2"])
    y2 = float(crop["y2"])
    box_w = max(1.0, x2 - x1)
    box_h = max(1.0, y2 - y1)

    if slot == "front":
        pad_l, pad_r, pad_t, pad_b = 0.24, 0.24, 0.16, 0.34
    elif slot == "oblique":
        pad_l, pad_r, pad_t, pad_b = 0.18, 0.38, 0.16, 0.42
    else:
        pad_l, pad_r, pad_t, pad_b = 0.46, 0.56, 0.24, 0.56

    if "jawline" in protection_targets:
        pad_b = max(pad_b, 0.48 if slot != "side" else 0.68)
        pad_l = max(pad_l, 0.30 if slot == "front" else pad_l)
        pad_r = max(pad_r, 0.30 if slot == "front" else pad_r)
    if "mouth_corner" in protection_targets:
        pad_l = max(pad_l, 0.26 if slot != "side" else 0.50)
        pad_r = max(pad_r, 0.38 if slot != "front" else 0.30)
        pad_b = max(pad_b, 0.42)
    if "nose_bridge" in protection_targets:
        pad_t = max(pad_t, 0.24)
        pad_l = max(pad_l, 0.28)
        pad_r = max(pad_r, 0.28)
    if "neck_shoulder" in protection_targets:
        pad_b = max(pad_b, 0.70)

    return (
        max(0.0, x1 - box_w * pad_l),
        max(0.0, y1 - box_h * pad_t),
        min(float(width), x2 + box_w * pad_r),
        min(float(height), y2 + box_h * pad_b),
    )


def compute_protected_transform(
    image_shape: tuple[int, int],
    protection_box: tuple[float, float, float, float],
    size: tuple[int, int],
    slot: str,
    scale: float,
) -> dict:
    target_w, target_h = size
    src_h, src_w = image_shape
    px1, py1, px2, py2 = protection_box
    pcx = (px1 + px2) / 2
    pcy = (py1 + py2) / 2

    if slot == "front":
        anchor_x, anchor_y = target_w * 0.50, target_h * 0.48
    elif slot == "oblique":
        anchor_x, anchor_y = target_w * 0.53, target_h * 0.49
    else:
        anchor_x, anchor_y = target_w * 0.50, target_h * 0.50

    x = int(round(anchor_x - pcx * scale))
    y = int(round(anchor_y - pcy * scale))

    def box_in_cell(offset_x: int, offset_y: int) -> tuple[float, float, float, float]:
        return (
            px1 * scale + offset_x,
            py1 * scale + offset_y,
            px2 * scale + offset_x,
            py2 * scale + offset_y,
        )

    safe = 14
    for _ in range(2):
        bx1, by1, bx2, by2 = box_in_cell(x, y)
        if bx1 < safe:
            x += int(round(safe - bx1))
        if bx2 > target_w - safe:
            x -= int(round(bx2 - (target_w - safe)))
        if by1 < safe:
            y += int(round(safe - by1))
        if by2 > target_h - safe:
            y -= int(round(by2 - (target_h - safe)))

    scaled_w = int(round(src_w * scale))
    scaled_h = int(round(src_h * scale))
    # ④底部断图贴框（保护区主路径）：源图 bottom edge 高于 cell 下边线时，底部
    # 行无源像素、被 dark fill，rembg 黑底化后成了人物截断边浮空 + 黑带。整体
    # 下移使截断边与下框线齐平；只 shift 有 gap 的一侧（对齐 tradeoff owner 已
    # 接受），口径与 render_prepared_cell 的 shift_cell_bottom_edge_to_frame 一致。
    bottom_gap = target_h - (y + scaled_h)
    if bottom_gap > 0:
        y += bottom_gap
    clipped = {
        "left": max(0, -x),
        "right": max(0, x + scaled_w - target_w),
        "top": max(0, -y),
        "bottom": max(0, y + scaled_h - target_h),
    }
    return {
        "scale": scale,
        "offset": [x, y],
        "bottom_edge_shift_px": max(0, int(bottom_gap)),
        "protection_cell_box": [round(value, 1) for value in box_in_cell(x, y)],
        "clipped_px": clipped,
    }


def transform_box_in_cell(
    protection_box: tuple[float, float, float, float],
    transform: dict,
) -> list[float]:
    px1, py1, px2, py2 = protection_box
    scale = float(transform["scale"])
    x, y = transform["offset"]
    return [
        round(px1 * scale + x, 1),
        round(py1 * scale + y, 1),
        round(px2 * scale + x, 1),
        round(py2 * scale + y, 1),
    ]


def face_crop_box_from_face(face: dict) -> tuple[float, float, float, float]:
    crop = CASE_LAYOUT.build_crop_box(face)
    return (
        float(crop["x1"]),
        float(crop["y1"]),
        float(crop["x2"]),
        float(crop["y2"]),
    )


def clipped_px_for_transform(
    image_shape: tuple[int, int],
    size: tuple[int, int],
    transform: dict,
) -> dict:
    target_w, target_h = size
    src_h, src_w = image_shape
    scale = float(transform["scale"])
    x, y = transform["offset"]
    scaled_w = int(round(src_w * scale))
    scaled_h = int(round(src_h * scale))
    return {
        "left": max(0, -int(x)),
        "right": max(0, int(x) + scaled_w - target_w),
        "top": max(0, -int(y)),
        "bottom": max(0, int(y) + scaled_h - target_h),
    }


def align_before_transform_to_after_reference(
    before_transform: dict,
    after_transform: dict,
    before_box: tuple[float, float, float, float],
    before_shape: tuple[int, int],
    size: tuple[int, int],
) -> tuple[dict, dict]:
    target_w, target_h = size
    aligned = dict(before_transform)
    before_offset = list(before_transform["offset"])
    after_offset = list(after_transform["offset"])
    x, y = int(after_offset[0]), int(after_offset[1])
    scale = float(aligned["scale"])
    px1, py1, px2, py2 = before_box
    safe = 14

    # Clamp only enough to preserve the medical observation region. Scale is never changed.
    for _ in range(2):
        bx1 = px1 * scale + x
        by1 = py1 * scale + y
        bx2 = px2 * scale + x
        by2 = py2 * scale + y
        if bx1 < safe:
            x += int(round(safe - bx1))
        if bx2 > target_w - safe:
            x -= int(round(bx2 - (target_w - safe)))
        if by1 < safe:
            y += int(round(safe - by1))
        if by2 > target_h - safe:
            y -= int(round(by2 - (target_h - safe)))

    aligned["offset"] = [x, y]
    aligned["protection_cell_box"] = transform_box_in_cell(before_box, aligned)
    aligned["clipped_px"] = clipped_px_for_transform(before_shape, size, aligned)
    return aligned, {
        "enabled": aligned["offset"] != before_offset,
        "strategy": "match_after_offset_crop_only",
        "scale_changed": False,
        "before_offset": before_offset,
        "after_reference_offset": after_offset,
        "applied_offset": aligned["offset"],
    }


def paste_clipped(canvas: np.ndarray, image: np.ndarray, x: int, y: int) -> None:
    target_h, target_w = canvas.shape[:2]
    src_h, src_w = image.shape[:2]
    dst_x1 = max(0, x)
    dst_y1 = max(0, y)
    dst_x2 = min(target_w, x + src_w)
    dst_y2 = min(target_h, y + src_h)
    if dst_x1 >= dst_x2 or dst_y1 >= dst_y2:
        return
    src_x1 = dst_x1 - x
    src_y1 = dst_y1 - y
    src_x2 = src_x1 + (dst_x2 - dst_x1)
    src_y2 = src_y1 + (dst_y2 - dst_y1)
    canvas[dst_y1:dst_y2, dst_x1:dst_x2] = image[src_y1:src_y2, src_x1:src_x2]


def paste_clipped_with_mask(canvas: np.ndarray, image: np.ndarray, x: int, y: int) -> np.ndarray:
    target_h, target_w = canvas.shape[:2]
    valid_mask = np.zeros((target_h, target_w), dtype=bool)
    src_h, src_w = image.shape[:2]
    dst_x1 = max(0, x)
    dst_y1 = max(0, y)
    dst_x2 = min(target_w, x + src_w)
    dst_y2 = min(target_h, y + src_h)
    if dst_x1 >= dst_x2 or dst_y1 >= dst_y2:
        return valid_mask
    src_x1 = dst_x1 - x
    src_y1 = dst_y1 - y
    src_x2 = src_x1 + (dst_x2 - dst_x1)
    src_y2 = src_y1 + (dst_y2 - dst_y1)
    canvas[dst_y1:dst_y2, dst_x1:dst_x2] = image[src_y1:src_y2, src_x1:src_x2]
    valid_mask[dst_y1:dst_y2, dst_x1:dst_x2] = True
    return valid_mask


def sample_wall_color_from_pixels(pixels: np.ndarray) -> np.ndarray:
    if pixels.size == 0:
        return np.asarray([244, 244, 243], dtype=np.float64)
    pixels_f = pixels.reshape(-1, 3).astype(np.float64)
    gray = pixels_f.mean(axis=1)
    spread = pixels_f.max(axis=1) - pixels_f.min(axis=1)
    wall_like = pixels_f[(gray > 190) & (spread < 38)]
    if len(wall_like) < 80:
        bright = gray > np.percentile(gray, 82)
        wall_like = pixels_f[bright & (spread < 55)]
    if len(wall_like) == 0:
        wall_like = pixels_f[gray > np.percentile(gray, 86)]
    if len(wall_like) == 0:
        return np.asarray([244, 244, 243], dtype=np.float64)
    sampled = np.median(wall_like, axis=0)
    clean_wall = np.asarray([244, 244, 243], dtype=np.float64)
    return sampled * 0.72 + clean_wall * 0.28


def sample_valid_wall_color(cell: np.ndarray, valid_mask: np.ndarray) -> np.ndarray:
    return sample_wall_color_from_pixels(cell[valid_mask.astype(bool)])


def sample_edge_wall_color(
    cell: np.ndarray,
    valid_mask: np.ndarray,
    rect: tuple[int, int, int, int],
    side: str,
) -> np.ndarray:
    x1, y1, x2, y2 = rect
    band = max(12, min(cell.shape[:2]) // 28)
    if side == "left":
        area = (slice(y1, y2), slice(x1, min(x2, x1 + band)))
    elif side == "right":
        area = (slice(y1, y2), slice(max(x1, x2 - band), x2))
    elif side == "top":
        area = (slice(y1, min(y2, y1 + band)), slice(x1, x2))
    else:
        area = (slice(max(y1, y2 - band), y2), slice(x1, x2))
    mask = valid_mask[area]
    pixels = cell[area][mask]
    sampled = sample_wall_color_from_pixels(pixels)
    global_sample = sample_valid_wall_color(cell, valid_mask)
    # If a side strip has too much subject color, pull it back toward the global wall tone.
    if sampled.mean() < global_sample.mean() - 12:
        sampled = global_sample
    elif np.linalg.norm(sampled - global_sample) > 24:
        sampled = sampled * 0.45 + global_sample * 0.55
    return sampled


def extend_padding_background(cell: np.ndarray, valid_mask: np.ndarray, slot: str) -> tuple[np.ndarray, dict]:
    invalid_mask = ~valid_mask.astype(bool)
    invalid_ratio = float(invalid_mask.mean())
    record: dict = {
        "mode": "none",
        "used_ai": False,
        "filled_ratio": round(invalid_ratio, 6),
    }
    if invalid_ratio <= 0:
        return cell, record

    dark_fill = np.array([25, 25, 25], dtype=np.uint8)
    seeded = cell.copy()
    seeded[invalid_mask] = dark_fill
    record.update({
        "mode": "dark_fill_for_studio_replacement",
        "fill_bgr": [25, 25, 25],
        "reason": "dark_fill_eliminates_paste_boundary_artifact",
    })
    return seeded, record


def render_protected_cell(
    image: np.ndarray,
    transform: dict,
    size: tuple[int, int],
) -> np.ndarray:
    target_w, target_h = size
    canvas = np.full((target_h, target_w, 3), (25, 25, 25), dtype=np.uint8)
    scale = max(float(transform["scale"]), 1e-6)
    resized_w = max(1, int(round(image.shape[1] * scale)))
    resized_h = max(1, int(round(image.shape[0] * scale)))
    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    resized = cv2.resize(image, (resized_w, resized_h), interpolation=interpolation)
    x, y = transform["offset"]
    valid_mask = paste_clipped_with_mask(canvas, resized, int(x), int(y))

    feather_w = 6
    dist_inside = cv2.distanceTransform(valid_mask.astype(np.uint8), cv2.DIST_L2, 5)
    feather_zone = (dist_inside > 0) & (dist_inside < feather_w)
    if np.any(feather_zone):
        feather_alpha = np.clip(dist_inside / feather_w, 0, 1)
        bg_dark = np.array([25, 25, 25], dtype=np.float64)
        for c in range(3):
            canvas[:, :, c] = np.where(
                feather_zone,
                np.clip(canvas[:, :, c].astype(np.float64) * feather_alpha + bg_dark[c] * (1 - feather_alpha), 0, 255),
                canvas[:, :, c],
            ).astype(np.uint8)

    slot = str(transform.get("slot") or "front")
    canvas, extension = extend_padding_background(canvas, valid_mask, slot)
    try:
        canvas, _foreground_mask, cleanup = CASE_LAYOUT.apply_conservative_background_policy(
            canvas,
            slot,
            valid_mask=valid_mask,
        )
        extension["background_policy"] = {
            key: value
            for key, value in {
                "policy": cleanup.get("policy"),
                "status": cleanup.get("status"),
                "reason": cleanup.get("reason"),
                "invalid_ratio": cleanup.get("invalid_ratio"),
                "invalid_component_ratio": cleanup.get("invalid_component_ratio"),
                "sampled_bgr": cleanup.get("sampled_bgr"),
                "bg_ratio": cleanup.get("bg_ratio"),
                "dirty_score": cleanup.get("dirty_score"),
            }.items()
            if value is not None
        }
    except Exception as exc:
        extension["background_policy"] = {
            "status": "skipped",
            "reason": str(exc)[:160],
        }
    transform["background_extension"] = extension
    return canvas


def sample_cell_background(cell: np.ndarray) -> np.ndarray:
    h, w = cell.shape[:2]
    band = max(8, min(h, w) // 24)
    edge_pixels = np.concatenate(
        [
            cell[:band, :, :].reshape(-1, 3),
            cell[h - band:h, :, :].reshape(-1, 3),
            cell[:, :band, :].reshape(-1, 3),
            cell[:, w - band:w, :].reshape(-1, 3),
        ],
        axis=0,
    ).astype(np.float64)
    gray = edge_pixels.mean(axis=1)
    spread = edge_pixels.max(axis=1) - edge_pixels.min(axis=1)
    wall_like = edge_pixels[(gray > 150) & (spread < 40)]
    if len(wall_like) < 50:
        wall_like = edge_pixels[gray > np.percentile(gray, 65)]
    if len(wall_like) == 0:
        return np.asarray([238, 241, 242], dtype=np.float64)
    return np.median(wall_like, axis=0)


def foreground_bbox_in_cell(cell: np.ndarray) -> tuple[int, int, int, int] | None:
    bg = sample_cell_background(cell)
    diff = np.linalg.norm(cell.astype(np.float64) - bg.reshape(1, 1, 3), axis=2)
    gray = cell.astype(np.float64).mean(axis=2)
    foreground = (diff > 28) | (gray < 135)
    foreground = foreground.astype(np.uint8)
    kernel = np.ones((5, 5), dtype=np.uint8)
    foreground = cv2.morphologyEx(foreground, cv2.MORPH_OPEN, kernel)
    foreground = cv2.morphologyEx(foreground, cv2.MORPH_CLOSE, kernel)
    ys, xs = np.where(foreground > 0)
    if len(xs) == 0 or len(ys) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def cover_foreground_with_background(
    cell: np.ndarray,
    crop_mask: np.ndarray,
) -> np.ndarray:
    if not np.any(crop_mask):
        return cell
    bg = sample_cell_background(cell)
    sigma = max(3.0, min(cell.shape[:2]) * 0.008)
    alpha = cv2.GaussianBlur(crop_mask.astype(np.uint8) * 255, (0, 0), sigmaX=sigma, sigmaY=sigma)
    alpha_f = (alpha.astype(np.float64) / 255.0)[:, :, None]
    filled = cell.astype(np.float64) * (1.0 - alpha_f) + bg.reshape(1, 1, 3) * alpha_f
    return np.clip(filled, 0, 255).astype(np.uint8)


def composition_diagnostic_from_cells(
    before_arr: np.ndarray,
    after_arr: np.ndarray,
    slot: str,
) -> dict:
    before_bbox = foreground_bbox_in_cell(before_arr)
    after_bbox = foreground_bbox_in_cell(after_arr)
    diagnostic = {
        "slot": slot,
        "slot_label": CASE_LAYOUT.ANGLE_LABELS.get(slot, slot),
        "status": "ok",
        "before_bbox": before_bbox,
        "after_bbox": after_bbox,
        "alerts": [],
    }
    if before_bbox is None or after_bbox is None:
        diagnostic["status"] = "unknown"
        diagnostic["reason"] = "foreground_bbox_unavailable"
        return diagnostic

    before_height = before_bbox[3] - before_bbox[1]
    after_height = after_bbox[3] - after_bbox[1]
    height_delta = before_height - after_height
    top_delta = after_bbox[1] - before_bbox[1]
    bottom_delta = before_bbox[3] - after_bbox[3]
    diagnostic["metrics"] = {
        "before_height": int(before_height),
        "after_height": int(after_height),
        "height_delta": int(height_delta),
        "top_delta": int(top_delta),
        "bottom_delta": int(bottom_delta),
    }

    threshold = 40 if slot == "side" else 36
    if height_delta >= threshold and top_delta >= 24:
        diagnostic["status"] = "warning"
        diagnostic["alerts"].append({
            "code": "before_body_scope_larger_than_after",
            "severity": "warning",
            "message": (
                f"{CASE_LAYOUT.ANGLE_LABELS.get(slot, slot)} 术前主体高度比术后多 {int(height_delta)}px，"
                "多出的肩颈/躯干范围可能影响正式对比一致性。建议人工重选更接近构图的术前图，"
                "或进入人工微调/AI边缘修复；系统未自动裁切以避免产生切口。"
            ),
            "recommended_action": "manual_reselect_or_edge_repair",
        })
    return diagnostic


def shift_cell_down_with_background(cell: np.ndarray, shift_y: int) -> np.ndarray:
    if shift_y <= 0:
        return cell
    h, w = cell.shape[:2]
    shift_y = min(int(shift_y), h - 1)
    bg = sample_cell_background(cell)
    shifted = np.full((h, w, 3), np.clip(bg, 0, 255).astype(np.uint8), dtype=np.uint8)
    shifted[shift_y:h, :, :] = cell[: h - shift_y, :, :]
    return shifted


def crop_cell_bottom_to_background(cell: np.ndarray, from_y: int) -> np.ndarray:
    h, _w = cell.shape[:2]
    from_y = int(max(0, min(from_y, h)))
    if from_y >= h:
        return cell
    bg = sample_cell_background(cell)
    cropped = cell.copy()
    cropped[from_y:h, :, :] = np.clip(bg, 0, 255).astype(np.uint8)
    return cropped


def bottom_valid_gap_px(valid_mask: np.ndarray) -> int:
    rows_valid = np.asarray(valid_mask, dtype=bool).any(axis=1)
    if not rows_valid.any():
        return 0
    last_valid = int(np.flatnonzero(rows_valid)[-1])
    return int(rows_valid.shape[0] - 1 - last_valid)


def shift_cell_bottom_edge_to_frame(
    cell: np.ndarray,
    valid_mask: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, int]:
    # 源图 bottom edge 高于 cell 下边线时，底部 invalid 行被背景填充，到
    # slot_transform（rembg 黑底化）阶段就成了人物截断边浮空 + 黑带。规范：
    # 人像整体下移使截断边与下框线齐平；只 shift 有 gap 的一侧（眼对齐
    # tradeoff owner 已接受）。顶部空出的行标 invalid，交由后续 conservative
    # background policy 填充，与未 shift 路径口径一致。
    gap = bottom_valid_gap_px(valid_mask)
    if gap <= 0:
        return cell, valid_mask, 0
    h = cell.shape[0]
    gap = min(int(gap), h - 1)
    shifted_cell = shift_cell_down_with_background(cell, gap)
    mask = np.asarray(valid_mask, dtype=bool)
    shifted_mask = np.zeros_like(mask)
    shifted_mask[gap:, :] = mask[: h - gap, :]
    return shifted_cell, shifted_mask, gap


def paste_resized_cell_with_background(
    cell: np.ndarray,
    scale: float,
    offset_x: int,
    offset_y: int,
) -> np.ndarray:
    h, w = cell.shape[:2]
    bg = np.array([25, 25, 25], dtype=np.uint8)
    canvas = np.full((h, w, 3), bg, dtype=np.uint8)
    resized_w = max(1, int(round(w * scale)))
    resized_h = max(1, int(round(h * scale)))
    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    resized = cv2.resize(cell, (resized_w, resized_h), interpolation=interpolation)
    paste_clipped(canvas, resized, offset_x, offset_y)
    return canvas


def apply_manual_preop_transform(
    cell: np.ndarray | Image.Image,
    transform: dict | None,
) -> tuple[np.ndarray | Image.Image, dict]:
    result = {
        "enabled": False,
        "strategy": "manual_layer_transform_after_auto_alignment",
        "reason": "no_manual_transform",
    }
    if not isinstance(transform, dict):
        return cell, result
    try:
        offset_x_pct = float(transform.get("offset_x_pct") or 0)
        offset_y_pct = float(transform.get("offset_y_pct") or 0)
        scale = float(transform.get("scale") or 1)
    except (TypeError, ValueError):
        result["reason"] = "invalid_manual_transform"
        return cell, result

    offset_x_pct = max(-0.25, min(0.25, offset_x_pct))
    offset_y_pct = max(-0.25, min(0.25, offset_y_pct))
    scale = max(0.85, min(1.15, scale))
    if abs(offset_x_pct) < 0.0005 and abs(offset_y_pct) < 0.0005 and abs(scale - 1.0) < 0.0005:
        result["reason"] = "manual_transform_is_identity"
        return cell, result

    input_is_pil = isinstance(cell, Image.Image)
    arr = np.asarray(cell.convert("RGB")) if input_is_pil else cell
    h, w = arr.shape[:2]
    offset_x = int(round((w - w * scale) / 2 + offset_x_pct * w))
    offset_y = int(round((h - h * scale) / 2 + offset_y_pct * h))
    before_bbox = foreground_bbox_in_cell(arr)
    aligned = paste_resized_cell_with_background(arr, scale, offset_x, offset_y)
    after_bbox = foreground_bbox_in_cell(aligned)
    result.update({
        "enabled": True,
        "reason": "user_adjusted_preop_layer",
        "offset_x_pct": round(offset_x_pct, 4),
        "offset_y_pct": round(offset_y_pct, 4),
        "scale": round(scale, 4),
        "pixel_offset": [int(offset_x), int(offset_y)],
        "before_bbox": before_bbox,
        "after_bbox": after_bbox,
    })
    if input_is_pil:
        return Image.fromarray(aligned), result
    return aligned, result


def attach_manual_preop_transform_record(
    records: list[dict],
    slot: str,
    before_path: str,
    after_path: str,
    transform_record: dict,
) -> None:
    if not records:
        records.append({
            "slot": slot,
            "strategy": "manual_layer_transform_after_auto_alignment",
            "before": Path(before_path).name,
            "after": Path(after_path).name,
            "manual_preop_transform": transform_record,
        })
        return
    for record in reversed(records):
        if not isinstance(record, dict):
            continue
        if record.get("slot") != slot:
            continue
        if record.get("before") and record.get("before") != Path(before_path).name:
            continue
        record["manual_preop_transform"] = transform_record
        return
    records[-1]["manual_preop_transform"] = transform_record


def align_before_size_to_after_cell(
    before_arr: np.ndarray,
    before_ref_box: list[float],
    after_ref_box: list[float],
    slot: str,
) -> tuple[np.ndarray, dict]:
    result = {
        "enabled": False,
        "strategy": "match_after_face_box_limited_scale",
        "scale_changed": False,
        "before_ref_box": [round(float(v), 1) for v in before_ref_box],
        "after_ref_box": [round(float(v), 1) for v in after_ref_box],
        "scale_factor": 1.0,
        "reason": "invalid_reference_box",
    }
    bx1, by1, bx2, by2 = [float(v) for v in before_ref_box]
    ax1, ay1, ax2, ay2 = [float(v) for v in after_ref_box]
    before_w = max(1.0, bx2 - bx1)
    before_h = max(1.0, by2 - by1)
    after_w = max(1.0, ax2 - ax1)
    after_h = max(1.0, ay2 - ay1)
    height_ratio = after_h / before_h
    width_ratio = after_w / before_w
    if not (0.58 <= height_ratio <= 1.72):
        result.update({
            "reason": "face_box_ratio_out_of_safe_range",
            "height_ratio": round(height_ratio, 4),
            "width_ratio": round(width_ratio, 4),
        })
        return before_arr, result

    raw_scale = float((height_ratio * width_ratio) ** 0.5)
    lower = 0.65 if slot != "side" else 0.62
    upper = 1.45 if slot != "side" else 1.50
    scale = float(max(lower, min(upper, raw_scale)))
    before_center_x = (bx1 + bx2) / 2.0
    after_center_x = (ax1 + ax2) / 2.0
    offset_x = int(round(after_center_x - before_center_x * scale))
    offset_y = int(round(ay1 - by1 * scale))
    if abs(scale - 1.0) < 0.018 and abs(offset_x) < 4 and abs(offset_y) < 4:
        result.update({
            "reason": "scale_and_position_delta_too_small",
            "height_ratio": round(height_ratio, 4),
            "width_ratio": round(width_ratio, 4),
            "raw_scale": round(raw_scale, 4),
        })
        return before_arr, result
    aligned = paste_resized_cell_with_background(before_arr, scale, offset_x, offset_y)
    aligned_ref_box = [
        round(bx1 * scale + offset_x, 1),
        round(by1 * scale + offset_y, 1),
        round(bx2 * scale + offset_x, 1),
        round(by2 * scale + offset_y, 1),
    ]
    result.update({
        "enabled": True,
        "scale_changed": abs(scale - 1.0) >= 0.018,
        "reason": "术前脸部检测框与术后大小/位置不一致，已按术后检测框做受限缩放和平移",
        "height_ratio": round(height_ratio, 4),
        "width_ratio": round(width_ratio, 4),
        "raw_scale": round(raw_scale, 4),
        "scale_factor": round(scale, 4),
        "offset": [offset_x, offset_y],
        "aligned_ref_box": aligned_ref_box,
    })
    return aligned, result


def align_before_side_silhouette_to_after_cell(
    before_arr: np.ndarray,
    after_arr: np.ndarray,
) -> tuple[np.ndarray, dict]:
    before_bbox = foreground_bbox_in_cell(before_arr)
    after_bbox = foreground_bbox_in_cell(after_arr)
    result = {
        "enabled": False,
        "strategy": "match_after_side_silhouette_limited_scale",
        "scale_changed": False,
        "before_bbox": before_bbox,
        "after_bbox": after_bbox,
        "scale_factor": 1.0,
        "reason": "no_foreground_bbox",
    }
    if before_bbox is None or after_bbox is None:
        return before_arr, result

    bx1, by1, bx2, by2 = [float(v) for v in before_bbox]
    ax1, ay1, ax2, ay2 = [float(v) for v in after_bbox]
    before_w = max(1.0, bx2 - bx1)
    before_h = max(1.0, by2 - by1)
    after_w = max(1.0, ax2 - ax1)
    after_h = max(1.0, ay2 - ay1)
    width_ratio = after_w / before_w
    height_ratio = after_h / before_h
    raw_scale = float((width_ratio * height_ratio) ** 0.5)
    scale = float(max(0.90, min(1.06, raw_scale)))
    before_center_x = (bx1 + bx2) / 2.0
    after_center_x = (ax1 + ax2) / 2.0
    offset_x = int(round(after_center_x - before_center_x * scale))
    offset_y = int(round(ay1 - by1 * scale))
    if abs(scale - 1.0) < 0.015 and abs(offset_x) < 4 and abs(offset_y) < 4:
        result.update({
            "reason": "silhouette_delta_too_small",
            "width_ratio": round(width_ratio, 4),
            "height_ratio": round(height_ratio, 4),
            "raw_scale": round(raw_scale, 4),
        })
        return before_arr, result

    aligned = paste_resized_cell_with_background(before_arr, scale, offset_x, offset_y)
    aligned_bbox = foreground_bbox_in_cell(aligned)
    result.update({
        "enabled": True,
        "scale_changed": abs(scale - 1.0) >= 0.015,
        "reason": "侧面人脸检测框比例异常，已改用人物轮廓做受限缩放和平移",
        "width_ratio": round(width_ratio, 4),
        "height_ratio": round(height_ratio, 4),
        "raw_scale": round(raw_scale, 4),
        "scale_factor": round(scale, 4),
        "offset": [offset_x, offset_y],
        "aligned_bbox": aligned_bbox,
    })
    return aligned, result


def align_before_position_to_after_cell(
    before_arr: np.ndarray,
    after_arr: np.ndarray,
    slot: str,
) -> tuple[np.ndarray, dict]:
    before_bbox = foreground_bbox_in_cell(before_arr)
    after_bbox = foreground_bbox_in_cell(after_arr)
    result = {
        "enabled": False,
        "strategy": "match_after_foreground_top_crop_bottom",
        "scale_changed": False,
        "before_bbox": before_bbox,
        "after_bbox": after_bbox,
        "shift_y": 0,
        "reason": "no_foreground_bbox",
    }
    if before_bbox is None or after_bbox is None:
        return before_arr, result

    before_height = before_bbox[3] - before_bbox[1]
    after_height = after_bbox[3] - after_bbox[1]
    height_delta = before_height - after_height
    top_delta = after_bbox[1] - before_bbox[1]
    bottom_delta = before_bbox[3] - after_bbox[3]
    threshold = 40 if slot == "side" else 36
    result["metrics_before"] = {
        "before_height": int(before_height),
        "after_height": int(after_height),
        "height_delta": int(height_delta),
        "top_delta": int(top_delta),
        "bottom_delta": int(bottom_delta),
    }

    max_shift = max(1, int(round(before_arr.shape[0] * (0.15 if slot != "side" else 0.12))))
    shift_y = 0
    shifted = before_arr
    if height_delta >= threshold and top_delta >= 24 and bottom_delta >= -8:
        shift_y = int(max(0, min(top_delta, height_delta, max_shift)))
        shifted = shift_cell_down_with_background(before_arr, shift_y)
    after_shift_bbox = foreground_bbox_in_cell(shifted)
    bottom_crop_from_y = None
    bottom_cropped_px = 0
    if after_shift_bbox is not None and after_shift_bbox[3] > after_bbox[3] + 4:
        pre_crop_bottom = int(after_shift_bbox[3])
        bottom_crop_from_y = int(after_bbox[3] + 4)
        shifted = crop_cell_bottom_to_background(shifted, bottom_crop_from_y)
        after_shift_bbox = foreground_bbox_in_cell(shifted)
        bottom_cropped_px = max(0, pre_crop_bottom - int(bottom_crop_from_y))
    if shift_y <= 0 and bottom_crop_from_y is None:
        result["reason"] = "before_scope_not_significantly_larger"
        return before_arr, result
    result.update({
        "enabled": True,
        "reason": "术前主体与术后位置不一致，已按术后位置做受限平移/底部裁切",
        "shift_y": int(shift_y),
        "before_bbox_after_shift": after_shift_bbox,
        "cropped_bottom_px": int(bottom_cropped_px),
        "bottom_crop_from_y": bottom_crop_from_y,
    })
    if after_shift_bbox is not None:
        result["metrics_after"] = {
            "before_height": int(after_shift_bbox[3] - after_shift_bbox[1]),
            "after_height": int(after_height),
            "height_delta": int((after_shift_bbox[3] - after_shift_bbox[1]) - after_height),
            "top_delta": int(after_bbox[1] - after_shift_bbox[1]),
            "bottom_delta": int(after_shift_bbox[3] - after_bbox[3]),
        }
    return shifted, result


def trim_before_excess_to_after_reference(
    before_arr: np.ndarray,
    after_arr: np.ndarray,
    before_transform: dict,
    after_transform: dict,
    slot: str,
) -> tuple[np.ndarray, dict]:
    before_bbox = foreground_bbox_in_cell(before_arr)
    after_bbox = foreground_bbox_in_cell(after_arr)
    result = {
        "enabled": False,
        "reason": "no_foreground_bbox",
        "before_bbox": before_bbox,
        "after_bbox": after_bbox,
        "crop_edges": {},
        "scale_changed": False,
    }
    if before_bbox is None or after_bbox is None:
        return before_arr, result

    h, w = before_arr.shape[:2]
    before_protect = before_transform.get("protection_cell_box") or [0, 0, w, h]
    protect_left, protect_top, protect_right, protect_bottom = [float(v) for v in before_protect]
    tolerance = 26 if slot == "front" else 34
    protect_margin = 18

    before_left, before_top, before_right, before_bottom = before_bbox
    after_left, after_top, after_right, after_bottom = after_bbox
    crop_mask = np.zeros((h, w), dtype=bool)
    crop_edges: dict[str, int] = {}

    if before_bottom > after_bottom + tolerance:
        limit = int(max(after_bottom + tolerance, protect_bottom + protect_margin))
        if limit < h:
            crop_mask[limit:h, :] = True
            crop_edges["bottom_from_y"] = limit
    if before_top < after_top - tolerance:
        limit = int(min(after_top - tolerance, protect_top - protect_margin))
        if limit > 0:
            crop_mask[:limit, :] = True
            crop_edges["top_to_y"] = limit
    if before_right > after_right + tolerance:
        limit = int(max(after_right + tolerance, protect_right + protect_margin))
        if limit < w:
            crop_mask[:, limit:w] = True
            crop_edges["right_from_x"] = limit
    if before_left < after_left - tolerance:
        limit = int(min(after_left - tolerance, protect_left - protect_margin))
        if limit > 0:
            crop_mask[:, :limit] = True
            crop_edges["left_to_x"] = limit

    if not crop_edges:
        result["reason"] = "before_not_larger_than_after_reference"
        return before_arr, result

    if not np.any(crop_mask):
        result["reason"] = "empty_crop_area"
        result["crop_edges"] = crop_edges
        return before_arr, result

    trimmed = cover_foreground_with_background(before_arr, crop_mask)
    result.update({
        "enabled": True,
        "reason": "crop_before_excess_to_after_reference",
        "crop_edges": crop_edges,
        "trimmed_pixel_ratio": round(float(crop_mask.mean()), 6),
    })
    return trimmed, result


_REMBG_SESSION = None


def _get_rembg_session():
    global _REMBG_SESSION
    if _REMBG_SESSION is None:
        import os
        from rembg import new_session
        _REMBG_SESSION = new_session(os.environ.get("CASE_REMBG_MODEL", "birefnet-portrait"))
    return _REMBG_SESSION


def _decontaminate_edge_color(
    cell: np.ndarray,
    alpha: np.ndarray,
    fg_binary: np.ndarray,
) -> np.ndarray:
    """alpha 边缘颜色去污：partial alpha 像素用最近纯前景色替代，消除白墙泄漏。"""
    h, w = cell.shape[:2]
    edge_zone = (alpha > 0.05) & (alpha < 0.92) & (fg_binary == 0)
    if not np.any(edge_zone):
        return cell

    solid_fg = (alpha > 0.92).astype(np.uint8)
    dist_to_solid = cv2.distanceTransform(1 - solid_fg, cv2.DIST_L2, 5)

    blurred = cv2.GaussianBlur(cell.astype(np.float64), (0, 0), sigmaX=5)
    solid_color = np.zeros_like(cell, dtype=np.float64)
    for c in range(3):
        ch = cell[:, :, c].astype(np.float64)
        ch_blur = cv2.GaussianBlur(ch * solid_fg, (0, 0), sigmaX=8)
        weight_blur = cv2.GaussianBlur(solid_fg.astype(np.float64), (0, 0), sigmaX=8)
        safe_weight = np.maximum(weight_blur, 1e-6)
        solid_color[:, :, c] = ch_blur / safe_weight

    result = cell.copy().astype(np.float64)
    blend = np.clip(dist_to_solid / 12.0, 0, 1)
    for c in range(3):
        result[:, :, c] = np.where(
            edge_zone,
            cell[:, :, c] * (1 - blend) + solid_color[:, :, c] * blend,
            result[:, :, c],
        )
    return np.clip(result, 0, 255).astype(np.uint8)


def _recover_hair_alpha(
    cell: np.ndarray,
    fg_binary: np.ndarray,
    raw_alpha: np.ndarray,
) -> np.ndarray:
    """头部区域发丝 alpha 恢复：对 rembg 丢掉的碎发用颜色相似度重新给 alpha。"""
    h, w = cell.shape[:2]
    hair_alpha = np.zeros((h, w), dtype=np.float64)

    fg_ys = np.where(fg_binary.any(axis=1))[0]
    if len(fg_ys) == 0:
        return hair_alpha
    fg_top = int(fg_ys[0])
    fg_xs = np.where(fg_binary[fg_top:fg_top + max(1, h // 8), :].any(axis=0))[0]
    if len(fg_xs) == 0:
        return hair_alpha
    fg_cx = int((fg_xs[0] + fg_xs[-1]) / 2)

    hair_sample_y0 = max(0, fg_top)
    hair_sample_y1 = min(h, fg_top + max(20, h // 6))
    hair_sample_x0 = max(0, fg_cx - w // 4)
    hair_sample_x1 = min(w, fg_cx + w // 4)
    hair_region_mask = (fg_binary[hair_sample_y0:hair_sample_y1, hair_sample_x0:hair_sample_x1] > 0)
    if hair_region_mask.sum() < 100:
        return hair_alpha
    hair_pixels = cell[hair_sample_y0:hair_sample_y1, hair_sample_x0:hair_sample_x1][hair_region_mask]
    hair_mean = hair_pixels.mean(axis=0).astype(np.float64)
    hair_std = max(hair_pixels.std(), 15.0)

    search_y0 = max(0, fg_top - h // 6)
    search_y1 = min(h, fg_top + h // 4)
    search_region = cell[search_y0:search_y1, :].astype(np.float64)
    color_dist = np.sqrt(((search_region - hair_mean) ** 2).sum(axis=2))
    similarity = np.clip(1.0 - (color_dist - hair_std) / (hair_std * 2.5), 0, 1)

    dist_from_fg_local = cv2.distanceTransform(
        1 - fg_binary[search_y0:search_y1, :], cv2.DIST_L2, 5
    )
    proximity = np.clip(1.0 - (dist_from_fg_local - 2) / 25.0, 0, 1)

    local_alpha = similarity * proximity * 0.8
    local_alpha[fg_binary[search_y0:search_y1, :] > 0] = 0

    brightness_local = cv2.cvtColor(
        cell[search_y0:search_y1, :], cv2.COLOR_BGR2GRAY
    ).astype(np.float64)
    local_alpha[brightness_local > 200] = 0

    hair_alpha[search_y0:search_y1, :] = local_alpha
    return hair_alpha


def _predarken_bright_background(
    cell: np.ndarray, bg_color: np.ndarray,
) -> tuple[np.ndarray, bool]:
    """rembg 前预处理：检测并压暗白墙/高亮低饱和背景区域。

    检测条件：亮度 > 130 + 饱和度 < 50 + 连通域 > 1.5% + 触碰图像边缘。
    """
    h, w = cell.shape[:2]

    hsv = cv2.cvtColor(cell, cv2.COLOR_BGR2HSV)
    brightness = cell.astype(np.float64).mean(axis=2)
    saturation = hsv[:, :, 1].astype(np.float64)

    wall_cand = ((brightness > 130) & (saturation < 50)).astype(np.uint8)

    num, labels, stats, _ = cv2.connectedComponentsWithStats(wall_cand, connectivity=8)
    min_area = int(h * w * 0.015)
    wall_mask = np.zeros((h, w), dtype=np.uint8)
    for i in range(1, num):
        if stats[i, cv2.CC_STAT_AREA] < min_area:
            continue
        x0, y0 = stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_TOP]
        cw, ch = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]
        if x0 == 0 or y0 == 0 or x0 + cw >= w or y0 + ch >= h:
            wall_mask[labels == i] = 1

    wall_px = int(wall_mask.sum())
    if wall_px < min_area:
        return cell, False

    darken_map = cv2.GaussianBlur(
        wall_mask.astype(np.float32), (0, 0), sigmaX=12,
    ).astype(np.float64)
    darken_map = np.clip(darken_map, 0, 1)

    result = cell.astype(np.float64).copy()
    for c in range(3):
        result[:, :, c] = (
            cell[:, :, c].astype(np.float64) * (1 - darken_map)
            + float(bg_color[c]) * darken_map
        )

    return np.clip(result, 0, 255).astype(np.uint8), True


def replace_studio_background(cell: np.ndarray) -> tuple[np.ndarray, dict]:
    """rembg 人像分割 → 影棚背景替换为亮黑色。

    改进：
    - 衣服扩展增加向心约束，防止白墙/门框被当衣服保护
    - 头部区域发丝颜色相似度 alpha 恢复
    - guided filter 头部区域参数放大
    """
    h, w = cell.shape[:2]
    record: dict = {"enabled": False, "method": "rembg_studio_replace"}

    bg_color = np.array([25, 25, 25], dtype=np.float64)

    gray = cv2.cvtColor(cell, cv2.COLOR_BGR2GRAY).astype(np.float64)
    dark_ratio = float((gray < 80).sum()) / gray.size
    if dark_ratio < 0.05:
        record["reason"] = f"dark_ratio={dark_ratio:.3f} < 0.05, skip"
        return cell, record

    margin = max(8, min(h, w) // 8)
    corners = np.concatenate([
        gray[:margin, :margin].ravel(),
        gray[:margin, -margin:].ravel(),
        gray[-margin:, :margin].ravel(),
        gray[-margin:, -margin:].ravel(),
    ])
    bg_brightness = float(np.median(corners))
    dark_bg = bg_brightness < 70

    try:
        from rembg import remove
    except ImportError:
        record["reason"] = "rembg not available"
        return cell, record

    # 预处理：白墙压暗，消除 rembg alpha 过渡带的白色泄漏
    cell_predarkened, bg_was_darkened = _predarken_bright_background(cell, bg_color)

    cell_rgb = cv2.cvtColor(cell_predarkened, cv2.COLOR_BGR2RGB)
    from PIL import Image as PILImage
    pil_img = PILImage.fromarray(cell_rgb)
    session = _get_rembg_session()
    mask_pil = remove(pil_img, session=session, only_mask=True, post_process_mask=True)
    raw_alpha = np.asarray(mask_pil.convert("L"), dtype=np.float64) / 255.0

    binary = (raw_alpha > 0.4).astype(np.uint8)
    num, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if num > 2:
        areas = stats[1:, cv2.CC_STAT_AREA]
        min_area = max(1, int(areas.max() * 0.01))
        keep = np.zeros_like(binary)
        for i in range(1, num):
            if stats[i, cv2.CC_STAT_AREA] >= min_area:
                keep[labels == i] = 1
        raw_alpha = raw_alpha * keep

    fg_ratio = float((raw_alpha > 0.5).sum()) / raw_alpha.size
    if fg_ratio < 0.15 or fg_ratio > 0.92:
        record["reason"] = f"fg_ratio={fg_ratio:.3f} out of safe range"
        return cell, record

    fg_binary = (raw_alpha > 0.3).astype(np.uint8)
    brightness = cell_predarkened.astype(np.float64).mean(axis=2)

    fg_ys, fg_xs = np.where(fg_binary > 0)
    fg_cy = float(fg_ys.mean()) if len(fg_ys) > 0 else h / 2.0
    fg_cx = float(fg_xs.mean()) if len(fg_xs) > 0 else w / 2.0

    bright_mask = (brightness > 130).astype(np.uint8)
    hsv = cv2.cvtColor(cell_predarkened, cv2.COLOR_BGR2HSV)
    low_saturation = (hsv[:, :, 1].astype(np.float64) < 40).astype(np.uint8)
    wall_suspect = bright_mask & low_saturation

    dist_from_fg = cv2.distanceTransform(1 - fg_binary, cv2.DIST_L2, 5)

    edge_margin = 48
    edge_interior = np.zeros((h, w), dtype=np.uint8)
    edge_interior[edge_margin:h - edge_margin, edge_margin:w - edge_margin] = 1
    dist_from_cell_edge = cv2.distanceTransform(edge_interior, cv2.DIST_L2, 5)

    yy, xx = np.indices((h, w), dtype=np.float64)
    dist_to_centroid = np.sqrt((yy - fg_cy) ** 2 + (xx - fg_cx) ** 2)

    max_extend = 10
    extended = fg_binary.copy()
    kernel = np.ones((3, 3), np.uint8)
    for step in range(max_extend):
        dilated = cv2.dilate(extended, kernel, iterations=1)
        candidates = (dilated > 0) & (extended == 0) & (bright_mask > 0)
        candidates &= (dist_from_cell_edge > 40)
        candidates &= (wall_suspect == 0)
        candidates &= (dist_to_centroid < dist_to_centroid[fg_binary > 0].max() * 0.95)
        if not np.any(candidates):
            break
        extended[candidates] = 1

    clothing_recovered = int((extended - fg_binary).sum())

    extend_zone = (extended > 0) & (fg_binary == 0)
    extend_alpha = np.zeros((h, w), dtype=np.float64)
    if np.any(extend_zone):
        dist_vals = dist_from_fg[extend_zone]
        alpha_vals = np.clip(1.0 - (dist_vals - 1) / 8.0, 0.0, 0.75)
        extend_alpha[extend_zone] = alpha_vals

    hair_alpha = _recover_hair_alpha(cell, fg_binary, raw_alpha)

    guide = cv2.cvtColor(cell_predarkened, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    try:
        alpha_body = cv2.ximgproc.guidedFilter(guide, raw_alpha.astype(np.float32),
                                                radius=6, eps=1e-4)
        alpha_head = cv2.ximgproc.guidedFilter(guide, raw_alpha.astype(np.float32),
                                                radius=16, eps=5e-4)
        alpha_refined = np.zeros((h, w), dtype=np.float64)
        fg_top_ys = np.where(fg_binary.any(axis=1))[0]
        head_bottom = int(fg_top_ys[0] + (fg_top_ys[-1] - fg_top_ys[0]) * 0.35) if len(fg_top_ys) > 1 else h // 3
        head_blend = np.zeros((h, w), dtype=np.float64)
        head_blend[:head_bottom, :] = 1.0
        blend_zone = min(h // 10, 50)
        for row in range(head_bottom, min(head_bottom + blend_zone, h)):
            head_blend[row, :] = 1.0 - (row - head_bottom) / blend_zone
        alpha_refined = (alpha_head * head_blend + alpha_body * (1 - head_blend)).astype(np.float64)
        alpha_refined = np.clip(alpha_refined, 0, 1)
    except Exception:
        alpha_refined = raw_alpha

    final_alpha = np.maximum(alpha_refined, extend_alpha)
    final_alpha = np.maximum(final_alpha, hair_alpha)

    dilate_r = 3
    possible_fg = cv2.dilate((final_alpha > 0.05).astype(np.uint8),
                             np.ones((dilate_r, dilate_r), np.uint8)) > 0
    final_alpha[~possible_fg] = 0

    final_alpha[final_alpha > 0.5] = 1.0

    fg_solid = (final_alpha == 1.0)[..., np.newaxis]
    cell_hybrid = np.where(fg_solid, cell, cell_predarkened).astype(np.uint8)

    cell_clean = _decontaminate_edge_color(cell_hybrid, final_alpha, fg_binary)

    bg_u8 = np.clip(bg_color, 0, 255).astype(np.uint8)
    canvas = np.full_like(cell_clean, bg_u8)
    alpha_3ch = final_alpha[..., np.newaxis]
    result = np.clip(
        cell_clean.astype(np.float64) * alpha_3ch + canvas.astype(np.float64) * (1 - alpha_3ch),
        0, 255
    ).astype(np.uint8)

    result[final_alpha < 0.15] = bg_u8

    halo_ceiling = 0.25 if dark_bg else 0.45
    halo_push = np.clip((halo_ceiling - final_alpha) / max(halo_ceiling - 0.10, 0.01), 0, 1)
    for c in range(3):
        result[:, :, c] = np.clip(
            result[:, :, c].astype(np.float64) * (1 - halo_push) + bg_color[c] * halo_push,
            0, 255,
        ).astype(np.uint8)

    record.update({
        "enabled": True,
        "dark_ratio": round(dark_ratio, 3),
        "fg_ratio": round(fg_ratio, 3),
        "clothing_recovered_px": clothing_recovered,
        "hair_recovered": bool(hair_alpha.max() > 0.1),
        "bg_color_bgr": [int(v) for v in bg_color.tolist()],
        "bg_predarkened": bg_was_darkened,
    })
    return result, record


def fill_dark_cell_edges(cell: np.ndarray) -> tuple[np.ndarray, dict]:
    """replace_studio_background 的入口，rembg 不可用时回退到像素级边缘填充。"""
    return replace_studio_background(cell)


def compute_pair_eye_signal(
    before_face: dict,
    after_face: dict,
    before_scale: float,
    after_scale: float,
) -> dict:
    """G2 配对眼距信号：终格眼距比 = 源图眼距 × 终格 scale 之比（解析零成本）。

    供 board_pair_gate 灾难级兜底核对（front 槽阈值 [0.78, 1.30]，
    2026-06-11 19+3 板标定）。失败 fail-open 返回 valid=False，永不抛。
    """
    try:
        b = float(before_face.get("eye_distance") or 0)
        a = float(after_face.get("eye_distance") or 0)
        if b <= 0 or a <= 0:
            return {"valid": False, "reason": "eye_distance_unavailable"}
        return {
            "valid": True,
            "eye_ratio": round((b * before_scale) / max(a * after_scale, 1e-6), 4),
            "before_final_eye": round(b * before_scale, 1),
            "after_final_eye": round(a * after_scale, 1),
        }
    except Exception as exc:  # noqa: BLE001 — 信号永不让渲染崩溃
        return {"valid": False, "reason": f"signal_error: {exc}"}


def render_protected_pair(
    before_path: str,
    after_path: str,
    before_face: dict,
    after_face: dict,
    size: tuple[int, int],
    slot: str,
    protection_targets: list[str],
    render_plan_records: list[dict] | None = None,
    smart_crop_ratio: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    before_raw = cv2.imread(before_path)
    after_raw = cv2.imread(after_path)
    if before_raw is None or after_raw is None:
        raise FileNotFoundError("保护区渲染无法读取术前或术后图片")
    before_image = CASE_LAYOUT.FACE_ALIGN.auto_orient(before_raw, before_path)
    after_image = CASE_LAYOUT.FACE_ALIGN.auto_orient(after_raw, after_path)

    before_box = protection_box_from_face(before_face, slot, protection_targets)
    after_box = protection_box_from_face(after_face, slot, protection_targets)

    target_w, target_h = size
    target_ratio = 0.72 if slot == "front" else (0.74 if slot == "side" else 0.70)
    target_protection_h = target_h * target_ratio

    before_protection_h = max(1.0, before_box[3] - before_box[1])
    after_protection_h = max(1.0, after_box[3] - after_box[1])
    face_ratio = after_protection_h / before_protection_h

    before_contain = min(
        target_w / max(before_image.shape[1], 1),
        target_h / max(before_image.shape[0], 1),
    )
    # 对称 clamp：before 天花板与 after 同为 contain*2.50，允许放大术前裁切匹配术后
    # 近景（旧 1.10 天花板把 before 压死在 contain 附近，板面术后脸恒大 ~2x，见
    # handoff-enhance-pipeline-v5 探针实数）。owner 先例：放大裁切而非缩小。
    before_raw_scale = target_protection_h / before_protection_h
    before_scale = float(max(before_contain * 0.92, min(before_raw_scale, before_contain * 2.50)))

    after_contain = min(
        target_w / max(after_image.shape[1], 1),
        target_h / max(after_image.shape[0], 1),
    )

    if smart_crop_ratio:
        _BASELINE_RATIO = 0.31
        if smart_crop_ratio > _BASELINE_RATIO:
            before_eye_d = float(before_face.get("eye_distance", 0))
            if before_eye_d:
                target_ted = target_w * smart_crop_ratio
                smart_before = target_ted / max(before_eye_d, 1.0)
                if smart_before > before_scale:
                    before_scale = float(min(smart_before, before_contain * 2.50))

    # 面部匹配硬约束（板面 face_ratio→1.0±0.08）：smart 只推 before 定缩放档位，
    # after 永远按保护框高度比反推跟随。旧逻辑 smart 按各自 eye_d 独立推两边，
    # 斜/侧位前后转头角度不同时 eye_d 比 ≠ 保护框高度比 + 单边触 2.50 顶，
    # 配对断裂（oblique 板面比 0.87）。after 触 clamp 时回拉 before 保持等大。
    after_target_scale = before_scale * (before_protection_h / after_protection_h)
    after_scale = float(max(after_contain * 0.92, min(after_target_scale, after_contain * 2.50)))
    if abs(after_scale - after_target_scale) > 1e-9:
        before_scale = float(
            max(
                before_contain * 0.92,
                min(after_scale * (after_protection_h / before_protection_h), before_contain * 2.50),
            )
        )

    before_transform = compute_protected_transform(before_image.shape[:2], before_box, size, slot, before_scale)
    after_transform = compute_protected_transform(after_image.shape[:2], after_box, size, slot, after_scale)
    before_transform["slot"] = slot
    after_transform["slot"] = slot

    before_arr = render_protected_cell(before_image, before_transform, size)
    after_arr = render_protected_cell(after_image, after_transform, size)

    after_arr, after_trim = trim_before_excess_to_after_reference(
        after_arr, before_arr, after_transform, before_transform, slot
    )

    before_arr, position_alignment = align_before_position_to_after_cell(before_arr, after_arr, slot)
    composition_diagnostic = composition_diagnostic_from_cells(before_arr, after_arr, slot)

    if render_plan_records is not None:
        render_plan_records.append({
            "slot": slot,
            "strategy": "independent_scale_zoom_after",
            "targets": protection_targets,
            "before": Path(before_path).name,
            "after": Path(after_path).name,
            "pair_eye_signal": compute_pair_eye_signal(
                before_face, after_face, before_scale, after_scale
            ),
            "before_scale": round(before_scale, 4),
            "after_scale": round(after_scale, 4),
            "face_ratio": round(face_ratio, 4),
            "smart_crop_ratio": smart_crop_ratio,
            "before_transform": before_transform,
            "after_transform": after_transform,
            "after_trim": after_trim,
            "preop_position_alignment": position_alignment,
            "composition_diagnostic": composition_diagnostic,
        })
    return before_arr, after_arr


def render_prepared_cell(
    image_path: str,
    face: dict,
    size: tuple[int, int],
    target_eye_distance: float,
    target_eye_center: np.ndarray,
    slot: str,
    phase: str,
    forced_effective_scale: float | None = None,
) -> np.ndarray:
    cell_arr, valid_mask = CASE_LAYOUT.render_detected_face_cell_with_mask(
        face,
        size,
        target_eye_distance,
        target_eye_center,
        forced_effective_scale=forced_effective_scale,
    )
    cell_arr, valid_mask, _bottom_shift = shift_cell_bottom_edge_to_frame(cell_arr, valid_mask)
    return CASE_LAYOUT.prepare_face_cell_for_board(
        image_path,
        cell_arr,
        slot,
        phase,
        None,
        valid_mask,
    )


def compute_pair_alignment_scales(
    before_face: dict,
    after_face: dict,
    size: tuple[int, int],
    target_eye_distance: float,
) -> tuple[float, float]:
    # 术后源图通常拍得更近（面部占画幅更大）：独立对齐时术前常被 face-height cap
    # 压小、术后却放满 target eye distance，导致板面术后更紧凑（构图不匹配根因，
    # 见 handoff-enhance-pipeline-v4）。这里以术前最终眼距为锚反推术后 scale，
    # 锁定两侧面部大小一致；同时保留术后自身 face-height cap 防止面部被裁切。
    before_scale = CASE_LAYOUT.estimate_alignment_effective_scale(
        before_face, size, target_eye_distance
    )
    before_final_eye = max(float(before_face["eye_distance"]), 1e-6) * before_scale
    after_eye = max(float(after_face["eye_distance"]), 1e-6)
    match_scale = before_final_eye / after_eye
    after_height_cap = (
        size[1] * CASE_LAYOUT.ALIGN_TARGET_FACE_HEIGHT_RATIO
        / max(float(after_face["face_height"]), 1e-6)
        * CASE_LAYOUT.ALIGN_FACE_HEIGHT_SCALE_HEADROOM
    )
    return before_scale, float(min(match_scale, after_height_cap))


def render_side_profile_contain_cell(image_path: str, size: tuple[int, int]) -> np.ndarray:
    image = cv2.imread(image_path)
    if image is None:
        raise FileNotFoundError(f"无法读取图片: {image_path}")
    image = CASE_LAYOUT.FACE_ALIGN.auto_orient(image, image_path)
    target_w, target_h = size
    src_h, src_w = image.shape[:2]
    scale = min(target_w / max(src_w, 1), target_h / max(src_h, 1))
    resized_w = max(1, int(round(src_w * scale)))
    resized_h = max(1, int(round(src_h * scale)))
    resized = cv2.resize(image, (resized_w, resized_h), interpolation=cv2.INTER_AREA)
    background = CASE_LAYOUT.FACE_ALIGN.estimate_background_color(image)
    canvas = np.full((target_h, target_w, 3), background, dtype=np.uint8)
    x = (target_w - resized_w) // 2
    y = (target_h - resized_h) // 2
    canvas[y:y + resized_h, x:x + resized_w] = resized
    return canvas


def render_aligned_pair(
    before_path: str,
    after_paths: list[str],
    size: tuple[int, int],
    slot: str,
    allow_direction_mismatch: bool = False,
    protection_targets: list[str] | None = None,
    render_plan_records: list[dict] | None = None,
    focus_targets: list[dict] | None = None,
    slot_index: int = 0,
    angle_count: int = 3,
) -> tuple[Image.Image, Image.Image]:
    protection_targets = protection_targets or []
    smart_debug = None
    if SMART_CROP is not None and focus_targets:
        ted, ecx, ecy, smart_debug = SMART_CROP.compute_smart_alignment(
            size, focus_targets, slot_index, angle_count,
        )
        target_eye_distance = ted
        target_eye_center = np.array([ecx, ecy], dtype=np.float64)
    else:
        target_eye_distance, target_eye_center = CASE_LAYOUT.build_alignment_target(size)
    before_face = CASE_LAYOUT.detect_face_for_alignment(before_path)
    before_direction = None if slot == "front" else view_direction_from_face(before_face)

    after_errors = []
    after_arr = None
    before_arr = None
    used_after_path = None
    used_after_direction = None
    seen = set()
    for path in after_paths:
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            current_face = CASE_LAYOUT.detect_face_for_alignment(path)
            current_direction = None if slot == "front" else view_direction_from_face(current_face)
            if (
                slot != "front"
                and not allow_direction_mismatch
                and before_direction
                and current_direction
                and before_direction != current_direction
            ):
                after_errors.append(
                    f"{Path(path).name}: 方向不一致(before={before_direction}, after={current_direction})"
                )
                continue
            if should_use_protected_alignment(slot, protection_targets):
                smart_ratio = smart_debug.get("eye_distance_ratio") if smart_debug else None
                try:
                    before_arr, candidate = render_protected_pair(
                        before_path,
                        path,
                        before_face,
                        current_face,
                        size,
                        slot,
                        protection_targets,
                        render_plan_records,
                        smart_crop_ratio=smart_ratio,
                    )
                except Exception as exc:
                    after_errors.append(f"{Path(path).name}: 保护区对齐失败，回退常规对齐: {exc}")
                    candidate = render_prepared_cell(
                        path,
                        current_face,
                        size,
                        target_eye_distance,
                        target_eye_center,
                        slot,
                        "after",
                    )
                    before_arr = render_prepared_cell(
                        before_path,
                        before_face,
                        size,
                        target_eye_distance,
                        target_eye_center,
                        slot,
                        "before",
                    )
                    if render_plan_records is not None:
                        render_plan_records.append({
                            "slot": slot,
                            "strategy": "face_align_fallback_after_protected_error",
                            "targets": protection_targets,
                            "before": Path(before_path).name,
                            "after": Path(path).name,
                            "error": str(exc),
                        })
            elif slot == "side" and (is_profile_fallback_face(before_face) or is_profile_fallback_face(current_face)):
                before_arr = render_side_profile_contain_cell(before_path, size)
                candidate = render_side_profile_contain_cell(path, size)
                if render_plan_records is not None:
                    render_plan_records.append({
                        "slot": slot,
                        "strategy": "side_profile_contain",
                        "targets": protection_targets,
                        "before": Path(before_path).name,
                        "after": Path(path).name,
                    })
            else:
                before_forced_scale, after_forced_scale = compute_pair_alignment_scales(
                    before_face, current_face, size, target_eye_distance,
                )
                candidate = render_prepared_cell(
                    path,
                    current_face,
                    size,
                    target_eye_distance,
                    target_eye_center,
                    slot,
                    "after",
                    forced_effective_scale=after_forced_scale,
                )
                before_arr = render_prepared_cell(
                    before_path,
                    before_face,
                    size,
                    target_eye_distance,
                    target_eye_center,
                    slot,
                    "before",
                    forced_effective_scale=before_forced_scale,
                )
                if render_plan_records is not None:
                    record = {
                        "slot": slot,
                        "strategy": "face_landmark_align",
                        "targets": protection_targets,
                        "before": Path(before_path).name,
                        "after": Path(path).name,
                        "before_scale": round(before_forced_scale, 4),
                        "after_scale": round(after_forced_scale, 4),
                        "scale_lock": "after_matched_to_before_final_eye_distance",
                    }
                    if smart_debug:
                        record["smart_crop"] = smart_debug
                    render_plan_records.append(record)
            after_arr = candidate
            used_after_path = path
            used_after_direction = current_direction
            break
        except Exception as exc:
            after_errors.append(f"{Path(path).name}: {exc}")

    if after_arr is None or before_arr is None or used_after_path is None:
        joined = "; ".join(after_errors) if after_errors else "无可用术后图"
        raise RuntimeError(f"术后严格对齐失败: {joined}")

    if (
        slot != "front"
        and not allow_direction_mismatch
        and before_direction
        and used_after_direction
        and before_direction != used_after_direction
    ):
        raise RuntimeError(
            f"术前术后方向不一致(slot={slot}, before={before_direction}, after={used_after_direction})"
        )

    before_arr, _ = fill_dark_cell_edges(before_arr)
    after_arr, _ = fill_dark_cell_edges(after_arr)

    before_arr, after_arr = CASE_LAYOUT.FACE_ALIGN.harmonize_pair(before_arr, after_arr)
    after_arr = CASE_LAYOUT.FACE_ALIGN.lift_face_shadows(after_arr, slot=slot)
    return whiten_background(CASE_LAYOUT.cv_to_pil(before_arr)), whiten_background(CASE_LAYOUT.cv_to_pil(after_arr))


def whiten_background(img: Image.Image) -> Image.Image:
    return img


def prepare_closeup_group(manifest: dict, size: tuple[int, int]) -> dict | None:
    """G3 纹类近景对比区：manifest["closeup_section"] → 板尾 prepared group。

    section 由 backend board_closeup_section 构建（front 源图 clarity 近景裁剪）。
    cell 用 ImageOps.fit cover 到与角度行同尺寸（近景无需人脸眼距对齐）。
    任何缺失/损坏 fail-open 返回 None，板照常出——近景是增益不是门槛。
    """
    section = manifest.get("closeup_section")
    if not isinstance(section, dict):
        return None
    try:
        cells = {}
        for side in ("before", "after"):
            p = Path(str(section[f"{side}_path"]))
            with Image.open(p) as img:
                cells[side] = ImageOps.fit(img.convert("RGB"), size, method=Image.LANCZOS)
        label = str(section.get("label") or "").strip()
        return {
            "name": "closeup_section",
            "slots": [{
                "slot": "closeup",
                "title": f"{label}近景对比" if label else "近景对比",
                "before": cells["before"],
                "after": cells["after"],
            }],
        }
    except Exception as exc:  # noqa: BLE001 — fail-open，近景缺失不挡板
        print(f"  ⚠️ closeup_section 渲染准备失败（跳过近景区）: {exc}")
        return None


def render_from_manifest(manifest: dict, out_path: Path, *, after_transform=None, slot_transform=None, scale: float = 1.0) -> Path:
    # body/颈纹 案例走专用渲染器（不依赖人脸眼距对齐）
    if manifest.get("case_mode") == "body":
        return CASE_LAYOUT.render_body_board(manifest, out_path)
    meta = resolve_meta(manifest)
    brand = manifest["brand"]
    groups = manifest["groups"]
    if not groups:
        raise ValueError("manifest 中没有 groups")
    protection_targets = collect_protection_targets(manifest, meta)
    render_plan_records: list[dict] = []
    manifest["render_plan"] = {
        "version": 1,
        "renderer": "render_brand_clean",
        "alignment_policy": "protected_region_first_when_targeted",
        "protection_targets": protection_targets,
        "slots": render_plan_records,
    }

    bg = (244, 238, 231)
    panel = (253, 250, 246)
    ink = (56, 49, 43)
    accent = (116, 99, 84)
    soft_green = (226, 235, 216)
    green = (132, 154, 98)
    outline_color = (227, 218, 209)
    date_fill = (236, 227, 216)

    s = lambda v: int(v * scale)

    board_w = s(1920)
    pad = s(58)
    inner_gap = s(34)
    section_gap = s(41)
    section_title_h = s(67)
    footer_h = s(106)
    header_h = s(216)
    image_w, image_h = s(516), s(624)

    name_font = CASE_LAYOUT.load_font(s(60), bold=True)
    date_font = CASE_LAYOUT.load_font(s(26), bold=True)
    project_font = CASE_LAYOUT.load_font(s(31), bold=False)
    section_font = CASE_LAYOUT.load_font(s(36), bold=True)
    label_font = CASE_LAYOUT.load_font(s(26), bold=True)

    manifest_focus = manifest.get("focus_targets") or []
    prepared_groups = []
    for group in groups:
        render_slots = group.get("render_slots") or list(CASE_LAYOUT.ANGLE_SLOTS)
        active_slot_count = sum(
            1 for sl in render_slots if group["selected_slots"].get(sl)
        )
        slots = []
        slot_idx = 0
        for slot in render_slots:
            selection = group["selected_slots"].get(slot)
            if not selection:
                continue
            before_path = selection["before"]["path"]
            enhancement = selection["after"].get("enhancement") or {}
            is_native_crop = enhancement.get("input_mode") == "aligned-after"
            after_candidates = [
                None if is_native_crop else enhancement.get("enhanced_path"),
                selection["after"]["path"],
            ]
            allow_direction_mismatch = (
                selection["before"].get("phase_source") == "manual"
                and selection["after"].get("phase_source") == "manual"
            ) or (
                selection["before"].get("angle_source") == "manual"
                and selection["after"].get("angle_source") == "manual"
            )
            before_img, after_img = render_aligned_pair(
                before_path,
                after_candidates,
                (image_w, image_h),
                slot,
                allow_direction_mismatch=allow_direction_mismatch,
                protection_targets=protection_targets,
                render_plan_records=render_plan_records,
                focus_targets=manifest_focus,
                slot_index=slot_idx,
                angle_count=active_slot_count,
            )
            slot_idx += 1
            manual_transform = selection["before"].get("manual_transform")
            before_img, manual_transform_record = apply_manual_preop_transform(before_img, manual_transform)
            if manual_transform_record.get("enabled"):
                attach_manual_preop_transform_record(
                    render_plan_records,
                    slot,
                    before_path,
                    selection["after"]["path"],
                    manual_transform_record,
                )
            if slot_transform is not None:
                before_img, after_img = slot_transform(before_img, after_img, slot)
            elif after_transform is not None:
                after_img = after_transform(after_img, slot, before_img=before_img)
            slots.append({
                "slot": slot,
                "title": f"{CASE_LAYOUT.ANGLE_LABELS[slot]}对比",
                "before": before_img,
                "after": after_img,
            })
        if slots:
            prepared_groups.append({"name": group["name"], "slots": slots})

    # G3 纹类近景对比区：manifest["closeup_section"] 存在则板尾追加一行。
    # cell 与角度行同尺寸 → 高度公式零改动；fail-open 失败不挡板。
    closeup_group = prepare_closeup_group(manifest, (image_w, image_h))
    if closeup_group:
        prepared_groups.append(closeup_group)

    if not prepared_groups:
        raise ValueError("没有可渲染的角度槽位")

    total_sections = sum(len(group["slots"]) for group in prepared_groups)
    section_body_h = image_h + s(78)
    board_h = header_h + total_sections * (section_title_h + section_body_h) + section_gap * (total_sections - 1) + footer_h + pad * 2
    canvas = Image.new("RGB", (board_w, board_h), bg)
    draw = ImageDraw.Draw(canvas)

    card_x1, card_y1 = pad, s(26)
    card_x2, card_y2 = board_w - pad, header_h
    draw.rounded_rectangle((card_x1, card_y1, card_x2, card_y2), radius=s(32), fill=panel, outline=outline_color, width=max(1, s(2)))
    for x in range(card_x1 + s(28), card_x2 - s(20), s(20)):
        draw.rounded_rectangle((x, card_y1 + s(16), x + s(10), card_y1 + s(22)), radius=s(3), fill=(226, 216, 206))

    pill = (card_x1 + s(28), card_y1 + s(42), card_x1 + s(190), card_y1 + s(82))
    draw.rounded_rectangle(pill, radius=s(18), fill=date_fill)
    db = CASE_LAYOUT.textbbox_with_fallback(draw, (0, 0), meta["date"], date_font, fill=accent, bold=True)
    CASE_LAYOUT.draw_text_with_fallback(
        draw,
        (
            pill[0] + ((pill[2] - pill[0]) - (db[2] - db[0])) / 2,
            pill[1] + ((pill[3] - pill[1]) - (db[3] - db[1])) / 2 - 1,
        ),
        meta["date"],
        date_font,
        accent,
        bold=True,
    )

    name_bbox = CASE_LAYOUT.textbbox_with_fallback(draw, (0, 0), meta["customer_name"], name_font, fill=ink, bold=True)
    name_w = name_bbox[2] - name_bbox[0]
    name_y = card_y1 + s(104)
    name_x = card_x1 + s(28)
    CASE_LAYOUT.draw_text_with_fallback(draw, (name_x, name_y), meta["customer_name"], name_font, ink, bold=True)

    project_x = name_x + name_w + s(24)
    project_y = name_y + s(13)
    max_project_w = card_x2 - s(30) - project_x
    while True:
        pb = CASE_LAYOUT.textbbox_with_fallback(draw, (0, 0), meta["project"], project_font, fill=ink)
        if (pb[2] - pb[0]) <= max_project_w or project_font.size <= s(20):
            break
        project_font = CASE_LAYOUT.load_font(project_font.size - 1, bold=False)
    if meta["project"]:
        CASE_LAYOUT.draw_text_with_fallback(draw, (project_x, project_y), meta["project"], project_font, ink)

    y = header_h + s(8)
    rendered_count = 0
    for group in prepared_groups:
        for slot in group["slots"]:
            draw.rounded_rectangle((pad, y, board_w - pad, y + section_title_h), radius=s(18), fill=accent)
            bbox = CASE_LAYOUT.textbbox_with_fallback(draw, (0, 0), slot["title"], section_font, fill=(255, 255, 255), bold=True)
            tx = pad + ((board_w - pad * 2) - (bbox[2] - bbox[0])) / 2
            ty = y + (section_title_h - (bbox[3] - bbox[1])) / 2 - 2
            CASE_LAYOUT.draw_text_with_fallback(draw, (tx, ty), slot["title"], section_font, (255, 255, 255), bold=True)
            y += section_title_h + s(14)

            box_w = slot["before"].width + slot["after"].width + inner_gap + s(76)
            box_h = max(slot["before"].height, slot["after"].height) + s(72)
            box_x = (board_w - box_w) // 2
            draw.rounded_rectangle((box_x, y, box_x + box_w, y + box_h), radius=s(26), fill=panel, outline=outline_color, width=max(1, s(2)))

            left_x = box_x + s(24)
            right_x = left_x + slot["before"].width + inner_gap
            label_y = y + s(16)
            draw.rounded_rectangle((left_x, label_y, left_x + slot["before"].width, label_y + s(34)), radius=s(12), fill=(245, 240, 234))
            draw.rounded_rectangle((right_x, label_y, right_x + slot["after"].width, label_y + s(34)), radius=s(12), fill=soft_green)
            for x0, w, text_label, label_fill in [
                (left_x, slot["before"].width, "术前", ink),
                (right_x, slot["after"].width, "术后", green),
            ]:
                bb = CASE_LAYOUT.textbbox_with_fallback(draw, (0, 0), text_label, label_font, fill=label_fill, bold=True)
                tx = x0 + (w - (bb[2] - bb[0])) / 2
                ty = label_y + (s(34) - (bb[3] - bb[1])) / 2 - 1
                CASE_LAYOUT.draw_text_with_fallback(draw, (tx, ty), text_label, label_font, label_fill, bold=True)

            img_y = y + s(54)
            canvas.paste(slot["before"], (left_x, img_y))
            canvas.paste(slot["after"], (right_x, img_y))
            y += box_h
            rendered_count += 1
            if rendered_count != total_sections:
                y += section_gap

    if Path(brand["logo_path"]).exists():
        logo = Image.open(brand["logo_path"]).convert("RGBA")
        logo = ImageOps.contain(logo, (s(260), s(88)))
        logo_x = (board_w - logo.width) // 2
        logo_y = board_h - footer_h + (footer_h - logo.height) // 2 - s(6)
        canvas.paste(logo.convert("RGB"), (logo_x, logo_y), logo)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "JPEG", quality=95)
    return out_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="渲染正式品牌版案例图")
    parser.add_argument("manifest_path", help="inspect manifest.json 路径")
    parser.add_argument("--out", required=True, help="输出图片路径")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = json.loads(Path(args.manifest_path).read_text(encoding="utf-8"))
    out_path = Path(args.out).resolve()
    render_from_manifest(manifest, out_path)
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
