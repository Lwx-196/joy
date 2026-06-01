"""Deterministic pixel telemetry for rendered delivery boards.

The metrics here are signal-only: they may lower a render quality score and
surface flags, but they never publish, unhold, or bypass the D6 delivery gate.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageStat

try:  # numpy is an optional accelerator in local envs, not a runtime contract.
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover - exercised by monkeypatch/ad hoc smoke
    np = None  # type: ignore[assignment]


DOWNSCALE_LONG_EDGE = 1024
LETTERBOX_STD_THRESH = 8.0
LETTERBOX_MEAN_DELTA = 6.0
LETTERBOX_FLAG_RATIO = 0.12
CELL_UNDERFILL_RATIO = 0.55
BG_DELTA_FLAG = 25.0
MASK_WHITENESS_FLAG = 0.30
BLANK_AREA_FLAG = 0.35
CV_PENALTY_CAP = 30.0

_TILE_SIZE = 8
_EDGE_MAX_SIDE = 256


def compute_pixel_metrics(board_path: str | None) -> dict[str, Any]:
    """Compute low-cost CV telemetry for a rendered board image.

    Failure is fail-open on the signal side: unavailable metrics contribute no
    penalty and no flags. D6 remains the delivery recall gate.
    """
    if not board_path:
        return _unavailable("missing_path")
    path = Path(str(board_path))
    if not path.is_file():
        return _unavailable("missing_file")
    try:
        with Image.open(path) as opened:
            image = opened.convert("RGB")
        if max(image.size) > DOWNSCALE_LONG_EDGE:
            image.thumbnail((DOWNSCALE_LONG_EDGE, DOWNSCALE_LONG_EDGE), Image.Resampling.BILINEAR)

        board_w, board_h = image.size
        edge_bands = _scan_uniform_edge_bands(image.convert("L"))
        letterbox_ratio = _edge_area_ratio(board_w, board_h, edge_bands)
        cell_boxes, inferred = _cell_boxes(board_w, board_h)

        cell_fill_ratios = [_cell_fill_ratio(image.crop(box)) for box in cell_boxes]
        min_cell_fill_ratio = min(cell_fill_ratios) if cell_fill_ratios else 1.0
        max_bg_color_delta = _max_bg_color_delta(image, board_w, board_h)
        max_mask_edge_whiteness = max((_mask_edge_whiteness(image.crop(box)) for box in cell_boxes), default=0.0)
        blank_area_ratio = _blank_area_ratio(image, edge_bands)

        flags: list[str] = []
        penalty = 0.0
        if letterbox_ratio > LETTERBOX_FLAG_RATIO:
            flags.append("bg_letterbox")
            penalty += min((letterbox_ratio - LETTERBOX_FLAG_RATIO) * 100.0, 18.0)
        if min_cell_fill_ratio < CELL_UNDERFILL_RATIO:
            flags.append("cell_underfill")
            penalty += min((CELL_UNDERFILL_RATIO - min_cell_fill_ratio) * 60.0, 18.0)
        if max_bg_color_delta > BG_DELTA_FLAG:
            flags.append("bg_mismatch")
            penalty += min((max_bg_color_delta - BG_DELTA_FLAG) * 0.5, 15.0)
        if max_mask_edge_whiteness > MASK_WHITENESS_FLAG:
            flags.append("cutout_artifact")
            penalty += min((max_mask_edge_whiteness - MASK_WHITENESS_FLAG) * 40.0, 15.0)
        if blank_area_ratio > BLANK_AREA_FLAG:
            flags.append("blank_region")
            penalty += min((blank_area_ratio - BLANK_AREA_FLAG) * 40.0, 12.0)

        return {
            "available": True,
            "board_w": int(board_w),
            "board_h": int(board_h),
            "letterbox_ratio": round(float(letterbox_ratio), 4),
            "min_cell_fill_ratio": round(float(min_cell_fill_ratio), 4),
            "max_bg_color_delta": round(float(max_bg_color_delta), 2),
            "max_mask_edge_whiteness": round(float(max_mask_edge_whiteness), 4),
            "blank_area_ratio": round(float(blank_area_ratio), 4),
            "flags": flags,
            "cv_penalty": round(min(max(penalty, 0.0), CV_PENALTY_CAP), 1),
            "cell_grid": {"rows": len(cell_boxes) // 2 if cell_boxes else 1, "cols": 2, "inferred": inferred},
            "numpy_available": np is not None,
        }
    except (OSError, ValueError) as exc:
        return _unavailable(type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - telemetry must not crash render quality
        return _unavailable(type(exc).__name__)


def _unavailable(error: str) -> dict[str, Any]:
    return {
        "available": False,
        "error": error,
        "flags": [],
        "cv_penalty": 0.0,
    }


def _cell_boxes(width: int, height: int) -> tuple[list[tuple[int, int, int, int]], bool]:
    rows = _infer_row_count(width, height)
    row_edges = [round(height * idx / rows) for idx in range(rows + 1)]
    boxes: list[tuple[int, int, int, int]] = []
    mid = width // 2
    for idx in range(rows):
        top = row_edges[idx]
        bottom = max(top + 1, row_edges[idx + 1])
        boxes.append((0, top, mid, bottom))
        boxes.append((mid, top, width, bottom))
    return boxes, True


def _infer_row_count(width: int, height: int) -> int:
    if width <= 0:
        return 1
    ratio = height / float(width)
    if ratio < 0.95:
        return 1
    if ratio < 1.55:
        return 2
    return 3


def _scan_uniform_edge_bands(gray: Image.Image) -> dict[str, int]:
    width, height = gray.size
    return {
        "top": _scan_rows(gray, from_start=True),
        "bottom": _scan_rows(gray, from_start=False),
        "left": _scan_cols(gray, from_start=True),
        "right": _scan_cols(gray, from_start=False),
    }


def _scan_rows(gray: Image.Image, *, from_start: bool) -> int:
    width, height = gray.size
    if height <= 0 or width <= 0:
        return 0
    first = 0 if from_start else height - 1
    baseline = _line_stats(gray.crop((0, first, width, first + 1)))[0]
    count = 0
    indices = range(height) if from_start else range(height - 1, -1, -1)
    for y in indices:
        mean, stddev = _line_stats(gray.crop((0, y, width, y + 1)))
        if stddev < LETTERBOX_STD_THRESH and abs(mean - baseline) < LETTERBOX_MEAN_DELTA:
            count += 1
        else:
            break
    return count


def _scan_cols(gray: Image.Image, *, from_start: bool) -> int:
    width, height = gray.size
    if height <= 0 or width <= 0:
        return 0
    first = 0 if from_start else width - 1
    baseline = _line_stats(gray.crop((first, 0, first + 1, height)))[0]
    count = 0
    indices = range(width) if from_start else range(width - 1, -1, -1)
    for x in indices:
        mean, stddev = _line_stats(gray.crop((x, 0, x + 1, height)))
        if stddev < LETTERBOX_STD_THRESH and abs(mean - baseline) < LETTERBOX_MEAN_DELTA:
            count += 1
        else:
            break
    return count


def _line_stats(line: Image.Image) -> tuple[float, float]:
    stat = ImageStat.Stat(line)
    return float(stat.mean[0]), float(stat.stddev[0])


def _edge_area_ratio(width: int, height: int, bands: dict[str, int]) -> float:
    top = min(max(int(bands.get("top") or 0), 0), height)
    bottom = min(max(int(bands.get("bottom") or 0), 0), max(0, height - top))
    remaining_h = max(0, height - top - bottom)
    left = min(max(int(bands.get("left") or 0), 0), width)
    right = min(max(int(bands.get("right") or 0), 0), max(0, width - left))
    area = top * width + bottom * width + remaining_h * (left + right)
    total = max(1, width * height)
    return min(1.0, max(0.0, area / float(total)))


def _cell_fill_ratio(cell: Image.Image) -> float:
    cell = _crop_margin(cell, 0.03)
    width, height = cell.size
    if width <= 0 or height <= 0:
        return 1.0
    bands = _scan_uniform_edge_bands(cell.convert("L"))
    return 1.0 - _edge_area_ratio(width, height, bands)


def _max_bg_color_delta(image: Image.Image, width: int, height: int) -> float:
    rows = _infer_row_count(width, height)
    row_edges = [round(height * idx / rows) for idx in range(rows + 1)]
    mid = width // 2
    max_delta = 0.0
    for idx in range(rows):
        top = row_edges[idx]
        bottom = max(top + 1, row_edges[idx + 1])
        left = image.crop((0, top, mid, bottom))
        right = image.crop((mid, top, width, bottom))
        delta = _rgb_delta(_perimeter_mean_rgb(left), _perimeter_mean_rgb(right))
        max_delta = max(max_delta, delta)
    return max_delta


def _perimeter_mean_rgb(cell: Image.Image) -> tuple[float, float, float]:
    width, height = cell.size
    band_x = max(1, int(width * 0.15))
    band_y = max(1, int(height * 0.15))
    samples = [
        cell.crop((0, 0, width, band_y)),
        cell.crop((0, max(0, height - band_y), width, height)),
        cell.crop((0, 0, band_x, height)),
        cell.crop((max(0, width - band_x), 0, width, height)),
    ]
    means = [ImageStat.Stat(sample).mean[:3] for sample in samples if sample.size[0] > 0 and sample.size[1] > 0]
    if not means:
        return (0.0, 0.0, 0.0)
    return tuple(sum(float(item[idx]) for item in means) / len(means) for idx in range(3))  # type: ignore[return-value]


def _rgb_delta(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return math.sqrt(sum((left[idx] - right[idx]) ** 2 for idx in range(3)))


def _mask_edge_whiteness(cell: Image.Image) -> float:
    work = _crop_margin(cell, 0.05).convert("RGB")
    if max(work.size) > _EDGE_MAX_SIDE:
        work.thumbnail((_EDGE_MAX_SIDE, _EDGE_MAX_SIDE), Image.Resampling.BILINEAR)
    edges = work.convert("L").filter(ImageFilter.FIND_EDGES)
    hsv = work.convert("HSV")
    edge_data = list(edges.getdata())
    hsv_data = list(hsv.getdata())
    edge_count = 0
    white_edge_count = 0
    for edge, (_, saturation, value) in zip(edge_data, hsv_data, strict=False):
        if int(edge) <= 24:
            continue
        edge_count += 1
        if int(value) > 209 and int(saturation) < 46:
            white_edge_count += 1
    if edge_count < 20:
        return 0.0
    return white_edge_count / float(edge_count)


def _crop_margin(image: Image.Image, ratio: float) -> Image.Image:
    width, height = image.size
    mx = int(width * ratio)
    my = int(height * ratio)
    if mx <= 0 and my <= 0:
        return image
    if width - (2 * mx) < 4 or height - (2 * my) < 4:
        return image
    return image.crop((mx, my, width - mx, height - my))


def _blank_area_ratio(image: Image.Image, edge_bands: dict[str, int]) -> float:
    width, height = image.size
    small_size = (128, max(1, round(128 * height / max(1, width))))
    small_rgb = image.convert("RGB").resize(small_size, Image.Resampling.BILINEAR)
    gray = small_rgb.convert("L")
    small_w, small_h = small_rgb.size
    if small_w <= 0 or small_h <= 0:
        return 0.0
    bg_rgb = _perimeter_mean_rgb(small_rgb)
    sx = small_w / float(max(1, width))
    sy = small_h / float(max(1, height))
    excluded = {
        "top": round((edge_bands.get("top") or 0) * sy),
        "bottom": small_h - round((edge_bands.get("bottom") or 0) * sy),
        "left": round((edge_bands.get("left") or 0) * sx),
        "right": small_w - round((edge_bands.get("right") or 0) * sx),
    }
    blank_area = 0
    measured_area = 0
    for y in range(0, small_h, _TILE_SIZE):
        for x in range(0, small_w, _TILE_SIZE):
            box = (x, y, min(small_w, x + _TILE_SIZE), min(small_h, y + _TILE_SIZE))
            area = (box[2] - box[0]) * (box[3] - box[1])
            if area <= 0 or _inside_outer_band(box, excluded):
                continue
            measured_area += area
            tile_gray = gray.crop(box)
            tile_rgb = small_rgb.crop(box)
            tile_mean = tuple(float(value) for value in ImageStat.Stat(tile_rgb).mean[:3])
            if ImageStat.Stat(tile_gray).stddev[0] < LETTERBOX_STD_THRESH and _rgb_delta(tile_mean, bg_rgb) < 35.0:
                blank_area += area
    if measured_area <= 0:
        return 0.0
    return blank_area / float(measured_area)


def _inside_outer_band(box: tuple[int, int, int, int], excluded: dict[str, int]) -> bool:
    left, top, right, bottom = box
    return (
        bottom <= excluded["top"]
        or top >= excluded["bottom"]
        or right <= excluded["left"]
        or left >= excluded["right"]
    )
