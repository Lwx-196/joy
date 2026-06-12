#!/usr/bin/env python3
"""case_layout_board.py

医美案例自动排版执行器：
- inspect：真实案例目录识别、预检、预览图
- render：基于 inspect 结果生成最终三角度术前术后拼图
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from functools import lru_cache
from datetime import datetime
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FACE_ALIGN_PATH = Path(__file__).resolve().parent / "face_align_compare.py"
SEMANTIC_SCREEN_SCRIPT = Path(__file__).resolve().parent / "case_layout_screen.js"
SEMANTIC_PAIR_REVIEW_SCRIPT = Path(__file__).resolve().parent / "case_layout_pair_review.js"
SEMANTIC_FINAL_QA_SCRIPT = Path(__file__).resolve().parent / "case_layout_final_qa.js"
FACE_ALIGN_SPEC = importlib.util.spec_from_file_location("face_align_compare", FACE_ALIGN_PATH)
if FACE_ALIGN_SPEC is None or FACE_ALIGN_SPEC.loader is None:
    raise RuntimeError(f"无法加载底层 face_align_compare.py: {FACE_ALIGN_PATH}")
FACE_ALIGN = importlib.util.module_from_spec(FACE_ALIGN_SPEC)
FACE_ALIGN_SPEC.loader.exec_module(FACE_ALIGN)
_SMART_CROP_PATH = Path(__file__).resolve().parent / "smart_crop.py"
_smart_crop_spec = importlib.util.spec_from_file_location("smart_crop", _SMART_CROP_PATH)
if _smart_crop_spec and _smart_crop_spec.loader:
    SMART_CROP = importlib.util.module_from_spec(_smart_crop_spec)
    _smart_crop_spec.loader.exec_module(SMART_CROP)
else:
    SMART_CROP = None

SEGMENTER_MODEL_URL = "https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_multiclass_256x256/float32/latest/selfie_multiclass_256x256.tflite"
SEGMENTER_MODEL_PATH = Path("/tmp/selfie_multiclass_256x256.tflite")
_IMAGE_SEGMENTER = None

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
WORKBENCH_TRASH_DIR_NAME = ".case-workbench-trash"
ANGLE_SLOTS = ["front", "oblique", "side"]
BODY_RENDER_SCRIPT = Path(__file__).resolve().parent / "render_body_dual_compare.py"
ALIGN_TARGET_EYE_DISTANCE_RATIO = 0.31
ALIGN_TARGET_EYE_CENTER_Y_RATIO = 0.37
ALIGN_TARGET_FACE_HEIGHT_RATIO = 0.70
ALIGN_FACE_HEIGHT_SCALE_HEADROOM = 1.08
PROFILE_FALLBACK_MIN_SIZE_RATIO = 0.18
ENHANCE_QUALITY = "4k"
DEFAULT_ENHANCE_MODEL = os.environ.get("CASE_LAYOUT_ENHANCE_MODEL", "gemini-3-pro-image-preview-4k").strip()
ENHANCEMENT_INPUT_SIZE = (1290, 1560)
BACKGROUND_MODE = os.environ.get("CASE_LAYOUT_BACKGROUND_MODE", "auto-preserve-original-tone").strip() or "auto-preserve-original-tone"
DIRECTION_CHECK_MODE = "required-for-nonfront"
ENHANCEMENT_INPUT_MODE = "aligned-after"
ENHANCEMENT_SCOPE = "focus-scoped-light"
NON_TARGET_POLICY = "light-unify-only"
SEMANTIC_JUDGE_MODES = {"off", "auto"}
DEFAULT_SEMANTIC_TIMEOUT_SEC = 60.0
SEMANTIC_SCREEN_LOW_CONFIDENCE = 0.72
PADFILL_MODE = os.environ.get("CASE_LAYOUT_PADFILL_MODE", "white-only")
PADFILL_TRIGGER_RATIO = 0.06
PADFILL_TRIGGER_MAX_COMPONENT_RATIO = 0.035
PADFILL_AI_MAX_TOTAL_RATIO = 0.14
PADFILL_AI_MAX_EDGE_THICKNESS_RATIO = 0.12
PADFILL_CACHE_ROOT = Path("/tmp/case-layout-padfill")
PHASE_DIR_ALIASES = {
    "before": {
        "术前",
        "术前图",
        "术前图片",
        "术前照片",
        "术前原图",
        "术前组",
        "治疗前",
        "治疗前图",
        "治疗前图片",
        "治疗前照片",
        "操作前",
        "项目前",
        "注射前",
        "before",
        "pre",
        "preop",
        "pre-op",
    },
    "after": {
        "术后",
        "术后图",
        "术后图片",
        "术后照片",
        "术后原图",
        "术后组",
        "治疗后",
        "治疗后图",
        "治疗后图片",
        "治疗后照片",
        "操作后",
        "项目后",
        "注射后",
        "after",
        "post",
        "postop",
        "post-op",
    },
}
SHARPNESS_BLURRY_THRESHOLD = 8.0
SHARPNESS_SOFT_THRESHOLD = 15.0
PAIR_SHARPNESS_RATIO_THRESHOLD = 0.35
POSE_DELTA_THRESHOLDS = {
    "front": {"yaw": 8.0, "pitch": 7.0, "roll": 6.0, "weighted": 14.0},
    "oblique": {"yaw": 12.0, "pitch": 8.0, "roll": 8.0, "weighted": 18.0},
    "side": {"yaw": 12.0, "pitch": 8.0, "roll": 8.0, "weighted": 20.0},
}
NEUTRAL_ANGLE_ORDER = ["front", "oblique", "side"]
AREA_ANGLE_PRIORITY_RULES = [
    {
        "keywords": [
            "面颊凹陷",
            "面颊",
            "脸颊",
            "苹果肌",
            "太阳穴",
            "面中支撑",
            "面中",
            "泪沟",
            "法令纹",
            "法令",
            "印第安纹",
        ],
        "order": ["front", "oblique", "side"],
        "profile": "midface",
    },
    {
        "keywords": [
            "鼻背",
            "山根",
            "鼻基底",
            "鼻子",
            "鼻",
            "下巴",
            "下颌缘",
            "下颌线",
            "下颌",
            "轮廓线",
            "轮廓",
            "颏",
        ],
        "order": ["oblique", "side", "front"],
        "profile": "contour",
    },
]
SOFT_OBLIQUE_ALIAS_MIN_YAW = 35.0
SOFT_OBLIQUE_ALIAS_MAX_YAW = 52.0
ANGLE_LABELS = {
    "front": "正面",
    "oblique": "45°侧",
    "side": "侧面",
    "back": "背面",
}
BODY_CASE_KEYWORDS = ["直角肩", "瘦肩", "后背", "手背", "颈纹", "肩", "身体"]
BODY_SECTION_PRIORITY_RULES = [
    {
        "keywords": ["颈纹", "颈部"],
        "sections": ["front", "oblique"],
        "template": "body-dual-compare",
    },
    {
        "keywords": ["直角肩", "瘦肩", "肩", "后背", "手背", "身体"],
        "sections": ["front", "back"],
        "template": "body-dual-compare",
    },
]
SEMANTIC_VIEW_TO_SLOT = {
    "正面": "front",
    "45侧": "oblique",
    "侧面": "side",
    "背面": "back",
}
BODY_SECTION_FACE_EXPECTATION = {
    "front": True,
    "back": False,
}
BODY_SECTION_VISUAL_MISMATCH_REASON = "body_section_visual_mismatch"
ANGLE_TARGET_YAW = {
    "front": 0.0,
    "oblique": 22.5,
    "side": 45.0,
}
ANGLE_SOURCE_PRIORITY = {
    "manual": 5,
    "filename": 3,
    "compare_hint": 2,
    "profile_fallback": 1.6,
    "pose": 1,
}
BLOCKING_DUP_SOURCES = {"filename", "compare_hint"}

BRANDS = {
    "fumei": {
        "brand": "芙美和颜",
        "brand_line": "芙美和颜·轻医美",
        "logo_path": "/Users/a1234/Desktop/芙美&莳美/芙美logo.png",
    },
    "shimei": {
        "brand": "莳美",
        "brand_line": "莳美·轻医美",
        "logo_path": "/Users/a1234/Desktop/芙美&莳美/莳美设计/莳美LOGO:二维码/莳美LOGO白.jpg",
    },
    "md_ai": {
        "brand": "MD-AI",
        "brand_line": "MD-AI 临床高保真",
        "logo_path": "/Users/a1234/Desktop/案例生成器/case-workbench/md_logo.png",
    },
}

PHASE_LABELS = {
    "before": "术前",
    "after": "术后",
}
TEMPLATE_LABELS = {
    "tri-compare": "三角度术前术后对比",
    "bi-compare": "双角度术前术后对比",
    "single-compare": "单角度术前术后对比",
}

FRONT_RE = re.compile(r"(?<![一-鿿])(?:正面照|正面|正脸)(?![一-鿿])")
OBLIQUE_RE = re.compile(
    r"(?:(?<![\d\.一-鿿])(?:3/4|34|45)(?![\d\.])(?:°|度角?|度)?(?![一-鿿])|(?<![一-鿿])(?:微侧|斜侧|半侧脸|半侧)(?![一-鿿]))"
)
SIDE_RE = re.compile(r"(?<![一-鿿])(?:侧面照|侧面|侧脸)(?![一-鿿])")
BACK_RE = re.compile(r"(背面|背部|后背)")
LEFT_RE = re.compile(r"(左)")
RIGHT_RE = re.compile(r"(右)")
COMPARE_HINT_RE = re.compile(r"术前术后对比图\s*(\d+)?(?:[_-](.*))?$")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def is_image_file(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in IMAGE_EXTS
        and not path.name.startswith(".")
        and WORKBENCH_TRASH_DIR_NAME not in path.parts
        and not any(part.startswith(".case-layout") for part in path.parts)
    )


def is_generated_case_layout_path(path: Path, scan_root: Path | None = None) -> bool:
    if scan_root is not None:
        try:
            relative_parts = path.resolve().relative_to(scan_root.resolve()).parts
            return any(part.startswith(".case-layout") for part in relative_parts)
        except ValueError:
            pass
    return any(part.startswith(".case-layout") for part in path.parts)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def json_dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def write_json(path: Path, data) -> None:
    ensure_dir(path.parent)
    path.write_text(json_dumps(data) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")


def relpath(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return str(path.resolve())


def resolve_brand(brand_id: str) -> dict:
    if brand_id not in BRANDS:
        raise ValueError(f"不支持的 brand: {brand_id}")
    brand = dict(BRANDS[brand_id])
    logo_path = Path(brand["logo_path"])
    if not logo_path.exists():
        raise FileNotFoundError(f"品牌 logo 不存在: {logo_path}")
    brand["id"] = brand_id
    brand["logo_path"] = str(logo_path)
    return brand


def enhancement_slots_from_arg(raw: str | None) -> list[str]:
    if not raw:
        return ANGLE_SLOTS[:]
    if raw == "all":
        return ANGLE_SLOTS[:]
    slots = []
    for token in raw.split(","):
        token = token.strip()
        if token not in ANGLE_SLOTS:
            raise ValueError(f"不支持的增强角度: {token}")
        if token not in slots:
            slots.append(token)
    return slots


def normalize_enhance_model(raw: str | None) -> str:
    return (raw or DEFAULT_ENHANCE_MODEL or "").strip()


def ensure_segmenter_model() -> str:
    if SEGMENTER_MODEL_PATH.exists() and SEGMENTER_MODEL_PATH.stat().st_size > 0:
        return str(SEGMENTER_MODEL_PATH)
    proc = subprocess.run(
        [
            "curl",
            "-L",
            "--fail",
            "--silent",
            "--show-error",
            SEGMENTER_MODEL_URL,
            "-o",
            str(SEGMENTER_MODEL_PATH),
        ],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "下载 ImageSegmenter 模型失败")
    return str(SEGMENTER_MODEL_PATH)


def get_image_segmenter():
    global _IMAGE_SEGMENTER
    if _IMAGE_SEGMENTER is not None:
        return _IMAGE_SEGMENTER
    base_options = mp_python.BaseOptions(model_asset_path=ensure_segmenter_model())
    options = mp_vision.ImageSegmenterOptions(
        base_options=base_options,
        output_category_mask=True,
        output_confidence_masks=False,
    )
    _IMAGE_SEGMENTER = mp_vision.ImageSegmenter.create_from_options(options)
    return _IMAGE_SEGMENTER


def padfill_cache_path(source_path: str, slot: str, phase: str, model: str, size: tuple[int, int]) -> Path:
    path = Path(source_path)
    stat = path.stat()
    digest = hashlib.sha1(
        f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{slot}|{phase}|{model}|{size[0]}x{size[1]}".encode("utf-8")
    ).hexdigest()[:16]
    return PADFILL_CACHE_ROOT / f"{path.stem}-{slot}-{phase}-{digest}.jpg"


def default_output_root(case_dir: Path, brand_id: str, template: str) -> Path:
    return case_dir.resolve() / ".case-layout-board" / brand_id / template


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_paths = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc" if bold else "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/Library/Fonts/Arial.ttf",
    ]
    for font_path in font_paths:
        if not font_path or not Path(font_path).exists():
            continue
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            continue
    return ImageFont.load_default()


FONT_FALLBACK_PATHS = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/CJKSymbolsFallback.ttc",
    "/System/Library/Fonts/Apple Symbols.ttf",
    "/System/Library/Fonts/Symbol.ttf",
]
COLOR_EMOJI_FONT_PATH = "/System/Library/Fonts/Apple Color Emoji.ttc"
COLOR_EMOJI_SIZES = (20, 32, 40, 48, 64, 96, 160)
MISSING_GLYPH_PROBE = "\U0010ffff"
VARIATION_SELECTOR_RANGES = (
    (0xFE00, 0xFE0F),
    (0xE0100, 0xE01EF),
)
ZERO_WIDTH_JOINER = "\u200d"
_GLYPH_SUPPORT_CACHE: dict[tuple[str, int, str], bool] = {}


@lru_cache(maxsize=128)
def load_font_path(font_path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path, size)


def nearest_color_emoji_size(size: int) -> int:
    return min(COLOR_EMOJI_SIZES, key=lambda candidate: (abs(candidate - size), 0 if candidate >= size else 1))


def load_color_emoji_font(size: int) -> ImageFont.FreeTypeFont | None:
    emoji_path = Path(COLOR_EMOJI_FONT_PATH)
    if not emoji_path.exists():
        return None
    for candidate_size in [nearest_color_emoji_size(size), *COLOR_EMOJI_SIZES]:
        try:
            return load_font_path(str(emoji_path), candidate_size)
        except Exception:
            continue
    return None


def is_variation_selector(char: str) -> bool:
    if not char:
        return False
    codepoint = ord(char)
    return any(start <= codepoint <= end for start, end in VARIATION_SELECTOR_RANGES)


def iter_text_units(text: str):
    unit = ""
    join_next = False
    for char in text:
        if not unit:
            unit = char
            join_next = char == ZERO_WIDTH_JOINER
            continue
        if join_next or char == ZERO_WIDTH_JOINER or is_variation_selector(char) or unicodedata.combining(char):
            unit += char
        else:
            yield unit
            unit = char
        join_next = char == ZERO_WIDTH_JOINER
    if unit:
        yield unit


def font_cache_key(font, text_unit: str) -> tuple[str, int, str]:
    return (str(getattr(font, "path", "")), int(getattr(font, "size", 0) or 0), text_unit)


def glyph_signature(font, text: str) -> tuple[tuple[int, int], bytes] | None:
    try:
        mask = font.getmask(text)
        return mask.size, bytes(mask)
    except Exception:
        return None


def font_supports_text_unit(font, text_unit: str) -> bool:
    if not text_unit or text_unit.isspace():
        return True
    key = font_cache_key(font, text_unit)
    if key in _GLYPH_SUPPORT_CACHE:
        return _GLYPH_SUPPORT_CACHE[key]
    signature = glyph_signature(font, text_unit)
    missing_signature = glyph_signature(font, MISSING_GLYPH_PROBE)
    supported = bool(signature and signature != missing_signature and signature[0][1] > 0)
    _GLYPH_SUPPORT_CACHE[key] = supported
    return supported


def fallback_font_candidates(size: int, bold: bool = False):
    seen = set()
    for font_path in FONT_FALLBACK_PATHS:
        if not font_path or not Path(font_path).exists() or font_path in seen:
            continue
        seen.add(font_path)
        try:
            yield load_font_path(font_path, size)
        except Exception:
            continue
    emoji_font = load_color_emoji_font(size)
    if emoji_font is not None:
        yield emoji_font


def resolve_font_for_text_unit(text_unit: str, base_font, bold: bool = False):
    if font_supports_text_unit(base_font, text_unit):
        return base_font
    size = int(getattr(base_font, "size", 24) or 24)
    for fallback_font in fallback_font_candidates(size, bold=bold):
        if font_supports_text_unit(fallback_font, text_unit):
            return fallback_font
    return base_font


def is_color_emoji_font(font) -> bool:
    return Path(str(getattr(font, "path", ""))).name == Path(COLOR_EMOJI_FONT_PATH).name


def text_unit_advance(draw: ImageDraw.ImageDraw, text_unit: str, font) -> float:
    try:
        advance = draw.textlength(text_unit, font=font)
        if advance > 0:
            return float(advance)
    except Exception:
        pass
    bbox = draw.textbbox((0, 0), text_unit, font=font)
    return float(max(bbox[2] - bbox[0], 0))


def textbbox_with_fallback(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font,
    fill=None,
    bold: bool = False,
) -> tuple[float, float, float, float]:
    x, y = xy
    cursor = float(x)
    left = float(x)
    top = float(y)
    right = float(x)
    bottom = float(y)
    has_visible = False
    for text_unit in iter_text_units(text):
        unit_font = resolve_font_for_text_unit(text_unit, font, bold=bold)
        try:
            bbox = draw.textbbox((cursor, y), text_unit, font=unit_font)
        except Exception:
            bbox = (cursor, y, cursor, y)
        advance = text_unit_advance(draw, text_unit, unit_font)
        if not text_unit.isspace() and bbox[2] > bbox[0] and bbox[3] > bbox[1]:
            left = min(left, float(bbox[0])) if has_visible else float(bbox[0])
            top = min(top, float(bbox[1])) if has_visible else float(bbox[1])
            right = max(right, float(bbox[2])) if has_visible else float(bbox[2])
            bottom = max(bottom, float(bbox[3])) if has_visible else float(bbox[3])
            has_visible = True
        cursor += advance
        right = max(right, cursor)
    if not has_visible:
        bottom = y + int(getattr(font, "size", 0) or 0)
    return (left, top, right, bottom)


def draw_text_with_fallback(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font,
    fill,
    bold: bool = False,
) -> None:
    x, y = xy
    cursor = float(x)
    base_bbox = draw.textbbox((0, 0), "国", font=font)
    base_h = max(base_bbox[3] - base_bbox[1], int(getattr(font, "size", 0) or 0))
    for text_unit in iter_text_units(text):
        unit_font = resolve_font_for_text_unit(text_unit, font, bold=bold)
        unit_bbox = draw.textbbox((0, 0), text_unit, font=unit_font)
        unit_h = unit_bbox[3] - unit_bbox[1]
        unit_y = float(y)
        if unit_h > 0 and unit_font is not font:
            unit_y += (base_h - unit_h) / 2
        kwargs = {"embedded_color": True} if is_color_emoji_font(unit_font) else {}
        draw.text((cursor, unit_y), text_unit, font=unit_font, fill=fill, **kwargs)
        cursor += text_unit_advance(draw, text_unit, unit_font)


def build_alignment_target(cell_size: tuple[int, int]) -> tuple[float, np.ndarray]:
    width, height = cell_size
    return (
        width * ALIGN_TARGET_EYE_DISTANCE_RATIO,
        np.array([width / 2, height * ALIGN_TARGET_EYE_CENTER_Y_RATIO], dtype=np.float64),
    )


def infer_phase(text: str) -> str | None:
    normalized = (text or "").strip().lower()
    if "术前" in normalized or "治疗前" in normalized or "操作前" in normalized or "项目前" in normalized or "注射前" in normalized:
        return "before"
    if "术后" in normalized or "治疗后" in normalized or "操作后" in normalized or "项目后" in normalized or "注射后" in normalized:
        return "after"
    return None


def phase_from_dir_name(name: str | None) -> str | None:
    normalized = (name or "").strip().lower()
    if not normalized:
        return None
    for phase, aliases in PHASE_DIR_ALIASES.items():
        if normalized in aliases:
            return phase
    return None


def infer_phase_from_path(file_path: Path, case_root: Path) -> tuple[str | None, str | None]:
    try:
        parts = file_path.relative_to(case_root).parts[:-1]
    except ValueError:
        parts = file_path.parts[:-1]
    for part in parts:
        phase = phase_from_dir_name(part)
        if phase:
            return phase, part
    return None, None


def split_focus_token(raw: str) -> tuple[str, str]:
    text = (raw or "").strip()
    for sep in ("：", ":"):
        if sep in text:
            area, effect = text.split(sep, 1)
            area = area.strip()
            effect = effect.strip()
            if area and effect:
                return area, effect
    raise ValueError(f"非法 --focus 格式: {raw}，应为 <部位>:<效果>")


def infer_angle_order_for_area(area: str) -> tuple[list[str], str]:
    for rule in AREA_ANGLE_PRIORITY_RULES:
        if any(keyword in area for keyword in rule["keywords"]):
            return list(rule["order"]), rule["profile"]
    return list(NEUTRAL_ANGLE_ORDER), "neutral"


def parse_focus_targets(raw_items: list[str] | None) -> list[dict]:
    items = [item for item in (raw_items or []) if (item or "").strip()]
    parsed = []
    total = len(items)
    for idx, raw in enumerate(items):
        area, effect = split_focus_token(raw)
        angle_order, profile = infer_angle_order_for_area(area)
        parsed.append({
            "area": area,
            "effect": effect,
            "priority": idx + 1,
            "weight": max(total - idx, 1),
            "angle_order": angle_order,
            "angle_profile": profile,
        })
    return parsed


def build_angle_priority_profile(focus_targets: list[dict]) -> dict:
    scores = {slot: 0 for slot in ANGLE_SLOTS}
    if not focus_targets:
        for rank, slot in enumerate(NEUTRAL_ANGLE_ORDER):
            scores[slot] = 3 - rank
        return {
            "source": "default",
            "slot_scores": scores,
            "preferred_slots": list(NEUTRAL_ANGLE_ORDER),
        }

    for target in focus_targets:
        weight = int(target.get("weight") or 1)
        for rank, slot in enumerate(target["angle_order"]):
            scores[slot] += weight * (3 - rank)

    preferred_slots = sorted(
        ANGLE_SLOTS,
        key=lambda slot: (-scores[slot], NEUTRAL_ANGLE_ORDER.index(slot)),
    )
    return {
        "source": "focus",
        "slot_scores": scores,
        "preferred_slots": preferred_slots,
        "targets": [
            {
                "area": item["area"],
                "effect": item["effect"],
                "priority": item["priority"],
                "weight": item["weight"],
                "angle_order": item["angle_order"],
                "angle_profile": item["angle_profile"],
            }
            for item in focus_targets
        ],
    }


def normalize_semantic_judge_mode(raw: str | None) -> str:
    mode = (raw or "auto").strip().lower()
    if mode not in SEMANTIC_JUDGE_MODES:
        raise ValueError(f"不支持的 --semantic-judge: {raw}，可选 off|auto")
    return mode


def build_semantic_context(mode: str) -> dict:
    normalized = normalize_semantic_judge_mode(mode)
    return {
        "mode": normalized,
        "screen_cache": {},
        "pair_cache": {},
        "final_qa_cache": {},
        "errors": [],
        "summary": {
            "screen_calls": 0,
            "screen_applied": 0,
            "pair_calls": 0,
            "pair_reviews": 0,
            "pair_rejects": 0,
            "final_calls": 0,
            "final_reviews": 0,
            "final_rejects": 0,
        },
    }


def format_timeout_seconds(seconds: float) -> str:
    if float(seconds).is_integer():
        return f"{int(seconds)}s"
    return f"{seconds:g}s"


def semantic_subprocess_timeout_seconds(stage: str) -> float:
    stage_env = {
        "screen": "CASE_LAYOUT_SCREEN_TIMEOUT_SEC",
        "pair_review": "CASE_LAYOUT_PAIR_REVIEW_TIMEOUT_SEC",
        "final_qa": "CASE_LAYOUT_FINAL_QA_TIMEOUT_SEC",
    }.get(stage)
    env_names = [name for name in [stage_env, "CASE_LAYOUT_SEMANTIC_TIMEOUT_SEC"] if name]
    for env_name in env_names:
        raw = os.environ.get(env_name)
        if raw is None or not raw.strip():
            continue
        try:
            timeout_sec = float(raw)
        except ValueError as exc:
            raise ValueError(f"{env_name} 必须是秒数，当前为: {raw}") from exc
        if timeout_sec <= 0:
            raise ValueError(f"{env_name} 必须大于 0，当前为: {raw}")
        return timeout_sec
    return DEFAULT_SEMANTIC_TIMEOUT_SEC


def record_semantic_error(context: dict | None, stage: str, target: str, error: str) -> None:
    if not context:
        return
    context["errors"].append({
        "stage": stage,
        "target": target,
        "error": error,
    })


def semantic_summary(context: dict | None) -> dict:
    if not context:
        return {
            "screen_calls": 0,
            "screen_applied": 0,
            "pair_calls": 0,
            "pair_reviews": 0,
            "pair_rejects": 0,
            "final_calls": 0,
            "final_reviews": 0,
            "final_rejects": 0,
        }
    return {
        **context.get("summary", {}),
    }


def phase_guess_to_phase(value: str | None) -> str | None:
    if value == "术前":
        return "before"
    if value == "术后":
        return "after"
    return None


def semantic_confidence_score(label: str | None) -> float:
    return {
        "high": 0.88,
        "medium": 0.68,
        "low": 0.42,
    }.get((label or "").strip().lower(), 0.0)


def normalize_semantic_screen_payload(payload: dict, image_path: str) -> dict:
    confidence = str(payload.get("confidence") or "low").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    return {
        "image_path": image_path,
        "phase_guess": payload.get("phase_guess") or "不确定",
        "view_guess": payload.get("view_guess") or "其他",
        "subject": payload.get("subject") or "其他",
        "quality": payload.get("quality") or "poor",
        "usable": bool(payload.get("usable", False)),
        "confidence": confidence,
        "direction_guess": payload.get("direction_guess") or "unknown",
        "reason": str(payload.get("reason") or payload.get("error") or "").strip(),
        "error": payload.get("error"),
    }


def normalize_pair_review_payload(payload: dict) -> dict:
    same_person_likely = bool(payload.get("same_person_likely"))
    comparable_view = bool(payload.get("comparable_view"))
    focus_visible = bool(payload.get("focus_visible", True))
    non_target_drift_risk = bool(payload.get("non_target_drift_risk"))

    decision = str(payload.get("decision") or "pass").strip().lower()
    if decision not in {"pass", "review", "reject"}:
        decision = "pass"
    return {
        "same_person_likely": same_person_likely,
        "comparable_view": comparable_view,
        "focus_visible": focus_visible,
        "non_target_drift_risk": non_target_drift_risk,
        "decision": decision,
        "reason": str(payload.get("reason") or "").strip() or "未提供原因",
    }


def normalize_final_qa_payload(payload: dict) -> dict:
    left_right_ok = bool(payload.get("left_right_ok"))
    labels_ok = bool(payload.get("labels_ok"))
    focus_present = bool(payload.get("focus_present", True))
    enhancement_drift_ok = bool(payload.get("enhancement_drift_ok", True))

    decision = str(payload.get("decision") or "pass").strip().lower()
    if decision not in {"pass", "review", "reject"}:
        decision = "pass"
    return {
        "left_right_ok": left_right_ok,
        "labels_ok": labels_ok,
        "focus_present": focus_present,
        "enhancement_drift_ok": enhancement_drift_ok,
        "decision": decision,
        "reason": str(payload.get("reason") or "").strip() or "未提供原因",
    }


def run_semantic_screen(image_path: str) -> dict:
    timeout_sec = semantic_subprocess_timeout_seconds("screen")
    try:
        proc = subprocess.run(
            ["node", str(SEMANTIC_SCREEN_SCRIPT), image_path],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            check=False,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"单图语义补判超时({format_timeout_seconds(timeout_sec)})") from exc
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "单图语义补判失败")
    parsed = json.loads(proc.stdout)
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("单图语义补判未返回数组")
    return normalize_semantic_screen_payload(parsed[0], image_path)


def safe_run_semantic_screen(image_path: str, context: dict | None) -> dict | None:
    if not context or context.get("mode") != "auto":
        return None
    cache_key = str(Path(image_path).resolve())
    if cache_key in context["screen_cache"]:
        return context["screen_cache"][cache_key]
    try:
        context["summary"]["screen_calls"] += 1
        result = run_semantic_screen(cache_key)
        context["screen_cache"][cache_key] = result
        return result
    except Exception as exc:
        record_semantic_error(context, "screen", cache_key, str(exc))
        context["screen_cache"][cache_key] = None
        return None


def run_semantic_pair_review(before_path: str, after_path: str, slot: str, focus_targets: list[dict]) -> dict:
    focus_args = []
    for item in focus_targets:
        focus_args.extend(["--focus", f"{item['area']}:{item['effect']}"])
    timeout_sec = semantic_subprocess_timeout_seconds("pair_review")
    try:
        proc = subprocess.run(
            [
                "node",
                str(SEMANTIC_PAIR_REVIEW_SCRIPT),
                "--before",
                before_path,
                "--after",
                after_path,
                "--slot",
                ANGLE_LABELS.get(slot, slot),
                *focus_args,
            ],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            check=False,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"pair 语义复核超时({format_timeout_seconds(timeout_sec)})") from exc
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "pair 语义复核失败")
    return normalize_pair_review_payload(json.loads(proc.stdout))


def safe_run_semantic_pair_review(before_path: str, after_path: str, slot: str, focus_targets: list[dict], context: dict | None) -> dict | None:
    if not context or context.get("mode") != "auto":
        return None
    cache_key = "||".join([
        str(Path(before_path).resolve()),
        str(Path(after_path).resolve()),
        slot,
        "|".join(f"{item['area']}:{item['effect']}" for item in focus_targets),
    ])
    if cache_key in context["pair_cache"]:
        return context["pair_cache"][cache_key]
    try:
        context["summary"]["pair_calls"] += 1
        result = run_semantic_pair_review(before_path, after_path, slot, focus_targets)
        context["pair_cache"][cache_key] = result
        if result["decision"] == "review":
            context["summary"]["pair_reviews"] += 1
        elif result["decision"] == "reject":
            context["summary"]["pair_rejects"] += 1
        return result
    except Exception as exc:
        record_semantic_error(context, "pair_review", cache_key, str(exc))
        context["pair_cache"][cache_key] = None
        return None


def build_semantic_final_reference_paths(manifest: dict) -> list[str]:
    refs = []
    preview_original_path = (manifest.get("outputs") or {}).get("preview_original_path")
    if preview_original_path and Path(preview_original_path).exists():
        refs.append(str(Path(preview_original_path).resolve()))

    for group in manifest.get("groups") or []:
        for slot in group.get("render_slots") or []:
            selected = (group.get("selected_slots") or {}).get(slot)
            if not selected:
                continue
            for key in ("before", "after"):
                path_value = selected[key].get("path")
                if path_value and Path(path_value).exists():
                    resolved = str(Path(path_value).resolve())
                    if resolved not in refs:
                        refs.append(resolved)
                if len(refs) >= 4:
                    return refs
    return refs


def run_semantic_final_qa(board_path: str, reference_paths: list[str], focus_targets: list[dict]) -> dict:
    focus_args = []
    for item in focus_targets:
        focus_args.extend(["--focus", f"{item['area']}:{item['effect']}"])
    ref_args = []
    for ref_path in reference_paths:
        ref_args.extend(["--reference", ref_path])
    timeout_sec = semantic_subprocess_timeout_seconds("final_qa")
    try:
        proc = subprocess.run(
            [
                "node",
                str(SEMANTIC_FINAL_QA_SCRIPT),
                "--board",
                board_path,
                *ref_args,
                *focus_args,
            ],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            check=False,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"最终质检超时({format_timeout_seconds(timeout_sec)})") from exc
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "最终质检失败")
    return normalize_final_qa_payload(json.loads(proc.stdout))


def safe_run_semantic_final_qa(board_path: str, reference_paths: list[str], focus_targets: list[dict], context: dict | None) -> dict | None:
    if not context or context.get("mode") != "auto":
        return None
    cache_key = "||".join([
        str(Path(board_path).resolve()),
        *[str(Path(item).resolve()) for item in reference_paths],
        "|".join(f"{item['area']}:{item['effect']}" for item in focus_targets),
    ])
    if cache_key in context["final_qa_cache"]:
        return context["final_qa_cache"][cache_key]
    try:
        context["summary"]["final_calls"] += 1
        result = run_semantic_final_qa(board_path, reference_paths, focus_targets)
        context["final_qa_cache"][cache_key] = result
        if result["decision"] == "review":
            context["summary"]["final_reviews"] += 1
        elif result["decision"] == "reject":
            context["summary"]["final_rejects"] += 1
        return result
    except Exception as exc:
        record_semantic_error(context, "final_qa", cache_key, str(exc))
        context["final_qa_cache"][cache_key] = None
        return None


def should_run_semantic_screen(*, phase: str | None, angle: str | None, angle_source: str | None, angle_confidence: float, sharpness_level: str | None, face_detected: bool) -> bool:
    return True


def maybe_apply_semantic_screen(item: dict, semantic_screen: dict | None, context: dict | None) -> None:
    if not semantic_screen:
        return
    item["semantic_screen"] = semantic_screen
    confidence = semantic_screen.get("confidence")
    if confidence in {"medium", "low"}:
        item["issues"].append(
            f"视觉补判仅供参考(confidence={confidence})：{semantic_screen.get('reason') or semantic_screen.get('view_guess') or '无'}"
        )

    explicit_angle = bool(item.get("filename_angle") or item.get("compare_hint_angle"))
    explicit_phase = item.get("phase_source") in {"filename", "path"}
    high_conf_face = (
        confidence == "high"
        and semantic_screen.get("subject") == "面部"
        and bool(semantic_screen.get("usable"))
    )

    if not explicit_phase and not item.get("phase") and high_conf_face:
        semantic_phase = phase_guess_to_phase(semantic_screen.get("phase_guess"))
        if semantic_phase:
            item["phase"] = semantic_phase
            item["phase_source"] = "semantic_screen"
            item["semantic_applied_fields"].append("phase")

    semantic_slot = SEMANTIC_VIEW_TO_SLOT.get(semantic_screen.get("view_guess"))
    direction_guess = semantic_screen.get("direction_guess") or "unknown"
    if semantic_slot and high_conf_face:
        item["angle"] = semantic_slot
        item["angle_source"] = "semantic_screen"
        item["angle_confidence"] = round(semantic_confidence_score(confidence), 4)
        if semantic_slot == "front":
            item["direction"] = "center"
        elif direction_guess != "unknown":
            item["direction"] = direction_guess
        elif (item.get("view") or {}).get("direction"):
            item["direction"] = item["view"]["direction"]
        if "angle" not in item["semantic_applied_fields"]:
            item["semantic_applied_fields"].append("angle")

    if item["semantic_applied_fields"] and context:
        context["summary"]["screen_applied"] += 1

    if (
        not explicit_angle
        and confidence == "high"
        and (
            semantic_screen.get("subject") != "面部"
            or not bool(semantic_screen.get("usable"))
            or semantic_screen.get("view_guess") in {"局部", "其他"}
        )
    ):
        item["issues"].append(
            f"视觉补判认为当前图片不适合面部标准对比：{semantic_screen.get('reason') or semantic_screen.get('subject')}"
        )
        item["rejection_reason"] = item["rejection_reason"] or "semantic_screen_rejected"


def slugify_token(text: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", text.strip())
    normalized = normalized.strip("-")
    return normalized or "group"


def compute_pose_delta(before_pose: dict | None, after_pose: dict | None) -> dict:
    before_pose = before_pose or {}
    after_pose = after_pose or {}
    yaw = abs(float(before_pose.get("yaw", 0.0)) - float(after_pose.get("yaw", 0.0)))
    pitch = abs(float(before_pose.get("pitch", 0.0)) - float(after_pose.get("pitch", 0.0)))
    roll = abs(float(before_pose.get("roll", 0.0)) - float(after_pose.get("roll", 0.0)))
    weighted = yaw + pitch + roll * 0.5
    return {
        "yaw": round(yaw, 2),
        "pitch": round(pitch, 2),
        "roll": round(roll, 2),
        "weighted": round(weighted, 2),
    }


def pose_delta_within_threshold(slot: str, pose_delta: dict) -> bool:
    limits = POSE_DELTA_THRESHOLDS[slot]
    return (
        pose_delta["yaw"] <= limits["yaw"]
        and pose_delta["pitch"] <= limits["pitch"]
        and pose_delta["roll"] <= limits["roll"]
        and pose_delta["weighted"] <= limits["weighted"]
    )


def format_pose_delta_threshold(slot: str) -> str:
    limits = POSE_DELTA_THRESHOLDS[slot]
    return (
        f"yaw<={limits['yaw']:.1f}, pitch<={limits['pitch']:.1f}, "
        f"roll<={limits['roll']:.1f}, weighted<={limits['weighted']:.1f}"
    )


def rejection_entry(
    *,
    group_name: str,
    slot: str | None,
    phase: str | None,
    reason: str,
    detail: str,
    file_path: str | None = None,
) -> dict:
    return {
        "group_name": group_name,
        "slot": slot,
        "phase": phase,
        "reason": reason,
        "detail": detail,
        "file_path": file_path,
    }


def extract_index(text: str) -> int | None:
    candidates = re.findall(r"(\d+)", text)
    if not candidates:
        return None
    return int(candidates[-1])


def parse_angle_hint(text: str | None) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None
    basename = os.path.basename(raw)

    angle = None
    if OBLIQUE_RE.search(basename):
        angle = "oblique"
    elif SIDE_RE.search(basename):
        angle = "side"
    elif FRONT_RE.search(basename):
        angle = "front"

    if not angle:
        return None

    if angle == "front":
        direction = "center"
    elif LEFT_RE.search(basename):
        direction = "left"
    elif RIGHT_RE.search(basename):
        direction = "right"
    else:
        direction = "unspecified"

    return {
        "angle": angle,
        "direction": direction,
        "raw": raw,
    }


def parse_body_section_hint(text: str | None) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    basename = os.path.basename(raw)
    if BACK_RE.search(basename):
        return "back"
    if OBLIQUE_RE.search(basename):
        return "oblique"
    if SIDE_RE.search(basename):
        return "side"
    if FRONT_RE.search(basename):
        return "front"
    return None


def infer_body_section_from_index(index: int | None, sections: list[str]) -> str | None:
    if not index or index < 1:
        return None
    if index > len(sections):
        return None
    return sections[index - 1]


def discover_group_dirs(case_dir: Path) -> list[Path]:
    case_dir = case_dir.resolve()
    subdirs = [
        item
        for item in sorted(case_dir.iterdir())
        if item.is_dir() and not item.name.startswith(".") and item.name != ".case-layout-board"
    ]
    phase_subdirs = [item for item in subdirs if phase_from_dir_name(item.name) and any(is_image_file(p) for p in item.rglob("*"))]
    if phase_subdirs:
        return [case_dir]

    img_subdirs = [item for item in subdirs if any(is_image_file(p) for p in item.rglob("*"))]
    if img_subdirs:
        return img_subdirs

    if any(is_image_file(p) for p in case_dir.rglob("*")):
        return [case_dir]
    raise ValueError(f"目录内未发现图片: {case_dir}")


def detect_case_mode(case_dir: Path) -> str:
    text = str(case_dir)
    if any(keyword in text for keyword in BODY_CASE_KEYWORDS):
        return "body"
    return "face"


def body_section_priority(case_dir: Path) -> tuple[list[str], str]:
    text = str(case_dir)
    for rule in BODY_SECTION_PRIORITY_RULES:
        if any(keyword in text for keyword in rule["keywords"]):
            return list(rule["sections"]), rule["template"]
    return ["front", "back"], "body-dual-compare"


def parse_case_meta(case_dir: Path) -> dict:
    customer_name = case_dir.parent.name
    case_name = case_dir.name
    date_match = re.search(r"(20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}|\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2})", case_name)
    date_text = date_match.group(1).replace("-", ".").replace("/", ".") if date_match else ""
    project = case_name
    if date_match:
        project = case_name.replace(date_match.group(1), "").strip(" -_，,、")
    return {
        "customer_name": customer_name,
        "date": date_text or case_name[:10],
        "project": project or case_name,
    }


def body_mode_for_section(section: str, case_dir: Path) -> str:
    text = str(case_dir)
    if "颈纹" in text and section in {"front", "oblique", "side"}:
        return "neck"
    return {
        "front": "front",
        "back": "back",
        "oblique": "oblique",
        "side": "side",
    }.get(section, "front")


def build_compare_hint_map(group_dir: Path) -> dict[int, dict]:
    hints = {}
    for file_path in sorted(group_dir.rglob("*")):
        if is_generated_case_layout_path(file_path, group_dir):
            continue
        if not is_image_file(file_path):
            continue
        stem = file_path.stem
        if "术前术后对比图" not in stem:
            continue
        match = COMPARE_HINT_RE.search(stem)
        if not match:
            continue
        idx = match.group(1)
        detail = (match.group(2) or "").strip()
        angle = parse_angle_hint(detail)
        if idx and angle:
            hints[int(idx)] = angle
    return hints


def build_crop_box(face_info: dict) -> dict:
    eye_center = np.asarray(face_info["eye_center"], dtype=np.float64)
    eye_distance = float(face_info["eye_distance"])
    face_height = float(face_info["face_height"])
    width, height = face_info["size"]

    box_w = max(eye_distance * 3.1, face_height * 1.25)
    box_h = max(face_height * 1.18, eye_distance * 3.0)
    x1 = int(round(eye_center[0] - box_w / 2))
    y1 = int(round(eye_center[1] - box_h * 0.36))
    x2 = int(round(x1 + box_w))
    y2 = int(round(y1 + box_h))

    x1 = max(0, min(width - 1, x1))
    y1 = max(0, min(height - 1, y1))
    x2 = max(x1 + 1, min(width, x2))
    y2 = max(y1 + 1, min(height, y2))

    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}


def measure_sharpness(image_bgr: np.ndarray) -> float:
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def classify_sharpness(score: float) -> str:
    if score < SHARPNESS_BLURRY_THRESHOLD:
        return "blurry"
    if score < SHARPNESS_SOFT_THRESHOLD:
        return "soft"
    return "clear"


def analyze_image(file_path: Path, group_dir: Path, hint_map: dict[int, dict], case_root: Path, semantic_context: dict | None = None) -> dict:
    stem = file_path.stem
    phase = infer_phase(stem)
    path_phase, path_phase_dir = infer_phase_from_path(file_path, case_root)
    if not phase and path_phase:
        phase = path_phase
    index = extract_index(stem)
    filename_hint = parse_angle_hint(stem)
    compare_hint = hint_map.get(index)

    item = {
        "name": file_path.name,
        "path": str(file_path.resolve()),
        "relative_path": relpath(file_path, case_root),
        "group_relative_path": relpath(file_path, group_dir),
        "phase": phase,
        "phase_source": "filename" if infer_phase(stem) else ("path" if phase else None),
        "phase_source_path": path_phase_dir,
        "index": index,
        "filename_angle": filename_hint["angle"] if filename_hint else None,
        "filename_direction": filename_hint["direction"] if filename_hint else None,
        "compare_hint_angle": compare_hint["angle"] if compare_hint else None,
        "compare_hint_direction": compare_hint["direction"] if compare_hint else None,
        "angle": None,
        "angle_source": None,
        "angle_confidence": 0.0,
        "direction": None,
        "pose": None,
        "view": None,
        "crop_box": None,
        "sharpness_score": 0.0,
        "sharpness_level": None,
        "issues": [],
        "rejection_reason": None,
        "semantic_screen": None,
        "semantic_applied_fields": [],
        "profile_fallback": None,
    }

    try:
        face = FACE_ALIGN.detect_face_landmarks(str(file_path))
    except Exception as exc:
        landmark_error = str(exc)
        semantic_screen = None
        if semantic_context and semantic_context.get("mode") == "auto":
            semantic_screen = safe_run_semantic_screen(str(file_path), semantic_context)
            maybe_apply_semantic_screen(item, semantic_screen, semantic_context)
        semantic_slot = SEMANTIC_VIEW_TO_SLOT.get((semantic_screen or {}).get("view_guess"))
        wants_profile = (
            (filename_hint and filename_hint.get("angle") == "side")
            or (compare_hint and compare_hint.get("angle") == "side")
            or semantic_slot == "side"
        )
        if wants_profile:
            try:
                face = detect_profile_fallback_face(str(file_path))
                item["profile_fallback"] = {
                    "status": "hit",
                    "source": face.get("fallback") or "profile",
                    "landmark_error": landmark_error,
                }
                item["issues"].append(f"正脸检测失败，已使用侧脸检测兜底: {landmark_error}")
            except Exception as profile_exc:
                item["profile_fallback"] = {
                    "status": "miss",
                    "source": "profile-cascade",
                    "landmark_error": landmark_error,
                    "error": str(profile_exc),
                }
                item["issues"].append(f"面部检测失败: {landmark_error}")
                item["issues"].append(f"侧脸检测兜底失败: {profile_exc}")
                if not item.get("phase"):
                    item["issues"].append("文件名缺少术前/术后关键词")
                    item["rejection_reason"] = item["rejection_reason"] or "phase_missing"
                item["rejection_reason"] = "face_detection_failure"
                return item
        else:
            item["profile_fallback"] = {
                "status": "skipped",
                "source": "profile-cascade",
                "landmark_error": landmark_error,
                "reason": "not_side_candidate",
            }
            item["issues"].append(f"面部检测失败: {landmark_error}")
            if not item.get("phase"):
                item["issues"].append("文件名缺少术前/术后关键词")
                item["rejection_reason"] = item["rejection_reason"] or "phase_missing"
            item["rejection_reason"] = "face_detection_failure"
            return item

    pose = face.get("pose") or {}
    view = face.get("view") or {}
    angle = None
    angle_source = None
    direction = None
    confidence = 0.0

    if filename_hint:
        angle = filename_hint["angle"]
        angle_source = "filename"
        direction = filename_hint["direction"]
        confidence = 1.0
    elif compare_hint:
        angle = compare_hint["angle"]
        angle_source = "compare_hint"
        direction = compare_hint["direction"]
        confidence = 0.95
    elif view.get("bucket"):
        angle = view["bucket"]
        angle_source = "profile_fallback" if (item.get("profile_fallback") or {}).get("status") == "hit" else "pose"
        direction = view.get("direction")
        confidence = float(view.get("confidence", 0.6))

    if direction in {"unspecified", None} and view.get("direction"):
        direction = view["direction"]
    if angle == "front":
        direction = "center"

    item["pose"] = {
        "pitch": round(float(pose.get("pitch", 0.0)), 2),
        "yaw": round(float(pose.get("yaw", 0.0)), 2),
        "roll": round(float(pose.get("roll", 0.0)), 2),
    }
    item["view"] = {
        "bucket": view.get("bucket"),
        "direction": view.get("direction"),
        "confidence": round(float(view.get("confidence", 0.0)), 4),
    }
    item["crop_box"] = build_crop_box(face)
    item["angle"] = angle
    item["angle_source"] = angle_source
    item["angle_confidence"] = round(confidence, 4)
    item["direction"] = direction or "unknown"
    sharpness_score = measure_sharpness(face["image"])
    item["sharpness_score"] = round(sharpness_score, 2)
    item["sharpness_level"] = classify_sharpness(sharpness_score)

    if semantic_context and semantic_context.get("mode") == "auto":
        semantic_screen = None
        if should_run_semantic_screen(
            phase=item.get("phase"),
            angle=item.get("angle"),
            angle_source=item.get("angle_source"),
            angle_confidence=float(item.get("angle_confidence") or 0.0),
            sharpness_level=item.get("sharpness_level"),
            face_detected=True,
        ):
            semantic_screen = safe_run_semantic_screen(str(file_path), semantic_context)
        maybe_apply_semantic_screen(item, semantic_screen, semantic_context)

    if not item.get("phase"):
        item["issues"].append("文件名缺少术前/术后关键词")
        item["rejection_reason"] = item["rejection_reason"] or "phase_missing"
    if not item.get("angle"):
        item["issues"].append("无法判定角度")
        item["rejection_reason"] = item["rejection_reason"] or "angle_unknown"
    if item["sharpness_level"] == "blurry":
        item["issues"].append(f"图片过糊，不适合做案例对比图（sharpness={item['sharpness_score']:.2f}）")
        item["rejection_reason"] = item["rejection_reason"] or "blurry_image"
    if not item.get("angle") and item.get("semantic_applied_fields"):
        item["rejection_reason"] = item["rejection_reason"] or "angle_unknown"

    return item


def export_aligned_image(image_path: str, output_path: Path, cell_size: tuple[int, int]) -> Path:
    target_eye_distance, target_eye_center = build_alignment_target(cell_size)
    arr = render_aligned_cell(image_path, cell_size, target_eye_distance, target_eye_center)
    ensure_dir(output_path.parent)
    cv_to_pil(arr).save(output_path, "JPEG", quality=95)
    return output_path


def prepare_enhancement_inputs(
    group_name: str,
    slot: str,
    before_source_path: str,
    after_source_path: str,
    output_root: Path,
) -> dict:
    group_slug = slugify_token(group_name)
    slot_root = ensure_dir(output_root / group_slug / slot)
    before_aligned_path = export_aligned_image(
        before_source_path,
        slot_root / "before-aligned.jpg",
        ENHANCEMENT_INPUT_SIZE,
    )
    after_aligned_path = export_aligned_image(
        after_source_path,
        slot_root / "after-aligned.jpg",
        ENHANCEMENT_INPUT_SIZE,
    )
    return {
        "mode": ENHANCEMENT_INPUT_MODE,
        "cell_size": {
            "width": ENHANCEMENT_INPUT_SIZE[0],
            "height": ENHANCEMENT_INPUT_SIZE[1],
        },
        "background_mode": BACKGROUND_MODE,
        "before_aligned_path": str(before_aligned_path.resolve()),
        "after_aligned_path": str(after_aligned_path.resolve()),
        "before_source_path": before_source_path,
        "after_source_path": after_source_path,
    }


def build_focus_summary(focus_targets: list[dict]) -> str:
    return "；".join(
        f"{item['priority']}. {item['area']} -> {item['effect']}"
        for item in focus_targets
    )


def build_after_enhancement_prompt(group_name: str, slot: str, focus_targets: list[dict], has_pose_ref: bool = False) -> str:
    if not focus_targets:
        raise ValueError("增强提示词缺少 focus_targets")
    pose_note = ""
    if has_pose_ref:
        pose_note = (
            "第二张参考图只用于锁定当前已经标准化后的姿态与构图，"
            "禁止大幅改变抬头角度、下巴高度、鼻尖朝向、颈部展开程度和整体头部姿态；"
            "不得把参考图的人物身份、肤质瑕疵或术前状态替换到术后图。"
        )
    focus_summary = build_focus_summary(focus_targets)
    if os.environ.get("CASE_LAYOUT_ENHANCE_DIRECTION", "strict").strip().lower() == "heal":
        return (
            f"这是医美术后恢复效果预览图，案例组名是“{group_name}”，"
            "用途是让求美者看到接受治疗后、充分恢复消肿后的良好样貌。"
            "输入图已经完成统一的人物位置、大小、裁切和朝向标准化。"
            f"必须严格保持同一人物身份与五官识别特征、同一{ANGLE_LABELS[slot]}角度、"
            f"同一背景、同一构图、同一画面裁切、同一发型与妆容不变，{pose_note}"
            "在身份与构图完全不变的前提下，呈现术后充分恢复、消肿消炎后的健康状态："
            f"重点改善以下确认目标：{focus_summary}；"
            "并可让整体气色更红润健康、肤质更光滑紧致有光泽、消除疲态与术后肿胀痕迹，"
            "呈现自然、年轻、恢复良好的效果。"
            "改善须真实自然、符合本人恢复预期，不得夸张到改变五官几何或身份识别，"
            "不得过度磨皮成塑料质感、不得漂白肤色，必须保留真实毛孔与皮肤纹理。"
            "不要新增文字、边框、饰品、服装变化，"
            "不要改变方向、抬头角、头部大小、背景或画面裁切。"
        )
    return (
        f"这是医美术后案例图，案例组名是“{group_name}”。"
        "输入图已经完成统一的人物位置、大小、裁切和朝向标准化。"
        f"保持同一人物身份、同一{ANGLE_LABELS[slot]}角度、同一背景、同一构图、同一妆容和同一发型不变，"
        f"{pose_note}"
        f"本次只允许围绕以下确认目标做展示增强：{focus_summary}。"
        f"如果当前{ANGLE_LABELS[slot]}角度对某个目标展示有限，也不得转而优化其他部位。"
        "未点名区域只允许极轻的亮度、白平衡和清晰度统一，"
        "严禁处理痘印、斑点、毛孔、肤色瑕疵、纹路分布和非目标轮廓。"
        "必须保持真实医美案例质感，不要夸张，不要磨皮过度，不要改变五官识别特征，"
        "不要新增文字、边框、饰品、服装变化，也不要改变方向、抬头角、头部大小、背景或画面裁切。"
    )


def enhance_selected_after_image(
    group_name: str,
    slot: str,
    source_path: str,
    output_dir: Path,
    focus_targets: list[dict],
    enhance_model: str,
    pose_ref_path: str | None = None,
    source_image_path: str | None = None,
    enhancement_input: dict | None = None,
) -> dict:
    enhancer = Path(__file__).resolve().parent / "case_layout_enhance.js"
    prompt = build_after_enhancement_prompt(group_name, slot, focus_targets, has_pose_ref=bool(pose_ref_path))
    last_error = None
    payload = None
    pose_ref_args = ["--pose-ref", pose_ref_path] if pose_ref_path else []
    for attempt in range(1, 3):
        model_args = ["--model", enhance_model] if enhance_model else []
        proc = subprocess.run(
            [
                "node",
                str(enhancer),
                "--image",
                source_path,
                *pose_ref_args,
                "--quality",
                ENHANCE_QUALITY,
                *model_args,
                "--prompt",
                prompt,
            ],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            check=False,
        )

        if proc.returncode != 0:
            last_error = proc.stderr.strip() or proc.stdout.strip() or "增强器执行失败"
            continue

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            last_error = f"增强器返回非 JSON: {exc}\nstdout={proc.stdout[:400]}"
            continue

        if payload.get("success") and payload.get("imagePath"):
            break

        last_error = f"增强器未生成图片: {json.dumps(payload, ensure_ascii=False)[:500]}"
        payload = None

    if not payload:
        raise RuntimeError(last_error or "增强器未生成图片")

    output_dir = ensure_dir(output_dir)
    target_name = f"{Path(source_path).stem}-{slot}-enhanced.jpg"
    target_path = output_dir / target_name
    shutil.copy2(payload["imagePath"], target_path)

    return {
        "enabled": True,
        "slot": slot,
        "quality": ENHANCE_QUALITY,
        "model": enhance_model or "auto(primary-chain)",
        "input_mode": ENHANCEMENT_INPUT_MODE,
        "scope": ENHANCEMENT_SCOPE,
        "focus_targets": focus_targets,
        "non_target_policy": NON_TARGET_POLICY,
        "prompt": prompt,
        "enhanced_path": str(target_path.resolve()),
        "input_image_path": source_path,
        "source_image_path": source_image_path or source_path,
        "pose_ref_path": pose_ref_path,
        "enhancement_input": enhancement_input or {},
        "attempt_count": attempt,
        "pose_ref_count": int(payload.get("poseRefCount") or 0),
        "planner_used": bool(payload.get("plannerUsed")),
        "planned_tasks": payload.get("plannedTasks") or "",
        "edit_prompt": payload.get("editPrompt") or "",
        "elapsed_seconds": payload.get("elapsedSeconds") or {},
        "degradations": payload.get("degradations") or [],
        "stabilization": payload.get("stabilization") or {},
        "generated_image_path": payload.get("generatedImagePath"),
    }


def candidate_score(item: dict, slot: str, preferred_direction: str | None = None) -> float:
    score = 0.0
    score += ANGLE_SOURCE_PRIORITY.get(item.get("angle_source"), 0) * 100
    score += float(item.get("angle_confidence") or 0) * 25
    if item.get("slot_alias") == "soft_oblique" and slot == "oblique":
        score -= 18

    yaw_abs = abs(float((item.get("pose") or {}).get("yaw", 0.0)))
    score -= abs(yaw_abs - ANGLE_TARGET_YAW[slot]) * 1.5
    score += min(float(item.get("sharpness_score") or 0.0), 60.0)

    direction = item.get("direction")
    if preferred_direction and direction == preferred_direction:
        score += 24
    elif preferred_direction and direction in {"center", "unspecified", "unknown"}:
        score += 8

    if slot == "front" and direction == "center":
        score += 12
    if item.get("sharpness_level") == "blurry":
        score -= 60
    elif item.get("sharpness_level") == "soft":
        score -= 10
    return score


def is_manual_candidate(item: dict | None) -> bool:
    if not isinstance(item, dict):
        return False
    return item.get("phase_source") == "manual" or item.get("angle_source") == "manual"


def has_manual_candidates(candidates: list[dict]) -> bool:
    return any(is_manual_candidate(item) for item in candidates)


def is_manual_pair(pair: dict | None) -> bool:
    if not isinstance(pair, dict):
        return False
    return is_manual_candidate(pair.get("before")) and is_manual_candidate(pair.get("after"))


def build_phase_slot_candidates(entries: list[dict], angle_priority_profile: dict | None = None) -> dict[str, dict[str, list[dict]]]:
    matrix = {
        "before": {slot: [] for slot in ANGLE_SLOTS},
        "after": {slot: [] for slot in ANGLE_SLOTS},
    }
    preferred_slots = (angle_priority_profile or {}).get("preferred_slots") or list(NEUTRAL_ANGLE_ORDER)
    prefer_oblique = preferred_slots.index("oblique") < preferred_slots.index("side")
    for item in entries:
        phase = item.get("phase")
        angle = item.get("angle")
        if phase in matrix and angle in matrix[phase]:
            matrix[phase][angle].append(item)
            if prefer_oblique and angle == "side":
                yaw_abs = abs(float((item.get("pose") or {}).get("yaw", 0.0)))
                if (
                    item.get("angle_source") == "pose"
                    and item.get("direction") not in {"center", "unknown", "unspecified"}
                    and SOFT_OBLIQUE_ALIAS_MIN_YAW <= yaw_abs <= SOFT_OBLIQUE_ALIAS_MAX_YAW
                ):
                    alias = dict(item)
                    alias["slot_alias"] = "soft_oblique"
                    alias["angle"] = "oblique"
                    alias["angle_confidence"] = round(min(float(item.get("angle_confidence") or 0.0), 0.72), 4)
                    matrix[phase]["oblique"].append(alias)
    return matrix


def prune_duplicate_slots(group_name: str, selected_slots: dict[str, dict], angle_priority_profile: dict) -> tuple[dict[str, dict], list[str], list[dict]]:
    preferred_slots = angle_priority_profile.get("preferred_slots") or list(NEUTRAL_ANGLE_ORDER)
    slot_order = {slot: index for index, slot in enumerate(preferred_slots)}
    warnings: list[str] = []
    rejections: list[dict] = []
    kept: dict[str, dict] = {}
    seen_pairs: dict[tuple[str, str], str] = {}

    for slot in sorted(selected_slots.keys(), key=lambda item: slot_order.get(item, 99)):
        selection = selected_slots[slot]
        pair_key = (selection["before"]["path"], selection["after"]["path"])
        existing_slot = seen_pairs.get(pair_key)
        if existing_slot:
            warnings.append(
                f"{selection['before']['name']} / {selection['after']['name']} 同时命中 `{existing_slot}` 和 `{slot}`，已按角度优先级保留 `{existing_slot}`"
            )
            rejections.append(rejection_entry(
                group_name=group_name,
                slot=slot,
                phase=None,
                reason="duplicate_slot_material",
                detail=f"同一对素材已被更高优先级槽位 `{existing_slot}` 占用",
                file_path=None,
            ))
            continue
        kept[slot] = selection
        seen_pairs[pair_key] = slot
    return kept, warnings, rejections


def derive_effective_template(selected_slots: list[str], angle_priority_profile: dict | None = None) -> tuple[str | None, list[str]]:
    slots = [slot for slot in ANGLE_SLOTS if slot in selected_slots]
    if slots == ANGLE_SLOTS:
        return "tri-compare", slots
    if "front" in slots and len(slots) >= 2:
        render_slots = ["front"]
        preferred_slots = (angle_priority_profile or {}).get("preferred_slots") or list(NEUTRAL_ANGLE_ORDER)
        for slot in [item for item in preferred_slots if item != "front"]:
            if slot in slots:
                render_slots.append(slot)
        return "bi-compare", render_slots[:2]
    if slots == ["front"]:
        return "single-compare", ["front"]
    return None, slots


def _slot_from_blocker(blocker: str) -> str | None:
    text = str(blocker or "")
    for slot in ANGLE_SLOTS:
        label = ANGLE_LABELS[slot]
        if label and label in text:
            return slot
    return None


def _downgrade_warning(blocker: str, slot: str) -> str:
    detail = str(blocker or "")
    detail = detail.replace("，已拒绝增强与出图", "，该角度已从本次出图中排除")
    detail = detail.replace("，已拒绝出图", "，该角度已从本次出图中排除")
    detail = detail.replace("，已废弃该角度", "，该角度已从本次出图中排除")
    return f"自动降级已排除 {ANGLE_LABELS[slot]}：{detail}"


def filter_downgrade_blockers(blockers: list[str], render_slots: list[str]) -> tuple[list[str], list[str]]:
    if not render_slots:
        return blockers, []
    kept = []
    downgraded_warnings = []
    render_slot_set = set(render_slots)
    for blocker in blockers:
        blocked_slot = _slot_from_blocker(blocker)
        if blocked_slot and blocked_slot not in render_slot_set:
            downgraded_warnings.append(_downgrade_warning(blocker, blocked_slot))
        else:
            kept.append(blocker)
    return kept, downgraded_warnings


def select_best_candidate(candidates: list[dict], slot: str, preferred_direction: str | None = None) -> dict | None:
    filtered = candidates
    if preferred_direction:
        directional = [
            item for item in candidates
            if item.get("direction") == preferred_direction or item.get("direction") in {"center", "unspecified"}
        ]
        if directional:
            filtered = directional

    if not filtered:
        return None
    ranked = sorted(
        filtered,
        key=lambda item: (
            candidate_score(item, slot, preferred_direction),
            item.get("name"),
        ),
        reverse=True,
    )
    return ranked[0]


def resolve_slot_pair(
    group_name: str,
    slot: str,
    before_candidates: list[dict],
    after_candidates: list[dict],
    brand_id: str | None = None,
    focus_targets: list[dict] | None = None,
    semantic_context: dict | None = None,
) -> tuple[dict | None, list[str], list[str], list[dict]]:
    blocking = []
    warnings = []
    rejections = []
    focus_targets = focus_targets or []

    explicit_before = [item for item in before_candidates if item.get("angle_source") in BLOCKING_DUP_SOURCES]
    explicit_after = [item for item in after_candidates if item.get("angle_source") in BLOCKING_DUP_SOURCES]

    def has_ambiguous_explicit(candidates: list[dict]) -> bool:
        buckets = {}
        for item in candidates:
            direction = item.get("direction") or "unknown"
            if direction == "unspecified":
                direction = "any"
            buckets[direction] = buckets.get(direction, 0) + 1
        return any(count > 1 for count in buckets.values())

    if has_ambiguous_explicit(explicit_before):
        blocking.append(f"{group_name}：术前 {ANGLE_LABELS[slot]} 命中过多显式候选，无法唯一确定")
        rejections.append(rejection_entry(
            group_name=group_name,
            slot=slot,
            phase="before",
            reason="ambiguous_candidates",
            detail=f"术前 {ANGLE_LABELS[slot]} 命中过多显式候选，无法唯一确定",
        ))
    if has_ambiguous_explicit(explicit_after):
        blocking.append(f"{group_name}：术后 {ANGLE_LABELS[slot]} 命中过多显式候选，无法唯一确定")
        rejections.append(rejection_entry(
            group_name=group_name,
            slot=slot,
            phase="after",
            reason="ambiguous_candidates",
            detail=f"术后 {ANGLE_LABELS[slot]} 命中过多显式候选，无法唯一确定",
        ))
    if blocking:
        return None, blocking, warnings, rejections

    if not before_candidates:
        blocking.append(f"{group_name}：缺少术前 {ANGLE_LABELS[slot]}")
        rejections.append(rejection_entry(
            group_name=group_name,
            slot=slot,
            phase="before",
            reason="missing_phase",
            detail=f"缺少术前 {ANGLE_LABELS[slot]}",
        ))
    if not after_candidates:
        blocking.append(f"{group_name}：缺少术后 {ANGLE_LABELS[slot]}")
        rejections.append(rejection_entry(
            group_name=group_name,
            slot=slot,
            phase="after",
            reason="missing_phase",
            detail=f"缺少术后 {ANGLE_LABELS[slot]}",
        ))
    if blocking:
        return None, blocking, warnings, rejections

    preferred_directions = [None]
    if slot != "front":
        before_dirs = {
            item.get("direction")
            for item in before_candidates
            if item.get("direction") and item.get("direction") not in {"center", "unspecified", "unknown"}
        }
        after_dirs = {
            item.get("direction")
            for item in after_candidates
            if item.get("direction") and item.get("direction") not in {"center", "unspecified", "unknown"}
        }
        overlap = sorted(before_dirs & after_dirs)
        if overlap:
            preferred_directions = overlap
        elif has_manual_candidates(before_candidates) and has_manual_candidates(after_candidates):
            warnings.append(
                f"{group_name}：{ANGLE_LABELS[slot]} 人工配对方向不一致，已按人工选择保留该角度，需人工复核"
            )
            preferred_directions = [None]
        else:
            blocking.append(f"{group_name}：{ANGLE_LABELS[slot]} 术前术后方向不一致，已拒绝出图")
            rejections.append(rejection_entry(
                group_name=group_name,
                slot=slot,
                phase=None,
                reason="direction_mismatch",
                detail=(
                    f"{ANGLE_LABELS[slot]} 术前术后方向不一致"
                    f"(before={sorted(before_dirs) or ['unknown']}, after={sorted(after_dirs) or ['unknown']})"
                ),
            ))
            return None, blocking, warnings, rejections

    best_pair = None
    best_score = -10_000.0
    best_direction = None
    for preferred_direction in preferred_directions:
        before_best = select_best_candidate(before_candidates, slot, preferred_direction)
        after_best = select_best_candidate(after_candidates, slot, preferred_direction)
        if not before_best or not after_best:
            continue

        score = candidate_score(before_best, slot, preferred_direction) + candidate_score(after_best, slot, preferred_direction)
        pose_delta = compute_pose_delta(before_best.get("pose"), after_best.get("pose"))
        score -= pose_delta["weighted"] * 1.6
        if preferred_direction and before_best.get("direction") == after_best.get("direction") == preferred_direction:
            score += 18
        if score > best_score:
            best_pair = {"before": before_best, "after": after_best, "pose_delta": pose_delta}
            best_score = score
            best_direction = preferred_direction

    if not best_pair:
        blocking.append(f"{group_name}：{ANGLE_LABELS[slot]} 无法配对出术前/术后同方向样本")
        rejections.append(rejection_entry(
            group_name=group_name,
            slot=slot,
            phase=None,
            reason="direction_mismatch",
            detail=f"{ANGLE_LABELS[slot]} 无法配对出术前/术后同方向样本",
        ))
        return None, blocking, warnings, rejections

    before_sharp = float(best_pair["before"].get("sharpness_score") or 0.0)
    after_sharp = float(best_pair["after"].get("sharpness_score") or 0.0)
    sharp_ratio = min(before_sharp, after_sharp) / max(before_sharp, after_sharp, 1e-6)
    pose_delta = best_pair["pose_delta"]
    manual_pair = is_manual_pair(best_pair)
    if best_pair["before"].get("sharpness_level") == "blurry":
        if manual_pair:
            warnings.append(f"{group_name}：术前 {ANGLE_LABELS[slot]} 过糊，人工配对已保留该角度，需人工复核")
        else:
            blocking.append(f"{group_name}：术前 {ANGLE_LABELS[slot]} 过糊，已废弃该角度")
            rejections.append(rejection_entry(
                group_name=group_name,
                slot=slot,
                phase="before",
                reason="blurry_image",
                detail=f"术前 {ANGLE_LABELS[slot]} 过糊，已废弃该角度",
                file_path=best_pair["before"].get("path"),
            ))
            return None, blocking, warnings, rejections
    if best_pair["after"].get("sharpness_level") == "blurry":
        if manual_pair:
            warnings.append(f"{group_name}：术后 {ANGLE_LABELS[slot]} 过糊，人工配对已保留该角度，需人工复核")
        else:
            blocking.append(f"{group_name}：术后 {ANGLE_LABELS[slot]} 过糊，已废弃该角度")
            rejections.append(rejection_entry(
                group_name=group_name,
                slot=slot,
                phase="after",
                reason="blurry_image",
                detail=f"术后 {ANGLE_LABELS[slot]} 过糊，已废弃该角度",
                file_path=best_pair["after"].get("path"),
            ))
            return None, blocking, warnings, rejections
    if sharp_ratio < PAIR_SHARPNESS_RATIO_THRESHOLD:
        if manual_pair:
            warnings.append(
                f"{group_name}：{ANGLE_LABELS[slot]} 前后清晰度差过大"
                f"(before={before_sharp:.2f}, after={after_sharp:.2f})，人工配对已保留该角度，需人工复核"
            )
        else:
            # MD-AI 品牌允许清晰度跨度较大，仅记录警告不阻塞
            if brand_id == "md_ai":
                warnings.append(
                    f"{group_name}：{ANGLE_LABELS[slot]} 前后清晰度差过大"
                    f"(before={before_sharp:.2f}, after={after_sharp:.2f})，MD-AI 模式已自动豁免并保留该角度"
                )
            else:
                blocking.append(
                    f"{group_name}：{ANGLE_LABELS[slot]} 前后清晰度差过大"
                    f"(before={before_sharp:.2f}, after={after_sharp:.2f})，已废弃该角度"
                )
                rejections.append(rejection_entry(
                    group_name=group_name,
                    slot=slot,
                    phase=None,
                    reason="sharpness_gap",
                    detail=(
                        f"{ANGLE_LABELS[slot]} 前后清晰度差过大"
                        f"(before={before_sharp:.2f}, after={after_sharp:.2f})"
                    ),
                ))
                return None, blocking, warnings, rejections
    if not pose_delta_within_threshold(slot, pose_delta):
        if manual_pair:
            warnings.append(
                f"{group_name}：{ANGLE_LABELS[slot]} 术前术后姿态差过大"
                f"(yaw={pose_delta['yaw']:.2f}, pitch={pose_delta['pitch']:.2f}, "
                f"roll={pose_delta['roll']:.2f}, weighted={pose_delta['weighted']:.2f})，人工配对已保留该角度，需人工复核"
            )
        else:
            blocking.append(
                f"{group_name}：{ANGLE_LABELS[slot]} 术前术后姿态差过大"
                f"(yaw={pose_delta['yaw']:.2f}, pitch={pose_delta['pitch']:.2f}, "
                f"roll={pose_delta['roll']:.2f}, weighted={pose_delta['weighted']:.2f})，该角度已从本次出图中排除"
            )
            rejections.append(rejection_entry(
                group_name=group_name,
                slot=slot,
                phase=None,
                reason="pose_delta_exceeded",
                detail=(
                    f"{ANGLE_LABELS[slot]} 术前术后姿态差过大"
                    f"(yaw={pose_delta['yaw']:.2f}, pitch={pose_delta['pitch']:.2f}, "
                    f"roll={pose_delta['roll']:.2f}, weighted={pose_delta['weighted']:.2f}; "
                    f"threshold={format_pose_delta_threshold(slot)})"
                ),
            ))
            return None, blocking, warnings, rejections

    pose_only_before = [item for item in before_candidates if item.get("angle_source") == "pose"]
    pose_only_after = [item for item in after_candidates if item.get("angle_source") == "pose"]
    if len(pose_only_before) > 1:
        warnings.append(f"{group_name}：术前 {ANGLE_LABELS[slot]} 存在多个姿态推断候选，已按最佳分数择优")
    if len(pose_only_after) > 1:
        warnings.append(f"{group_name}：术后 {ANGLE_LABELS[slot]} 存在多个姿态推断候选，已按最佳分数择优")

    selection = {
        "slot": slot,
        "label": ANGLE_LABELS[slot],
        "direction": "center" if slot == "front" else (best_direction or best_pair["before"].get("direction") or best_pair["after"].get("direction") or "unknown"),
        "pose_delta": pose_delta,
        "semantic_pair_review": None,
        "before": best_pair["before"],
        "after": best_pair["after"],
    }

    should_pair_review = bool(semantic_context and semantic_context.get("mode") == "auto")
    if should_pair_review:
        pair_review = safe_run_semantic_pair_review(
            selection["before"]["path"],
            selection["after"]["path"],
            slot,
            focus_targets,
            semantic_context,
        )
        if pair_review:
            if not pair_review["same_person_likely"] or not pair_review["comparable_view"] or (focus_targets and not pair_review["focus_visible"]):
                pair_review["decision"] = "reject"
            elif pair_review["non_target_drift_risk"]:
                pair_review["decision"] = "review"
            selection["semantic_pair_review"] = pair_review
            if pair_review["decision"] == "reject":
                blocking.append(f"{group_name}：{ANGLE_LABELS[slot]} 语义复核未通过，已拒绝该角度 - {pair_review['reason']}")
                rejections.append(rejection_entry(
                    group_name=group_name,
                    slot=slot,
                    phase=None,
                    reason="semantic_pair_reject",
                    detail=pair_review["reason"],
                ))
                return None, blocking, warnings, rejections
            if pair_review["decision"] == "review":
                warnings.append(f"{group_name}：{ANGLE_LABELS[slot]} 语义复核建议人工复核 - {pair_review['reason']}")

    return selection, blocking, warnings, rejections


def inspect_group(
    group_dir: Path,
    case_root: Path,
    angle_priority_profile: dict,
    brand_id: str | None = None,
    focus_targets: list[dict] | None = None,
    semantic_context: dict | None = None,
) -> dict:
    hint_map = build_compare_hint_map(group_dir)
    entries = []
    ignored = []
    for file_path in sorted(group_dir.rglob("*")):
        if is_generated_case_layout_path(file_path, group_dir):
            continue
        if not is_image_file(file_path):
            continue
        if "术前术后对比图" in file_path.stem or "模板" in file_path.stem:
            ignored.append(relpath(file_path, case_root))
            continue
        entries.append(analyze_image(file_path, group_dir, hint_map, case_root, semantic_context=semantic_context))

    group = {
        "name": group_dir.name,
        "path": str(group_dir.resolve()),
        "relative_path": relpath(group_dir, case_root),
        "compare_hint_map": {str(key): value for key, value in hint_map.items()},
        "source_file_count": len(entries),
        "ignored_files": ignored,
        "entries": entries,
        "selected_slots": {},
        "render_slots": [],
        "effective_template": None,
        "template_label": None,
        "downgraded_from": None,
        "blocking_issues": [],
        "warnings": [],
        "rejection_reasons": [],
        "status": "ok",
    }

    if not entries:
        group["blocking_issues"].append(f"{group_dir.name}：未找到带术前/术后命名的源图")
        group["status"] = "error"
        return group

    for item in entries:
        for issue in item.get("issues", []):
            if issue:
                group["warnings"].append(f"{group_dir.name}：{item['name']} - {issue}")
        if item.get("rejection_reason"):
            group["rejection_reasons"].append(rejection_entry(
                group_name=group_dir.name,
                slot=item.get("angle"),
                phase=item.get("phase"),
                reason=item["rejection_reason"],
                detail="; ".join(item.get("issues") or [item["rejection_reason"]]),
                file_path=item.get("path"),
            ))

    slot_candidates = build_phase_slot_candidates(entries, angle_priority_profile=angle_priority_profile)
    for slot in ANGLE_SLOTS:
        selected, blocking, warnings, rejections = resolve_slot_pair(
            group_dir.name,
            slot,
            slot_candidates["before"][slot],
            slot_candidates["after"][slot],
            brand_id=brand_id,
            focus_targets=focus_targets,
            semantic_context=semantic_context,
        )
        group["blocking_issues"].extend(blocking)
        group["warnings"].extend(warnings)
        group["rejection_reasons"].extend(rejections)
        if selected:
            group["selected_slots"][slot] = {
                "label": selected["label"],
                "direction": selected["direction"],
                "pose_delta": selected["pose_delta"],
                "semantic_pair_review": selected.get("semantic_pair_review"),
                "before": {
                    key: selected["before"][key]
                    for key in (
                        "name",
                        "path",
                        "relative_path",
                        "group_relative_path",
                        "index",
                        "angle",
                        "angle_source",
                        "angle_confidence",
                        "direction",
                        "pose",
                        "crop_box",
                        "sharpness_score",
                        "sharpness_level",
                        "phase_source",
                        "phase_source_path",
                        "profile_fallback",
                        "semantic_screen",
                        "semantic_applied_fields",
                    )
                },
                "after": {
                    key: selected["after"][key]
                    for key in (
                        "name",
                        "path",
                        "relative_path",
                        "group_relative_path",
                        "index",
                        "angle",
                        "angle_source",
                        "angle_confidence",
                        "direction",
                        "pose",
                        "crop_box",
                        "sharpness_score",
                        "sharpness_level",
                        "phase_source",
                        "phase_source_path",
                        "profile_fallback",
                        "semantic_screen",
                        "semantic_applied_fields",
                    )
                },
            }

    group["selected_slots"], duplicate_warnings, duplicate_rejections = prune_duplicate_slots(
        group_dir.name,
        group["selected_slots"],
        angle_priority_profile,
    )
    group["warnings"].extend(duplicate_warnings)
    group["rejection_reasons"].extend(duplicate_rejections)

    effective_template, render_slots = derive_effective_template(list(group["selected_slots"].keys()), angle_priority_profile=angle_priority_profile)
    if effective_template:
        group["effective_template"] = effective_template
        group["render_slots"] = render_slots
        group["template_label"] = TEMPLATE_LABELS[effective_template]
        if effective_template != "tri-compare":
            group["downgraded_from"] = "tri-compare"
        group["blocking_issues"], downgraded_warnings = filter_downgrade_blockers(group["blocking_issues"], render_slots)
        group["warnings"].extend(downgraded_warnings)
    else:
        if "front" not in group["selected_slots"]:
            group["blocking_issues"].append(f"{group_dir.name}：缺少术前术后都可用的正面样本，无法自动降级模板")
        group["effective_template"] = None
        group["render_slots"] = []

    if SMART_CROP is not None and focus_targets and group["render_slots"]:
        active_slots = [sl for sl in group["render_slots"] if group["selected_slots"].get(sl)]
        for si, sl in enumerate(active_slots):
            _, _, _, debug_info = SMART_CROP.compute_smart_alignment(
                (516, 624), focus_targets, si, len(active_slots),
            )
            group["selected_slots"][sl]["smart_crop"] = debug_info

    if group["blocking_issues"]:
        group["status"] = "error"
    return group


def analyze_body_image(
    file_path: Path,
    case_root: Path,
    semantic_context: dict | None = None,
    sections: list[str] | None = None,
) -> dict:
    sharpness_score, sharpness_level = 0.0, "poor"
    try:
        pil = ImageOps.exif_transpose(Image.open(file_path)).convert("RGB")
        arr = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
        sharpness_score = round(measure_sharpness(arr), 2)
        sharpness_level = classify_sharpness(sharpness_score)
    except Exception:
        pass

    stem = file_path.stem
    phase = infer_phase(stem)
    path_phase, path_phase_dir = infer_phase_from_path(file_path, case_root)
    if not phase and path_phase:
        phase = path_phase
    index = extract_index(stem)
    section = parse_body_section_hint(stem)
    section_source = "filename" if section else None
    if not section:
        section = infer_body_section_from_index(index, sections or [])
        section_source = "filename_index_fallback" if section else None
    item = {
        "name": file_path.name,
        "path": str(file_path.resolve()),
        "relative_path": relpath(file_path, case_root),
        "group_relative_path": relpath(file_path, case_root),
        "phase": phase,
        "phase_source": "filename" if infer_phase(stem) else ("path" if phase else None),
        "phase_source_path": path_phase_dir,
        "index": index,
        "section": section,
        "section_source": section_source,
        "subject": None,
        "direction": "unknown",
        "sharpness_score": sharpness_score,
        "sharpness_level": sharpness_level,
        "issues": [],
        "rejection_reason": None,
        "semantic_screen": None,
        "semantic_applied_fields": [],
    }

    semantic_screen = safe_run_semantic_screen(str(file_path), semantic_context)
    item["semantic_screen"] = semantic_screen
    if semantic_screen:
        item["subject"] = semantic_screen.get("subject")
        if semantic_screen.get("direction_guess"):
            item["direction"] = semantic_screen.get("direction_guess")
        semantic_slot = SEMANTIC_VIEW_TO_SLOT.get(semantic_screen.get("view_guess"))
        if semantic_slot in {"front", "back", "oblique", "side"}:
            item["section"] = semantic_slot
            item["section_source"] = "semantic_screen"
        if not item["phase"]:
            phase_guess = phase_guess_to_phase(semantic_screen.get("phase_guess"))
            if phase_guess:
                item["phase"] = phase_guess
                item["phase_source"] = "semantic_screen"
                item["semantic_applied_fields"].append("phase")
        if semantic_screen.get("confidence") in {"medium", "low"}:
            item["issues"].append(
                f"视觉补判仅供参考(confidence={semantic_screen.get('confidence')})：{semantic_screen.get('reason') or semantic_screen.get('view_guess') or '无'}"
            )
        if semantic_screen.get("subject") not in {"颈部", "身体", "手部"}:
            item["issues"].append(f"当前主体判定为 `{semantic_screen.get('subject')}`，身体链按低置信处理")
    if not item["phase"]:
        item["issues"].append("无法判定术前/术后")
        item["rejection_reason"] = item["rejection_reason"] or "phase_missing"
    if not item["section"]:
        item["issues"].append("无法判定身体视角")
        item["rejection_reason"] = item["rejection_reason"] or "body_view_missing"
    if sharpness_level == "blurry":
        item["issues"].append(f"图片过糊，不适合做案例对比图（sharpness={sharpness_score:.2f}）")
        item["rejection_reason"] = item["rejection_reason"] or "blurry_image"
    return item


def body_candidate_score(item: dict) -> float:
    score = 0.0
    score += min(float(item.get("sharpness_score") or 0.0), 60.0)
    confidence = semantic_confidence_score((item.get("semantic_screen") or {}).get("confidence"))
    score += confidence * 20
    if item.get("phase_source") == "filename":
        score += 8
    if item.get("section_source") == "filename":
        score += 8
    if item.get("section_source") == "semantic_screen":
        score += 6
    if item.get("sharpness_level") == "blurry":
        score -= 80
    return score


def evaluate_body_face_visibility(item: dict) -> dict:
    if "body_visual_guard" in item:
        return item["body_visual_guard"]

    result = {
        "source": "face_landmarker",
        "face_visible": False,
        "face_view": None,
        "confidence": 0.0,
        "error": None,
    }
    try:
        face = FACE_ALIGN.detect_face_landmarks(item["path"])
        view = face.get("view") or {}
        result["face_visible"] = True
        result["face_view"] = view.get("bucket") or "face"
        result["confidence"] = float(view.get("confidence") or 0.0)
    except Exception as exc:
        result["error"] = str(exc)
    item["body_visual_guard"] = result
    return result


def format_body_face_visibility(check: dict) -> str:
    if check.get("face_visible"):
        view = check.get("face_view") or "face"
        confidence = check.get("confidence") or 0.0
        return f"检测到可见人脸(view={view}, confidence={confidence:.2f})"
    return "未检测到可见人脸"


def validate_body_section_visual_pair(section: str, before_best: dict, after_best: dict) -> tuple[list[str], list[dict], dict | None]:
    expected_face_visible = BODY_SECTION_FACE_EXPECTATION.get(section)
    if expected_face_visible is None:
        return [], [], None

    checks = {
        "before": evaluate_body_face_visibility(before_best),
        "after": evaluate_body_face_visibility(after_best),
    }
    phase_conflicts = [
        phase
        for phase, check in checks.items()
        if bool(check.get("face_visible")) != expected_face_visible
    ]
    pair_mismatch = bool(checks["before"].get("face_visible")) != bool(checks["after"].get("face_visible"))
    guard = {
        "section": section,
        "expected_face_visible": expected_face_visible,
        "checks": checks,
        "phase_conflicts": phase_conflicts,
        "pair_mismatch": pair_mismatch,
        "decision": "reject" if phase_conflicts or pair_mismatch else "pass",
    }
    if guard["decision"] == "pass":
        return [], [], guard

    section_label = ANGLE_LABELS.get(section, section)
    before_desc = format_body_face_visibility(checks["before"])
    after_desc = format_body_face_visibility(checks["after"])
    expected_desc = "可见正脸" if expected_face_visible else "不可见正脸"
    detail = (
        f"{section_label}视觉复核未通过：术前{before_desc}，术后{after_desc}；"
        f"当前 `{section_label}` 槽位要求术前术后均为{expected_desc}。"
        "请人工确认正面/背面命名后再出图。"
    )
    rejections = []
    for phase in phase_conflicts or ["before", "after"]:
        item = before_best if phase == "before" else after_best
        rejections.append(rejection_entry(
            group_name="body",
            slot=section,
            phase=phase,
            reason=BODY_SECTION_VISUAL_MISMATCH_REASON,
            detail=detail,
            file_path=item.get("path"),
        ))
    return [detail], rejections, guard


def select_body_section_pair(entries: list[dict], section: str, body_visual_guard: bool = True) -> tuple[dict | None, list[str], list[dict]]:
    blocking = []
    rejections = []
    before = [item for item in entries if item.get("phase") == "before" and item.get("section") == section and not item.get("rejection_reason")]
    after = [item for item in entries if item.get("phase") == "after" and item.get("section") == section and not item.get("rejection_reason")]
    if not before:
        blocking.append(f"缺少术前 {ANGLE_LABELS.get(section, section)}")
        rejections.append(rejection_entry(group_name="body", slot=section, phase="before", reason="missing_phase", detail=blocking[-1]))
        return None, blocking, rejections
    if not after:
        blocking.append(f"缺少术后 {ANGLE_LABELS.get(section, section)}")
        rejections.append(rejection_entry(group_name="body", slot=section, phase="after", reason="missing_phase", detail=blocking[-1]))
        return None, blocking, rejections
    before_best = sorted(before, key=lambda item: (body_candidate_score(item), item["name"]), reverse=True)[0]
    after_best = sorted(after, key=lambda item: (body_candidate_score(item), item["name"]), reverse=True)[0]
    visual_guard = None
    if body_visual_guard:
        visual_blocking, visual_rejections, visual_guard = validate_body_section_visual_pair(section, before_best, after_best)
        if visual_blocking:
            blocking.extend(visual_blocking)
            rejections.extend(visual_rejections)
            return None, blocking, rejections
    return {
        "label": ANGLE_LABELS.get(section, section),
        "direction": "center",
        "before": before_best,
        "after": after_best,
        "body_visual_guard": visual_guard,
    }, blocking, rejections


def reassign_body_sections_by_face_visibility(entries: list[dict], sections: list[str]) -> None:
    """按人脸可见性重写编号兜底分配的 section（方向 C）。

    显式 filename hint 与 semantic_screen 给出的 section 不被覆盖；只有
    section_source 为 None 或 "filename_index_fallback" 时，才用真实人脸
    可见性强制改写：可见正脸 → front；不可见正脸 → back。仅当目标
    section 在当前模板 sections 列表里才生效，避免给颈纹这类 sections=
    [front, oblique] 的链路凭空塞 back。
    """
    if not sections:
        return
    has_front = "front" in sections
    has_back = "back" in sections
    if not (has_front or has_back):
        return
    overridable_sources = {None, "filename_index_fallback"}
    for item in entries:
        if item.get("section_source") not in overridable_sources:
            continue
        check = evaluate_body_face_visibility(item)
        # face_landmarker 抛异常视同检测不到可见正脸（身体/背面图常态），
        # 走 has_back 分支即可；只有上游真要求 has_front 时才需要可见正脸。
        face_visible = bool(check.get("face_visible")) and not check.get("error")
        target_section: str | None = None
        if face_visible and has_front:
            target_section = "front"
        elif not face_visible and has_back:
            target_section = "back"
        if target_section is None:
            continue
        if item.get("section") == target_section and item.get("section_source") == "face_visibility":
            continue
        item["section"] = target_section
        item["section_source"] = "face_visibility"
        if "无法判定身体视角" in (item.get("issues") or []):
            item["issues"] = [issue for issue in item["issues"] if issue != "无法判定身体视角"]
            if item.get("rejection_reason") == "body_view_missing":
                item["rejection_reason"] = None


def build_body_manifest(case_dir: Path, brand: dict, template: str, semantic_judge_mode: str = "auto", body_visual_guard: bool = True) -> dict:
    semantic_context = build_semantic_context(semantic_judge_mode)
    sections, effective_template = body_section_priority(case_dir)
    entries = []
    ignored = []
    for file_path in sorted(case_dir.rglob("*")):
        if is_generated_case_layout_path(file_path, case_dir):
            continue
        if not is_image_file(file_path):
            continue
        if "术前术后对比图" in file_path.stem or "模板" in file_path.stem:
            ignored.append(relpath(file_path, case_dir))
            continue
        entries.append(
            analyze_body_image(file_path, case_dir, semantic_context=semantic_context, sections=sections)
        )
    reassign_body_sections_by_face_visibility(entries, sections)
    group = {
        "name": case_dir.name,
        "path": str(case_dir.resolve()),
        "relative_path": ".",
        "source_file_count": len(entries),
        "ignored_files": ignored,
        "entries": entries,
        "selected_slots": {},
        "render_slots": [],
        "effective_template": effective_template,
        "template_label": "身体/颈纹双视图对比",
        "downgraded_from": None,
        "blocking_issues": [],
        "warnings": [],
        "rejection_reasons": [],
        "status": "ok",
    }
    for item in entries:
        if item.get("issues"):
            for issue in item["issues"]:
                group["warnings"].append(f"{item['name']} - {issue}")
        if item.get("rejection_reason"):
            group["rejection_reasons"].append(rejection_entry(
                group_name=case_dir.name,
                slot=item.get("section"),
                phase=item.get("phase"),
                reason=item["rejection_reason"],
                detail="; ".join(item.get("issues") or [item["rejection_reason"]]),
                file_path=item.get("path"),
            ))
    for section in sections:
        selected, blocking, rejections = select_body_section_pair(entries, section, body_visual_guard=body_visual_guard)
        group["blocking_issues"].extend(f"{case_dir.name}：{item}" for item in blocking)
        group["rejection_reasons"].extend(rejections)
        if selected:
            group["selected_slots"][section] = {
                "label": selected["label"],
                "direction": selected["direction"],
                "before": selected["before"],
                "after": selected["after"],
                "body_visual_guard": selected.get("body_visual_guard"),
                "semantic_pair_review": None,
            }
            group["render_slots"].append(section)
    if not group["render_slots"]:
        group["status"] = "error"
    if group["blocking_issues"]:
        group["status"] = "error"
    case_meta = parse_case_meta(case_dir)
    return {
        "command": "inspect",
        "created_at": now_iso(),
        "case_mode": "body",
        "case_meta": case_meta,
        "case_dir": str(case_dir.resolve()),
        "brand": brand,
        "template": template,
        "effective_templates": [effective_template],
        "semantic_judge_mode": semantic_context["mode"],
        "semantic_summary": semantic_summary(semantic_context),
        "semantic_errors": list(semantic_context.get("errors") or []),
        "status": "error" if group["blocking_issues"] else "ok",
        "blocking_issue_count": len(group["blocking_issues"]),
        "warning_count": len(group["warnings"]),
        "blocking_issues": group["blocking_issues"],
        "warnings": group["warnings"],
        "rejection_reasons": group["rejection_reasons"],
        "groups": [group],
        "outputs": {},
    }


def build_manifest(
    case_dir: Path,
    brand: dict,
    template: str,
    focus_targets: list[dict] | None = None,
    semantic_judge_mode: str = "auto",
    body_visual_guard: bool = True,
) -> dict:
    case_mode = detect_case_mode(case_dir)
    if case_mode == "body":
        return build_body_manifest(
            case_dir,
            brand,
            template,
            semantic_judge_mode=semantic_judge_mode,
            body_visual_guard=body_visual_guard,
        )

    focus_targets = focus_targets or []
    semantic_context = build_semantic_context(semantic_judge_mode)
    angle_priority_profile = build_angle_priority_profile(focus_targets)
    groups = [
        inspect_group(
            group_dir,
            case_dir,
            angle_priority_profile,
            brand_id=brand.get("id"),
            focus_targets=focus_targets,
            semantic_context=semantic_context,
        )
        for group_dir in discover_group_dirs(case_dir)
    ]
    top_blocking = []
    top_warnings = []
    top_rejections = []
    for group in groups:
        top_blocking.extend(group["blocking_issues"])
        top_warnings.extend(group["warnings"])
        top_rejections.extend(group.get("rejection_reasons") or [])

    effective_templates = sorted({group["effective_template"] for group in groups if group.get("effective_template")})

    return {
        "command": "inspect",
        "created_at": now_iso(),
        "case_mode": "face",
        "case_dir": str(case_dir.resolve()),
        "brand": brand,
        "template": template,
        "effective_templates": effective_templates,
        "background_mode": BACKGROUND_MODE,
        "direction_check": DIRECTION_CHECK_MODE,
        "enhancement_input": ENHANCEMENT_INPUT_MODE,
        "enhancement_scope": ENHANCEMENT_SCOPE,
        "non_target_policy": NON_TARGET_POLICY,
        "semantic_judge_mode": semantic_context["mode"],
        "semantic_summary": semantic_summary(semantic_context),
        "semantic_errors": list(semantic_context.get("errors") or []),
        "focus_status": "confirmed" if focus_targets else "missing",
        "focus_targets": focus_targets,
        "angle_priority_profile": angle_priority_profile,
        "pose_delta_thresholds": POSE_DELTA_THRESHOLDS,
        "status": "error" if top_blocking else "ok",
        "blocking_issue_count": len(top_blocking),
        "warning_count": len(top_warnings),
        "blocking_issues": top_blocking,
        "warnings": top_warnings,
        "rejection_reasons": top_rejections,
        "groups": groups,
        "outputs": {},
    }


def apply_after_enhancements(manifest: dict, inspect_root: Path, slots: list[str]) -> dict:
    enhancement_root = ensure_dir(inspect_root / "enhanced")
    enhancement_input_root = ensure_dir(inspect_root / "enhancement-inputs")
    focus_targets = manifest.get("focus_targets") or []
    enhancement_meta = {
        "enabled": True,
        "mode": "after",
        "slots": slots,
        "quality": ENHANCE_QUALITY,
        "model": manifest.get("enhance_model") or "auto(primary-chain)",
        "generated_count": 0,
        "fallback_count": 0,
        "input_mode": ENHANCEMENT_INPUT_MODE,
        "scope": ENHANCEMENT_SCOPE,
        "focus_targets": focus_targets,
        "non_target_policy": NON_TARGET_POLICY,
    }

    for group in manifest["groups"]:
        for slot in slots:
            selected = group["selected_slots"].get(slot)
            if not selected:
                continue
            try:
                enhancement_input = prepare_enhancement_inputs(
                    group["name"],
                    slot,
                    selected["before"]["path"],
                    selected["after"]["path"],
                    enhancement_input_root,
                )
                selected["after"]["enhancement_input"] = enhancement_input
                enhancement = enhance_selected_after_image(
                    group["name"],
                    slot,
                    enhancement_input["after_aligned_path"],
                    enhancement_root,
                    focus_targets,
                    manifest.get("enhance_model") or DEFAULT_ENHANCE_MODEL,
                    pose_ref_path=enhancement_input["before_aligned_path"],
                    source_image_path=selected["after"]["path"],
                    enhancement_input=enhancement_input,
                )
                selected["after"]["enhancement"] = enhancement
                enhancement_meta["generated_count"] += 1
                stabilization = enhancement.get("stabilization") or {}
                if stabilization.get("fallback"):
                    enhancement_meta["fallback_count"] += 1
                    message = (
                        f"{group['name']}：术后 {ANGLE_LABELS[slot]} 增强结果未通过方向/姿态/比例稳定校验，"
                        "已回退到已对齐术后输入图"
                    )
                    group["warnings"].append(message)
                    manifest["warnings"].append(message)
            except Exception as exc:
                selected["after"]["enhancement"] = {
                    "enabled": True,
                    "slot": slot,
                    "quality": ENHANCE_QUALITY,
                    "model": manifest.get("enhance_model") or "auto(primary-chain)",
                    "input_mode": ENHANCEMENT_INPUT_MODE,
                    "error": str(exc),
                }
                message = f"{group['name']}：术后 {ANGLE_LABELS[slot]} 4K 增强失败 - {exc}"
                group["blocking_issues"].append(message)
                manifest["blocking_issues"].append(message)

    manifest["enhancement"] = enhancement_meta
    manifest["blocking_issue_count"] = len(manifest["blocking_issues"])
    manifest["status"] = "error" if manifest["blocking_issues"] else "ok"
    return manifest


def draw_centered_text(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, font, fill) -> None:
    bbox = textbbox_with_fallback(draw, (0, 0), text, font, fill=fill)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = box[0] + (box[2] - box[0] - text_w) / 2
    y = box[1] + (box[3] - box[1] - text_h) / 2 - 2
    draw_text_with_fallback(draw, (x, y), text, font, fill)


def placeholder_cell(size: tuple[int, int], title: str, subtitle: str = "") -> Image.Image:
    img = Image.new("RGB", size, (233, 229, 223))
    draw = ImageDraw.Draw(img)
    title_font = load_font(32, bold=True)
    sub_font = load_font(22)
    draw.rounded_rectangle((12, 12, size[0] - 12, size[1] - 12), radius=22, outline=(189, 180, 170), width=3)
    draw_centered_text(draw, (0, int(size[1] * 0.35), size[0], int(size[1] * 0.55)), title, title_font, (120, 110, 100))
    if subtitle:
        draw_centered_text(draw, (0, int(size[1] * 0.56), size[0], int(size[1] * 0.72)), subtitle, sub_font, (145, 135, 125))
    return img


def cv_to_pil(img: np.ndarray) -> Image.Image:
    return Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))


def detect_profile_fallback_face(image_path: str) -> dict:
    image = cv2.imread(image_path)
    if image is None:
        raise FileNotFoundError(f"无法读取图片: {image_path}")
    image = FACE_ALIGN.auto_orient(image, image_path)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]
    min_size = max(120, int(min(width, height) * PROFILE_FALLBACK_MIN_SIZE_RATIO))
    cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_profileface.xml"))
    if cascade.empty():
        raise RuntimeError("无法加载侧面人脸级联分类器")

    candidates = []
    for scale_factor in (1.1, 1.08):
        for flipped in (False, True):
            probe = cv2.flip(gray, 1) if flipped else gray
            detections = cascade.detectMultiScale(
                probe,
                scaleFactor=scale_factor,
                minNeighbors=4,
                minSize=(min_size, min_size),
            )
            for x, y, box_w, box_h in detections:
                if flipped:
                    x = width - (x + box_w)
                candidates.append({
                    "x": int(x),
                    "y": int(y),
                    "w": int(box_w),
                    "h": int(box_h),
                    "direction": "right" if flipped else "left",
                    "scale_factor": scale_factor,
                })
        if candidates:
            break

    if not candidates:
        raise ValueError(f"未检测到可用侧面人脸: {image_path}")

    best = sorted(candidates, key=lambda item: (item["y"], -item["w"] * item["h"]))[0]
    x = float(best["x"])
    y = float(best["y"])
    box_w = float(best["w"])
    box_h = float(best["h"])
    direction = best["direction"]
    eye_center_x = x + (0.68 if direction == "right" else 0.32) * box_w
    eye_center_y = y + 0.40 * box_h
    pseudo_eye_distance = max(box_w * 0.18, 1.0)

    left_eye = np.array([eye_center_x - pseudo_eye_distance / 2, eye_center_y], dtype=np.float64)
    right_eye = np.array([eye_center_x + pseudo_eye_distance / 2, eye_center_y], dtype=np.float64)
    nose = np.array([x + (0.92 if direction == "right" else 0.08) * box_w, y + 0.58 * box_h], dtype=np.float64)
    forehead = np.array([x + 0.52 * box_w, y + 0.10 * box_h], dtype=np.float64)
    chin = np.array([x + 0.58 * box_w, y + 0.96 * box_h], dtype=np.float64)

    return {
        "image": image,
        "left_eye": left_eye,
        "right_eye": right_eye,
        "nose": nose,
        "chin": chin,
        "forehead": forehead,
        "eye_center": (left_eye + right_eye) / 2,
        "eye_distance": float(np.linalg.norm(right_eye - left_eye)),
        "face_height": float(np.linalg.norm(chin - forehead)),
        "size": (width, height),
        "transform_matrix": None,
        "pose": {
            "pitch": 0.0,
            "yaw": 45.0 if direction == "right" else -45.0,
            "roll": 0.0,
        },
        "view": {
            "bucket": "side",
            "direction": direction,
            "confidence": 0.58,
        },
        "fallback": "profile-cascade",
    }


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


def build_foreground_mask_for_face(image: np.ndarray, slot: str) -> np.ndarray:
    h, w = image.shape[:2]
    foreground = np.zeros((h, w), dtype=bool)
    used_segmenter = False
    try:
        segmenter = get_image_segmenter()
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=image)
        result = segmenter.segment(mp_image)
        category_mask = result.category_mask.numpy_view()
        if category_mask.ndim == 3:
            category_mask = category_mask[:, :, 0]
        foreground = category_mask > 0
        used_segmenter = foreground.any()
    except Exception:
        foreground = np.zeros((h, w), dtype=bool)

    if not used_segmenter:
        rect = (
            int(w * 0.16 if slot == "front" else w * 0.12),
            int(h * 0.06),
            int(w * 0.68 if slot == "front" else w * 0.76),
            int(h * 0.88),
        )
        grabcut_mask = np.full((h, w), cv2.GC_PR_BGD, dtype=np.uint8)
        bg_model = np.zeros((1, 65), dtype=np.float64)
        fg_model = np.zeros((1, 65), dtype=np.float64)
        try:
            cv2.grabCut(image.copy(), grabcut_mask, rect, bg_model, fg_model, 2, cv2.GC_INIT_WITH_RECT)
            foreground = np.isin(grabcut_mask, [cv2.GC_FGD, cv2.GC_PR_FGD])
        except Exception:
            foreground = np.zeros((h, w), dtype=bool)

    # 对 face case 做一个保守兜底，确保脸和肩颈不会被当背景清掉。
    fallback = np.zeros((h, w), dtype=np.uint8)
    if slot == "front":
        center = (int(w * 0.50), int(h * 0.58))
        axes = (int(w * 0.12), int(h * 0.22))
    else:
        center = (int(w * 0.50), int(h * 0.58))
        axes = (int(w * 0.14), int(h * 0.24))
    cv2.ellipse(fallback, center, axes, 0, 0, 360, 255, -1)
    fallback = cv2.GaussianBlur(fallback, (0, 0), sigmaX=max(w, h) * 0.008, sigmaY=max(w, h) * 0.008) > 48

    foreground = foreground | fallback
    foreground = cv2.morphologyEx(foreground.astype(np.uint8) * 255, cv2.MORPH_CLOSE, np.ones((5, 5), dtype=np.uint8), iterations=1) > 0
    return foreground


def soft_alpha_from_foreground(foreground_mask: np.ndarray, feather_px: int = 10) -> np.ndarray:
    sigma = max(float(feather_px), 1.0)
    alpha = cv2.GaussianBlur(foreground_mask.astype(np.uint8) * 255, (0, 0), sigmaX=sigma, sigmaY=sigma).astype(np.float64) / 255.0
    return np.clip(alpha, 0.0, 1.0)


def dilated_foreground_mask(foreground_mask: np.ndarray, padding_px: int = 12) -> np.ndarray:
    kernel = np.ones((padding_px * 2 + 1, padding_px * 2 + 1), dtype=np.uint8)
    return cv2.dilate(foreground_mask.astype(np.uint8) * 255, kernel, iterations=1) > 0


def compose_face_on_white_background(image: np.ndarray, slot: str, valid_mask: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    foreground_mask = build_foreground_mask_for_face(rgb, slot)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    candidate = (
        (hsv[:, :, 1] <= 58) &
        (hsv[:, :, 2] >= 105)
    )
    bg_mask = edge_connected_background_mask(candidate.astype(np.uint8))
    bg_mask &= ~dilated_foreground_mask(foreground_mask, padding_px=14)
    if valid_mask is not None:
        bg_mask |= ~valid_mask.astype(bool)

    white_bg = np.full_like(rgb, (248, 248, 246))
    alpha = cv2.GaussianBlur(bg_mask.astype(np.uint8) * 255, (0, 0), sigmaX=6.0, sigmaY=6.0).astype(np.float64) / 255.0
    composed = np.clip(
        rgb.astype(np.float64) * (1.0 - alpha[..., None]) + white_bg.astype(np.float64) * alpha[..., None],
        0,
        255,
    ).astype(np.uint8)
    return cv2.cvtColor(composed, cv2.COLOR_RGB2BGR), foreground_mask


def sample_edge_background_color(image: np.ndarray, valid_mask: np.ndarray | None = None) -> tuple[int, int, int]:
    h, w = image.shape[:2]
    band_w = max(8, int(w * 0.08))
    band_h = max(8, int(h * 0.08))
    edge_mask = np.zeros((h, w), dtype=bool)
    edge_mask[:, :band_w] = True
    edge_mask[:, w - band_w:w] = True
    edge_mask[:band_h, :] = True
    edge_mask[h - band_h:h, :] = True
    if valid_mask is not None:
        edge_mask &= valid_mask.astype(bool)
    pixels = image[edge_mask]
    if pixels.size == 0 and valid_mask is not None:
        pixels = image[valid_mask.astype(bool)]
    if pixels.size == 0:
        return (248, 245, 241)
    # 避免用仿射产生的纯黑边或强高光作为补边色。
    brightness = pixels.astype(np.float64).mean(axis=1)
    filtered = pixels[(brightness > 35) & (brightness < 252)]
    if filtered.size:
        pixels = filtered
    median = np.median(pixels.reshape(-1, 3), axis=0)
    return tuple(int(round(value)) for value in median.tolist())


def fill_invalid_background_to_sampled_tone(image: np.ndarray, valid_mask: np.ndarray | None) -> tuple[np.ndarray, np.ndarray, float, float, tuple[int, int, int]]:
    if valid_mask is None:
        empty = np.zeros(image.shape[:2], dtype=bool)
        return image.copy(), empty, 0.0, 0.0, sample_edge_background_color(image, None)
    invalid_mask = ~valid_mask.astype(bool)
    sampled_color = sample_edge_background_color(image, valid_mask)
    filled = image.copy()
    filled[invalid_mask] = sampled_color
    ratio = float(invalid_mask.mean())
    component_ratio = 0.0
    if np.any(invalid_mask):
        num_labels, labels = cv2.connectedComponents(invalid_mask.astype(np.uint8))
        if num_labels > 1:
            areas = [int((labels == idx).sum()) for idx in range(1, num_labels)]
            component_ratio = max(areas) / float(invalid_mask.size)
    return filled, invalid_mask, ratio, component_ratio, sampled_color


def evaluate_clean_background_candidate(rgb: np.ndarray, foreground_mask: np.ndarray, valid_mask: np.ndarray | None = None) -> tuple[bool, dict, np.ndarray]:
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    candidate = (
        (hsv[:, :, 1] <= 58) &
        (hsv[:, :, 2] >= 105)
    )
    bg_mask = edge_connected_background_mask(candidate.astype(np.uint8))
    bg_mask &= ~dilated_foreground_mask(foreground_mask, padding_px=4)
    if valid_mask is not None:
        bg_mask &= valid_mask.astype(bool)
    sample = hsv[bg_mask]
    if sample.size == 0:
        metrics = {
            "bg_ratio": round(float(bg_mask.mean()), 4),
            "sample_count": 0,
            "saturation_mean": None,
            "value_mean": None,
            "value_std": None,
            "dirty_score": 999.0,
        }
        return False, metrics, bg_mask
    sat_mean = float(sample[:, 1].mean())
    val_mean = float(sample[:, 2].mean())
    val_std = float(sample[:, 2].std())
    bg_ratio = float(bg_mask.mean())
    dirty_score = sat_mean + max(0.0, 218.0 - val_mean) * 0.5 + val_std * 0.7
    clean = bg_ratio >= 0.18 and sat_mean <= 24.0 and val_mean >= 218.0 and val_std <= 18.0 and dirty_score <= 38.0
    metrics = {
        "bg_ratio": round(bg_ratio, 4),
        "sample_count": int(sample.shape[0]),
        "saturation_mean": round(sat_mean, 2),
        "value_mean": round(val_mean, 2),
        "value_std": round(val_std, 2),
        "dirty_score": round(dirty_score, 2),
    }
    return clean, metrics, bg_mask


def apply_conservative_background_policy(image: np.ndarray, slot: str, valid_mask: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray, dict]:
    if os.environ.get("CASE_LAYOUT_BACKGROUND_MODE", "").lower() != "clean-white":
        h, w = image.shape[:2]
        dummy_mask = np.zeros((h, w), dtype=np.uint8)
        return image, dummy_mask, {"status": "noop", "reason": "env gate disabled"}

    policy = BACKGROUND_MODE
    if policy in {"white-only", "preserve-contour"}:
        filled, foreground_mask = compose_face_on_white_background(image, slot, valid_mask=valid_mask)
        invalid_ratio = float((~valid_mask.astype(bool)).mean()) if valid_mask is not None else 0.0
        return filled, foreground_mask, {
            "policy": policy,
            "status": "legacy_white_blend",
            "reason": "explicit_legacy_background_mode",
            "invalid_ratio": round(invalid_ratio, 4),
        }

    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    foreground_mask = build_foreground_mask_for_face(rgb, slot)
    clean, metrics, bg_mask = evaluate_clean_background_candidate(rgb, foreground_mask, valid_mask)
    allow_clean_white = policy in {"auto-clean-white", "clean-white"}
    if allow_clean_white and clean:
        white_bg = np.full_like(rgb, (248, 248, 246))
        if valid_mask is not None:
            bg_mask = bg_mask | ~valid_mask.astype(bool)
        alpha = cv2.GaussianBlur(bg_mask.astype(np.uint8) * 255, (0, 0), sigmaX=6.0, sigmaY=6.0).astype(np.float64) / 255.0
        composed = np.clip(
            rgb.astype(np.float64) * (1.0 - alpha[..., None]) + white_bg.astype(np.float64) * alpha[..., None],
            0,
            255,
        ).astype(np.uint8)
        return cv2.cvtColor(composed, cv2.COLOR_RGB2BGR), foreground_mask, {
            "policy": policy,
            "status": "clean_whitened",
            "reason": "high_confidence_clean_background",
            **metrics,
        }

    filled, invalid_mask, invalid_ratio, component_ratio, sampled_color = fill_invalid_background_to_sampled_tone(image, valid_mask)
    status = "padding_only" if np.any(invalid_mask) else "preserved_original_tone"
    return filled, foreground_mask, {
        "policy": policy,
        "status": status,
        "reason": "background_not_clean_enough_preserve_original_tone",
        "invalid_ratio": round(invalid_ratio, 4),
        "invalid_component_ratio": round(component_ratio, 4),
        "sampled_bgr": list(sampled_color),
        **metrics,
    }


def prefill_aligned_background_to_white(image: np.ndarray, slot: str) -> tuple[np.ndarray, np.ndarray, float, float]:
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    foreground_mask = build_foreground_mask_for_face(rgb, slot)
    foreground_keep = dilated_foreground_mask(foreground_mask, padding_px=4)
    alpha = soft_alpha_from_foreground(foreground_keep, feather_px=7)
    white_bg = np.full_like(rgb, (248, 248, 246))
    filled = np.clip(
        rgb.astype(np.float64) * alpha[..., None] + white_bg.astype(np.float64) * (1.0 - alpha[..., None]),
        0,
        255,
    ).astype(np.uint8)

    bg_mask = build_border_gap_mask(foreground_keep)

    ratio = float(bg_mask.mean())
    component_ratio = 0.0
    if np.any(bg_mask):
        num_labels, labels = cv2.connectedComponents(bg_mask.astype(np.uint8))
        if num_labels > 1:
            areas = [int((labels == idx).sum()) for idx in range(1, num_labels)]
            component_ratio = max(areas) / float(bg_mask.size)
    return cv2.cvtColor(filled, cv2.COLOR_RGB2BGR), bg_mask, ratio, component_ratio


def fill_invalid_background_to_white(image: np.ndarray, valid_mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, float, float]:
    invalid_mask = ~valid_mask.astype(bool)
    filled = image.copy()
    filled[invalid_mask] = (248, 248, 246)
    ratio = float(invalid_mask.mean())
    component_ratio = 0.0
    if np.any(invalid_mask):
        num_labels, labels = cv2.connectedComponents(invalid_mask.astype(np.uint8))
        if num_labels > 1:
            areas = [int((labels == idx).sum()) for idx in range(1, num_labels)]
            component_ratio = max(areas) / float(invalid_mask.size)
    return filled, invalid_mask, ratio, component_ratio


def largest_edge_gap_thickness_ratio(bg_mask: np.ndarray) -> float:
    if not np.any(bg_mask):
        return 0.0
    h, w = bg_mask.shape[:2]
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(bg_mask.astype(np.uint8), connectivity=8)
    ratios = []
    for idx in range(1, num_labels):
        x, y, comp_w, comp_h, area = stats[idx]
        touches_left = x == 0
        touches_right = (x + comp_w) >= w
        touches_top = y == 0
        touches_bottom = (y + comp_h) >= h
        if not any((touches_left, touches_right, touches_top, touches_bottom)):
            continue
        edge_thickness = 0.0
        if touches_left or touches_right:
            edge_thickness = max(edge_thickness, comp_w / float(w))
        if touches_top or touches_bottom:
            edge_thickness = max(edge_thickness, comp_h / float(h))
        ratios.append(edge_thickness)
    return max(ratios) if ratios else 0.0


def build_border_gap_mask(foreground_mask: np.ndarray) -> np.ndarray:
    h, w = foreground_mask.shape[:2]
    near_fg = dilated_foreground_mask(foreground_mask, padding_px=18) & ~dilated_foreground_mask(foreground_mask, padding_px=4)
    left_band = np.zeros((h, w), dtype=bool)
    right_band = np.zeros((h, w), dtype=bool)
    top_band = np.zeros((h, w), dtype=bool)
    bottom_band = np.zeros((h, w), dtype=bool)

    band_w = max(18, int(w * 0.12))
    band_h = max(18, int(h * 0.12))
    if foreground_mask[:, :3].any():
        left_band[:, :band_w] = True
    if foreground_mask[:, w - 3:w].any():
        right_band[:, w - band_w:w] = True
    if foreground_mask[:3, :].any():
        top_band[:band_h, :] = True
    if foreground_mask[h - 3:h, :].any():
        bottom_band[h - band_h:h, :] = True

    edge_band = left_band | right_band | top_band | bottom_band
    return near_fg & edge_band


def should_attempt_ai_padfill(bg_mask: np.ndarray, bg_ratio: float, component_ratio: float) -> bool:
    if bg_ratio < PADFILL_TRIGGER_RATIO or component_ratio < PADFILL_TRIGGER_MAX_COMPONENT_RATIO:
        return False
    if bg_ratio > PADFILL_AI_MAX_TOTAL_RATIO:
        return False
    if largest_edge_gap_thickness_ratio(bg_mask) > PADFILL_AI_MAX_EDGE_THICKNESS_RATIO:
        return False
    return True


def build_padfill_ai_mask(source_arr: np.ndarray, slot: str, bg_mask: np.ndarray) -> np.ndarray:
    foreground_mask = build_foreground_mask_for_face(cv2.cvtColor(source_arr, cv2.COLOR_BGR2RGB), slot)
    near_foreground = dilated_foreground_mask(foreground_mask, padding_px=20) & ~dilated_foreground_mask(foreground_mask, padding_px=3)
    return bg_mask & near_foreground


def build_padfill_prompt(slot: str, phase: str) -> str:
    return (
        f"这是医美案例对比图里的单格{PHASE_LABELS.get(phase, phase)}{ANGLE_LABELS.get(slot, slot)}照片。"
        "当前图片已经完成构图对齐。"
        "只处理画面边缘的空白/灰色补边区域："
        "把人物背景统一补成干净的白色诊室背景，"
        "并只在边缘被裁断的位置补齐人物外轮廓，例如头发边缘、耳侧、颈部、肩部和黑色披肩。"
        "不要重绘整个人物，不要改动主体面部，不要改动原图中已经存在的区域。"
        "必须保持同一人物身份、同一五官、同一肤质瑕疵、同一治疗状态、同一角度、同一光线和同一裁切。"
        "不要磨皮，不要美白，不要改变术前术后效果，不要改动脸部和未缺失区域。"
    )


def outpaint_aligned_face_cell(
    source_arr: np.ndarray,
    source_path: str,
    slot: str,
    phase: str,
    enhance_model: str,
    bg_mask: np.ndarray,
) -> np.ndarray:
    cache_path = padfill_cache_path(source_path, slot, phase, enhance_model, (source_arr.shape[1], source_arr.shape[0]))
    if cache_path.exists():
        cached = cv2.imread(str(cache_path))
        if cached is not None and validate_padfill_output(source_arr, cached, slot):
            return cached
        cache_path.unlink(missing_ok=True)

    ensure_dir(PADFILL_CACHE_ROOT)
    tmp_dir = Path("/tmp") / "case-layout-padfill-work"
    ensure_dir(tmp_dir)
    input_path = tmp_dir / f"padfill-{hashlib.sha1(f'{source_path}|{slot}|{phase}'.encode('utf-8')).hexdigest()[:12]}.jpg"
    cv_to_pil(source_arr).save(input_path, "JPEG", quality=95)

    proc = subprocess.run(
        [
            "node",
            str(Path(__file__).resolve().parent / "case_layout_enhance.js"),
            "--image",
            str(input_path),
            "--quality",
            ENHANCE_QUALITY,
            "--model",
            enhance_model,
            "--no-planner",
            "--prompt",
            build_padfill_prompt(slot, phase),
        ],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "边缘补齐模型执行失败")
    payload = json.loads(proc.stdout)
    if not payload.get("success") or not payload.get("imagePath"):
        raise RuntimeError(f"边缘补齐未生成图片: {json.dumps(payload, ensure_ascii=False)[:400]}")
    generated = cv2.imread(payload["imagePath"])
    if generated is None:
        raise RuntimeError(f"边缘补齐输出无法读取: {payload['imagePath']}")
    if generated.shape[:2] != source_arr.shape[:2]:
        generated = cv2.resize(generated, (source_arr.shape[1], source_arr.shape[0]), interpolation=cv2.INTER_LINEAR)
    composite_mask = build_padfill_ai_mask(source_arr, slot, bg_mask)
    blended = source_arr.copy()
    blended[composite_mask] = generated[composite_mask]
    if not validate_padfill_output(source_arr, blended, slot):
        raise RuntimeError("边缘补齐结果未通过主体保护校验")
    cv_to_pil(blended).save(cache_path, "JPEG", quality=95)
    return blended


def validate_padfill_output(source_arr: np.ndarray, generated_arr: np.ndarray, slot: str) -> bool:
    if generated_arr.shape[:2] != source_arr.shape[:2]:
        generated_arr = cv2.resize(generated_arr, (source_arr.shape[1], source_arr.shape[0]), interpolation=cv2.INTER_LINEAR)

    tmp_dir = Path("/tmp") / "case-layout-padfill-validate"
    ensure_dir(tmp_dir)
    probe_path = tmp_dir / f"probe-{hashlib.sha1(str(datetime.now().timestamp()).encode('utf-8')).hexdigest()[:12]}.jpg"
    cv_to_pil(generated_arr).save(probe_path, "JPEG", quality=95)
    try:
        try:
            FACE_ALIGN.detect_face_landmarks(str(probe_path))
        except Exception:
            return False
        protect = build_foreground_mask_for_face(cv2.cvtColor(source_arr, cv2.COLOR_BGR2RGB), slot).astype(np.float64)
        diff = np.abs(generated_arr.astype(np.float64) - source_arr.astype(np.float64)).mean(axis=2)
        weight_sum = max(float(protect.sum()), 1e-6)
        protected_diff = float((diff * protect).sum() / weight_sum)
        return protected_diff <= 28.0
    finally:
        probe_path.unlink(missing_ok=True)


def prepare_face_cell_for_board(
    source_path: str,
    aligned_arr: np.ndarray,
    slot: str,
    phase: str,
    enhance_model: str | None = None,
    valid_mask: np.ndarray | None = None,
    cleanup_records: list[dict] | None = None,
) -> np.ndarray:
    filled_arr, foreground_mask, cleanup = apply_conservative_background_policy(aligned_arr, slot, valid_mask=valid_mask)
    cleanup.update({
        "source_path": source_path,
        "slot": slot,
        "phase": phase,
    })
    if cleanup_records is not None:
        cleanup_records.append(cleanup)
    bg_mask = build_border_gap_mask(foreground_mask)
    bg_ratio = float(bg_mask.mean())
    component_ratio = largest_edge_gap_thickness_ratio(bg_mask)
    if PADFILL_MODE != "ai":
        return filled_arr
    if not should_attempt_ai_padfill(bg_mask, bg_ratio, component_ratio):
        return filled_arr
    model = normalize_enhance_model(enhance_model)
    try:
        return outpaint_aligned_face_cell(filled_arr, source_path, slot, phase, model, bg_mask)
    except Exception:
        return filled_arr


def detect_face_for_alignment(image_path: str) -> dict:
    try:
        return FACE_ALIGN.detect_face_landmarks(image_path)
    except Exception:
        return detect_profile_fallback_face(image_path)


def estimate_alignment_effective_scale(
    face: dict,
    cell_size: tuple[int, int],
    target_eye_distance: float,
) -> float:
    eye_distance = max(float(face["eye_distance"]), 1e-6)
    face_height = max(float(face["face_height"]), 1e-6)
    eye_scale = target_eye_distance / eye_distance
    face_height_scale = (cell_size[1] * ALIGN_TARGET_FACE_HEIGHT_RATIO) / face_height
    return float(min(eye_scale, face_height_scale * ALIGN_FACE_HEIGHT_SCALE_HEADROOM))


def render_detected_face_cell_with_mask(
    face: dict,
    cell_size: tuple[int, int],
    target_eye_distance: float,
    target_eye_center: np.ndarray,
    forced_effective_scale: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    eye_distance = max(float(face["eye_distance"]), 1e-6)
    effective_scale = (
        max(float(forced_effective_scale), 1e-6)
        if forced_effective_scale is not None
        else estimate_alignment_effective_scale(face, cell_size, target_eye_distance)
    )
    return FACE_ALIGN.align_face(
        face["image"],
        face,
        target_eye_distance=eye_distance * effective_scale,
        target_eye_center=target_eye_center,
        output_size=cell_size,
        return_mask=True,
    )


def render_aligned_cell(
    image_path: str,
    cell_size: tuple[int, int],
    target_eye_distance: float,
    target_eye_center: np.ndarray,
    forced_effective_scale: float | None = None,
) -> np.ndarray:
    face = detect_face_for_alignment(image_path)
    eye_distance = max(float(face["eye_distance"]), 1e-6)
    effective_scale = (
        max(float(forced_effective_scale), 1e-6)
        if forced_effective_scale is not None
        else estimate_alignment_effective_scale(face, cell_size, target_eye_distance)
    )
    return FACE_ALIGN.align_face(
        face["image"],
        face,
        target_eye_distance=eye_distance * effective_scale,
        target_eye_center=target_eye_center,
        output_size=cell_size,
    )


def render_aligned_cell_with_mask(
    image_path: str,
    cell_size: tuple[int, int],
    target_eye_distance: float,
    target_eye_center: np.ndarray,
    forced_effective_scale: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    face = detect_face_for_alignment(image_path)
    return render_detected_face_cell_with_mask(
        face,
        cell_size,
        target_eye_distance,
        target_eye_center,
        forced_effective_scale=forced_effective_scale,
    )

def render_brand_footer(canvas: Image.Image, brand: dict, footer_box: tuple[int, int, int, int]) -> None:
    draw = ImageDraw.Draw(canvas)
    x1, y1, x2, y2 = footer_box
    draw.rounded_rectangle(footer_box, radius=28, fill=(245, 240, 232))

    logo = Image.open(brand["logo_path"]).convert("RGBA")
    logo = ImageOps.contain(logo, (180, y2 - y1 - 28))
    logo_x = x1 + 28
    logo_y = y1 + (y2 - y1 - logo.height) // 2
    canvas.alpha_composite(logo, (logo_x, logo_y))

    title_font = load_font(34, bold=True)
    sub_font = load_font(20)
    text_x = logo_x + logo.width + 28
    draw_text_with_fallback(draw, (text_x, y1 + 26), brand["brand_line"], title_font, (74, 61, 52), bold=True)
    effective_templates = canvas.info.get("effective_templates") or ["tri-compare"]
    template_text = " / ".join(TEMPLATE_LABELS.get(item, item) for item in effective_templates)
    draw_text_with_fallback(draw, (text_x, y1 + 70), f"自动排版模板 · {template_text}", sub_font, (120, 110, 100))


def render_board(manifest: dict, out_path: Path, preview: bool = False) -> Path:
    groups = manifest["groups"]
    if not groups:
        raise ValueError("manifest 中没有 group")
    cleanup_records: list[dict] = []
    manifest["background_cleanup"] = {
        "policy": BACKGROUND_MODE,
        "preview": bool(preview),
        "cells": cleanup_records,
        "summary": {},
    }

    board_w = 1280 if preview else 1920
    outer_pad = 32 if preview else 56
    title_h = 46 if preview else 64
    header_h = 52 if preview else 72
    row_gap = 14 if preview else 18
    section_gap = 26 if preview else 42
    footer_h = 88 if preview else 132
    angle_col_w = 90 if preview else 118
    col_gap = 22 if preview else 30
    image_h = 220 if preview else 348
    column_w = int((board_w - outer_pad * 2 - angle_col_w - col_gap) / 2)

    def section_height(group: dict) -> int:
        row_count = len(group.get("render_slots") or ANGLE_SLOTS)
        return title_h + header_h + image_h * row_count + row_gap * max(row_count - 1, 0) + 24

    board_h = outer_pad * 2 + footer_h + sum(section_height(group) for group in groups) + section_gap * max(len(groups) - 1, 0)
    canvas = Image.new("RGBA", (board_w, board_h), (238, 232, 225, 255))
    canvas.info["effective_templates"] = manifest.get("effective_templates") or ["tri-compare"]
    draw = ImageDraw.Draw(canvas)

    group_title_font = load_font(34 if preview else 42, bold=True)
    header_font = load_font(28 if preview else 34, bold=True)
    angle_font = load_font(22 if preview else 28, bold=True)

    before_header_fill = (108, 92, 77)
    after_header_fill = (131, 153, 101)
    row_fill = (248, 245, 241)

    y = outer_pad
    for group in groups:
        group_top = y
        draw_text_with_fallback(draw, (outer_pad, group_top), group["name"], group_title_font, (64, 52, 43), bold=True)
        y = group_top + title_h

        before_x = outer_pad + angle_col_w
        after_x = before_x + column_w + col_gap
        draw.rounded_rectangle((before_x, y, before_x + column_w, y + header_h), radius=18, fill=before_header_fill)
        draw.rounded_rectangle((after_x, y, after_x + column_w, y + header_h), radius=18, fill=after_header_fill)
        draw_centered_text(draw, (before_x, y, before_x + column_w, y + header_h), "术前", header_font, (255, 255, 255))
        draw_centered_text(draw, (after_x, y, after_x + column_w, y + header_h), "术后", header_font, (255, 255, 255))
        y += header_h

        group_slots = group.get("render_slots") or ANGLE_SLOTS
        manifest_focus = manifest.get("focus_targets") or []
        active_slots = [sl for sl in group_slots if group["selected_slots"].get(sl)]
        slot_alignments: dict[str, tuple[float, np.ndarray]] = {}
        for si, sl in enumerate(active_slots):
            if SMART_CROP is not None and manifest_focus:
                ted, ecx, ecy, _ = SMART_CROP.compute_smart_alignment(
                    (column_w, image_h), manifest_focus, si, len(active_slots),
                )
                slot_alignments[sl] = (ted, np.array([ecx, ecy], dtype=np.float64))
            else:
                slot_alignments[sl] = build_alignment_target((column_w, image_h))

        # 第一阶段：对齐并统一所有术前图的色彩（以第一张可用图为锚点，通常是正面）
        temp_slots_data = {}
        before_arrays = []
        before_masks = []
        valid_slots = []
        for slot in group_slots:
            selection = group["selected_slots"].get(slot)
            if not selection:
                continue
            target_eye_distance, target_eye_center = slot_alignments.get(
                slot, build_alignment_target((column_w, image_h))
            )
            before_source = selection["before"]["path"]
            b_arr, b_mask = render_aligned_cell_with_mask(before_source, (column_w, image_h), target_eye_distance, target_eye_center)
            b_arr = prepare_face_cell_for_board(
                before_source, b_arr, slot, "before", manifest.get("enhance_model"), b_mask, cleanup_records
            )
            before_arrays.append(b_arr)
            before_masks.append(b_mask)
            valid_slots.append(slot)
            temp_slots_data[slot] = {"before_arr": b_arr, "before_mask": b_mask}
        
        if before_arrays:
            harmo_befores = FACE_ALIGN.harmonize_group(before_arrays, before_masks)
            for slot, harmo in zip(valid_slots, harmo_befores):
                temp_slots_data[slot]["before_arr"] = harmo

        # 第二阶段：逐行渲染，术后图对齐到已标准化的术前图
        for idx, slot in enumerate(group_slots):
            if idx > 0:
                y += row_gap
            row_y = y
            draw.rounded_rectangle((before_x, row_y, before_x + column_w, row_y + image_h), radius=18, fill=row_fill)
            draw.rounded_rectangle((after_x, row_y, after_x + column_w, row_y + image_h), radius=18, fill=row_fill)
            draw_centered_text(draw, (outer_pad, row_y, outer_pad + angle_col_w - 12, row_y + image_h), ANGLE_LABELS[slot], angle_font, (111, 98, 88))

            selection = group["selected_slots"].get(slot)
            if selection and slot in temp_slots_data:
                before_arr = temp_slots_data[slot]["before_arr"]
                slot_ted, slot_tec = slot_alignments.get(
                    slot, build_alignment_target((column_w, image_h))
                )
                after_source = selection["after"].get("enhancement", {}).get("enhanced_path") or selection["after"]["path"]
                after_arr, after_mask = render_aligned_cell_with_mask(after_source, (column_w, image_h), slot_ted, slot_tec)
                after_arr = prepare_face_cell_for_board(
                    after_source,
                    after_arr,
                    slot,
                    "after",
                    manifest.get("enhance_model"),
                    after_mask,
                    cleanup_records,
                )
                
                # 配对统一：术后匹配术前，同时处理清晰度不对称
                before_arr, after_arr = FACE_ALIGN.harmonize_pair(before_arr, after_arr)
                after_arr = FACE_ALIGN.lift_face_shadows(after_arr, slot=slot)
                before_img = cv_to_pil(before_arr)
                after_img = cv_to_pil(after_arr)
            else:
                before_img = placeholder_cell((column_w, image_h), PHASE_LABELS["before"], f"缺少{ANGLE_LABELS[slot]}")
                after_img = placeholder_cell((column_w, image_h), PHASE_LABELS["after"], f"缺少{ANGLE_LABELS[slot]}")

            canvas.alpha_composite(before_img.convert("RGBA"), (before_x, row_y))
            canvas.alpha_composite(after_img.convert("RGBA"), (after_x, row_y))
            y += image_h

        y += 24
        if group is not groups[-1]:
            y += section_gap

    footer_box = (outer_pad, board_h - outer_pad - footer_h, board_w - outer_pad, board_h - outer_pad)
    render_brand_footer(canvas, manifest["brand"], footer_box)

    ensure_dir(out_path.parent)
    canvas.convert("RGB").save(out_path, "JPEG", quality=92)
    cleanup_counts = {}
    for item in cleanup_records:
        status = item.get("status") or "unknown"
        cleanup_counts[status] = cleanup_counts.get(status, 0) + 1
    manifest["background_cleanup"]["summary"] = {
        "cell_count": len(cleanup_records),
        "status_counts": cleanup_counts,
    }
    return out_path


def render_body_board(manifest: dict, out_path: Path) -> Path:
    if not BODY_RENDER_SCRIPT.exists():
        raise FileNotFoundError(f"缺少身体案例渲染器: {BODY_RENDER_SCRIPT}")
    group = manifest["groups"][0]
    case_meta = manifest.get("case_meta") or parse_case_meta(Path(manifest["case_dir"]))
    render_slots = group.get("render_slots") or []
    if not render_slots:
        raise ValueError("身体/颈纹案例缺少可渲染 section")

    args = [
        sys.executable,
        str(BODY_RENDER_SCRIPT),
        "--brand",
        manifest["brand"]["id"],
        "--date",
        case_meta["date"],
        "--customer-name",
        case_meta["customer_name"],
        "--project",
        case_meta["project"],
        "--section1-title",
        f"{ANGLE_LABELS.get(render_slots[0], render_slots[0])}对比",
        "--section1-before",
        group["selected_slots"][render_slots[0]]["before"]["path"],
        "--section1-after",
        group["selected_slots"][render_slots[0]]["after"]["path"],
        "--section1-mode",
        body_mode_for_section(render_slots[0], Path(manifest["case_dir"])),
    ]
    if len(render_slots) > 1:
        args.extend([
            "--section2-title",
            f"{ANGLE_LABELS.get(render_slots[1], render_slots[1])}对比",
            "--section2-before",
            group["selected_slots"][render_slots[1]]["before"]["path"],
            "--section2-after",
            group["selected_slots"][render_slots[1]]["after"]["path"],
            "--section2-mode",
            body_mode_for_section(render_slots[1], Path(manifest["case_dir"])),
        ])
    args.extend(["--out", str(out_path.resolve())])
    proc = subprocess.run(args, capture_output=True, text=True, cwd=str(PROJECT_ROOT), check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "身体/颈纹渲染失败")
    return out_path


def build_report(manifest: dict) -> str:
    lines = [
        "# case-layout-board inspect 报告",
        "",
        f"- 时间: {manifest['created_at']}",
        f"- 案例目录: `{manifest['case_dir']}`",
        f"- 品牌: `{manifest['brand']['id']}` / {manifest['brand']['brand_line']}",
        f"- 模板: `{manifest['template']}`",
        f"- 状态: `{manifest['status']}`",
        f"- 背景策略: `{manifest.get('background_mode')}`",
        f"- 方向校验: `{manifest.get('direction_check')}`",
        f"- 增强输入: `{manifest.get('enhancement_input')}`",
        f"- 增强范围: `{manifest.get('enhancement_scope')}`",
        f"- 非目标区域策略: `{manifest.get('non_target_policy')}`",
        f"- 语义判官模式: `{manifest.get('semantic_judge_mode')}`",
        f"- 增强模型: `{manifest.get('enhance_model')}`",
        f"- focus 状态: `{manifest.get('focus_status')}`",
        "",
    ]

    if manifest.get("semantic_summary"):
        summary = manifest["semantic_summary"]
        lines.extend([
            "## Semantic Judge",
            "",
            f"- screen_calls: `{summary.get('screen_calls', 0)}`",
            f"- screen_applied: `{summary.get('screen_applied', 0)}`",
            f"- pair_calls: `{summary.get('pair_calls', 0)}`",
            f"- pair_reviews: `{summary.get('pair_reviews', 0)}`",
            f"- pair_rejects: `{summary.get('pair_rejects', 0)}`",
            "",
        ])

    if manifest.get("semantic_errors"):
        lines.extend(["## Semantic Errors", ""])
        for item in manifest["semantic_errors"]:
            lines.append(f"- `{item.get('stage')}` / `{item.get('target')}` / {item.get('error')}")
        lines.append("")

    if manifest.get("background_cleanup"):
        cleanup = manifest["background_cleanup"]
        lines.extend([
            "## Background Cleanup",
            "",
            f"- policy: `{cleanup.get('policy')}`",
            f"- status_counts: `{json.dumps((cleanup.get('summary') or {}).get('status_counts') or {}, ensure_ascii=False)}`",
            "",
        ])

    if manifest.get("focus_targets"):
        lines.extend(["## Focus Targets", ""])
        for item in manifest["focus_targets"]:
            lines.append(
                f"- `P{item['priority']}` `{item['area']}` -> `{item['effect']}`"
                f" | angle_order=`{', '.join(item['angle_order'])}`"
            )
        lines.append("")
    elif manifest.get("focus_status") == "missing":
        lines.extend(["## Focus Targets", "", "- 未确认部位+效果；当前只允许纯识别，不允许术后增强", ""])

    if manifest.get("angle_priority_profile"):
        profile = manifest["angle_priority_profile"]
        lines.extend([
            "## Angle Priority",
            "",
            f"- 来源: `{profile.get('source')}`",
            f"- 优先级: `{', '.join(profile.get('preferred_slots') or [])}`",
            f"- 分数: `{json.dumps(profile.get('slot_scores') or {}, ensure_ascii=False)}`",
            "",
        ])

    if manifest.get("enhancement", {}).get("enabled"):
        lines.extend([
            "## Enhancement",
            "",
            f"- 模式: `{manifest['enhancement']['mode']}`",
            f"- 角度: `{', '.join(manifest['enhancement']['slots'])}`",
            f"- 模型: `{manifest['enhancement']['quality']}`",
            "",
        ])

    if manifest["blocking_issues"]:
        lines.extend(["## Blocking Issues", ""])
        for issue in manifest["blocking_issues"]:
            lines.append(f"- {issue}")
        lines.append("")

    if manifest["warnings"]:
        lines.extend(["## Warnings", ""])
        for warning in manifest["warnings"]:
            lines.append(f"- {warning}")
        lines.append("")

    if manifest.get("rejection_reasons"):
        lines.extend(["## Rejection Reasons", ""])
        for item in manifest["rejection_reasons"]:
            slot_label = ANGLE_LABELS.get(item.get("slot"), item.get("slot") or "n/a")
            phase = item.get("phase") or "pair"
            lines.append(
                f"- `{item['reason']}` / 组 `{item['group_name']}` / 角度 `{slot_label}` / 阶段 `{phase}` / {item['detail']}"
            )
        lines.append("")

    for group in manifest["groups"]:
        lines.extend([f"## {group['name']}", "", f"- 状态: `{group['status']}`"])
        if group.get("effective_template"):
            lines.append(f"- 生效模板: `{group['effective_template']}` / {group['template_label']}")
        if group.get("downgraded_from"):
            lines.append(f"- 自动降级: `{group['downgraded_from']} -> {group['effective_template']}`")
        if group.get("compare_hint_map"):
            lines.append(f"- 历史对比图角度提示: `{json.dumps(group['compare_hint_map'], ensure_ascii=False)}`")
        for slot in (group.get("render_slots") or ANGLE_SLOTS):
            selected = group["selected_slots"].get(slot)
            if not selected:
                lines.append(f"- {ANGLE_LABELS[slot]}: 缺失")
                continue
            lines.append(
                f"- {ANGLE_LABELS[slot]}: 术前 `{selected['before']['group_relative_path']}` / "
                f"术后 `{selected['after']['group_relative_path']}` / 方向 `{selected['direction']}`"
            )
            pose_delta = selected.get("pose_delta")
            if pose_delta:
                lines.append(
                    f"  - 姿态差: yaw=`{pose_delta['yaw']}` pitch=`{pose_delta['pitch']}` "
                    f"roll=`{pose_delta['roll']}` weighted=`{pose_delta['weighted']}`"
                )
            pair_review = selected.get("semantic_pair_review")
            if pair_review:
                lines.append(
                    f"  - 语义复核: decision=`{pair_review['decision']}` / reason=`{pair_review['reason']}`"
                )
            enhancement = selected["after"].get("enhancement")
            if enhancement and enhancement.get("enhanced_path"):
                lines.append(f"  - 术后增强图: `{enhancement['enhanced_path']}`")
                if enhancement.get("input_image_path"):
                    lines.append(f"  - 增强输入图: `{enhancement['input_image_path']}`")
                stabilization = enhancement.get("stabilization") or {}
                if stabilization:
                    lines.append(
                        f"  - 增强稳定化: fallback=`{bool(stabilization.get('fallback'))}` "
                        f"rotation=`{stabilization.get('rotation')}` reason=`{stabilization.get('reason')}`"
                    )
            elif enhancement and enhancement.get("error"):
                lines.append(f"  - 术后增强失败: `{enhancement['error']}`")
        if group["ignored_files"]:
            lines.append(f"- 忽略文件: {', '.join(f'`{name}`' for name in group['ignored_files'][:6])}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def run_inspect(
    case_dir: Path,
    brand: dict,
    template: str,
    output_root: Path,
    enhance_after: bool = False,
    enhance_slots: list[str] | None = None,
    focus_targets: list[dict] | None = None,
    semantic_judge_mode: str = "auto",
    enhance_model: str | None = None,
) -> tuple[dict, int]:
    focus_targets = focus_targets or []
    manifest = build_manifest(
        case_dir,
        brand,
        template,
        focus_targets=focus_targets,
        semantic_judge_mode=semantic_judge_mode,
    )
    manifest["enhance_model"] = normalize_enhance_model(enhance_model)
    inspect_root = ensure_dir(output_root / "inspect")
    manifest_path = inspect_root / "manifest.json"
    report_path = inspect_root / "report.md"
    preview_path = inspect_root / "preview.jpg"
    preview_original_path = inspect_root / "preview-original.jpg"

    manifest["outputs"] = {
        "manifest_path": str(manifest_path.resolve()),
        "report_path": str(report_path.resolve()),
        "preview_path": str(preview_path.resolve()),
    }
    if enhance_after:
        manifest["outputs"]["preview_original_path"] = str(preview_original_path.resolve())

    if enhance_after and not focus_targets:
        message = "开启 --enhance-after 前必须先确认本次优化的部位+效果；CLI 请重复传入 --focus \"部位:效果\""
        manifest["blocking_issues"].append(message)
        manifest["blocking_issue_count"] = len(manifest["blocking_issues"])
        manifest["status"] = "error"

    write_json(manifest_path, manifest)
    if enhance_after and manifest["status"] == "ok":
        render_board(manifest, preview_original_path, preview=True)
        manifest = apply_after_enhancements(manifest, inspect_root, enhance_slots or ANGLE_SLOTS[:])

    manifest["warning_count"] = len(manifest.get("warnings") or [])
    manifest["blocking_issue_count"] = len(manifest.get("blocking_issues") or [])
    write_text(report_path, build_report(manifest))
    if manifest.get("case_mode") == "body" and manifest.get("status") == "ok":
        render_body_board(manifest, preview_path)
    elif manifest.get("case_mode") != "body":
        render_board(manifest, preview_path, preview=True)
    write_json(manifest_path, manifest)

    exit_code = 0 if manifest["status"] == "ok" else 2
    return manifest, exit_code


def run_render(
    case_dir: Path,
    brand: dict,
    template: str,
    output_root: Path,
    semantic_judge_mode: str | None = None,
) -> tuple[dict, int]:
    inspect_manifest_path = output_root / "inspect" / "manifest.json"
    if not inspect_manifest_path.exists():
        raise FileNotFoundError(f"缺少 inspect 产物，请先运行 inspect: {inspect_manifest_path}")

    manifest = json.loads(inspect_manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "ok":
        raise ValueError("inspect 结果存在 blocking issues，render 已拒绝执行")
    if manifest.get("brand", {}).get("id") != brand["id"]:
        raise ValueError(f"render brand 与 inspect brand 不一致: {brand['id']} != {manifest.get('brand', {}).get('id')}")
    if manifest.get("template") != template:
        raise ValueError(f"render template 与 inspect template 不一致: {template} != {manifest.get('template')}")

    render_root = ensure_dir(output_root / "render")
    final_board_path = render_root / "final-board.jpg"
    final_manifest_path = render_root / "manifest.final.json"

    if manifest.get("case_mode") == "body":
        render_body_board(manifest, final_board_path)
    else:
        render_board(manifest, final_board_path, preview=False)
    effective_semantic_mode = normalize_semantic_judge_mode(semantic_judge_mode or manifest.get("semantic_judge_mode") or "auto")
    render_semantic_context = build_semantic_context(effective_semantic_mode)

    final_manifest = {
        **manifest,
        "command": "render",
        "rendered_at": now_iso(),
        "semantic_judge_mode": effective_semantic_mode,
        "semantic_final_qa": None,
        "outputs": {
            **manifest.get("outputs", {}),
            "final_board_path": str(final_board_path.resolve()),
            "final_manifest_path": str(final_manifest_path.resolve()),
        },
    }
    final_manifest["warnings"] = list(final_manifest.get("warnings") or [])
    final_manifest["blocking_issues"] = list(final_manifest.get("blocking_issues") or [])
    final_manifest["semantic_errors"] = list(final_manifest.get("semantic_errors") or [])

    if effective_semantic_mode == "auto":
        final_qa = safe_run_semantic_final_qa(
            str(final_board_path.resolve()),
            build_semantic_final_reference_paths(manifest),
            manifest.get("focus_targets") or [],
            render_semantic_context,
        )
        if final_qa:
            if not final_qa["left_right_ok"] or not final_qa["labels_ok"]:
                final_qa["decision"] = "reject"
            elif not final_qa["focus_present"] or not final_qa["enhancement_drift_ok"]:
                final_qa["decision"] = "review"
            final_manifest["semantic_final_qa"] = final_qa
            if final_qa["decision"] == "reject":
                final_manifest["status"] = "error"
                final_manifest["blocking_issues"].append(f"最终语义质检未通过：{final_qa['reason']}")
            elif final_qa["decision"] == "review":
                final_manifest["warnings"].append(f"最终语义质检建议人工复核：{final_qa['reason']}")

    final_manifest["semantic_errors"].extend(render_semantic_context.get("errors") or [])
    inspect_semantic_summary = final_manifest.get("semantic_summary") or {}
    render_semantic_summary = semantic_summary(render_semantic_context)
    final_manifest["semantic_summary"] = {
        key: int(inspect_semantic_summary.get(key, 0)) + int(render_semantic_summary.get(key, 0))
        for key in {
            *inspect_semantic_summary.keys(),
            *render_semantic_summary.keys(),
        }
    }
    final_manifest["warning_count"] = len(final_manifest.get("warnings") or [])
    final_manifest["blocking_issue_count"] = len(final_manifest.get("blocking_issues") or [])
    write_json(final_manifest_path, final_manifest)
    return final_manifest, (0 if final_manifest.get("status") == "ok" else 2)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="医美案例自动排版 skill 执行器")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_sub = subparsers.add_parser("inspect")
    inspect_sub.add_argument("case_dir", help="案例目录路径")
    inspect_sub.add_argument("--brand", required=True, choices=sorted(BRANDS.keys()))
    inspect_sub.add_argument("--template", default="tri-compare", choices=["tri-compare"])
    inspect_sub.add_argument("--out", help="输出根目录；默认写到 case_dir/.case-layout-board/<brand>/<template>")
    inspect_sub.add_argument("--enhance-after", action="store_true", help="对术后三角度图片做 4K 展示增强")
    inspect_sub.add_argument("--enhance-slots", default="all", help="增强角度，默认 all；可选 front,oblique,side")
    inspect_sub.add_argument("--focus", action="append", default=[], help="部位+效果，格式：部位:效果；可重复传参")
    inspect_sub.add_argument("--semantic-judge", choices=sorted(SEMANTIC_JUDGE_MODES), help="语义判官模式，默认 auto")
    inspect_sub.add_argument("--enhance-model", help="增强模型，默认 gemini-3-pro-image-preview-4k")

    render_sub = subparsers.add_parser("render")
    render_sub.add_argument("case_dir", help="案例目录路径")
    render_sub.add_argument("--brand", required=True, choices=sorted(BRANDS.keys()))
    render_sub.add_argument("--template", default="tri-compare", choices=["tri-compare"])
    render_sub.add_argument("--out", help="输出根目录；默认写到 case_dir/.case-layout-board/<brand>/<template>")
    render_sub.add_argument("--semantic-judge", choices=sorted(SEMANTIC_JUDGE_MODES), help="语义判官模式，默认继承 inspect")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    case_dir = Path(args.case_dir).resolve()
    if not case_dir.exists():
        raise FileNotFoundError(f"案例目录不存在: {case_dir}")

    brand = resolve_brand(args.brand)
    output_root = Path(args.out).resolve() if args.out else default_output_root(case_dir, args.brand, args.template)
    ensure_dir(output_root)

    if args.command == "inspect":
        manifest, exit_code = run_inspect(
            case_dir,
            brand,
            args.template,
            output_root,
            enhance_after=bool(args.enhance_after),
            enhance_slots=enhancement_slots_from_arg(args.enhance_slots),
            focus_targets=parse_focus_targets(args.focus),
            semantic_judge_mode=normalize_semantic_judge_mode(args.semantic_judge or "auto"),
            enhance_model=args.enhance_model,
        )
        print(json.dumps({
            "status": manifest["status"],
            "blocking_issue_count": manifest["blocking_issue_count"],
            "warning_count": manifest["warning_count"],
            "manifest_path": manifest["outputs"]["manifest_path"],
            "report_path": manifest["outputs"]["report_path"],
            "preview_path": manifest["outputs"]["preview_path"],
            "preview_original_path": manifest["outputs"].get("preview_original_path"),
        }, ensure_ascii=False))
        return exit_code

    if args.command == "render":
        final_manifest, exit_code = run_render(
            case_dir,
            brand,
            args.template,
            output_root,
            semantic_judge_mode=args.semantic_judge,
        )
        print(json.dumps({
            "status": final_manifest["status"],
            "final_board_path": final_manifest["outputs"]["final_board_path"],
            "final_manifest_path": final_manifest["outputs"]["final_manifest_path"],
            "semantic_final_qa": final_manifest.get("semantic_final_qa"),
        }, ensure_ascii=False))
        return exit_code

    raise ValueError(f"未知命令: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise
