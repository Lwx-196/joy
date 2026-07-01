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
MASK_EDGE_HALO_FLAG = 0.06
MASK_EDGE_HALO_PENALTY_SCALE = 500.0
MASK_EDGE_HALO_MIN_BOUNDARY = 500
BLANK_AREA_FLAG = 0.35
CV_PENALTY_CAP = 30.0
PHOTO_PANEL_DARK_LUMA_THRESH = 36
PHOTO_PANEL_MIN_AREA_RATIO = 0.012
POSTOP_SKIN_R_MINUS_G_MIN = 14.0
POSTOP_SKIN_R_MINUS_B_MIN = 32.0
POSTOP_SKIN_WARMTH_DROP_MIN = 6.0
POSTOP_SKIN_CAST_PENALTY = 10.0
SIDE_SCALE_SKIN_HEIGHT_RATIO_MAX = 1.28
SIDE_SCALE_SKIN_AREA_RATIO_MAX = 1.35
SIDE_SCALE_FOREGROUND_WIDTH_RATIO_MAX = 1.18
SIDE_SCALE_MISMATCH_PENALTY = 12.0

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
        photo_boxes = _photo_panel_boxes(image)
        cutout_boxes = photo_boxes or cell_boxes
        postop_skin_cast = _postop_skin_cast_metrics(image, photo_boxes)
        side_scale_mismatch = _side_scale_mismatch_metrics(image, photo_boxes)

        cell_fill_ratios = [_cell_fill_ratio(image.crop(box)) for box in cell_boxes]
        min_cell_fill_ratio = min(cell_fill_ratios) if cell_fill_ratios else 1.0
        max_bg_color_delta = _max_bg_color_delta(image, board_w, board_h)
        max_mask_edge_whiteness = max((_mask_edge_whiteness(image.crop(box)) for box in cutout_boxes), default=0.0)
        mask_edge_halo = _max_mask_edge_halo(image, cutout_boxes)
        max_mask_edge_halo_score = float(mask_edge_halo.get("score") or 0.0)
        blank_area_ratio = _blank_area_ratio(image, edge_bands, content_boxes=photo_boxes)

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
        if max_mask_edge_halo_score > MASK_EDGE_HALO_FLAG:
            if "cutout_artifact" not in flags:
                flags.append("cutout_artifact")
            penalty += min((max_mask_edge_halo_score - MASK_EDGE_HALO_FLAG) * MASK_EDGE_HALO_PENALTY_SCALE, 15.0)
        if blank_area_ratio > BLANK_AREA_FLAG:
            flags.append("blank_region")
            penalty += min((blank_area_ratio - BLANK_AREA_FLAG) * 40.0, 12.0)
        if postop_skin_cast.get("flagged"):
            flags.append("postop_cyan_cast")
            penalty += POSTOP_SKIN_CAST_PENALTY
        if side_scale_mismatch.get("flagged"):
            flags.append("side_scale_mismatch")
            penalty += SIDE_SCALE_MISMATCH_PENALTY

        return {
            "available": True,
            "board_w": int(board_w),
            "board_h": int(board_h),
            "letterbox_ratio": round(float(letterbox_ratio), 4),
            "min_cell_fill_ratio": round(float(min_cell_fill_ratio), 4),
            "max_bg_color_delta": round(float(max_bg_color_delta), 2),
            "max_mask_edge_whiteness": round(float(max_mask_edge_whiteness), 4),
            "max_mask_edge_halo_score": round(float(max_mask_edge_halo_score), 4),
            "mask_edge_halo": mask_edge_halo,
            "blank_area_ratio": round(float(blank_area_ratio), 4),
            "flags": flags,
            "cv_penalty": round(min(max(penalty, 0.0), CV_PENALTY_CAP), 1),
            "postop_skin_cast": postop_skin_cast,
            "side_scale_mismatch": side_scale_mismatch,
            "cell_grid": {"rows": len(cell_boxes) // 2 if cell_boxes else 1, "cols": 2, "inferred": inferred},
            "photo_panel_count": len(photo_boxes),
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


def _max_mask_edge_halo(
    image: Image.Image,
    boxes: list[tuple[int, int, int, int]],
) -> dict[str, Any]:
    best: dict[str, Any] = {
        "score": 0.0,
        "gray_ratio": 0.0,
        "cyan_ratio": 0.0,
        "boundary_count": 0,
        "box": None,
        "threshold": MASK_EDGE_HALO_FLAG,
    }
    if np is None:
        best["reason"] = "numpy_unavailable"
        return best
    for box in boxes:
        metrics = _mask_edge_halo(image.crop(box))
        if float(metrics.get("score") or 0.0) > float(best.get("score") or 0.0):
            best = {**metrics, "box": [int(value) for value in box], "threshold": MASK_EDGE_HALO_FLAG}
    return best


def _mask_edge_halo(cell: Image.Image) -> dict[str, Any]:
    """Detect gray/cyan matte residue where cutout subjects meet dark panels."""
    result: dict[str, Any] = {
        "score": 0.0,
        "gray_ratio": 0.0,
        "cyan_ratio": 0.0,
        "boundary_count": 0,
    }
    if np is None:
        result["reason"] = "numpy_unavailable"
        return result
    work = _crop_margin(cell, 0.05).convert("RGB")
    if max(work.size) > _EDGE_MAX_SIDE:
        work.thumbnail((_EDGE_MAX_SIDE, _EDGE_MAX_SIDE), Image.Resampling.BILINEAR)
    arr = np.asarray(work).astype("int16")
    if arr.size == 0:
        result["reason"] = "empty_crop"
        return result
    luma = (0.299 * arr[:, :, 0]) + (0.587 * arr[:, :, 1]) + (0.114 * arr[:, :, 2])
    dark = luma < 32
    if not bool(dark.any()):
        result["reason"] = "no_dark_panel"
        return result
    adjacent_dark = np.zeros(dark.shape, dtype=bool)
    adjacent_dark[1:, :] |= dark[:-1, :]
    adjacent_dark[:-1, :] |= dark[1:, :]
    adjacent_dark[:, 1:] |= dark[:, :-1]
    adjacent_dark[:, :-1] |= dark[:, 1:]
    boundary = (~dark) & adjacent_dark
    boundary_count = int(boundary.sum())
    result["boundary_count"] = boundary_count
    if boundary_count < MASK_EDGE_HALO_MIN_BOUNDARY:
        result["reason"] = "insufficient_boundary"
        return result
    red = arr[:, :, 0]
    green = arr[:, :, 1]
    blue = arr[:, :, 2]
    max_channel = arr.max(axis=2)
    min_channel = arr.min(axis=2)
    chroma = max_channel - min_channel
    gray_halo = boundary & (max_channel > 132) & (chroma < 38)
    cyan_halo = boundary & (blue > red + 18) & (blue > green + 8) & (blue > 95)
    gray_count = int(gray_halo.sum())
    cyan_count = int(cyan_halo.sum())
    gray_ratio = gray_count / float(boundary_count)
    cyan_ratio = cyan_count / float(boundary_count)
    score = gray_ratio + (2.0 * cyan_ratio)
    result.update({
        "score": round(float(score), 4),
        "gray_ratio": round(float(gray_ratio), 4),
        "cyan_ratio": round(float(cyan_ratio), 4),
        "gray_count": gray_count,
        "cyan_count": cyan_count,
        "reason": "ok",
    })
    return result


def _crop_margin(image: Image.Image, ratio: float) -> Image.Image:
    width, height = image.size
    mx = int(width * ratio)
    my = int(height * ratio)
    if mx <= 0 and my <= 0:
        return image
    if width - (2 * mx) < 4 or height - (2 * my) < 4:
        return image
    return image.crop((mx, my, width - mx, height - my))


def _blank_area_ratio(
    image: Image.Image,
    edge_bands: dict[str, int],
    *,
    content_boxes: list[tuple[int, int, int, int]] | None = None,
) -> float:
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
            if content_boxes:
                original_box = (
                    round(box[0] / sx),
                    round(box[1] / sy),
                    round(box[2] / sx),
                    round(box[3] / sy),
                )
                if not _intersects_any(original_box, content_boxes):
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


def _photo_panel_boxes(image: Image.Image) -> list[tuple[int, int, int, int]]:
    """Detect dark photo panels so template gutters do not masquerade as artifacts."""
    if np is None:
        return []
    width, height = image.size
    if width <= 0 or height <= 0:
        return []
    gray = image.convert("L")
    arr = np.asarray(gray)
    dark = arr < PHOTO_PANEL_DARK_LUMA_THRESH
    row_dark_fraction = dark.mean(axis=1)
    row_segments = _segments(row_dark_fraction > 0.08, min_len=max(24, height // 24))
    boxes: list[tuple[int, int, int, int]] = []
    min_area = max(1, int(width * height * PHOTO_PANEL_MIN_AREA_RATIO))
    for top, bottom in row_segments:
        row_mask = dark[top:bottom, :]
        if row_mask.size == 0:
            continue
        col_dark_fraction = row_mask.mean(axis=0)
        col_segments = _segments(col_dark_fraction > 0.08, min_len=max(24, width // 24))
        for left, right in col_segments:
            if (right - left) * (bottom - top) < min_area:
                continue
            # Pad a little to include pale labels or subtle borders adjacent to the panel,
            # but keep the metric focused on photo content rather than page chrome.
            pad_x = max(2, round((right - left) * 0.03))
            pad_y = max(2, round((bottom - top) * 0.03))
            boxes.append((
                max(0, left - pad_x),
                max(0, top - pad_y),
                min(width, right + pad_x),
                min(height, bottom + pad_y),
            ))
    return _merge_nearby_boxes(boxes)


def _postop_skin_cast_metrics(
    image: Image.Image,
    photo_boxes: list[tuple[int, int, int, int]],
) -> dict[str, Any]:
    """Detect obvious post-op green/cyan cast in the first before/after photo row.

    This is a conservative publish-safety signal for the front/single row only.
    It compares the after face warmth against the before cell and fails open when
    the board does not expose a stable before/after photo pair.
    """
    result: dict[str, Any] = {
        "evaluated": False,
        "flagged": False,
        "reason": "no_evaluable_pair",
        "thresholds": {
            "after_r_minus_g_min": POSTOP_SKIN_R_MINUS_G_MIN,
            "after_r_minus_b_min": POSTOP_SKIN_R_MINUS_B_MIN,
            "warmth_drop_min": POSTOP_SKIN_WARMTH_DROP_MIN,
        },
    }
    if np is None or len(photo_boxes) < 1:
        result["reason"] = "numpy_unavailable" if np is None else "no_photo_panels"
        return result
    cells = _photo_panel_cells(photo_boxes)
    if len(cells) < 2:
        return result
    before_box, after_box = sorted(cells, key=lambda item: (item[1], item[0]))[:2]
    before = _face_warmth_stats(image.crop(before_box))
    after = _face_warmth_stats(image.crop(after_box))
    if not before or not after:
        result["reason"] = "face_color_unavailable"
        return result
    warmth_drop = float(before["r_minus_g"]) - float(after["r_minus_g"])
    flagged = (
        float(after["r_minus_g"]) < POSTOP_SKIN_R_MINUS_G_MIN
        and float(after["r_minus_b"]) < POSTOP_SKIN_R_MINUS_B_MIN
        and warmth_drop > POSTOP_SKIN_WARMTH_DROP_MIN
    )
    result.update({
        "evaluated": True,
        "flagged": bool(flagged),
        "reason": "postop_cyan_cast" if flagged else "ok",
        "before": before,
        "after": after,
        "warmth_drop": round(warmth_drop, 2),
    })
    return result


def _side_scale_mismatch_metrics(
    image: Image.Image,
    photo_boxes: list[tuple[int, int, int, int]],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "evaluated": False,
        "flagged": False,
        "reason": "no_evaluable_side_pair",
        "thresholds": {
            "skin_height_ratio_max": SIDE_SCALE_SKIN_HEIGHT_RATIO_MAX,
            "skin_area_ratio_max": SIDE_SCALE_SKIN_AREA_RATIO_MAX,
            "foreground_width_ratio_max": SIDE_SCALE_FOREGROUND_WIDTH_RATIO_MAX,
        },
    }
    if np is None or len(photo_boxes) < 1:
        result["reason"] = "numpy_unavailable" if np is None else "no_photo_panels"
        return result
    rows = _photo_cell_rows(_photo_panel_cells(photo_boxes))
    if len(rows) < 2:
        return result
    side_row = sorted(rows, key=lambda row: _row_center_y(row))[-1]
    if len(side_row) < 2:
        return result
    before_box, after_box = sorted(side_row, key=lambda item: item[0])[:2]
    before = _subject_scale_stats(image.crop(before_box))
    after = _subject_scale_stats(image.crop(after_box))
    if not before or not after:
        result["reason"] = "subject_scale_unavailable"
        return result
    skin_height_ratio = _ratio_pair(float(before["skin"]["height"]), float(after["skin"]["height"]))
    skin_area_ratio = _ratio_pair(float(before["skin"]["area"]), float(after["skin"]["area"]))
    foreground_width_ratio = _ratio_pair(float(before["foreground"]["width"]), float(after["foreground"]["width"]))
    flagged = (
        skin_height_ratio >= SIDE_SCALE_SKIN_HEIGHT_RATIO_MAX
        and skin_area_ratio >= SIDE_SCALE_SKIN_AREA_RATIO_MAX
    ) or (
        foreground_width_ratio >= SIDE_SCALE_FOREGROUND_WIDTH_RATIO_MAX
        and skin_area_ratio >= SIDE_SCALE_SKIN_AREA_RATIO_MAX
    )
    result.update({
        "evaluated": True,
        "flagged": bool(flagged),
        "reason": "side_scale_mismatch" if flagged else "ok",
        "before_box": list(before_box),
        "after_box": list(after_box),
        "before": before,
        "after": after,
        "ratios": {
            "skin_height": round(skin_height_ratio, 3),
            "skin_area": round(skin_area_ratio, 3),
            "foreground_width": round(foreground_width_ratio, 3),
        },
    })
    return result


def _photo_panel_cells(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    cells: list[tuple[int, int, int, int]] = []
    for left, top, right, bottom in boxes:
        width = right - left
        height = bottom - top
        if width > height * 1.25:
            mid = (left + right) // 2
            cells.append((left, top, mid, bottom))
            cells.append((mid, top, right, bottom))
        else:
            cells.append((left, top, right, bottom))
    return cells


def _photo_cell_rows(cells: list[tuple[int, int, int, int]]) -> list[list[tuple[int, int, int, int]]]:
    rows: list[list[tuple[int, int, int, int]]] = []
    for cell in sorted(cells, key=lambda item: ((_box_center_y(item)), item[0])):
        center_y = _box_center_y(cell)
        matched: list[tuple[int, int, int, int]] | None = None
        for row in rows:
            row_height = max((item[3] - item[1] for item in row), default=0)
            tolerance = max(12.0, row_height * 0.35)
            if abs(center_y - _row_center_y(row)) <= tolerance:
                matched = row
                break
        if matched is None:
            rows.append([cell])
        else:
            matched.append(cell)
    return rows


def _row_center_y(row: list[tuple[int, int, int, int]]) -> float:
    if not row:
        return 0.0
    return sum(_box_center_y(item) for item in row) / float(len(row))


def _box_center_y(box: tuple[int, int, int, int]) -> float:
    return (float(box[1]) + float(box[3])) / 2.0


def _subject_scale_stats(cell: Image.Image) -> dict[str, Any] | None:
    if np is None:
        return None
    photo = _dark_photo_crop(cell) or cell
    photo_box = getattr(photo, "_source_box", (0, 0, photo.size[0], photo.size[1]))
    arr = np.asarray(cell.convert("RGB")).astype("int16")
    if photo is not cell:
        arr = np.asarray(photo.convert("RGB")).astype("int16")
    if arr.size == 0:
        return None
    luma = (0.299 * arr[:, :, 0]) + (0.587 * arr[:, :, 1]) + (0.114 * arr[:, :, 2])
    foreground_mask = (
        (luma > 38)
        & ~((arr[:, :, 0] > 235) & (arr[:, :, 1] > 235) & (arr[:, :, 2] > 235))
    )
    skin_mask = (
        (luma > 55)
        & (luma < 245)
        & (arr[:, :, 0] > 65)
        & (arr[:, :, 1] > 42)
        & (arr[:, :, 2] > 30)
        & (arr[:, :, 0] >= arr[:, :, 2] - 18)
        & ((arr.max(axis=2) - arr.min(axis=2)) < 115)
    )
    foreground = _mask_bbox_stats(foreground_mask)
    skin = _mask_bbox_stats(skin_mask)
    if not foreground or not skin:
        return None
    if int(skin["sample_count"]) < 60 or int(foreground["sample_count"]) < 120:
        return None
    return {
        "photo_box": list(photo_box),
        "foreground": foreground,
        "skin": skin,
    }


def _dark_photo_crop(cell: Image.Image) -> Image.Image | None:
    if np is None:
        return None
    arr = np.asarray(cell.convert("L"))
    if arr.size == 0:
        return None
    dark = arr < PHOTO_PANEL_DARK_LUMA_THRESH
    row_segments = _segments(dark.mean(axis=1) > 0.12, min_len=max(8, cell.size[1] // 12))
    col_segments = _segments(dark.mean(axis=0) > 0.12, min_len=max(8, cell.size[0] // 12))
    if not row_segments or not col_segments:
        return None
    top, bottom = max(row_segments, key=lambda item: item[1] - item[0])
    left, right = max(col_segments, key=lambda item: item[1] - item[0])
    if (right - left) * (bottom - top) < max(100, int(cell.size[0] * cell.size[1] * 0.2)):
        return None
    cropped = cell.crop((left, top, right, bottom))
    setattr(cropped, "_source_box", (left, top, right, bottom))
    return cropped


def _mask_bbox_stats(mask: Any) -> dict[str, Any] | None:
    ys, xs = np.where(mask)
    if len(xs) <= 0:
        return None
    x0 = int(xs.min())
    x1 = int(xs.max()) + 1
    y0 = int(ys.min())
    y1 = int(ys.max()) + 1
    width = x1 - x0
    height = y1 - y0
    if width <= 0 or height <= 0:
        return None
    return {
        "sample_count": int(len(xs)),
        "bbox": [x0, y0, x1, y1],
        "width": int(width),
        "height": int(height),
        "area": int(width * height),
        "center_x": round((x0 + x1) / 2.0, 3),
        "center_y": round((y0 + y1) / 2.0, 3),
    }


def _ratio_pair(left: float, right: float) -> float:
    low = min(abs(left), abs(right))
    high = max(abs(left), abs(right))
    if low <= 0:
        return 1.0
    return high / low


def _face_warmth_stats(cell: Image.Image) -> dict[str, Any] | None:
    if np is None:
        return None
    arr = np.asarray(cell.convert("RGB")).astype("int16")
    if arr.size == 0:
        return None
    luma = (0.299 * arr[:, :, 0]) + (0.587 * arr[:, :, 1]) + (0.114 * arr[:, :, 2])
    subject = (
        (luma > 45)
        & (luma < 245)
        & ~((arr[:, :, 0] > 235) & (arr[:, :, 1] > 235) & (arr[:, :, 2] > 235))
    )
    ys, xs = np.where(subject)
    if len(xs) < 50:
        return None
    x0 = int(xs.min())
    x1 = int(xs.max()) + 1
    y0 = int(ys.min())
    y1 = int(ys.max()) + 1
    width = x1 - x0
    height = y1 - y0
    if width <= 0 or height <= 0:
        return None
    fx0 = x0 + int(width * 0.18)
    fx1 = x1 - int(width * 0.18)
    fy0 = y0 + int(height * 0.12)
    fy1 = y0 + int(height * 0.58)
    face = arr[fy0:fy1, fx0:fx1, :]
    if face.size == 0:
        return None
    face_luma = (
        (0.299 * face[:, :, 0])
        + (0.587 * face[:, :, 1])
        + (0.114 * face[:, :, 2])
    )
    spread = face.max(axis=2) - face.min(axis=2)
    mask = (
        (face_luma > 70)
        & (face_luma < 235)
        & (face[:, :, 0] > 55)
        & (face[:, :, 1] > 45)
        & (face[:, :, 2] > 35)
        & (spread < 95)
    )
    if int(mask.sum()) < 30:
        mask = (face_luma > 70) & (face_luma < 235)
    if int(mask.sum()) < 30:
        return None
    pixels = face[mask]
    mean = pixels.mean(axis=0)
    r = float(mean[0])
    g = float(mean[1])
    b = float(mean[2])
    return {
        "sample_count": int(mask.sum()),
        "rgb": [round(r, 2), round(g, 2), round(b, 2)],
        "r_minus_g": round(r - g, 2),
        "r_minus_b": round(r - b, 2),
        "subject_box": [int(x0), int(y0), int(x1), int(y1)],
        "face_box": [int(fx0), int(fy0), int(fx1), int(fy1)],
    }


def _segments(mask: Any, *, min_len: int) -> list[tuple[int, int]]:
    segments: list[tuple[int, int]] = []
    start: int | None = None
    for idx, value in enumerate(mask):
        if bool(value):
            if start is None:
                start = idx
        elif start is not None:
            if idx - start >= min_len:
                segments.append((start, idx))
            start = None
    if start is not None and len(mask) - start >= min_len:
        segments.append((start, len(mask)))
    return segments


def _merge_nearby_boxes(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    merged: list[tuple[int, int, int, int]] = []
    for box in sorted(boxes, key=lambda item: (item[1], item[0])):
        match_idx = None
        for idx, existing in enumerate(merged):
            if _boxes_touch_or_overlap(existing, box, gap=8):
                match_idx = idx
                break
        if match_idx is None:
            merged.append(box)
        else:
            old = merged[match_idx]
            merged[match_idx] = (
                min(old[0], box[0]),
                min(old[1], box[1]),
                max(old[2], box[2]),
                max(old[3], box[3]),
            )
    return merged


def _boxes_touch_or_overlap(
    left: tuple[int, int, int, int],
    right: tuple[int, int, int, int],
    *,
    gap: int,
) -> bool:
    return not (
        left[2] + gap < right[0]
        or right[2] + gap < left[0]
        or left[3] + gap < right[1]
        or right[3] + gap < left[1]
    )


def _intersects_any(box: tuple[int, int, int, int], boxes: list[tuple[int, int, int, int]]) -> bool:
    return any(
        box[0] < other[2] and box[2] > other[0] and box[1] < other[3] and box[3] > other[1]
        for other in boxes
    )


def _inside_outer_band(box: tuple[int, int, int, int], excluded: dict[str, int]) -> bool:
    left, top, right, bottom = box
    return (
        bottom <= excluded["top"]
        or top >= excluded["bottom"]
        or right <= excluded["left"]
        or left >= excluded["right"]
    )
