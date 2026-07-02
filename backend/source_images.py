"""Helpers for deciding which images are real clinical source photos.

The workbench scans folders that also contain finished boards, posters and
layout exports. Those generated images are useful history, but they must not
enter the per-photo classification queue or formal render preflight as source
material.
"""
from __future__ import annotations

from functools import lru_cache
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


CASE_NOT_SOURCE_TAG = "素材归档"
CASE_NOT_SOURCE_CODE = "not_case_source_directory"
SOURCE_BINDINGS_META_KEY = "source_case_bindings"

GENERATED_DIR_TOKENS = (
    ".case-layout-",
    ".case-workbench-",
    "case-workbench-ai",
    "正式品牌版批量",
    "朋友圈海报",
)

GENERATED_FILE_RE = re.compile(
    r"("
    r"final[-_\s]?board|"
    r"preview|"
    r"正式品牌版|品牌版|"
    r"三联图|双联图|单行文案|文案|"
    r"居中logo|logo|"
    r"海报|朋友圈|封面|poster|banner|"
    r"拼图|对比|comparison|compare|"
    r"排版|成品|定稿|优化显眼"
    r")",
    re.IGNORECASE,
)


def is_probable_generated_artifact(image_path: str) -> bool:
    """Return True when an image path looks like a generated output artifact."""
    rel = str(image_path or "").strip()
    if not rel:
        return False
    path = Path(rel)
    parts = [part for part in path.parts if part not in {"", "."}]
    for part in parts[:-1]:
        lowered = part.lower()
        if lowered.startswith((".case-layout-", ".case-workbench-")):
            return True
        if any(token.lower() in lowered for token in GENERATED_DIR_TOKENS):
            return True
    stem = path.stem.strip()
    return bool(GENERATED_FILE_RE.search(stem))


def is_source_image_file(image_path: str) -> bool:
    return not is_probable_generated_artifact(image_path)


def filter_source_image_files(image_files: list[str]) -> list[str]:
    return [str(item) for item in image_files if item and is_source_image_file(str(item))]


COMPOSITE_ASPECT_RATIO_THRESHOLD = 2.5
COMPOSITE_CANVAS_WHITE_THRESHOLD = 238
COMPOSITE_CANVAS_WHITE_RATIO_MIN = 0.20
COMPOSITE_CANVAS_BORDER_WHITE_RATIO_MIN = 0.32
COMPOSITE_CANVAS_VERTICAL_RUN_MIN = 0.045
COMPOSITE_CANVAS_HORIZONTAL_RUN_MIN = 0.08
COMPOSITE_CANVAS_STRONG_BLANK_LINE_MIN = 0.95

_COMPOSITE_REASON_RE = re.compile(
    r"("
    r"side[-\s]?by[-\s]?side|"
    r"before[-\s]?and[-\s]?after|"
    r"before/after|"
    r"collage|"
    r"composite|"
    r"拼图|"
    r"对比图"
    r")",
    re.IGNORECASE,
)


def is_composite_by_dimensions(width: int, height: int) -> bool:
    """Return True when image dimensions suggest a composite (side-by-side or stacked)."""
    if width <= 0 or height <= 0:
        return False
    ratio = max(width / height, height / width)
    return ratio >= COMPOSITE_ASPECT_RATIO_THRESHOLD


def observation_reasons_indicate_composite(raw_reasons: object) -> bool:
    """Return True when VLM reason text explicitly says this is a comparison collage."""
    if isinstance(raw_reasons, str):
        candidates = [raw_reasons]
    elif isinstance(raw_reasons, list):
        candidates = [str(item) for item in raw_reasons if item]
    else:
        candidates = [str(raw_reasons)] if raw_reasons else []
    for text in candidates:
        if not text:
            continue
        if _COMPOSITE_REASON_RE.search(text):
            return True
        lowered = text.lower()
        has_cn_pair = "术前" in text and "术后" in text
        has_en_pair = "before" in lowered and "after" in lowered
        has_compare_context = any(
            token in lowered
            for token in (
                "comparison",
                "compare",
                "labeled",
                "labelled",
                "single image",
                "single composite",
            )
        ) or any(token in text for token in ("对比", "标签", "同一张", "单张"))
        if (has_cn_pair or has_en_pair) and has_compare_context:
            return True
    return False


def _max_run_fraction(values: list[float], *, threshold: float, start: int, end: int) -> float:
    best = 0
    current = 0
    for value in values[max(0, start):max(0, end)]:
        if value >= threshold:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best / max(len(values), 1)


def _white_canvas_composite_metrics(image_path: Path) -> dict[str, float] | None:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(image_path) as img:
            transposed = ImageOps.exif_transpose(img).convert("RGB")
            transposed.thumbnail((512, 512))
            width, height = transposed.size
            pixels = list(transposed.getdata())
    except Exception:
        return None
    if width <= 0 or height <= 0 or not pixels:
        return None
    row_white = [0] * height
    col_white = [0] * width
    white_total = 0
    threshold = COMPOSITE_CANVAS_WHITE_THRESHOLD
    for index, (red, green, blue) in enumerate(pixels):
        if red < threshold or green < threshold or blue < threshold:
            continue
        y, x = divmod(index, width)
        row_white[y] += 1
        col_white[x] += 1
        white_total += 1
    row_ratios = [count / width for count in row_white]
    col_ratios = [count / height for count in col_white]
    border = max(1, min(width, height) // 20)
    border_total = 0
    border_white = 0
    for y in range(height):
        for x in range(width):
            if y >= border and y < height - border and x >= border and x < width - border:
                continue
            border_total += 1
            if pixels[y * width + x][0] >= threshold and pixels[y * width + x][1] >= threshold and pixels[y * width + x][2] >= threshold:
                border_white += 1
    return {
        "white_ratio": white_total / max(width * height, 1),
        "border_white_ratio": border_white / max(border_total, 1),
        "central_vertical_run_fraction": _max_run_fraction(
            col_ratios,
            threshold=0.82,
            start=int(width * 0.12),
            end=int(width * 0.88),
        ),
        "central_horizontal_run_fraction": _max_run_fraction(
            row_ratios,
            threshold=0.82,
            start=int(height * 0.12),
            end=int(height * 0.88),
        ),
        "max_col_white_ratio": max(col_ratios, default=0.0),
        "max_row_white_ratio": max(row_ratios, default=0.0),
    }


def _looks_like_white_canvas_composite(metrics: dict[str, float] | None) -> bool:
    if not metrics:
        return False
    has_canvas = (
        metrics["white_ratio"] >= COMPOSITE_CANVAS_WHITE_RATIO_MIN
        and metrics["border_white_ratio"] >= COMPOSITE_CANVAS_BORDER_WHITE_RATIO_MIN
    )
    has_internal_gutter = (
        metrics["central_vertical_run_fraction"] >= COMPOSITE_CANVAS_VERTICAL_RUN_MIN
        or metrics["central_horizontal_run_fraction"] >= COMPOSITE_CANVAS_HORIZONTAL_RUN_MIN
    )
    has_strong_blank_line = (
        metrics["max_col_white_ratio"] >= COMPOSITE_CANVAS_STRONG_BLANK_LINE_MIN
        or metrics["max_row_white_ratio"] >= COMPOSITE_CANVAS_STRONG_BLANK_LINE_MIN
    )
    return has_canvas and has_internal_gutter and has_strong_blank_line


@lru_cache(maxsize=512)
def _is_composite_image_cached(path_str: str, mtime_ns: int, file_size: int) -> bool:
    del mtime_ns, file_size
    image_path = Path(path_str)
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return False
    try:
        with Image.open(image_path) as img:
            transposed = ImageOps.exif_transpose(img)
            width, height = transposed.size
    except Exception:
        return False
    if is_composite_by_dimensions(width, height):
        logger.info(
            "composite image detected (aspect ratio %.2f): %s",
            max(width / height, height / width),
            image_path.name,
        )
        return True
    metrics = _white_canvas_composite_metrics(image_path)
    if _looks_like_white_canvas_composite(metrics):
        logger.info(
            "composite image detected (white canvas/gutter): %s metrics=%s",
            image_path.name,
            {key: round(value, 4) for key, value in (metrics or {}).items()},
        )
        return True
    return False


def is_composite_image(image_path: Path) -> bool:
    """Content-level composite detection using aspect ratio and white-canvas gutters.

    Returns True for images whose aspect ratio strongly suggests they are
    side-by-side or stacked composites, or whose content looks like a
    before/after comparison board embedded on a white canvas.
    """
    if not image_path.is_file():
        return False
    try:
        stat = image_path.stat()
    except OSError:
        return False
    return _is_composite_image_cached(str(image_path), int(stat.st_mtime_ns), int(stat.st_size))


def existing_source_image_files(abs_path: str, image_files: list[str]) -> dict[str, object]:
    """Split source image metadata into files that actually exist on disk.

    DB metadata can outlive moved source folders. Formal render preflight must
    be based on readable files, not historical `meta.image_files` entries.
    """
    base = Path(abs_path or "").resolve()
    existing: list[str] = []
    missing: list[str] = []
    for item in filter_source_image_files([str(x) for x in image_files if x]):
        rel = Path(item)
        if rel.is_absolute() or ".." in rel.parts:
            missing.append(item)
            continue
        target = (base / rel).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            missing.append(item)
            continue
        if target.is_file():
            existing.append(item)
        else:
            missing.append(item)
    return {
        "existing": existing,
        "missing": missing,
        "existing_count": len(existing),
        "missing_count": len(missing),
        "missing_samples": missing[:8],
    }


def source_filter_summary(image_files: list[str]) -> dict[str, object]:
    source_files = filter_source_image_files(image_files)
    excluded = [str(item) for item in image_files if item and not is_source_image_file(str(item))]
    return {
        "source_count": len(source_files),
        "generated_artifact_count": len(excluded),
        "generated_artifact_samples": excluded[:8],
    }


def _manual_issue_code(item: object) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("code") or "")
    return ""


