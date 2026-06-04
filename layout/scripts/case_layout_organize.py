#!/usr/bin/env python3
"""case_layout_organize.py

整理无术前/术后命名的案例素材目录，只输出候选清单，不自动改名。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import cv2
from PIL import Image, ImageDraw, ImageFont, ImageOps


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASE_LAYOUT_PATH = Path(__file__).resolve().parent / "case_layout_board.py"
CASE_LAYOUT_SPEC = importlib.util.spec_from_file_location("case_layout_board", CASE_LAYOUT_PATH)
if CASE_LAYOUT_SPEC is None or CASE_LAYOUT_SPEC.loader is None:
    raise RuntimeError(f"无法加载 case_layout_board.py: {CASE_LAYOUT_PATH}")
CASE_LAYOUT = importlib.util.module_from_spec(CASE_LAYOUT_SPEC)
CASE_LAYOUT_SPEC.loader.exec_module(CASE_LAYOUT)

SCREEN_SCRIPT = Path(__file__).resolve().parent / "case_layout_screen.js"

VIEW_ORDER = ["正面", "45侧", "侧面", "局部", "其他"]
QUALITY_SCORE = {"good": 30, "fair": 12, "poor": 0}
PHASE_SCORE = {"术前": 30, "术后": 30, "不确定": 0}
DEFAULT_SCREEN_TIMEOUT_SEC = 90.0
DEFAULT_SCREEN_BATCH_SIZE = 12
CLASSIFY_VIEW_LABELS = {
    "front": "正面",
    "oblique": "45侧",
    "side": "侧面",
    "partial": "局部",
    "back": "背面",
    "other": "其他",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def discover_images(case_dir: Path) -> list[Path]:
    direct_images = [
        item
        for item in sorted(case_dir.iterdir())
        if item.is_file() and CASE_LAYOUT.is_image_file(item) and not item.name.startswith(".")
    ]
    if direct_images:
        return direct_images
    phase_child_images = []
    for child in sorted(item for item in case_dir.iterdir() if item.is_dir() and not item.name.startswith(".")):
        if not CASE_LAYOUT.phase_from_dir_name(child.name):
            continue
        phase_child_images.extend(
            item
            for item in sorted(child.rglob("*"))
            if item.is_file() and CASE_LAYOUT.is_image_file(item) and not any(part.startswith(".") for part in item.relative_to(case_dir).parts)
        )
    return phase_child_images


def measure_sharpness(image_path: Path) -> tuple[float, str]:
    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return 0.0, "poor"
    score = float(cv2.Laplacian(img, cv2.CV_64F).var())
    if score < CASE_LAYOUT.SHARPNESS_BLURRY_THRESHOLD:
        return score, "poor"
    if score < CASE_LAYOUT.SHARPNESS_SOFT_THRESHOLD:
        return score, "fair"
    return score, "good"


def screen_timeout_seconds() -> float:
    for env_name in ("CASE_LAYOUT_ORGANIZE_SCREEN_TIMEOUT_SEC", "CASE_LAYOUT_SCREEN_TIMEOUT_SEC"):
        raw = os.environ.get(env_name)
        if raw:
            try:
                timeout = float(raw)
            except ValueError:
                continue
            if timeout > 0:
                return timeout
    return DEFAULT_SCREEN_TIMEOUT_SEC


def screen_batch_size() -> int:
    raw = os.environ.get("CASE_LAYOUT_ORGANIZE_SCREEN_BATCH_SIZE")
    if raw:
        try:
            batch_size = int(raw)
        except ValueError:
            return DEFAULT_SCREEN_BATCH_SIZE
        if batch_size > 0:
            return batch_size
    return DEFAULT_SCREEN_BATCH_SIZE


def chunk_image_paths(image_paths: list[Path], batch_size: int | None = None) -> list[list[Path]]:
    if not image_paths:
        return []
    size = batch_size or screen_batch_size()
    if size <= 0:
        size = DEFAULT_SCREEN_BATCH_SIZE
    return [image_paths[index:index + size] for index in range(0, len(image_paths), size)]


def timeout_screen_results(image_paths: list[Path], timeout_sec: float) -> list[dict]:
    message = f"批量单图判读超时({CASE_LAYOUT.format_timeout_seconds(timeout_sec)})，已降级为人工整理"
    return [
        {
            "usable": False,
            "phase_guess": "不确定",
            "view_guess": "其他",
            "direction_guess": "unknown",
            "subject": "其他",
            "quality": "poor",
            "reason": message,
            "error": "screen_timeout",
        }
        for _ in image_paths
    ]


def run_screen_batch(image_paths: list[Path], timeout_sec: float) -> list[dict]:
    try:
        proc = subprocess.run(
            ["node", str(SCREEN_SCRIPT), *[str(path) for path in image_paths]],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            check=False,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return timeout_screen_results(image_paths, timeout_sec)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "单图判读脚本执行失败")
    return json.loads(proc.stdout)


def run_screen_helper(image_paths: list[Path]) -> list[dict]:
    timeout_sec = screen_timeout_seconds()
    results = []
    for batch in chunk_image_paths(image_paths):
        results.extend(run_screen_batch(batch, timeout_sec))
    return results


def normalize_record(image_path: Path, screen_result: dict, order_index: int, case_dir: Path | None = None) -> dict:
    sharpness_score, sharpness_level = measure_sharpness(image_path)
    quality = screen_result.get("quality") or sharpness_level
    phase_guess = screen_result.get("phase_guess") or "不确定"
    if case_dir:
        path_phase, _source = CASE_LAYOUT.infer_phase_from_path(image_path, case_dir)
        if path_phase == "before":
            phase_guess = "术前"
        elif path_phase == "after":
            phase_guess = "术后"
    view_guess = screen_result.get("view_guess") or "其他"
    subject = screen_result.get("subject") or "其他"
    reason = screen_result.get("reason") or screen_result.get("error") or ""
    if (
        quality == "poor"
        and bool(screen_result.get("usable", False))
        and sharpness_level != "poor"
        and view_guess in {"正面", "45侧", "侧面", "背面"}
        and (subject in {"面部", "颈部", "身体", "手部"} or any(marker in reason for marker in ("清晰", "完整", "可见")))
    ):
        quality = "fair"
    usable = bool(screen_result.get("usable", False)) and quality != "poor" and sharpness_level != "poor"

    return {
        "name": image_path.name,
        "path": str(image_path.resolve()),
        "order_index": order_index,
        "phase_guess": phase_guess,
        "view_guess": view_guess,
        "subject": subject,
        "quality": quality,
        "sharpness_score": round(sharpness_score, 2),
        "sharpness_level": sharpness_level,
        "usable": usable,
        "reason": reason,
        "error": screen_result.get("error"),
    }


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def classify_view_label(record: dict) -> str:
    label = str(record.get("view_guess_label") or "").strip()
    if label:
        return label
    view_key = str(record.get("view_guess") or "").strip()
    return CLASSIFY_VIEW_LABELS.get(view_key, view_key or "其他")


def normalize_classify_record(case_dir: Path, record: dict, fallback_order_index: int) -> dict:
    image_path = Path(str(record.get("file_path") or record.get("path") or "")).resolve()
    if not image_path.exists():
        raise FileNotFoundError(f"复用判读记录指向的图片不存在: {image_path}")
    if not is_relative_to(image_path, case_dir):
        raise ValueError(f"复用判读记录不属于当前案例目录: {image_path}")
    if not CASE_LAYOUT.is_image_file(image_path):
        raise ValueError(f"复用判读记录不是图片文件: {image_path}")

    sharpness_score = record.get("sharpness_score")
    if sharpness_score is None:
        sharpness_score, sharpness_level = measure_sharpness(image_path)
    else:
        sharpness_score = float(sharpness_score)
        sharpness_level = str(record.get("sharpness_level") or "")
        if not sharpness_level:
            if sharpness_score < CASE_LAYOUT.SHARPNESS_BLURRY_THRESHOLD:
                sharpness_level = "poor"
            elif sharpness_score < CASE_LAYOUT.SHARPNESS_SOFT_THRESHOLD:
                sharpness_level = "fair"
            else:
                sharpness_level = "good"

    quality = str(record.get("quality") or sharpness_level or "unknown")
    usable = bool(record.get("usable", False)) and quality != "poor" and sharpness_level != "poor"

    return {
        "name": image_path.name,
        "path": str(image_path),
        "order_index": int(record.get("order_index") or fallback_order_index),
        "phase_guess": str(record.get("phase_guess") or "不确定"),
        "view_guess": classify_view_label(record),
        "subject": str(record.get("subject") or "其他"),
        "quality": quality,
        "sharpness_score": round(float(sharpness_score or 0.0), 2),
        "sharpness_level": sharpness_level,
        "usable": usable,
        "reason": str(record.get("reason") or record.get("error") or ""),
        "error": record.get("error"),
        "direction_guess": record.get("direction_guess") or "unknown",
    }


def load_classify_records(case_dir: Path, records_path: Path) -> list[dict]:
    payload = json.loads(records_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"复用判读记录必须是 classify-images.json 数组: {records_path}")

    normalized = []
    for index, record in enumerate(payload, start=1):
        if not isinstance(record, dict):
            continue
        record_case_dir = record.get("case_dir")
        if record_case_dir and Path(str(record_case_dir)).resolve() != case_dir:
            continue
        image_path = Path(str(record.get("file_path") or record.get("path") or "")).resolve()
        if not is_relative_to(image_path, case_dir):
            continue
        normalized.append(normalize_classify_record(case_dir, record, index))

    if not normalized:
        raise ValueError(f"未在复用判读记录中找到当前案例目录: {case_dir}")
    return sorted(normalized, key=lambda item: (item["order_index"], item["path"]))


def candidate_score(item: dict) -> float:
    score = 0.0
    score += QUALITY_SCORE.get(item["quality"], 0)
    score += min(item["sharpness_score"], 120.0)
    score += 10 if item["usable"] else 0
    return score


def build_candidates(records: list[dict]) -> dict:
    phase_groups = {"术前": [], "术后": [], "不确定": []}
    for item in records:
        phase_groups.setdefault(item["phase_guess"], []).append(item)

    candidates = {}
    for phase, items in phase_groups.items():
        ranked = sorted(items, key=lambda item: (candidate_score(item), -item["order_index"]), reverse=True)
        candidates[phase] = ranked
    return candidates


def build_pair_suggestions(records: list[dict]) -> list[dict]:
    usable = [item for item in records if item["usable"]]
    by_view_phase = defaultdict(lambda: {"术前": [], "术后": []})
    for item in usable:
        phase = item["phase_guess"]
        if phase not in ("术前", "术后"):
            continue
        by_view_phase[item["view_guess"]][phase].append(item)

    suggestions = []
    for view in VIEW_ORDER:
        items = by_view_phase.get(view)
        if not items:
            continue
        before_items = sorted(items["术前"], key=lambda item: candidate_score(item), reverse=True)
        after_items = sorted(items["术后"], key=lambda item: candidate_score(item), reverse=True)
        if before_items and after_items:
            suggestions.append({
                "view": view,
                "before": before_items[0],
                "after": after_items[0],
            })
    return suggestions


def summarize_action(records: list[dict], suggestions: list[dict]) -> dict:
    subjects = Counter(item["subject"] for item in records if item["subject"])
    dominant_subject = subjects.most_common(1)[0][0] if subjects else "其他"
    available_views = [item["view"] for item in suggestions]
    if dominant_subject != "面部":
        return {
            "recommended_flow": "manual-curation",
            "reason": f"主体更接近{dominant_subject}，不建议直接进入当前面部排版链路",
        }
    if "正面" in available_views and len(available_views) >= 3:
        return {
            "recommended_flow": "case-layout-board",
            "recommended_template": "tri-compare",
            "reason": "已有术前/术后候选，且至少覆盖正面+两组非正面",
        }
    if "正面" in available_views and len(available_views) >= 2:
        return {
            "recommended_flow": "case-layout-board",
            "recommended_template": "bi-compare",
            "reason": "已有正面对比，且还能补出一组非正面候选",
        }
    if "正面" in available_views:
        return {
            "recommended_flow": "case-layout-board",
            "recommended_template": "single-compare",
            "reason": "当前只找到正面对比候选，建议先做单正面对比",
        }
    return {
        "recommended_flow": "manual-curation",
        "reason": "当前未找到可直接进入排版的术前/术后正面对比候选",
    }


def build_summary(case_dir: Path, records: list[dict]) -> dict:
    candidates = build_candidates(records)
    suggestions = build_pair_suggestions(records)
    return {
        "created_at": now_iso(),
        "case_dir": str(case_dir.resolve()),
        "image_count": len(records),
        "subject_counts": dict(Counter(item["subject"] for item in records)),
        "phase_counts": dict(Counter(item["phase_guess"] for item in records)),
        "view_counts": dict(Counter(item["view_guess"] for item in records)),
        "usable_count": sum(1 for item in records if item["usable"]),
        "candidates": {
            phase: [
                {
                    "name": item["name"],
                    "view_guess": item["view_guess"],
                    "quality": item["quality"],
                    "sharpness_score": item["sharpness_score"],
                    "usable": item["usable"],
                    "reason": item["reason"],
                }
                for item in items[:6]
            ]
            for phase, items in candidates.items()
        },
        "pair_suggestions": [
            {
                "view": pair["view"],
                "before": {
                    "name": pair["before"]["name"],
                    "quality": pair["before"]["quality"],
                    "sharpness_score": pair["before"]["sharpness_score"],
                },
                "after": {
                    "name": pair["after"]["name"],
                    "quality": pair["after"]["quality"],
                    "sharpness_score": pair["after"]["sharpness_score"],
                },
            }
            for pair in suggestions
        ],
        "action": summarize_action(records, suggestions),
        "records": records,
    }


def draw_text(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, font, fill) -> None:
    draw.text((x, y), text, font=font, fill=fill)


def render_preview(records: list[dict], output_path: Path) -> Path:
    thumb_w, thumb_h = 280, 200
    card_h = 310
    cols = 3
    rows = math.ceil(len(records) / cols) if records else 1
    margin = 24
    gap = 18
    board_w = margin * 2 + cols * thumb_w + (cols - 1) * gap
    board_h = margin * 2 + rows * card_h + (rows - 1) * gap
    canvas = Image.new("RGB", (board_w, board_h), (244, 239, 231))

    title_font = CASE_LAYOUT.load_font(20, bold=True)
    text_font = CASE_LAYOUT.load_font(15)
    small_font = CASE_LAYOUT.load_font(13)

    for idx, item in enumerate(records):
        row = idx // cols
        col = idx % cols
        x = margin + col * (thumb_w + gap)
        y = margin + row * (card_h + gap)

        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((x, y, x + thumb_w, y + card_h), radius=18, fill=(255, 255, 255), outline=(222, 214, 204))

        try:
            img = Image.open(item["path"]).convert("RGB")
            img = ImageOps.contain(img, (thumb_w - 16, thumb_h - 16))
        except Exception:
            img = Image.new("RGB", (thumb_w - 16, thumb_h - 16), (225, 220, 214))
        img_x = x + (thumb_w - img.width) // 2
        img_y = y + 8
        canvas.paste(img, (img_x, img_y))

        status_fill = (93, 140, 94) if item["usable"] else (176, 94, 83)
        draw.rounded_rectangle((x + 12, y + thumb_h - 8, x + 92, y + thumb_h + 18), radius=12, fill=status_fill)
        draw_text(draw, x + 22, y + thumb_h - 3, "可用" if item["usable"] else "待确认", small_font, (255, 255, 255))

        text_y = y + thumb_h + 34
        draw_text(draw, x + 12, text_y, item["name"], title_font, (60, 52, 44))
        draw_text(draw, x + 12, text_y + 28, f"{item['phase_guess']} · {item['view_guess']} · {item['subject']}", text_font, (102, 91, 82))
        draw_text(draw, x + 12, text_y + 52, f"quality={item['quality']} · sharpness={item['sharpness_score']}", text_font, (102, 91, 82))
        reason = item["reason"] or item.get("error") or ""
        draw_text(draw, x + 12, text_y + 76, reason[:40], small_font, (122, 111, 101))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, "JPEG", quality=92)
    return output_path


def build_report(summary: dict) -> str:
    lines = [
        "# case-layout-organize 报告",
        "",
        f"- 时间: `{summary['created_at']}`",
        f"- 目录: `{summary['case_dir']}`",
        f"- 图片数: `{summary['image_count']}`",
        f"- 可用数: `{summary['usable_count']}`",
        f"- 主体分布: `{json.dumps(summary['subject_counts'], ensure_ascii=False)}`",
        f"- 术前术后分布: `{json.dumps(summary['phase_counts'], ensure_ascii=False)}`",
        f"- 角度分布: `{json.dumps(summary['view_counts'], ensure_ascii=False)}`",
        "",
        "## 建议动作",
        "",
        f"- flow: `{summary['action']['recommended_flow']}`",
    ]
    if summary["action"].get("recommended_template"):
        lines.append(f"- template: `{summary['action']['recommended_template']}`")
    lines.append(f"- 原因: {summary['action']['reason']}")
    lines.append("")

    lines.extend(["## 候选配对", ""])
    if summary["pair_suggestions"]:
        for item in summary["pair_suggestions"]:
            lines.append(
                f"- `{item['view']}`: 术前 `{item['before']['name']}` / 术后 `{item['after']['name']}`"
            )
    else:
        lines.append("- 暂无可直接使用的术前/术后配对")
    lines.append("")

    lines.extend(["## 单图清单", ""])
    for item in summary["records"]:
        lines.append(
            f"- `{item['name']}` | {item['phase_guess']} | {item['view_guess']} | "
            f"{item['subject']} | {item['quality']} | sharpness={item['sharpness_score']} | "
            f"{'可用' if item['usable'] else '待确认'}"
            + (f" | {item['reason']}" if item['reason'] else "")
        )
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="整理无术前/术后命名的案例素材目录")
    parser.add_argument("case_dir", help="素材目录路径")
    parser.add_argument("--out", help="输出目录，默认 <case_dir>/.case-layout-organize")
    parser.add_argument("--records-json", help="复用 classify-images.json 中已有判读记录（自动续跑使用）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    case_dir = Path(args.case_dir).resolve()
    if not case_dir.exists():
        raise FileNotFoundError(f"目录不存在: {case_dir}")

    records_source_path = Path(args.records_json).resolve() if args.records_json else None
    if records_source_path:
        records = load_classify_records(case_dir, records_source_path)
    else:
        image_paths = discover_images(case_dir)
        if not image_paths:
            raise ValueError(f"目录内没有可整理图片: {case_dir}")

        raw_results = run_screen_helper(image_paths)
        records = [
            normalize_record(image_path, raw_result, index, case_dir=case_dir)
            for index, (image_path, raw_result) in enumerate(zip(image_paths, raw_results), start=1)
        ]
    summary = build_summary(case_dir, records)
    if records_source_path:
        summary["screen_source"] = "classify-images"
        summary["records_source_path"] = str(records_source_path)

    out_dir = Path(args.out).resolve() if args.out else case_dir / ".case-layout-organize"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "organize-summary.json"
    report_path = out_dir / "organize-report.md"
    preview_path = out_dir / "organize-preview.jpg"

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(build_report(summary), encoding="utf-8")
    render_preview(records, preview_path)

    print(json.dumps({
        "case_dir": str(case_dir),
        "summary_path": str(summary_path.resolve()),
        "report_path": str(report_path.resolve()),
        "preview_path": str(preview_path.resolve()),
        "recommended_flow": summary["action"]["recommended_flow"],
        "recommended_template": summary["action"].get("recommended_template"),
        "screen_source": summary.get("screen_source", "live-screen"),
        "records_source_path": summary.get("records_source_path"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
