"""T69 primary-signal enrichment for source image metadata.

This script reads real source images from the live case DB and merges local CV
signals into `cases.skill_image_metadata_json`. It does not fabricate identity
embeddings: if a real embedding provider is unavailable, identity remains
`未验证/无法获取` and downstream gates keep human review required.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import warnings
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/Users/a1234/Desktop/案例生成器/case-workbench")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import source_images  # noqa: E402

DEFAULT_DB_PATH = ROOT / "case-workbench.db"
DEFAULT_JSON_OUTPUT = ROOT / "tasks" / "t69_primary_signal_enrichment_report.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "tasks" / "t69_primary_signal_enrichment_report.md"
DEFAULT_IDENTITY_WORKER_SCRIPT = ROOT / "backend" / "scripts" / "identity_embedding_worker.py"
UNVERIFIED = "未验证/无法获取"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic"}
SIPS_CONVERTIBLE_EXTS = {".heic", ".heif"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_load(raw: str | None, default: Any) -> Any:
    try:
        data = json.loads(raw or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return data


def _json_dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stddev(values: list[float], mean: float) -> float:
    return math.sqrt(_mean([(value - mean) ** 2 for value in values])) if values else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


def _mean_rgb(pixels: list[tuple[int, int, int]]) -> tuple[float, float, float]:
    if not pixels:
        return (0.0, 0.0, 0.0)
    return (
        sum(pixel[0] for pixel in pixels) / len(pixels),
        sum(pixel[1] for pixel in pixels) / len(pixels),
        sum(pixel[2] for pixel in pixels) / len(pixels),
    )


def _skin_like(pixel: tuple[int, int, int]) -> bool:
    r, g, b = pixel
    return (
        r > 88
        and g > 42
        and b > 24
        and r > g * 1.05
        and g >= b * 0.78
        and max(pixel) - min(pixel) > 20
    )


def _exposure_label(mean_luma: float, p10_luma: float, p90_luma: float) -> str:
    if mean_luma >= 0.82 or p90_luma >= 0.94:
        return "overexposed"
    if mean_luma <= 0.20 or (p10_luma <= 0.08 and mean_luma < 0.35):
        return "underexposed"
    return "normal"


def _active_bbox(
    pixels: list[tuple[int, int, int]],
    gray_values: list[float],
    edge_values: list[float],
    *,
    grid: int,
) -> tuple[dict[str, float] | None, float | None, bool]:
    border_pixels = [
        pixels[row * grid + col]
        for row in range(grid)
        for col in range(grid)
        if row in {0, grid - 1} or col in {0, grid - 1}
    ]
    bg_r, bg_g, bg_b = _mean_rgb(border_pixels)
    active_positions: list[tuple[int, int]] = []
    for row in range(grid):
        for col in range(grid):
            index = row * grid + col
            pixel = pixels[index]
            color_distance = math.sqrt(
                (pixel[0] - bg_r) ** 2 + (pixel[1] - bg_g) ** 2 + (pixel[2] - bg_b) ** 2
            ) / 441.7
            is_outer_border = row in {0, grid - 1} or col in {0, grid - 1}
            edge_active = edge_values[index] > 0.20 and not is_outer_border
            if color_distance > 0.10 or edge_active or _skin_like(pixel):
                active_positions.append((row, col))
    if len(active_positions) < max(8, int(grid * grid * 0.015)):
        return None, None, False
    min_row = min(row for row, _col in active_positions)
    max_row = max(row for row, _col in active_positions)
    min_col = min(col for _row, col in active_positions)
    max_col = max(col for _row, col in active_positions)
    x = min_col / grid
    y = min_row / grid
    width = (max_col - min_col + 1) / grid
    height = (max_row - min_row + 1) / grid
    margins = [x, y, 1.0 - (x + width), 1.0 - (y + height)]
    margin = round(max(0.0, min(margins)), 4)
    bbox = {
        "x": round(x, 4),
        "y": round(y, 4),
        "width_ratio": round(width, 4),
        "height_ratio": round(height, 4),
        "center_x": round(x + width / 2.0, 4),
        "center_y": round(y + height / 2.0, 4),
        "active_pixel_ratio": round(len(active_positions) / (grid * grid), 4),
    }
    return bbox, margin, True


def _load_rgb_image(image_path: Path) -> tuple[Any, str]:
    try:
        from PIL import Image, ImageOps
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("PIL unavailable") from exc

    try:
        with Image.open(image_path) as opened:
            return ImageOps.exif_transpose(opened).convert("RGB"), "local_pil_cv_v1"
    except Exception:
        if image_path.suffix.lower() not in SIPS_CONVERTIBLE_EXTS:
            raise
    with tempfile.TemporaryDirectory(prefix="case-workbench-heic-") as tmp_dir:
        converted = Path(tmp_dir) / f"{image_path.stem}.jpg"
        subprocess.run(
            ["sips", "-s", "format", "jpeg", str(image_path), "--out", str(converted)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        with Image.open(converted) as opened:
            return ImageOps.exif_transpose(opened).convert("RGB"), "local_sips_cv_v1"


def compute_local_cv_signals(image_path: Path | str) -> dict[str, Any]:
    """Compute deterministic local CV signals from one real image file."""
    image_path = Path(image_path)
    if not image_path.is_file():
        raise FileNotFoundError(str(image_path))
    try:
        from PIL import Image, ImageFilter, ImageOps
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("PIL unavailable") from exc

    image, signal_source = _load_rgb_image(image_path)
    try:
        width, height = image.size
        small = image.resize((64, 64), Image.Resampling.BILINEAR)
        gray = ImageOps.grayscale(small)
        edge = gray.filter(ImageFilter.FIND_EDGES)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            pixels = list(small.getdata())
            gray_values = [float(value) / 255.0 for value in gray.getdata()]
            edge_values = [float(value) / 255.0 for value in edge.getdata()]
    finally:
        try:
            image.close()
        except Exception:
            pass

    mean_luma = _mean(gray_values)
    p10_luma = _percentile(gray_values, 0.10)
    p90_luma = _percentile(gray_values, 0.90)
    contrast = _stddev(gray_values, mean_luma)
    edge_density = _mean(edge_values)
    bbox, crop_margin, crop_observed = _active_bbox(pixels, gray_values, edge_values, grid=64)
    crop_touches = bool(crop_observed and crop_margin is not None and crop_margin <= 0.02)
    exposure = _exposure_label(mean_luma, p10_luma, p90_luma)
    return {
        "source": signal_source,
        "width": int(width),
        "height": int(height),
        "brightness": round(mean_luma, 4),
        "mean_luma": round(mean_luma, 4),
        "luma": round(mean_luma, 4),
        "luma_p10": round(p10_luma, 4),
        "luma_p90": round(p90_luma, 4),
        "contrast": round(contrast, 4),
        "edge_density": round(edge_density, 4),
        "exposure": exposure,
        "exposure_score": round(max(0.0, 1.0 - abs(mean_luma - 0.52) / 0.52), 4),
        "crop_observed": crop_observed,
        "crop_margin": crop_margin,
        "face_crop_margin": crop_margin,
        "crop_touches_frame": crop_touches,
        "face_crop_touches_frame": crop_touches,
        "subject_bbox": bbox,
    }


def identity_provider_readiness() -> dict[str, Any]:
    external = _external_identity_provider_readiness()
    if external is not None:
        return external

    candidates = {
        "insightface": importlib.util.find_spec("insightface") is not None,
        "face_recognition": importlib.util.find_spec("face_recognition") is not None,
        "numpy": importlib.util.find_spec("numpy") is not None,
    }
    available = bool(candidates["numpy"] and (candidates["insightface"] or candidates["face_recognition"]))
    if available:
        provider = "insightface" if candidates["insightface"] else "face_recognition"
        return {
            "status": "available",
            "provider": provider,
            "dependencies": candidates,
            "can_generate_embeddings": True,
            "fallback_policy": "human_review_required_for_low_confidence",
        }
    return {
        "status": "unavailable",
        "provider": None,
        "dependencies": candidates,
        "can_generate_embeddings": False,
        "readiness": UNVERIFIED,
        "fallback_policy": "human_review_required",
        "reason": "No real InsightFace/ArcFace-compatible embedding dependency is installed.",
    }


def _parse_worker_json(stdout: str) -> dict[str, Any]:
    for raw_line in reversed(str(stdout or "").splitlines()):
        line = raw_line.strip()
        if line.startswith("{") and line.endswith("}"):
            parsed = json.loads(line)
            return parsed if isinstance(parsed, dict) else {}
    return {}


def _external_identity_provider_readiness() -> dict[str, Any] | None:
    provider_python = os.environ.get("CASE_WORKBENCH_IDENTITY_PROVIDER_PYTHON")
    if not provider_python:
        return None
    worker_script = Path(os.environ.get("CASE_WORKBENCH_IDENTITY_WORKER_SCRIPT") or DEFAULT_IDENTITY_WORKER_SCRIPT)
    provider_path = Path(provider_python).expanduser()
    if not provider_path.is_file():
        return {
            "status": "unavailable",
            "provider": None,
            "provider_python": str(provider_path),
            "worker_script": str(worker_script),
            "can_generate_embeddings": False,
            "readiness": UNVERIFIED,
            "fallback_policy": "human_review_required",
            "reason": "Configured identity provider Python is not an executable file.",
        }
    if not worker_script.is_file():
        return {
            "status": "unavailable",
            "provider": None,
            "provider_python": str(provider_path),
            "worker_script": str(worker_script),
            "can_generate_embeddings": False,
            "readiness": UNVERIFIED,
            "fallback_policy": "human_review_required",
            "reason": "Configured identity worker script is missing.",
        }
    try:
        result = subprocess.run(
            [str(provider_path), str(worker_script), "--probe"],
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "unavailable",
            "provider": None,
            "provider_python": str(provider_path),
            "worker_script": str(worker_script),
            "can_generate_embeddings": False,
            "readiness": UNVERIFIED,
            "fallback_policy": "human_review_required",
            "reason": str(exc)[:240],
        }
    payload = _parse_worker_json(result.stdout)
    if result.returncode == 0 and payload.get("status") == "available":
        return {
            "status": "available",
            "provider": payload.get("provider"),
            "dependencies": payload.get("dependencies") or {},
            "embedding_dim": payload.get("embedding_dim"),
            "can_generate_embeddings": True,
            "provider_python": str(provider_path),
            "worker_script": str(worker_script),
            "isolation": "external_provider_python",
            "fallback_policy": "human_review_required_for_low_confidence",
        }
    return {
        "status": "unavailable",
        "provider": payload.get("provider"),
        "dependencies": payload.get("dependencies") or {},
        "provider_python": str(provider_path),
        "worker_script": str(worker_script),
        "can_generate_embeddings": False,
        "readiness": UNVERIFIED,
        "fallback_policy": "human_review_required",
        "reason": (payload.get("reason") or payload.get("error") or result.stderr or "Identity provider probe failed")[:240],
    }


def _skill_index(items: list[Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    ordered: list[dict[str, Any]] = [dict(item) for item in items if isinstance(item, dict)]
    index: dict[str, dict[str, Any]] = {}
    for item in ordered:
        for key in (item.get("filename"), item.get("relative_path")):
            if key:
                index[str(key)] = item
                index[Path(str(key)).name] = item
    return ordered, index


def _merge_cv_signals(item: dict[str, Any], signals: dict[str, Any], *, generated_at: str) -> dict[str, Any]:
    merged = dict(item)
    merged.update(
        {
            "cv_signal_source": signals["source"],
            "cv_signal_generated_at": generated_at,
            "width": signals["width"],
            "height": signals["height"],
            "brightness": signals["brightness"],
            "mean_luma": signals["mean_luma"],
            "luma": signals["luma"],
            "luma_p10": signals["luma_p10"],
            "luma_p90": signals["luma_p90"],
            "contrast": signals["contrast"],
            "edge_density": signals["edge_density"],
            "exposure": signals["exposure"],
            "exposure_score": signals["exposure_score"],
            "crop_observed": signals["crop_observed"],
            "crop_margin": signals["crop_margin"],
            "face_crop_margin": signals["face_crop_margin"],
            "crop_touches_frame": signals["crop_touches_frame"],
            "face_crop_touches_frame": signals["face_crop_touches_frame"],
            "subject_bbox": signals["subject_bbox"],
        }
    )
    return merged


def _case_image_files(row: sqlite3.Row) -> list[str]:
    meta = _json_load(row["meta_json"] if "meta_json" in row.keys() else None, {})
    if not isinstance(meta, dict):
        return []
    files = [str(item) for item in (meta.get("image_files") or []) if str(item)]
    return [
        item
        for item in source_images.filter_source_image_files(files)
        if Path(item).suffix.lower() in IMAGE_EXTS
    ]


def _selected_case_clause(case_ids: set[int] | None) -> tuple[str, list[Any]]:
    if not case_ids:
        return "", []
    placeholders = ",".join("?" * len(case_ids))
    return f" AND id IN ({placeholders})", sorted(case_ids)


def enrich_database(
    db_path: Path | str,
    *,
    dry_run: bool = True,
    case_ids: set[int] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    db_path = Path(db_path).expanduser().resolve()
    generated_at = _now()
    identity_ready = identity_provider_readiness()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        clause, params = _selected_case_clause(case_ids)
        sql = (
            "SELECT id, abs_path, meta_json, skill_image_metadata_json "
            f"FROM cases WHERE trashed_at IS NULL{clause} ORDER BY id"
        )
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params = [*params, int(limit)]
        rows = conn.execute(sql, params).fetchall()
        case_results: list[dict[str, Any]] = []
        status_counts: Counter[str] = Counter()
        updated_case_count = 0
        processed_image_count = 0
        cv_success_count = 0
        exposure_signal_count = 0
        crop_signal_count = 0
        identity_embedding_signal_count = 0
        for row in rows:
            case_id = int(row["id"])
            case_dir = Path(str(row["abs_path"] or ""))
            raw_items = _json_load(row["skill_image_metadata_json"], [])
            if not isinstance(raw_items, list):
                raw_items = []
            ordered, index = _skill_index(raw_items)
            changed = False
            image_results: list[dict[str, Any]] = []
            for filename in _case_image_files(row):
                image_path = (case_dir / filename).resolve()
                if not image_path.is_file():
                    status_counts["missing_file"] += 1
                    image_results.append({"filename": filename, "status": "missing_file"})
                    continue
                processed_image_count += 1
                try:
                    signals = compute_local_cv_signals(image_path)
                except Exception as exc:  # noqa: BLE001
                    status_counts["cv_unavailable"] += 1
                    image_results.append({"filename": filename, "status": "cv_unavailable", "reason": str(exc)[:180]})
                    continue
                item = index.get(filename) or index.get(Path(filename).name)
                if item is None:
                    item = {"filename": filename, "relative_path": filename}
                    ordered.append(item)
                    index[filename] = item
                    index[Path(filename).name] = item
                merged = _merge_cv_signals(item, signals, generated_at=generated_at)
                if merged != item:
                    item.clear()
                    item.update(merged)
                    changed = True
                cv_success_count += 1
                exposure_signal_count += 1 if merged.get("mean_luma") is not None and merged.get("exposure") else 0
                crop_signal_count += 1 if merged.get("crop_observed") and merged.get("crop_margin") is not None else 0
                identity_embedding_signal_count += 1 if merged.get("identity_embedding") else 0
                status_counts["cv_enriched"] += 1
                image_results.append(
                    {
                        "filename": filename,
                        "status": "cv_enriched",
                        "exposure": merged.get("exposure"),
                        "crop_touches_frame": merged.get("crop_touches_frame"),
                        "crop_margin": merged.get("crop_margin"),
                    }
                )
            if changed:
                updated_case_count += 1
                if not dry_run:
                    conn.execute(
                        "UPDATE cases SET skill_image_metadata_json = ? WHERE id = ?",
                        (_json_dump(ordered), case_id),
                    )
            case_results.append(
                {
                    "case_id": case_id,
                    "image_count": len(image_results),
                    "changed": changed,
                    "images": image_results[:12],
                }
            )
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    summary = {
        "case_count": len(rows),
        "updated_case_count": updated_case_count,
        "processed_image_count": processed_image_count,
        "cv_success_count": cv_success_count,
        "exposure_signal_count": exposure_signal_count,
        "crop_signal_count": crop_signal_count,
        "identity_embedding_signal_count": identity_embedding_signal_count,
        "status_counts": dict(sorted(status_counts.items())),
    }
    readiness = {
        "exposure": {
            "ready": exposure_signal_count > 0,
            "status": "ready" if exposure_signal_count > 0 else UNVERIFIED,
            "count": exposure_signal_count,
        },
        "crop": {
            "ready": crop_signal_count > 0,
            "status": "ready" if crop_signal_count > 0 else UNVERIFIED,
            "count": crop_signal_count,
        },
        "identity": {
            "ready": identity_embedding_signal_count > 0,
            "status": "ready" if identity_embedding_signal_count > 0 else UNVERIFIED,
            "count": identity_embedding_signal_count,
            "provider": identity_ready,
        },
    }
    run_status = "dry_run_completed" if dry_run else "applied_real_cv_signals"
    decision = (
        "本地 CV exposure/crop 信号已写入真实 skill metadata；identity embedding 仍需真实 provider。"
        if not dry_run and exposure_signal_count > 0 and crop_signal_count > 0
        else f"{UNVERIFIED}: exposure/crop 未形成真实写回，或当前为 dry-run。"
    )
    return {
        "generated_at": generated_at,
        "run_status": run_status,
        "decision": decision,
        "used_mock_data": False,
        "db_path": str(db_path),
        "dry_run": dry_run,
        "summary": summary,
        "readiness": readiness,
        "case_results": case_results[:80],
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    readiness = report.get("readiness") or {}
    lines = [
        "# T69 真实 CV/Identity 信号采集报告",
        "",
        f"- run_status: `{report.get('run_status')}`",
        f"- decision: {report.get('decision')}",
        f"- used_mock_data: `{report.get('used_mock_data')}`",
        f"- dry_run: `{report.get('dry_run')}`",
        f"- case_count: {summary.get('case_count')}",
        f"- updated_case_count: {summary.get('updated_case_count')}",
        f"- processed_image_count: {summary.get('processed_image_count')}",
        f"- cv_success_count: {summary.get('cv_success_count')}",
        f"- exposure_signal_count: {summary.get('exposure_signal_count')}",
        f"- crop_signal_count: {summary.get('crop_signal_count')}",
        f"- identity_embedding_signal_count: {summary.get('identity_embedding_signal_count')}",
        "",
        "## Readiness",
        "",
    ]
    for key in ("exposure", "crop", "identity"):
        item = readiness.get(key) if isinstance(readiness, dict) else {}
        lines.append(f"- `{key}`: ready={item.get('ready')} status={item.get('status')} count={item.get('count')}")
    identity_provider = ((readiness.get("identity") or {}).get("provider") or {}) if isinstance(readiness, dict) else {}
    lines.extend(
        [
            "",
            "## Identity Provider",
            "",
            f"- status: `{identity_provider.get('status')}`",
            f"- provider: `{identity_provider.get('provider')}`",
            f"- readiness: {identity_provider.get('readiness') or 'ready'}",
            f"- fallback_policy: `{identity_provider.get('fallback_policy')}`",
            "",
            "## Status Counts",
            "",
        ]
    )
    for key, value in (summary.get("status_counts") or {}).items():
        lines.append(f"- `{key}`: {value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _parse_case_ids(value: str) -> set[int]:
    out: set[int] = set()
    for raw in str(value or "").replace(" ", "").split(","):
        if not raw:
            continue
        try:
            parsed = int(raw)
        except ValueError:
            continue
        if parsed > 0:
            out.add(parsed)
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Enrich real source image metadata with local CV primary signals.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN_OUTPUT)
    parser.add_argument("--case-ids", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true", help="Write enriched metadata to the live DB.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    report = enrich_database(
        Path(args.db),
        dry_run=not bool(args.apply),
        case_ids=_parse_case_ids(str(args.case_ids)),
        limit=int(args.limit or 0) or None,
    )
    write_json(Path(args.output), report)
    write_markdown(Path(args.markdown_output), report)
    print(
        json.dumps(
            {"run_status": report["run_status"], "summary": report["summary"], "readiness": report["readiness"]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