def case_marked_not_source(tags: object, manual_issues: object) -> bool:
    """Return True when a human marked this case as a material/archive folder.

    The marker is stored in existing case fields only: a tag for quick browsing
    and a manual blocking code for render safety. No schema change is needed.
    """
    tag_values = [str(item) for item in tags] if isinstance(tags, list) else []
    issue_values = [_manual_issue_code(item) for item in manual_issues] if isinstance(manual_issues, list) else []
    return CASE_NOT_SOURCE_TAG in tag_values or CASE_NOT_SOURCE_CODE in issue_values


BEFORE_TOKENS = ("术前", "治疗前", "before", "pre")
AFTER_TOKENS = ("术后", "治疗后", "after", "post")


def _phase_from_filename(filename: str) -> str | None:
    basename = Path(filename).name.lower()
    if any(token.lower() in basename for token in BEFORE_TOKENS):
        return "before"
    if any(token.lower() in basename for token in AFTER_TOKENS):
        return "after"
    lowered = filename.lower()
    if any(token.lower() in lowered for token in BEFORE_TOKENS):
        return "before"
    if any(token.lower() in lowered for token in AFTER_TOKENS):
        return "after"
    return None


def classify_source_profile(image_files: list[str]) -> dict[str, object]:
    """Classify a case folder's source-photo readiness.

    This intentionally uses only filenames/paths, so it can run inside batch
    preview and queue preflight without invoking heavy visual analysis.
    """
    raw_files = [str(item) for item in image_files if item]
    source_files = filter_source_image_files(raw_files)
    generated_files = [item for item in raw_files if not is_source_image_file(item)]
    before_files = [item for item in source_files if _phase_from_filename(item) == "before"]
    after_files = [item for item in source_files if _phase_from_filename(item) == "after"]
    unlabeled_files = [item for item in source_files if _phase_from_filename(item) is None]
    if not raw_files:
        source_kind = "unknown_not_scanned"
    elif source_files:
        if len(source_files) < 2:
            source_kind = "insufficient_source_photos"
        elif not before_files or not after_files:
            source_kind = "missing_before_after_pair"
        else:
            source_kind = "ready_source"
    elif generated_files:
        source_kind = "generated_output_collection"
    else:
        source_kind = "empty"
    return {
        "source_kind": source_kind,
        "raw_image_count": len(raw_files),
        "source_count": len(source_files),
        "generated_artifact_count": len(generated_files),
        "before_count": len(before_files),
        "after_count": len(after_files),
        "unlabeled_source_count": len(unlabeled_files),
        "source_samples": source_files[:8],
        "generated_artifact_samples": generated_files[:8],
    }


