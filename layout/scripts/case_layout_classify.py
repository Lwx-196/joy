#!/usr/bin/env python3
"""case_layout_classify.py

批量扫描案例目录，输出：
- classify-summary.json
- classify-summary.csv
- classify-summary.md
- classify-images.json
- classified/<bucket>/<customer>/<case>/<view>/...
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import shutil
import time
from collections import Counter
from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASE_LIBRARY_ROOT = PROJECT_ROOT / "医美资料/陈院案例(1)"
CASE_LAYOUT_PATH = Path(__file__).resolve().parent / "case_layout_board.py"
CASE_LAYOUT_ORGANIZE_PATH = Path(__file__).resolve().parent / "case_layout_organize.py"
CASE_LAYOUT_AUDIT_PATH = Path(__file__).resolve().parent / "case_layout_audit.py"
DEFAULT_SCREEN_CACHE_PATH = Path(__file__).resolve().parents[1] / ".cache" / "screen-cache.json"
SCREEN_CACHE_VERSION = 1
SCREEN_CACHE_DISABLED_VALUES = {"0", "false", "off", "no"}


def load_module(module_name: str, module_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CASE_LAYOUT = load_module("case_layout_board", CASE_LAYOUT_PATH)
CASE_LAYOUT_ORGANIZE = load_module("case_layout_organize", CASE_LAYOUT_ORGANIZE_PATH)
CASE_LAYOUT_AUDIT = load_module("case_layout_audit", CASE_LAYOUT_AUDIT_PATH)

PHASE_DIR_NAMES = set().union(*CASE_LAYOUT.PHASE_DIR_ALIASES.values(), {"术中"})
PHASE_PATH_HINTS = (
    ("术前", "术前"),
    ("治疗前", "术前"),
    ("操作前", "术前"),
    ("项目前", "术前"),
    ("注射前", "术前"),
    ("before", "术前"),
    ("pre", "术前"),
    ("术后", "术后"),
    ("治疗后", "术后"),
    ("操作后", "术后"),
    ("项目后", "术后"),
    ("注射后", "术后"),
    ("after", "术后"),
    ("post", "术后"),
)
PHASE_GROUP_HINTS = tuple(sorted(PHASE_DIR_NAMES))
DERIVED_DIR_NAMES = {".Temp", ".case-layout-board", ".case-layout-organize", ".case-layout-classify"}
DATE_RE = re.compile(r"(20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}|\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2})")
VIEW_LABEL_TO_KEY = {
    "正面": "front",
    "45侧": "oblique",
    "侧面": "side",
    "背面": "back",
    "局部": "partial",
    "其他": "other",
}
VIEW_KEY_TO_LABEL = {
    "front": "正面",
    "oblique": "45侧",
    "side": "侧面",
    "back": "背面",
    "partial": "局部",
    "other": "其他",
}
PHASE_TO_LABEL = {
    "before": "术前",
    "after": "术后",
    None: "不确定",
}
SHARPNESS_TO_QUALITY = {
    "clear": "good",
    "soft": "fair",
    "blurry": "poor",
    None: "poor",
}


def is_skipped_path(path: Path) -> bool:
    return any(part.startswith(".") or part in DERIVED_DIR_NAMES for part in path.parts if part not in {"."})


def has_any_images(directory: Path) -> bool:
    for file_path in directory.rglob("*"):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(directory)
        if is_skipped_path(rel):
            continue
        if CASE_LAYOUT.is_image_file(file_path):
            return True
    return False


def has_direct_images(directory: Path) -> bool:
    for item in directory.iterdir():
        if item.name.startswith("."):
            continue
        if item.is_file() and CASE_LAYOUT.is_image_file(item):
            return True
    return False


def has_phase_children(directory: Path) -> bool:
    for item in directory.iterdir():
        if not item.is_dir():
            continue
        if CASE_LAYOUT.phase_from_dir_name(item.name) and has_any_images(item):
            return True
    return False


def list_case_dir_infos(root_dir: Path) -> list[dict]:
    results = []

    def walk(current_dir: Path, depth: int) -> bool:
        subtree_has_images = False
        direct_images = False
        phase_children = False

        for item in current_dir.iterdir():
            if item.name.startswith(".") or item.name in DERIVED_DIR_NAMES:
                continue
            if item.is_file():
                if CASE_LAYOUT.is_image_file(item):
                    direct_images = True
                    subtree_has_images = True
                continue
            if not item.is_dir():
                continue
            if CASE_LAYOUT.phase_from_dir_name(item.name) and has_any_images(item):
                phase_children = True
            if walk(item, depth + 1):
                subtree_has_images = True

        if subtree_has_images and depth >= 1:
            results.append({
                "path": str(current_dir.resolve()),
                "depth": depth,
                "direct_images": direct_images,
                "phase_children": phase_children,
            })
        return subtree_has_images

    if root_dir.exists():
        walk(root_dir, 0)
    return results


def filter_specific_candidates(candidates: list[dict]) -> list[dict]:
    filtered = []
    by_path = {item["path"]: item for item in candidates}
    for candidate in candidates:
        candidate_path = Path(candidate["path"])
        parent_candidate = by_path.get(str(candidate_path.parent.resolve()))
        if CASE_LAYOUT.phase_from_dir_name(candidate_path.name) and parent_candidate and parent_candidate.get("phase_children"):
            continue
        if candidate["direct_images"] or candidate["phase_children"]:
            filtered.append(candidate)
            continue
        prefix = str(candidate_path) + "/"
        if not any(
            other["path"] != candidate["path"] and str(other["path"]).startswith(prefix)
            for other in candidates
        ):
            filtered.append(candidate)
    return filtered


def normalize_root_dir(root_dir: Path) -> Path:
    try:
        rel = root_dir.resolve().relative_to(CASE_LIBRARY_ROOT.resolve())
    except ValueError:
        return root_dir.resolve()

    if len(rel.parts) >= 2:
        return (CASE_LIBRARY_ROOT / rel.parts[0] / rel.parts[1]).resolve()
    return root_dir.resolve()


def make_case_meta(case_dir: Path) -> dict:
    case_dir = case_dir.resolve()
    try:
        rel = case_dir.relative_to(CASE_LIBRARY_ROOT.resolve())
        parts = rel.parts
        customer = parts[0] if parts else case_dir.parent.name
        case_name = parts[1] if len(parts) >= 2 else case_dir.name
    except ValueError:
        customer = case_dir.parent.name
        case_name = case_dir.name
    return {
        "customer": customer,
        "case_name": case_name,
        "case_dir": case_dir,
    }


def discover_case_dirs(root_dir: Path) -> list[dict]:
    if has_any_images(root_dir) and (has_direct_images(root_dir) or has_phase_children(root_dir) or DATE_RE.search(root_dir.name)):
        return [make_case_meta(root_dir)]

    normalized_root = normalize_root_dir(root_dir)
    if normalized_root != root_dir.resolve() and has_any_images(normalized_root):
        return [make_case_meta(normalized_root)]

    candidates = filter_specific_candidates(list_case_dir_infos(root_dir))
    if not candidates and has_any_images(root_dir):
        return [make_case_meta(root_dir)]

    return [make_case_meta(Path(item["path"])) for item in candidates]


def phase_guess_from_value(value: str | None) -> str:
    return PHASE_TO_LABEL.get(value, value or "不确定")


def map_view_label_to_key(view_label: str | None) -> tuple[str, str]:
    normalized = view_label or "其他"
    key = VIEW_LABEL_TO_KEY.get(normalized, "other")
    return key, normalized if normalized in VIEW_LABEL_TO_KEY else "其他"


def quality_from_sharpness(sharpness_level: str | None) -> str:
    return SHARPNESS_TO_QUALITY.get(sharpness_level, "poor")


def bucket_from_primary_category(primary_category: str) -> str:
    return primary_category if primary_category.startswith("ready_") else "manual-curation"


READY_CATEGORIES = {"ready_tri_compare", "ready_bi_compare", "ready_single_compare", "ready_body_dual_compare"}
BODY_CASE_KEYWORDS = ("颈纹", "直角肩", "瘦肩", "肩", "身体", "颈部")
PHASE_MISSING_MARKERS = ("文件名缺少术前/术后", "无法判定术前/术后")
BODY_VIEW_MISSING_MARKERS = ("无法判定身体视角", "body_view_missing")
BODY_SUBJECT_MARKERS = ("身体", "颈", "肩", "手")
BODY_STANDARD_VIEWS = ("front", "back", "oblique", "side")
QUALITY_BLOCK_MARKERS = ("清晰度差过大", "过糊", "图片过糊", "sharpness_gap", "blurry_image")
POSE_BLOCK_MARKERS = ("姿态差过大", "pose_delta_exceeded")
DIRECTION_BLOCK_MARKERS = ("方向不一致", "无法配对出术前/术后同方向")
AMBIGUOUS_BLOCK_MARKERS = ("命中过多显式候选", "ambiguous_candidates")
NO_LABELED_BLOCK_MARKERS = ("未找到带术前/术后命名的源图", "no_labeled_sources")
FACE_DETECTION_MARKERS = ("面部检测失败", "face_detection_failure")
SCREEN_TIMEOUT_MARKERS = ("批量单图判读超时", "screen_timeout")
JSON_PARSE_MARKERS = ("无法解析判读 JSON",)
NONFRONT_PRIMARY_CATEGORIES = {"front_only", "missing_nonfront"}
NONFRONT_VIEW_LABELS = {"45°侧", "45侧", "侧面", "背面"}
ACTION_PICK = "pick"
ACTION_ORGANIZE = "organize"
ACTION_RESHOOT_FRONT = "reshoot_front"
ACTION_RESHOOT_QUALITY = "reshoot_quality"
ACTION_RESHOOT_NONFRONT = "reshoot_nonfront"
ACTION_RESELECT_PAIR = "reselect_pair"
ACTION_REVIEW_CANDIDATES = "review_candidates"
ACTION_MANUAL_REVIEW = "manual_review"
ACTION_BODY_FOLLOWUP = "body_followup"
WORKFLOW_CONTINUE = "continue"
WORKFLOW_BLOCKED = "blocked"
CANDIDATE_REVIEW_LIMIT_PER_PHASE = 4
QUALITY_LABELS = {
    "good": "清晰",
    "fair": "可用偏软",
    "poor": "不可用/过糊",
    "unknown": "未知",
}
CLEAR_REASON_MARKERS = ("清晰", "完整", "可见", "面部", "正面", "侧面", "45")


def elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)


def screen_cache_enabled() -> bool:
    raw = os.environ.get("CASE_LAYOUT_CLASSIFY_SCREEN_CACHE")
    if raw is None:
        return True
    return raw.strip().lower() not in SCREEN_CACHE_DISABLED_VALUES


def screen_cache_path() -> Path:
    raw = os.environ.get("CASE_LAYOUT_SCREEN_CACHE_PATH")
    if raw:
        return Path(raw).expanduser().resolve()
    return DEFAULT_SCREEN_CACHE_PATH


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def screen_cache_fingerprint(image_path: Path) -> dict:
    resolved = image_path.resolve()
    stat = resolved.stat()
    return {
        "file_path": str(resolved),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sha256": file_sha256(resolved),
    }


class ScreenResultCache:
    def __init__(self, path: Path, enabled: bool):
        self.path = path
        self.enabled = enabled
        self.entries = {}
        self.dirty = False
        self.error_count = 0
        if enabled:
            self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.error_count += 1
            self.entries = {}
            return
        if payload.get("version") != SCREEN_CACHE_VERSION:
            self.entries = {}
            self.dirty = True
            return
        entries = payload.get("entries")
        self.entries = entries if isinstance(entries, dict) else {}

    def get(self, image_path: Path) -> tuple[dict | None, dict | None]:
        if not self.enabled:
            return None, None
        try:
            fingerprint = screen_cache_fingerprint(image_path)
        except OSError:
            self.error_count += 1
            return None, None
        entry = self.entries.get(fingerprint["file_path"])
        if not isinstance(entry, dict):
            return None, fingerprint
        if entry.get("version") != SCREEN_CACHE_VERSION:
            return None, fingerprint
        for field in ("size", "mtime_ns", "sha256"):
            if entry.get(field) != fingerprint[field]:
                return None, fingerprint
        result = entry.get("screen_result")
        if not isinstance(result, dict):
            return None, fingerprint
        return dict(result), fingerprint

    def set(self, fingerprint: dict | None, screen_result: dict) -> bool:
        if not self.enabled or not fingerprint:
            return False
        if screen_result.get("error"):
            return False
        self.entries[fingerprint["file_path"]] = {
            "version": SCREEN_CACHE_VERSION,
            "file_path": fingerprint["file_path"],
            "size": fingerprint["size"],
            "mtime_ns": fingerprint["mtime_ns"],
            "sha256": fingerprint["sha256"],
            "cached_at": CASE_LAYOUT.now_iso(),
            "screen_result": screen_result,
        }
        self.dirty = True
        return True

    def save(self) -> None:
        if not self.enabled or not self.dirty:
            return
        payload = {
            "version": SCREEN_CACHE_VERSION,
            "updated_at": CASE_LAYOUT.now_iso(),
            "entries": self.entries,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            self.error_count += 1


def build_case_copy_root(run_out_dir: Path, case_record: dict) -> Path:
    return (
        run_out_dir
        / "classified"
        / case_record["bucket"]
        / case_record["customer"]
        / case_record["case_name"]
    )


def discover_recursive_images(case_dir: Path) -> list[Path]:
    return [
        file_path
        for file_path in sorted(case_dir.rglob("*"))
        if file_path.is_file()
        and CASE_LAYOUT.is_image_file(file_path)
        and not is_skipped_path(file_path.relative_to(case_dir))
    ]


def relative_parts(case_dir: Path, image_path: Path) -> tuple[str, ...]:
    try:
        return image_path.relative_to(case_dir).parts
    except ValueError:
        return image_path.parts


def path_phase_hint(case_dir: Path, image_path: Path) -> str | None:
    phase, _source = CASE_LAYOUT.infer_phase_from_path(image_path, case_dir)
    if phase == "before":
        return "术前"
    if phase == "after":
        return "术后"
    parts = relative_parts(case_dir, image_path)
    for part in parts[:-1]:
        for token, phase in PHASE_PATH_HINTS:
            if token in part or token in part.lower():
                return phase
    return None


def screen_group_key(case_dir: Path, image_path: Path) -> str:
    parts = relative_parts(case_dir, image_path)
    if len(parts) >= 2:
        top_level = parts[0]
        phase = CASE_LAYOUT.phase_from_dir_name(top_level)
        if phase:
            label = "术前" if phase == "before" else "术后"
            return f"{label}:{top_level}"
        if "术中" in top_level:
            return f"术中:{top_level}"
    return "__mixed__"


def screen_timing_path(case_dir: Path, image_path: Path) -> str:
    try:
        return str(image_path.relative_to(case_dir))
    except ValueError:
        return str(image_path.resolve())


def run_grouped_screen_helper(case_dir: Path, image_paths: list[Path]) -> list[dict]:
    helper_start = time.perf_counter()
    grouped_paths = {}
    for image_path in image_paths:
        grouped_paths.setdefault(screen_group_key(case_dir, image_path), []).append(image_path)

    cache = ScreenResultCache(screen_cache_path(), screen_cache_enabled())
    results_by_path = {}
    fingerprints_by_path = {}
    group_timings = []
    cache_hit_count = 0
    cache_miss_count = 0
    cache_save_count = 0
    cache_lookup_ms = 0.0
    screen_call_total_ms = 0.0
    for group_key, paths in grouped_paths.items():
        group_start = time.perf_counter()
        lookup_start = time.perf_counter()
        miss_paths = []
        group_miss_paths = []
        group_save_paths = []
        group_error_paths = []
        group_hit_count = 0
        group_miss_count = 0
        for image_path in paths:
            resolved = str(image_path.resolve())
            cached_result, fingerprint = cache.get(image_path)
            if fingerprint:
                fingerprints_by_path[resolved] = fingerprint
            if cached_result is not None:
                results_by_path[resolved] = cached_result
                group_hit_count += 1
                cache_hit_count += 1
            else:
                miss_paths.append(image_path)
                if cache.enabled:
                    group_miss_count += 1
                    cache_miss_count += 1
                    group_miss_paths.append(screen_timing_path(case_dir, image_path))
        group_lookup_ms = elapsed_ms(lookup_start)
        cache_lookup_ms = round(cache_lookup_ms + group_lookup_ms, 3)

        raw_results = []
        screen_call_ms = 0.0
        if miss_paths:
            screen_call_start = time.perf_counter()
            raw_results = CASE_LAYOUT_ORGANIZE.run_screen_helper(miss_paths)
            screen_call_ms = elapsed_ms(screen_call_start)
            screen_call_total_ms = round(screen_call_total_ms + screen_call_ms, 3)
            if len(raw_results) != len(miss_paths):
                raise RuntimeError(f"单图判读结果数量不匹配: expected={len(miss_paths)} actual={len(raw_results)}")
            for image_path, raw_result in zip(miss_paths, raw_results):
                resolved = str(image_path.resolve())
                results_by_path[resolved] = raw_result
                fingerprint = fingerprints_by_path.get(resolved)
                if fingerprint is None and cache.enabled:
                    try:
                        fingerprint = screen_cache_fingerprint(image_path)
                        fingerprints_by_path[resolved] = fingerprint
                    except OSError:
                        cache.error_count += 1
                        fingerprint = None
                if raw_result.get("error"):
                    group_error_paths.append(screen_timing_path(case_dir, image_path))
                if cache.set(fingerprint, raw_result):
                    cache_save_count += 1
                    group_save_paths.append(screen_timing_path(case_dir, image_path))
        duration = elapsed_ms(group_start)
        group_timings.append({
            "group": group_key,
            "image_count": len(paths),
            "screen_image_count": len(miss_paths),
            "duration_ms": duration,
            "screen_call_ms": screen_call_ms,
            "screen_cache_hit_count": group_hit_count,
            "screen_cache_miss_count": group_miss_count,
            "screen_cache_save_count": len(group_save_paths),
            "screen_cache_lookup_ms": group_lookup_ms,
            "screen_cache_miss_paths": group_miss_paths,
            "screen_cache_save_paths": group_save_paths,
            "screen_error_paths": group_error_paths,
            "timeout_count": sum(1 for item in raw_results if item.get("error") == "screen_timeout"),
            "error_count": sum(1 for item in raw_results if item.get("error")),
        })

    cache.save()

    run_grouped_screen_helper.last_timing = {
        "duration_ms": elapsed_ms(helper_start),
        "group_count": len(group_timings),
        "groups": group_timings,
        "screen_call_ms": screen_call_total_ms,
        "screen_cache_enabled": cache.enabled,
        "screen_cache_path": str(cache.path),
        "screen_cache_hit_count": cache_hit_count,
        "screen_cache_miss_count": cache_miss_count,
        "screen_cache_save_count": cache_save_count,
        "screen_cache_error_count": cache.error_count,
        "screen_cache_lookup_ms": cache_lookup_ms,
    }
    return [results_by_path[str(image_path.resolve())] for image_path in image_paths]


def normalize_organize_record(case_dir: Path, image_path: Path, screen_result: dict, order_index: int) -> dict:
    sharpness_score, sharpness_level = CASE_LAYOUT_ORGANIZE.measure_sharpness(image_path)
    view_label = screen_result.get("view_guess") or "其他"
    view_key, normalized_view_label = map_view_label_to_key(view_label)
    reason = screen_result.get("reason") or screen_result.get("error") or ""
    quality = screen_result.get("quality") or sharpness_level
    quality_original = quality
    quality_override_reason = None
    if (
        quality == "poor"
        and bool(screen_result.get("usable", False))
        and sharpness_level != "poor"
        and view_key in {"front", "oblique", "side", "back"}
        and (screen_result.get("subject") in {"面部", "颈部", "身体", "手部"} or any(marker in reason for marker in CLEAR_REASON_MARKERS))
    ):
        quality = "fair"
        quality_override_reason = "semantic_usable_sharpness_clear"
    usable = bool(screen_result.get("usable", False)) and quality != "poor" and sharpness_level != "poor"
    phase_guess = screen_result.get("phase_guess") or "不确定"
    phase_hint = path_phase_hint(case_dir, image_path)
    if phase_hint:
        phase_guess = phase_hint
    return {
        "customer": make_case_meta(case_dir)["customer"],
        "case_name": make_case_meta(case_dir)["case_name"],
        "case_dir": str(case_dir.resolve()),
        "file_path": str(image_path.resolve()),
        "relative_path": str(image_path.relative_to(case_dir)),
        "route_source": "organize",
        "phase_guess": phase_guess,
        "view_guess": view_key,
        "view_guess_label": normalized_view_label,
        "direction_guess": screen_result.get("direction_guess") or "unknown",
        "subject": screen_result.get("subject") or "其他",
        "quality": quality,
        "quality_original": quality_original,
        "quality_override_reason": quality_override_reason,
        "usable": usable,
        "reason": reason,
        "sharpness_score": round(sharpness_score, 2),
        "sharpness_level": sharpness_level,
        "order_index": order_index,
        "copied_to": None,
    }


def organize_category(summary: dict) -> str:
    action = summary.get("action") or {}
    if action.get("recommended_flow") != "case-layout-board":
        return "manual-curation"
    template = action.get("recommended_template")
    if template == "tri-compare":
        return "ready_tri_compare"
    if template == "bi-compare":
        return "ready_bi_compare"
    if template == "single-compare":
        return "ready_single_compare"
    return "manual-curation"


def run_organize_case(case_meta: dict) -> tuple[dict, list[dict]]:
    case_start = time.perf_counter()
    case_dir = Path(case_meta["case_dir"])
    discover_start = time.perf_counter()
    image_paths = discover_recursive_images(case_dir)
    discover_ms = elapsed_ms(discover_start)
    if not image_paths:
        raise ValueError(f"目录内没有可分类图片: {case_dir}")

    screen_start = time.perf_counter()
    raw_results = run_grouped_screen_helper(case_dir, image_paths)
    screen_ms = elapsed_ms(screen_start)
    screen_timing = getattr(run_grouped_screen_helper, "last_timing", {})
    normalize_start = time.perf_counter()
    image_records = [
        normalize_organize_record(case_dir, image_path, raw_result, index)
        for index, (image_path, raw_result) in enumerate(zip(image_paths, raw_results), start=1)
    ]
    normalize_ms = elapsed_ms(normalize_start)

    summary_start = time.perf_counter()
    organize_records = []
    for item in image_records:
        organize_records.append({
            "name": Path(item["file_path"]).name,
            "path": item["file_path"],
            "order_index": item["order_index"],
            "phase_guess": item["phase_guess"],
            "view_guess": item["view_guess_label"],
            "subject": item["subject"],
            "quality": item["quality"],
            "sharpness_score": item["sharpness_score"],
            "sharpness_level": item["sharpness_level"],
            "usable": item["usable"],
            "reason": item["reason"],
            "direction_guess": item["direction_guess"],
        })

    summary = CASE_LAYOUT_ORGANIZE.build_summary(case_dir, organize_records)
    organize_summary_ms = elapsed_ms(summary_start)
    primary_category = organize_category(summary)
    case_record = {
        "customer": case_meta["customer"],
        "case_name": case_meta["case_name"],
        "case_dir": str(case_dir.resolve()),
        "route_source": "organize",
        "primary_category": primary_category,
        "bucket": bucket_from_primary_category(primary_category),
        "reason": (summary.get("action") or {}).get("reason") or primary_category,
        "image_count": len(image_records),
        "usable_image_count": sum(1 for item in image_records if item["usable"]),
        "copied_to": None,
    }
    case_record["_timing"] = {
        "route_source": "organize",
        "image_count": len(image_paths),
        "duration_ms": elapsed_ms(case_start),
        "discover_images_ms": discover_ms,
        "screen_ms": screen_ms,
        "screen_call_ms": screen_timing.get("screen_call_ms", 0.0),
        "screen_group_count": int(screen_timing.get("group_count") or 0),
        "screen_groups": screen_timing.get("groups") or [],
        "screen_cache_enabled": bool(screen_timing.get("screen_cache_enabled", False)),
        "screen_cache_path": screen_timing.get("screen_cache_path"),
        "screen_cache_hit_count": int(screen_timing.get("screen_cache_hit_count") or 0),
        "screen_cache_miss_count": int(screen_timing.get("screen_cache_miss_count") or 0),
        "screen_cache_save_count": int(screen_timing.get("screen_cache_save_count") or 0),
        "screen_cache_error_count": int(screen_timing.get("screen_cache_error_count") or 0),
        "screen_cache_lookup_ms": float(screen_timing.get("screen_cache_lookup_ms") or 0.0),
        "normalize_ms": normalize_ms,
        "organize_summary_ms": organize_summary_ms,
    }
    return case_record, image_records


def infer_inspect_view(entry: dict, case_mode: str) -> tuple[str, str]:
    if case_mode == "body" and entry.get("section"):
        key = entry.get("section")
        return key if key in VIEW_KEY_TO_LABEL else "other", VIEW_KEY_TO_LABEL.get(key, "其他")
    if entry.get("angle"):
        key = entry.get("angle")
        return key if key in VIEW_KEY_TO_LABEL else "other", VIEW_KEY_TO_LABEL.get(key, "其他")
    semantic_view = (entry.get("semantic_screen") or {}).get("view_guess")
    return map_view_label_to_key(semantic_view)


def infer_inspect_subject(entry: dict, case_mode: str) -> str:
    semantic_subject = (entry.get("semantic_screen") or {}).get("subject")
    if semantic_subject:
        return semantic_subject
    return "面部" if case_mode == "face" else "身体"


def build_inspect_image_record(case_meta: dict, case_mode: str, entry: dict) -> dict:
    view_key, view_label = infer_inspect_view(entry, case_mode)
    quality = quality_from_sharpness(entry.get("sharpness_level"))
    issues = entry.get("issues") or []
    reason = "；".join(issues[:2]) if issues else ((entry.get("semantic_screen") or {}).get("reason") or "")
    usable = not bool(entry.get("rejection_reason")) and quality != "poor"
    return {
        "customer": case_meta["customer"],
        "case_name": case_meta["case_name"],
        "case_dir": str(Path(case_meta["case_dir"]).resolve()),
        "file_path": entry["path"],
        "relative_path": entry.get("relative_path") or Path(entry["path"]).name,
        "route_source": "inspect",
        "phase_guess": phase_guess_from_value(entry.get("phase")),
        "view_guess": view_key,
        "view_guess_label": view_label,
        "direction_guess": entry.get("direction") or "unknown",
        "subject": infer_inspect_subject(entry, case_mode),
        "quality": quality,
        "usable": usable,
        "reason": reason or entry.get("rejection_reason") or "",
        "profile_fallback": entry.get("profile_fallback"),
        "sharpness_score": float(entry.get("sharpness_score") or 0.0),
        "sharpness_level": entry.get("sharpness_level"),
        "copied_to": None,
    }


def run_inspect_case(case_meta: dict, brand: dict) -> tuple[dict, list[dict]]:
    case_start = time.perf_counter()
    case_dir = Path(case_meta["case_dir"])
    manifest_start = time.perf_counter()
    manifest = CASE_LAYOUT.build_manifest(
        case_dir,
        brand,
        "tri-compare",
        semantic_judge_mode="off",
        body_visual_guard=False,
    )
    manifest_ms = elapsed_ms(manifest_start)
    audit_start = time.perf_counter()
    audit_record = CASE_LAYOUT_AUDIT.make_record(case_meta, manifest)
    audit_ms = elapsed_ms(audit_start)
    primary_category = audit_record["primary_category"]
    records_start = time.perf_counter()
    case_record = {
        "customer": case_meta["customer"],
        "case_name": case_meta["case_name"],
        "case_dir": str(case_dir.resolve()),
        "route_source": "inspect",
        "primary_category": primary_category,
        "bucket": bucket_from_primary_category(primary_category),
        "reason": audit_record.get("first_blocker") or ",".join(audit_record.get("effective_templates") or []) or primary_category,
        "image_count": 0,
        "usable_image_count": 0,
        "copied_to": None,
    }
    image_records = []
    case_mode = manifest.get("case_mode") or "face"
    for group in manifest.get("groups", []):
        for entry in group.get("entries", []):
            image_records.append(build_inspect_image_record(case_meta, case_mode, entry))
    case_record["image_count"] = len(image_records)
    case_record["usable_image_count"] = sum(1 for item in image_records if item["usable"])
    case_record["_timing"] = {
        "route_source": "inspect",
        "image_count": len(image_records),
        "duration_ms": elapsed_ms(case_start),
        "manifest_ms": manifest_ms,
        "audit_ms": audit_ms,
        "image_records_ms": elapsed_ms(records_start),
    }
    return case_record, image_records


def copy_case_images(run_out_dir: Path, case_record: dict, image_records: list[dict]) -> None:
    case_root = build_case_copy_root(run_out_dir, case_record)
    for image_record in image_records:
        src = Path(image_record["file_path"])
        rel_path = Path(image_record["relative_path"])
        dest = case_root / image_record["view_guess"] / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        image_record["copied_to"] = str(dest.resolve())
    case_record["copied_to"] = str(case_root.resolve())


def infer_reason_group(case_record: dict, image_records: list[dict]) -> str:
    primary_category = case_record["primary_category"]
    if primary_category in READY_CATEGORIES:
        return primary_category

    reason = str(case_record.get("reason") or "")
    case_name = str(case_record.get("case_name") or "")
    view_counts = Counter(item.get("view_guess") for item in image_records)

    if any(keyword in reason or keyword in case_name for keyword in BODY_CASE_KEYWORDS):
        return "body_case"
    if has_substantial_body_subject_evidence(image_records):
        return "body_case"
    if "缺少术前 正面" in reason or "缺少术后 正面" in reason:
        return "missing_front"
    if image_records and view_counts.get("partial", 0) >= max(1, int(len(image_records) * 0.75)):
        return "partial_only"
    if case_record.get("route_source") == "inspect":
        return "inspect_blocked"
    return "manual_curation"


def is_body_subject_marker_record(item: dict) -> bool:
    subject = str(item.get("subject") or "")
    reason = str(item.get("reason") or "")
    return any(marker in subject or marker in reason for marker in BODY_SUBJECT_MARKERS)


def body_subject_marker_threshold(image_count: int) -> int:
    return max(2, (image_count + 1) // 2)


def has_substantial_body_subject_evidence(image_records: list[dict]) -> bool:
    if not image_records:
        return False
    marker_count = sum(1 for item in image_records if is_body_subject_marker_record(item))
    return marker_count >= body_subject_marker_threshold(len(image_records))


def missing_front_phase(case_record: dict) -> str | None:
    reason = str(case_record.get("reason") or "")
    if "缺少术前 正面" in reason:
        return "术前"
    if "缺少术后 正面" in reason:
        return "术后"
    return None


def summarize_front_evidence(image_records: list[dict]) -> dict:
    front_records = [item for item in image_records if item.get("view_guess") == "front"]
    usable_front = [item for item in front_records if item.get("usable")]
    before_front = [item for item in usable_front if item.get("phase_guess") == "术前"]
    after_front = [item for item in usable_front if item.get("phase_guess") == "术后"]
    uncertain_front = [item for item in front_records if item.get("phase_guess") == "不确定"]
    phase_missing_records = [
        item for item in image_records
        if any(marker in str(item.get("reason") or "") for marker in PHASE_MISSING_MARKERS)
    ]
    return {
        "front_count": len(front_records),
        "usable_front_count": len(usable_front),
        "before_front_count": len(before_front),
        "after_front_count": len(after_front),
        "uncertain_front_count": len(uncertain_front),
        "phase_missing_count": len(phase_missing_records),
    }


def front_candidate_records(image_records: list[dict], phase: str) -> list[dict]:
    records = [
        item for item in image_records
        if item.get("view_guess") == "front"
        and item.get("usable")
        and item.get("phase_guess") == phase
    ]
    return sorted(
        records,
        key=lambda item: (
            item.get("order_index") if item.get("order_index") is not None else 10**9,
            str(item.get("relative_path") or item.get("file_path") or ""),
        ),
    )


def candidate_quality_label(quality: str | None) -> str:
    return QUALITY_LABELS.get(quality or "unknown", quality or "未知")


def build_front_candidate_review(image_records: list[dict]) -> dict:
    before_records = front_candidate_records(image_records, "术前")
    after_records = front_candidate_records(image_records, "术后")

    def serialize(records: list[dict], phase: str) -> list[dict]:
        candidates = []
        for index, item in enumerate(records[:CANDIDATE_REVIEW_LIMIT_PER_PHASE], start=1):
            candidates.append({
                "id": f"{phase}#{index}",
                "phase": phase,
                "view": "正面",
                "relative_path": item.get("relative_path") or Path(item.get("file_path") or "").name,
                "quality": item.get("quality") or "unknown",
                "quality_label": candidate_quality_label(item.get("quality")),
                "sharpness_score": item.get("sharpness_score"),
                "direction_guess": item.get("direction_guess") or "unknown",
            })
        return candidates

    return {
        "view": "正面",
        "before_total": len(before_records),
        "after_total": len(after_records),
        "before": serialize(before_records, "术前"),
        "after": serialize(after_records, "术后"),
        "before_omitted": max(0, len(before_records) - CANDIDATE_REVIEW_LIMIT_PER_PHASE),
        "after_omitted": max(0, len(after_records) - CANDIDATE_REVIEW_LIMIT_PER_PHASE),
    }


def format_candidate_entry(candidate: dict, include_quality: bool = True) -> str:
    entry = f"{candidate.get('id')} {candidate.get('relative_path')}"
    if not include_quality:
        return entry
    sharpness = candidate.get("sharpness_score")
    if isinstance(sharpness, (int, float)) and sharpness > 0:
        return f"{entry}（{candidate.get('quality_label') or '未知'}，清晰度 {sharpness:.2f}）"
    return f"{entry}（{candidate.get('quality_label') or '未知'}）"


def format_candidate_group(review: dict, key: str, label: str) -> str:
    candidates = review.get(key) or []
    if not candidates:
        return f"{label}候选：暂无"
    omitted = int(review.get(f"{key}_omitted") or 0)
    text = "；".join(format_candidate_entry(candidate) for candidate in candidates)
    if omitted:
        text = f"{text}；另有 {omitted} 张未列出"
    return f"{label}候选：{text}"


def format_front_candidate_review(review: dict) -> str:
    return "；".join((
        format_candidate_group(review, "before", "术前"),
        format_candidate_group(review, "after", "术后"),
    ))


def format_candidate_command(review: dict) -> str:
    candidates = (review.get("before") or [])[:2] + (review.get("after") or [])[:2]
    if not candidates:
        return "候选复核正面后再分类"
    entries = "；".join(
        f"{candidate.get('id')}={candidate.get('relative_path')}"
        for candidate in candidates
    )
    return f"候选复核正面：{entries} 后再分类"


def infer_missing_front_action(case_record: dict, image_records: list[dict]) -> str:
    evidence = summarize_front_evidence(image_records)
    if evidence["before_front_count"] > 0 and evidence["after_front_count"] > 0:
        return ACTION_REVIEW_CANDIDATES
    if evidence["front_count"] > 0 and (
        evidence["uncertain_front_count"] > 0 or evidence["phase_missing_count"] > 0
    ):
        return ACTION_ORGANIZE
    return ACTION_RESHOOT_FRONT


def is_body_evidence_record(item: dict) -> bool:
    subject = str(item.get("subject") or "")
    if any(marker in subject for marker in BODY_SUBJECT_MARKERS):
        return True
    if item.get("view_guess") in BODY_STANDARD_VIEWS:
        return True
    reason = str(item.get("reason") or "")
    return any(marker in reason for marker in BODY_SUBJECT_MARKERS)


def summarize_body_evidence(image_records: list[dict]) -> dict:
    body_records = [item for item in image_records if is_body_evidence_record(item)]
    if not body_records:
        body_records = list(image_records)
    usable_records = [item for item in body_records if item.get("usable")]
    before_records = [item for item in usable_records if item.get("phase_guess") == "术前"]
    after_records = [item for item in usable_records if item.get("phase_guess") == "术后"]
    uncertain_phase_records = [item for item in body_records if item.get("phase_guess") == "不确定"]
    phase_missing_records = [
        item for item in body_records
        if any(marker in str(item.get("reason") or "") for marker in PHASE_MISSING_MARKERS)
    ]
    body_view_missing_records = [
        item for item in body_records
        if item.get("view_guess") == "other"
        or any(marker in str(item.get("reason") or "") for marker in BODY_VIEW_MISSING_MARKERS)
    ]
    view_counts = Counter(item.get("view_guess") or "other" for item in body_records)
    usable_view_counts = Counter(item.get("view_guess") or "other" for item in usable_records)
    return {
        "body_count": len(body_records),
        "usable_body_count": len(usable_records),
        "before_count": len(before_records),
        "after_count": len(after_records),
        "uncertain_phase_count": len(uncertain_phase_records),
        "phase_missing_count": len(phase_missing_records),
        "body_view_missing_count": len(body_view_missing_records),
        "view_counts": dict(view_counts),
        "usable_view_counts": dict(usable_view_counts),
    }


def missing_body_phase_from_evidence(case_record: dict, image_records: list[dict]) -> str | None:
    reason = str(case_record.get("reason") or "")
    if "缺少术前" in reason:
        return "术前"
    if "缺少术后" in reason:
        return "术后"
    evidence = summarize_body_evidence(image_records)
    if evidence["before_count"] == 0 and evidence["after_count"] > 0:
        return "术前"
    if evidence["after_count"] == 0 and evidence["before_count"] > 0:
        return "术后"
    if evidence["before_count"] == 0 and evidence["after_count"] == 0:
        return "术前/术后"
    return None


def infer_body_action(case_record: dict, image_records: list[dict]) -> str:
    if case_record.get("route_source") == "organize":
        return ACTION_ORGANIZE
    evidence = summarize_body_evidence(image_records)
    if evidence["usable_body_count"] == 0:
        if evidence["uncertain_phase_count"] > 0 or evidence["body_view_missing_count"] > 0:
            return ACTION_ORGANIZE
        return ACTION_MANUAL_REVIEW
    if missing_body_phase_from_evidence(case_record, image_records):
        return ACTION_BODY_FOLLOWUP
    if evidence["body_view_missing_count"] > 0 or evidence["phase_missing_count"] > 0:
        return ACTION_ORGANIZE
    return ACTION_MANUAL_REVIEW


def text_has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def count_records_with_markers(image_records: list[dict], markers: tuple[str, ...]) -> int:
    return sum(1 for item in image_records if text_has_any(str(item.get("reason") or ""), markers))


def format_count_summary(counts: dict, key_order: tuple[str, ...], labels: dict[str, str]) -> str:
    parts = []
    for key in key_order:
        count = int(counts.get(key) or 0)
        if count:
            parts.append(f"{labels.get(key, key)} {count} 张")
    return "、".join(parts) if parts else "无"


def summarize_general_evidence(image_records: list[dict]) -> dict:
    view_counts = Counter(item.get("view_guess") or "other" for item in image_records)
    usable_view_counts = Counter(
        item.get("view_guess") or "other"
        for item in image_records
        if item.get("usable")
    )
    phase_counts = Counter(item.get("phase_guess") or "不确定" for item in image_records)
    quality_counts = Counter(item.get("quality") or "unknown" for item in image_records)
    return {
        "image_count": len(image_records),
        "usable_count": sum(1 for item in image_records if item.get("usable")),
        "view_counts": dict(view_counts),
        "usable_view_counts": dict(usable_view_counts),
        "phase_counts": dict(phase_counts),
        "quality_counts": dict(quality_counts),
        "phase_missing_count": count_records_with_markers(image_records, PHASE_MISSING_MARKERS),
        "face_detection_failure_count": count_records_with_markers(image_records, FACE_DETECTION_MARKERS),
        "screen_timeout_count": count_records_with_markers(image_records, SCREEN_TIMEOUT_MARKERS),
        "json_parse_error_count": count_records_with_markers(image_records, JSON_PARSE_MARKERS),
    }


def blocked_view_label(case_record: dict) -> str | None:
    reason = str(case_record.get("reason") or "")
    if "45°侧" in reason or "45侧" in reason:
        return "45侧"
    for label in ("侧面", "背面", "正面"):
        if label in reason:
            return label
    return None


def blocked_phase_label(case_record: dict) -> str | None:
    reason = str(case_record.get("reason") or "")
    if "术前术后" in reason:
        return None
    if "术前" in reason:
        return "术前"
    if "术后" in reason:
        return "术后"
    return None


def inspect_blocker_kind(case_record: dict, image_records: list[dict]) -> str:
    reason = str(case_record.get("reason") or "")
    primary_category = str(case_record.get("primary_category") or "")
    view_label = blocked_view_label(case_record)
    evidence = summarize_general_evidence(image_records)

    if primary_category == "ambiguous_candidates" or text_has_any(reason, AMBIGUOUS_BLOCK_MARKERS):
        return "ambiguous_candidates"
    if primary_category == "no_labeled_sources" or text_has_any(reason, NO_LABELED_BLOCK_MARKERS):
        return "no_labeled_sources"
    if text_has_any(reason, QUALITY_BLOCK_MARKERS):
        return "quality"
    if text_has_any(reason, DIRECTION_BLOCK_MARKERS) or text_has_any(reason, POSE_BLOCK_MARKERS):
        if primary_category in NONFRONT_PRIMARY_CATEGORIES or view_label in NONFRONT_VIEW_LABELS:
            return "nonfront"
        return "pair_mismatch"
    if primary_category in NONFRONT_PRIMARY_CATEGORIES:
        return "nonfront"
    if evidence["face_detection_failure_count"] > 0:
        return "face_detection"
    return "unstable"


def infer_inspect_blocked_action(case_record: dict, image_records: list[dict]) -> str:
    kind = inspect_blocker_kind(case_record, image_records)
    if kind in {"ambiguous_candidates", "no_labeled_sources"}:
        return ACTION_ORGANIZE
    if kind == "quality":
        return ACTION_RESHOOT_QUALITY
    if kind == "nonfront":
        return ACTION_RESHOOT_NONFRONT
    if kind == "pair_mismatch":
        return ACTION_RESELECT_PAIR
    return ACTION_MANUAL_REVIEW


def infer_recommended_action(case_record: dict, reason_group: str, image_records: list[dict] | None = None) -> str:
    if reason_group in READY_CATEGORIES:
        return ACTION_PICK
    if reason_group == "body_case":
        return infer_body_action(case_record, image_records or [])
    if case_record.get("route_source") == "organize":
        return ACTION_ORGANIZE
    if reason_group == "missing_front":
        return infer_missing_front_action(case_record, image_records or [])
    if reason_group == "inspect_blocked":
        return infer_inspect_blocked_action(case_record, image_records or [])
    return ACTION_MANUAL_REVIEW


def build_recommended_command(
    case_record: dict,
    recommended_action: str,
    reason_group: str,
    image_records: list[dict] | None = None,
) -> str:
    if recommended_action == ACTION_PICK:
        return f"案例挑图 {case_record.get('copied_to') or case_record['case_dir']}"
    if recommended_action == ACTION_RESHOOT_FRONT:
        phase = missing_front_phase(case_record)
        if phase:
            return f"补拍{phase}正面后再分类"
        return "补拍术前/术后正面后再分类"
    if recommended_action == ACTION_RESHOOT_QUALITY:
        phase = blocked_phase_label(case_record)
        view = blocked_view_label(case_record)
        target = "".join(item for item in (phase, view) if item)
        if target:
            return f"重拍或替换{target}清晰图后再分类"
        return "重拍或替换清晰术前术后图后再分类"
    if recommended_action == ACTION_RESHOOT_NONFRONT:
        view = blocked_view_label(case_record)
        if view and view != "正面":
            return f"补拍或重选{view}同方向图后再分类"
        return "补拍或重选45侧/侧面同方向图后再分类"
    if recommended_action == ACTION_RESELECT_PAIR:
        view = blocked_view_label(case_record) or "同角度"
        return f"重选{view}同姿态术前术后图后再分类"
    if recommended_action == ACTION_REVIEW_CANDIDATES:
        return format_candidate_command(build_front_candidate_review(image_records or []))
    if recommended_action == ACTION_ORGANIZE:
        return f"案例整理 {case_record['case_dir']}"
    if recommended_action == ACTION_BODY_FOLLOWUP:
        phase = missing_body_phase_from_evidence(case_record, image_records or [])
        if phase:
            return f"补齐{phase}身体/颈纹图后再分类"
        return "补齐身体/颈纹术前术后图后再分类"
    if recommended_action == ACTION_MANUAL_REVIEW and reason_group == "inspect_blocked":
        if inspect_blocker_kind(case_record, image_records or []) == "face_detection":
            return "人工复核面部图后再分类"
    return "人工挑图后再分类"


def infer_workflow_state(recommended_action: str) -> str:
    if recommended_action in {ACTION_PICK, ACTION_ORGANIZE}:
        return WORKFLOW_CONTINUE
    return WORKFLOW_BLOCKED


def build_action_title(
    recommended_action: str,
    reason_group: str,
    case_record: dict | None = None,
    image_records: list[dict] | None = None,
) -> str:
    if recommended_action == ACTION_PICK:
        return "进入案例挑图"
    if recommended_action == ACTION_ORGANIZE:
        if reason_group == "missing_front":
            return "整理命名"
        if reason_group == "body_case":
            return "整理身体案例"
        if reason_group == "inspect_blocked":
            kind = inspect_blocker_kind(case_record or {}, image_records or [])
            if kind == "ambiguous_candidates":
                return "整理候选"
            if kind == "no_labeled_sources":
                return "整理命名"
        return "先整理案例"
    if recommended_action == ACTION_RESHOOT_FRONT:
        return "补拍正面"
    if recommended_action == ACTION_RESHOOT_QUALITY:
        return "重拍清晰图"
    if recommended_action == ACTION_RESHOOT_NONFRONT:
        return "补拍/重选非正面"
    if recommended_action == ACTION_RESELECT_PAIR:
        return "重选配对图"
    if recommended_action == ACTION_REVIEW_CANDIDATES:
        return "候选复核"
    if recommended_action == ACTION_BODY_FOLLOWUP:
        return "补齐身体/颈纹图"
    if recommended_action == ACTION_MANUAL_REVIEW and reason_group == "missing_front":
        return "人工挑图"
    if recommended_action == ACTION_MANUAL_REVIEW and reason_group == "body_case":
        return "人工复核身体图"
    if recommended_action == ACTION_MANUAL_REVIEW and reason_group == "inspect_blocked":
        kind = inspect_blocker_kind(case_record or {}, image_records or [])
        if kind == "face_detection":
            return "人工复核面部图"
    return "人工复核后再继续"


def build_missing_front_blocking_reason(
    case_record: dict,
    recommended_action: str,
    image_records: list[dict],
) -> str:
    reason = str(case_record.get("reason") or "").strip()
    evidence = summarize_front_evidence(image_records)
    phase = missing_front_phase(case_record) or "术前/术后"
    if recommended_action == ACTION_ORGANIZE:
        return (
            f"{reason or '当前案例正面对照阶段不清'}；"
            f"已发现正面图 {evidence['front_count']} 张，"
            f"阶段不确定正面 {evidence['uncertain_front_count']} 张，"
            f"阶段/命名缺失记录 {evidence['phase_missing_count']} 张，优先整理命名。"
        )
    if recommended_action == ACTION_REVIEW_CANDIDATES:
        review = build_front_candidate_review(image_records)
        return (
            f"{reason or '当前案例正面对照需要人工确认'}；"
            f"已找到术前/术后可用正面候选"
            f"（术前 {evidence['before_front_count']} 张、术后 {evidence['after_front_count']} 张），"
            "但自动分组或配对未通过。"
            f"{format_front_candidate_review(review)}。"
            "请按编号各选 1 张术前正面和术后正面，再执行分类或挑图。"
        )
    if recommended_action == ACTION_MANUAL_REVIEW:
        return (
            f"{reason or '当前案例正面对照需要人工确认'}；"
            f"已找到术前/术后可用正面候选"
            f"（术前 {evidence['before_front_count']} 张、术后 {evidence['after_front_count']} 张），"
            "但自动分组或配对未通过。"
        )
    if evidence["front_count"] == 0:
        return f"{reason or f'缺少{phase} 正面'}；当前未识别到正面图。"
    if evidence["usable_front_count"] == 0:
        return f"{reason or f'缺少{phase} 正面'}；当前没有可用正面图，请补拍清晰{phase}正面。"
    return (
        f"{reason or f'缺少{phase} 正面'}；"
        f"当前可用正面只覆盖一侧阶段"
        f"（术前 {evidence['before_front_count']} 张、术后 {evidence['after_front_count']} 张）。"
    )


def format_body_view_summary(evidence: dict) -> str:
    view_counts = evidence.get("usable_view_counts") or evidence.get("view_counts") or {}
    parts = []
    for key in ("front", "back", "oblique", "side", "partial", "other"):
        count = int(view_counts.get(key) or 0)
        if count:
            parts.append(f"{VIEW_KEY_TO_LABEL.get(key, key)} {count} 张")
    return "、".join(parts) if parts else "暂无清晰身体视角"


def build_body_blocking_reason(
    case_record: dict,
    recommended_action: str,
    image_records: list[dict],
) -> str:
    reason = str(case_record.get("reason") or "").strip()
    evidence = summarize_body_evidence(image_records)
    view_summary = format_body_view_summary(evidence)
    base = (
        f"{reason or '当前案例识别为身体/颈纹类'}；"
        f"可用术前 {evidence['before_count']} 张、可用术后 {evidence['after_count']} 张，"
        f"可用视角：{view_summary}。"
    )
    if recommended_action == ACTION_ORGANIZE:
        return (
            f"{base}"
            f"阶段不确定 {evidence['uncertain_phase_count']} 张、"
            f"身体视角不清 {evidence['body_view_missing_count']} 张；"
            "请先整理命名，分清术前/术后和正面/背面/45侧。"
        )
    if recommended_action == ACTION_BODY_FOLLOWUP:
        phase = missing_body_phase_from_evidence(case_record, image_records) or "术前/术后"
        return (
            f"{base}"
            f"当前缺少{phase}可用身体/颈纹图，需补齐对应阶段后再分类。"
        )
    if evidence["usable_body_count"] == 0:
        return (
            f"{reason or '当前案例识别为身体/颈纹类'}；"
            "没有可用身体/颈纹候选，请人工复核清晰度、阶段和视角。"
        )
    return (
        f"{base}"
        "已有身体/颈纹候选，但自动视角配对未通过，请人工挑出可对比的身体视角。"
    )


def format_general_view_summary(evidence: dict, usable: bool = True) -> str:
    counts = evidence.get("usable_view_counts" if usable else "view_counts") or {}
    return format_count_summary(
        counts,
        ("front", "oblique", "side", "back", "partial", "other"),
        VIEW_KEY_TO_LABEL,
    )


def format_general_phase_summary(evidence: dict) -> str:
    return format_count_summary(
        evidence.get("phase_counts") or {},
        ("术前", "术后", "术中", "不确定"),
        {"术前": "术前", "术后": "术后", "术中": "术中", "不确定": "阶段不确定"},
    )


def format_quality_summary(evidence: dict) -> str:
    return format_count_summary(
        evidence.get("quality_counts") or {},
        ("good", "fair", "poor", "unknown"),
        {"good": "清晰", "fair": "可用但偏软", "poor": "不可用/过糊", "unknown": "未知"},
    )


def build_inspect_blocking_reason(
    case_record: dict,
    recommended_action: str,
    image_records: list[dict],
) -> str:
    reason = str(case_record.get("reason") or "").strip()
    evidence = summarize_general_evidence(image_records)
    view_summary = format_general_view_summary(evidence)
    phase_summary = format_general_phase_summary(evidence)
    quality_summary = format_quality_summary(evidence)
    base = (
        f"{reason or '当前案例 inspect 未通过'}；"
        f"可用 {evidence['usable_count']}/{evidence['image_count']} 张，"
        f"可用视角：{view_summary}，阶段：{phase_summary}。"
    )
    kind = inspect_blocker_kind(case_record, image_records)
    view = blocked_view_label(case_record)
    phase = blocked_phase_label(case_record)
    target = "".join(item for item in (phase, view) if item)

    if recommended_action == ACTION_ORGANIZE:
        if kind == "ambiguous_candidates":
            return (
                f"{base}"
                "命中过多显式候选，先整理命名或缩小候选范围，确保每个阶段/角度只保留少量清晰候选。"
            )
        if kind == "no_labeled_sources":
            return (
                f"{reason or '未找到带术前/术后命名的源图'}；"
                "请先把源图按术前/术后和角度整理命名，再执行案例分类。"
            )
        return f"{base}请先整理术前/术后和角度命名，再执行案例分类。"
    if recommended_action == ACTION_RESHOOT_QUALITY:
        return (
            f"{base}"
            f"清晰度统计：{quality_summary}；"
            f"请重拍或替换{target or '对应角度'}清晰图后再分类。"
        )
    if recommended_action == ACTION_RESHOOT_NONFRONT:
        target_view = view if view and view != "正面" else "45侧/侧面"
        return (
            f"{base}"
            f"当前非正面角度无法形成可出图配对，请补拍或重选{target_view}同方向清晰图。"
        )
    if recommended_action == ACTION_RESELECT_PAIR:
        return (
            f"{base}"
            f"已找到候选但{view or '对应角度'}术前术后姿态或方向不一致，请人工重选同角度、同方向、同姿态配对图。"
        )
    if kind == "face_detection":
        return (
            f"{base}"
            f"面部检测失败 {evidence['face_detection_failure_count']} 张；"
            "请剔除遮挡、非脸或过糊图，并补充清晰可识别面部图后再分类。"
        )
    return f"{base}请人工复核角度、阶段和成对关系后再继续。"


def build_manual_curation_blocking_reason(case_record: dict, image_records: list[dict]) -> str:
    reason = str(case_record.get("reason") or "").strip()
    evidence = summarize_general_evidence(image_records)
    view_summary = format_general_view_summary(evidence)
    phase_summary = format_general_phase_summary(evidence)
    quality_summary = format_quality_summary(evidence)
    base = (
        f"{reason or '当前案例暂未形成可直接排版候选'}；"
        f"可用 {evidence['usable_count']}/{evidence['image_count']} 张，"
        f"可用视角：{view_summary}，阶段：{phase_summary}，清晰度：{quality_summary}。"
    )
    if evidence["screen_timeout_count"] > 0:
        return (
            f"{base}"
            f"单图判读超时 {evidence['screen_timeout_count']} 张，建议先缩小目录或按术前/术后/角度分文件夹后再分类。"
        )
    if evidence["phase_missing_count"] > 0 or (evidence["phase_counts"].get("不确定") or 0) > 0:
        return (
            f"{base}"
            f"阶段/命名缺失 {evidence['phase_missing_count']} 张，请先整理命名，把术前/术后和角度分清。"
        )
    if evidence["json_parse_error_count"] > 0:
        return (
            f"{base}"
            f"单图判读结果不稳定 {evidence['json_parse_error_count']} 张，请先整理候选并剔除无法判断的图片。"
        )
    if evidence["usable_count"] == 0:
        return f"{base}暂无可用候选，请补充清晰面部图或移除非案例图后再分类。"
    return f"{base}请先整理候选配对，再继续分类或出图。"


def build_blocking_reason(
    case_record: dict,
    recommended_action: str,
    reason_group: str,
    image_records: list[dict] | None = None,
) -> str:
    reason = str(case_record.get("reason") or "").strip()
    if reason_group == "missing_front":
        return build_missing_front_blocking_reason(case_record, recommended_action, image_records or [])
    if reason_group == "body_case":
        return build_body_blocking_reason(case_record, recommended_action, image_records or [])
    if reason_group == "inspect_blocked":
        return build_inspect_blocking_reason(case_record, recommended_action, image_records or [])
    if reason_group in {"manual_curation", "partial_only"} and recommended_action == ACTION_ORGANIZE:
        return build_manual_curation_blocking_reason(case_record, image_records or [])
    if recommended_action == ACTION_BODY_FOLLOWUP:
        return "当前案例识别为身体/颈纹类，不走面部标准案例模板"
    if recommended_action in {ACTION_RESHOOT_QUALITY, ACTION_RESHOOT_NONFRONT, ACTION_RESELECT_PAIR}:
        return build_inspect_blocking_reason(case_record, recommended_action, image_records or [])
    if recommended_action == ACTION_REVIEW_CANDIDATES:
        return build_missing_front_blocking_reason(case_record, recommended_action, image_records or [])
    if recommended_action == ACTION_MANUAL_REVIEW:
        return reason or "当前案例自动分类还不稳定，需要人工复核"
    return ""


def build_action_message(
    case_record: dict,
    recommended_action: str,
    reason_group: str,
    image_records: list[dict] | None = None,
) -> str:
    if recommended_action == ACTION_PICK:
        return "分类已完成，可直接进入案例挑图。"
    if recommended_action == ACTION_ORGANIZE:
        if reason_group == "missing_front":
            return "已发现正面图，但术前/术后命名或阶段不清；先执行案例整理，把正面图分清术前术后后再继续。"
        if reason_group == "body_case":
            return "身体/颈纹图阶段或视角还不清；先执行案例整理，把术前/术后和正面/背面/45侧分清后再继续。"
        if reason_group == "inspect_blocked":
            kind = inspect_blocker_kind(case_record, image_records or [])
            if kind == "ambiguous_candidates":
                return "当前候选命中过多，先整理命名并收敛每个阶段/角度的候选，再执行案例分类。"
            if kind == "no_labeled_sources":
                return "当前未找到术前/术后命名源图，请先按术前/术后和角度整理命名，再执行案例分类。"
        if reason_group in {"manual_curation", "partial_only"}:
            return "当前散图阶段或候选还不稳定；请先按术前/术后和正面/45侧/侧面整理命名后再分类。"
        return "当前案例更适合先整理术前术后和角度，再继续分类或出图。"
    if recommended_action == ACTION_RESHOOT_FRONT:
        if reason_group == "missing_front":
            phase = missing_front_phase(case_record) or "术前/术后"
            return f"当前案例缺少{phase}正面，请补拍对应阶段的清晰正面后，再执行案例分类。"
        return "当前案例缺少术前或术后正面，先补拍正面后，再执行案例分类。"
    if recommended_action == ACTION_RESHOOT_QUALITY:
        target = "".join(item for item in (blocked_phase_label(case_record), blocked_view_label(case_record)) if item)
        if target:
            return f"当前案例{target}清晰度不稳定，请重拍或替换对应清晰图后，再执行案例分类。"
        return "当前案例清晰度不稳定，请重拍或替换对应阶段/角度的清晰图后，再执行案例分类。"
    if recommended_action == ACTION_RESHOOT_NONFRONT:
        view = blocked_view_label(case_record)
        if view and view != "正面":
            return f"当前案例{view}无法形成同方向术前术后配对，请补拍或重选同方向清晰图后，再执行案例分类。"
        return "当前案例缺少可配对的45侧/侧面角度，请补拍或重选同方向非正面图后，再执行案例分类。"
    if recommended_action == ACTION_RESELECT_PAIR:
        view = blocked_view_label(case_record) or "对应角度"
        return f"当前案例{view}术前术后姿态或方向不一致，请人工重选同角度同姿态配对图后，再执行案例分类。"
    if recommended_action == ACTION_REVIEW_CANDIDATES:
        review = build_front_candidate_review(image_records or [])
        before_count = int(review.get("before_total") or 0)
        after_count = int(review.get("after_total") or 0)
        return (
            f"已找到术前 {before_count} 张、术后 {after_count} 张正面候选，但自动配对未通过；"
            "请按候选编号复核并选出 1 组正面对照后再继续。"
        )
    if recommended_action == ACTION_BODY_FOLLOWUP:
        phase = missing_body_phase_from_evidence(case_record, image_records or [])
        if phase:
            return f"当前案例缺少{phase}身体/颈纹图，请补齐对应阶段的清晰身体视角后，再执行案例分类。"
        return "当前案例缺少身体/颈纹术前术后配对图，请补齐清晰身体视角后，再执行案例分类。"
    if recommended_action == ACTION_MANUAL_REVIEW and reason_group == "missing_front":
        return "已找到术前/术后正面候选，但自动分组或配对未通过；请人工挑出正面对照后再继续。"
    if recommended_action == ACTION_MANUAL_REVIEW and reason_group == "body_case":
        return "已识别为身体/颈纹案例，但自动配对未通过；请人工挑出可对比的身体视角后再继续。"
    if recommended_action == ACTION_MANUAL_REVIEW and reason_group == "inspect_blocked":
        kind = inspect_blocker_kind(case_record, image_records or [])
        if kind == "face_detection":
            return "部分图片未检测到面部，请先人工剔除遮挡、非脸或过糊图，并补充清晰面部图后再分类。"
    return "当前案例自动分类还不稳定，请先人工复核角度、阶段和成对关系，再继续。"


def enrich_case_record(case_record: dict, image_records: list[dict]) -> dict:
    reason_group = infer_reason_group(case_record, image_records)
    recommended_action = infer_recommended_action(case_record, reason_group, image_records)
    workflow_state = infer_workflow_state(recommended_action)
    case_record["reason_group"] = reason_group
    case_record["workflow_state"] = workflow_state
    case_record["recommended_action"] = recommended_action
    if recommended_action == ACTION_REVIEW_CANDIDATES:
        case_record["candidate_review"] = build_front_candidate_review(image_records)
    else:
        case_record.pop("candidate_review", None)
    case_record["action_title"] = build_action_title(
        recommended_action,
        reason_group,
        case_record,
        image_records,
    )
    case_record["blocking_reason"] = build_blocking_reason(
        case_record,
        recommended_action,
        reason_group,
        image_records,
    )
    case_record["action_message"] = build_action_message(
        case_record,
        recommended_action,
        reason_group,
        image_records,
    )
    case_record["recommended_command"] = build_recommended_command(
        case_record,
        recommended_action,
        reason_group,
        image_records,
    )
    return case_record


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_summary_csv(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "customer",
        "case_name",
        "case_dir",
        "route_source",
        "primary_category",
        "bucket",
        "reason_group",
        "workflow_state",
        "recommended_action",
        "action_title",
        "blocking_reason",
        "action_message",
        "recommended_command",
        "reason",
        "image_count",
        "usable_image_count",
        "copied_to",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in records:
            writer.writerow({field: row.get(field, "") for field in fields})


def build_summary_markdown(summary: dict) -> str:
    lines = [
        "# case-layout-board 精细化批量分类",
        "",
        f"- 时间: `{summary['created_at']}`",
        f"- 根目录: `{summary['root_dir']}`",
        f"- 品牌: `{summary['brand']}`",
        f"- 案例数: `{summary['case_count']}`",
        f"- 图片数: `{summary['image_count']}`",
        "",
        "## 案例级分类",
        "",
    ]
    for bucket, count in sorted(summary["bucket_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- `{bucket}`: {count}")

    lines.extend(["", "## 图片级角度统计", ""])
    for view_key, count in sorted(summary["view_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- `{view_key}` ({VIEW_KEY_TO_LABEL.get(view_key, view_key)}): {count}")

    lines.extend(["", "## 案例明细", ""])
    for record in summary["records"][:40]:
        lines.append(
            f"- `{record['customer']} / {record['case_name']}`"
            f" | route=`{record['route_source']}`"
            f" | category=`{record['primary_category']}`"
            f" | bucket=`{record['bucket']}`"
            f" | reason_group=`{record.get('reason_group')}`"
            f" | workflow=`{record.get('workflow_state')}`"
            f" | action=`{record.get('recommended_action')}`"
            f" | title=`{record.get('action_title')}`"
        )
        if record.get("blocking_reason"):
            lines.append(f"  blocking: {record['blocking_reason']}")
        if record.get("action_message"):
            lines.append(f"  message: {record['action_message']}")
        if record.get("recommended_command"):
            lines.append(f"  next: `{record['recommended_command']}`")
        if record.get("reason"):
            lines.append(f"  reason: {record['reason']}")

    return "\n".join(lines).strip() + "\n"


def build_run_out_dir(root_dir: Path, out_dir: str | None) -> Path:
    run_id = CASE_LAYOUT.now_iso().replace(":", "").replace("+", "_").replace("-", "").replace(".", "")
    if out_dir:
        return Path(out_dir).resolve() / run_id
    return root_dir.resolve() / ".case-layout-classify" / run_id


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量精细化分类医美案例目录")
    parser.add_argument("root_dir", help="案例库根目录、客户目录或案例目录")
    parser.add_argument("--brand", default="fumei", choices=sorted(CASE_LAYOUT.BRANDS.keys()))
    parser.add_argument("--out", help="输出根目录；默认写到 <root_dir>/.case-layout-classify/<timestamp>")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    total_start = time.perf_counter()
    args = parse_args(argv)
    root_dir = Path(args.root_dir).resolve()
    if not root_dir.exists():
        raise FileNotFoundError(f"目录不存在: {root_dir}")

    setup_start = time.perf_counter()
    brand = CASE_LAYOUT.resolve_brand(args.brand)
    brand_resolve_ms = elapsed_ms(setup_start)
    discover_start = time.perf_counter()
    case_dirs = discover_case_dirs(root_dir)
    case_discovery_ms = elapsed_ms(discover_start)
    if not case_dirs:
        raise ValueError(f"未在 {root_dir} 发现可分类案例目录")

    output_dir_start = time.perf_counter()
    run_out_dir = build_run_out_dir(root_dir, args.out)
    run_out_dir.mkdir(parents=True, exist_ok=True)
    output_dir_ms = elapsed_ms(output_dir_start)

    case_records = []
    image_records = []
    case_timings = []
    for case_meta in case_dirs:
        case_start = time.perf_counter()
        case_dir = Path(case_meta["case_dir"])
        route_start = time.perf_counter()
        has_labeled_images = CASE_LAYOUT_AUDIT.contains_labeled_images(case_dir)
        route_decision_ms = elapsed_ms(route_start)
        if has_labeled_images:
            case_record, records = run_inspect_case(case_meta, brand)
        else:
            case_record, records = run_organize_case(case_meta)
        analysis_timing = case_record.pop("_timing", {})
        copy_start = time.perf_counter()
        copy_case_images(run_out_dir, case_record, records)
        copy_ms = elapsed_ms(copy_start)
        enrich_start = time.perf_counter()
        enrich_case_record(case_record, records)
        enrich_ms = elapsed_ms(enrich_start)
        case_records.append(case_record)
        image_records.extend(records)
        case_timing = {
            "customer": case_record["customer"],
            "case_name": case_record["case_name"],
            "case_dir": case_record["case_dir"],
            "route_source": case_record["route_source"],
            "image_count": len(records),
            "duration_ms": elapsed_ms(case_start),
            "route_decision_ms": route_decision_ms,
            "analysis_ms": analysis_timing.get("duration_ms"),
            "copy_ms": copy_ms,
            "enrich_ms": enrich_ms,
        }
        if analysis_timing:
            case_timing["analysis"] = analysis_timing
            if analysis_timing.get("screen_ms") is not None:
                case_timing["screen_ms"] = analysis_timing.get("screen_ms")
                case_timing["screen_group_count"] = analysis_timing.get("screen_group_count", 0)
        case_timings.append(case_timing)

    bucket_counts = Counter(record["bucket"] for record in case_records)
    category_counts = Counter(record["primary_category"] for record in case_records)
    view_counts = Counter(record["view_guess"] for record in image_records)
    screen_total_ms = round(sum(float(item.get("screen_ms") or 0.0) for item in case_timings), 3)
    screen_call_total_ms = round(sum(
        float((item.get("analysis") or {}).get("screen_call_ms") or 0.0)
        for item in case_timings
    ), 3)
    screen_cache_hit_count = sum(
        int((item.get("analysis") or {}).get("screen_cache_hit_count") or 0)
        for item in case_timings
    )
    screen_cache_miss_count = sum(
        int((item.get("analysis") or {}).get("screen_cache_miss_count") or 0)
        for item in case_timings
    )
    screen_cache_save_count = sum(
        int((item.get("analysis") or {}).get("screen_cache_save_count") or 0)
        for item in case_timings
    )
    screen_cache_error_count = sum(
        int((item.get("analysis") or {}).get("screen_cache_error_count") or 0)
        for item in case_timings
    )
    screen_cache_lookup_ms = round(sum(
        float((item.get("analysis") or {}).get("screen_cache_lookup_ms") or 0.0)
        for item in case_timings
    ), 3)
    screen_cache_enabled_any = any(
        bool((item.get("analysis") or {}).get("screen_cache_enabled"))
        for item in case_timings
    )
    copy_total_ms = round(sum(float(item.get("copy_ms") or 0.0) for item in case_timings), 3)
    enrich_total_ms = round(sum(float(item.get("enrich_ms") or 0.0) for item in case_timings), 3)
    inspect_total_ms = round(sum(
        float(item.get("analysis_ms") or 0.0)
        for item in case_timings
        if item.get("route_source") == "inspect"
    ), 3)
    organize_total_ms = round(sum(
        float(item.get("analysis_ms") or 0.0)
        for item in case_timings
        if item.get("route_source") == "organize"
    ), 3)
    timing = {
        "version": 1,
        "total_ms": 0.0,
        "brand_resolve_ms": brand_resolve_ms,
        "case_discovery_ms": case_discovery_ms,
        "output_dir_ms": output_dir_ms,
        "case_processing_ms": round(sum(float(item.get("duration_ms") or 0.0) for item in case_timings), 3),
        "screen_total_ms": screen_total_ms,
        "screen_call_total_ms": screen_call_total_ms,
        "screen_cache_enabled": screen_cache_enabled_any,
        "screen_cache_hit_count": screen_cache_hit_count,
        "screen_cache_miss_count": screen_cache_miss_count,
        "screen_cache_save_count": screen_cache_save_count,
        "screen_cache_error_count": screen_cache_error_count,
        "screen_cache_lookup_ms": screen_cache_lookup_ms,
        "inspect_total_ms": inspect_total_ms,
        "organize_total_ms": organize_total_ms,
        "copy_total_ms": copy_total_ms,
        "enrich_total_ms": enrich_total_ms,
        "write_outputs_ms": 0.0,
        "case_count": len(case_timings),
        "cases": case_timings,
    }
    summary = {
        "created_at": CASE_LAYOUT.now_iso(),
        "root_dir": str(root_dir),
        "brand": args.brand,
        "out_dir": str(run_out_dir.resolve()),
        "case_count": len(case_records),
        "image_count": len(image_records),
        "bucket_counts": dict(bucket_counts),
        "category_counts": dict(category_counts),
        "view_counts": dict(view_counts),
        "records": case_records,
        "timing": timing,
    }

    summary_json_path = run_out_dir / "classify-summary.json"
    summary_csv_path = run_out_dir / "classify-summary.csv"
    summary_md_path = run_out_dir / "classify-summary.md"
    images_json_path = run_out_dir / "classify-images.json"

    write_start = time.perf_counter()
    write_json(summary_json_path, summary)
    write_summary_csv(summary_csv_path, case_records)
    summary_md_path.write_text(build_summary_markdown(summary), encoding="utf-8")
    write_json(images_json_path, image_records)
    timing["write_outputs_ms"] = elapsed_ms(write_start)
    timing["total_ms"] = elapsed_ms(total_start)
    summary["timing"] = timing
    write_json(summary_json_path, summary)

    print(json.dumps({
        "root_dir": str(root_dir),
        "out_dir": str(run_out_dir.resolve()),
        "case_count": len(case_records),
        "image_count": len(image_records),
        "summary_json_path": str(summary_json_path.resolve()),
        "summary_csv_path": str(summary_csv_path.resolve()),
        "summary_md_path": str(summary_md_path.resolve()),
        "images_json_path": str(images_json_path.resolve()),
        "bucket_counts": dict(bucket_counts),
        "view_counts": dict(view_counts),
        "timing": timing,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