def classify_case_source_profile(abs_path: str, image_files: list[str]) -> dict[str, object]:
    """Classify source readiness using the case directory name as context.

    Real data often has cases split into sibling folders named `术前` / `术后`.
    The files inside those folders may be plain camera names, so filename-only
    classification would call them "unlabeled". This helper keeps public
    samples as original filenames while using `<case-dir-name>/<filename>` for
    phase and generated-artifact detection.
    """
    raw_files = [str(item) for item in image_files if item]
    case_name = Path(abs_path or "").name
    source_files: list[str] = []
    generated_files: list[str] = []
    before_files: list[str] = []
    after_files: list[str] = []
    unlabeled_files: list[str] = []
    for item in raw_files:
        contextual = str(Path(case_name) / item) if case_name else item
        if is_probable_generated_artifact(item) or is_probable_generated_artifact(contextual):
            generated_files.append(item)
            continue
        source_files.append(item)
        phase = _phase_from_filename(item) or _phase_from_filename(contextual)
        if phase == "before":
            before_files.append(item)
        elif phase == "after":
            after_files.append(item)
        else:
            unlabeled_files.append(item)
    if not raw_files:
        source_kind = "unknown_not_scanned"
    elif source_files:
        if len(source_files) < 2:
            source_kind = "insufficient_source_photos"
        elif not before_files or not after_files:
            source_kind = "missing_before_after_pair"
        else:
            source_kind = "ready_source"
    elif generated_files:
        source_kind = "generated_output_collection"
    else:
        source_kind = "empty"
    return {
        "source_kind": source_kind,
        "raw_image_count": len(raw_files),
        "source_count": len(source_files),
        "generated_artifact_count": len(generated_files),
        "before_count": len(before_files),
        "after_count": len(after_files),
        "unlabeled_source_count": len(unlabeled_files),
        "source_samples": source_files[:8],
        "generated_artifact_samples": generated_files[:8],
    }


def classify_existing_case_source_profile(abs_path: str, image_files: list[str]) -> dict[str, object]:
    raw_files = [str(item) for item in image_files if item]
    split = existing_source_image_files(abs_path, image_files)
    existing = [str(item) for item in split["existing"]]
    generated_files = [item for item in raw_files if not is_source_image_file(item)]
    profile = classify_case_source_profile(abs_path, [*existing, *generated_files])
    missing_count = int(split["missing_count"])
    profile["raw_meta_image_count"] = len(raw_files)
    profile["missing_source_count"] = missing_count
    profile["missing_source_samples"] = split["missing_samples"]
    if missing_count:
        profile["file_integrity_status"] = "missing_source_files"
        if not existing:
            profile["source_kind"] = "missing_source_files"
    else:
        profile["file_integrity_status"] = "ok"
    return profile
