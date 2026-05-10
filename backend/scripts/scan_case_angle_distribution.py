"""Scan case library for face pose angle distribution.

For each case, detect yaw/pitch/roll of every before/after image using MediaPipe
FaceLandmarker, then compute the best (before, after) pair with minimum Euler-angle
Euclidean distance.

Outputs:
  /tmp/case_angle_distribution.png   — histogram (3-bucket colour-coded)
  /tmp/case_best_pair.json           — per-case best pair list
  console summary line

Usage:
  python3 backend/scripts/scan_case_angle_distribution.py

Environment overrides:
  CASE_WORKBENCH_DB_PATH   — path to case-workbench.db
  FACE_LANDMARKER_TASK_PATH — path to face_landmarker.task (skips download)
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Optional

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent  # backend/scripts → backend → project root

DB_PATH = Path(
    os.environ.get(
        "CASE_WORKBENCH_DB_PATH",
        str(PROJECT_ROOT / "case-workbench.db"),
    )
).expanduser().resolve()

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "face_landmarker/face_landmarker/float16/1/face_landmarker.task"
)
DEFAULT_MODEL_CACHE_DIR = Path.home() / ".cache" / "feishu-claude" / "mediapipe"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".JPG", ".JPEG", ".PNG"}

# ── MediaPipe model ensure (same cache path as face_composite.py) ─────────────
def ensure_model() -> str:
    env = os.environ.get("FACE_LANDMARKER_TASK_PATH")
    if env:
        p = Path(env)
        if p.exists() and p.stat().st_size > 0:
            return str(p)

    candidates = [
        Path("/tmp/face_landmarker.task"),
        DEFAULT_MODEL_CACHE_DIR / "face_landmarker.task",
    ]
    for c in candidates:
        if c.exists() and c.stat().st_size > 0:
            return str(c)

    target = candidates[-1]
    target.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading face_landmarker.task …")
    tmp = target.with_suffix(target.suffix + ".download")
    urllib.request.urlretrieve(MODEL_URL, tmp)
    tmp.replace(target)
    logger.info("Model cached at %s", target)
    return str(target)


# ── singleton FaceLandmarker (reused across all images for speed) ─────────────
_LANDMARKER: Optional[vision.FaceLandmarker] = None


def get_landmarker() -> vision.FaceLandmarker:
    global _LANDMARKER
    if _LANDMARKER is None:
        base_options = mp_python.BaseOptions(model_asset_path=ensure_model())
        options = vision.FaceLandmarkerOptions(
            base_options=base_options,
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=True,
            num_faces=1,
            min_face_detection_confidence=0.4,
            min_face_presence_confidence=0.4,
        )
        _LANDMARKER = vision.FaceLandmarker.create_from_options(options)
    return _LANDMARKER


def close_landmarker() -> None:
    global _LANDMARKER
    if _LANDMARKER is not None:
        try:
            _LANDMARKER.close()
        except Exception:
            pass
        _LANDMARKER = None


# ── Euler angle extraction from 4×4 facial transformation matrix ──────────────
def matrix4x4_to_euler_deg(mat: np.ndarray) -> tuple[float, float, float]:
    """Extract (pitch, yaw, roll) in degrees from a 4×4 facial transform matrix.

    Uses cv2.RQDecomp3x3 on the 3×3 rotation sub-matrix (ZYX convention).
    Returns (rx_deg, ry_deg, rz_deg) where:
      rx ≈ pitch (nodding), ry ≈ yaw (turning), rz ≈ roll (tilting).
    """
    R = mat[:3, :3].astype(np.float64)
    angles, *_ = cv2.RQDecomp3x3(R)
    # angles is a 3-tuple (rx, ry, rz) in degrees
    return float(angles[0]), float(angles[1]), float(angles[2])


def detect_euler(img_path: Path) -> Optional[tuple[float, float, float]]:
    """Return (rx, ry, rz) Euler angles in degrees for the first face, or None."""
    bgr = cv2.imread(str(img_path))
    if bgr is None:
        logger.warning("  Cannot read image: %s", img_path.name)
        return None

    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    try:
        res = get_landmarker().detect(mp_image)
    except Exception as exc:
        logger.warning("  MediaPipe error (%s): %s", img_path.name, exc)
        return None

    if not res.facial_transformation_matrixes:
        return None

    mat = np.array(res.facial_transformation_matrixes[0])  # (4, 4)
    return matrix4x4_to_euler_deg(mat)


# ── phase classification ───────────────────────────────────────────────────────
# Keywords checked case-insensitively in filename (lower). "before" wins if both.
BEFORE_KEYWORDS = ("pre", "before", "术前", "前")
AFTER_KEYWORDS  = ("post", "after", "术后", "后")


def classify_by_filename(filename: str) -> Optional[str]:
    """Return 'before', 'after', or None if ambiguous / no keyword matched."""
    lower = filename.lower()
    has_before = any(kw in lower for kw in BEFORE_KEYWORDS)
    has_after  = any(kw in lower for kw in AFTER_KEYWORDS)
    # Skip composite images that contain both keywords
    if has_before and has_after:
        return None
    if has_before:
        return "before"
    if has_after:
        return "after"
    return None


# ── database helpers ───────────────────────────────────────────────────────────
def load_cases() -> list[dict]:
    """Return all non-trashed cases as dicts {id, abs_path}."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT id, abs_path FROM cases WHERE trashed_at IS NULL ORDER BY id"
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def load_overrides(case_id: int) -> dict[str, str]:
    """Return {filename: phase} for a case from case_image_overrides.manual_phase."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "SELECT filename, manual_phase FROM case_image_overrides "
            "WHERE case_id = ? AND manual_phase IS NOT NULL AND manual_phase != ''",
            (case_id,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}
    finally:
        conn.close()


# ── per-case scan ──────────────────────────────────────────────────────────────
def scan_case(case: dict) -> Optional[dict]:
    """Scan one case.  Returns best-pair dict, or None (skip)."""
    cid = case["id"]
    abs_path = Path(case["abs_path"])

    if not abs_path.is_dir():
        logger.warning("[case %d] directory not found: %s", cid, abs_path)
        return None

    overrides = load_overrides(cid)

    # Collect image files (non-recursive to stay within the case boundary)
    images = [
        p for p in abs_path.iterdir()
        if p.is_file() and p.suffix in IMAGE_EXTS
    ]
    if not images:
        logger.warning("[case %d] no image files in %s", cid, abs_path.name)
        return None

    before_imgs: list[Path] = []
    after_imgs:  list[Path] = []

    for img_path in sorted(images):
        fn = img_path.name
        phase = overrides.get(fn) or classify_by_filename(fn)
        if phase == "before":
            before_imgs.append(img_path)
        elif phase == "after":
            after_imgs.append(img_path)

    if not before_imgs or not after_imgs:
        logger.warning(
            "[case %d] missing phases: before=%d after=%d (total imgs=%d)",
            cid, len(before_imgs), len(after_imgs), len(images),
        )
        return None

    # Detect Euler angles for each classified image
    before_angles: list[tuple[Path, float, float, float]] = []
    after_angles:  list[tuple[Path, float, float, float]] = []

    for img in before_imgs:
        ea = detect_euler(img)
        if ea:
            before_angles.append((img, *ea))
        else:
            logger.warning("  [case %d] no face detected: %s", cid, img.name)

    for img in after_imgs:
        ea = detect_euler(img)
        if ea:
            after_angles.append((img, *ea))
        else:
            logger.warning("  [case %d] no face detected: %s", cid, img.name)

    if not before_angles or not after_angles:
        logger.warning(
            "[case %d] face detection failed: before_ok=%d after_ok=%d",
            cid, len(before_angles), len(after_angles),
        )
        return None

    # Find best (before, after) pair by minimum Euclidean distance of Euler angles
    best_delta = math.inf
    best_before: Optional[str] = None
    best_after:  Optional[str] = None

    for b_img, b_rx, b_ry, b_rz in before_angles:
        for a_img, a_rx, a_ry, a_rz in after_angles:
            delta = math.sqrt(
                (b_rx - a_rx) ** 2
                + (b_ry - a_ry) ** 2
                + (b_rz - a_rz) ** 2
            )
            if delta < best_delta:
                best_delta = delta
                best_before = b_img.name
                best_after  = a_img.name

    logger.info(
        "[case %d] best pair: %s + %s  Δ=%.1f°",
        cid, best_before, best_after, best_delta,
    )
    return {
        "case_id": cid,
        "before": best_before,
        "after":  best_after,
        "delta_deg": round(best_delta, 2),
    }


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # Import matplotlib only at runtime (not at import time) to fail gracefully
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch as MPatch
        _HAS_MPL = True
    except ImportError:
        logger.warning("matplotlib not found — PNG histogram will be skipped")
        _HAS_MPL = False

    logger.info("DB: %s", DB_PATH)
    if not DB_PATH.exists():
        logger.error("Database not found: %s", DB_PATH)
        sys.exit(1)

    cases = load_cases()
    logger.info("Total cases to scan: %d", len(cases))

    results: list[dict] = []
    skipped = 0

    for i, case in enumerate(cases, 1):
        logger.info("── [%d/%d] case %d ──────────────", i, len(cases), case["id"])
        r = scan_case(case)
        if r is None:
            skipped += 1
        else:
            results.append(r)

    close_landmarker()

    # ── stats ─────────────────────────────────────────────────────────────────
    deltas = [r["delta_deg"] for r in results]
    n_total   = len(cases)
    n_scanned = len(results)

    bucket_lt5  = sum(1 for d in deltas if d < 5)
    bucket_5_10 = sum(1 for d in deltas if 5 <= d < 10)
    bucket_gt10 = sum(1 for d in deltas if d >= 10)

    median_deg = float(np.median(deltas)) if deltas else float("nan")
    p90_deg    = float(np.percentile(deltas, 90)) if deltas else float("nan")

    # ── write JSON ────────────────────────────────────────────────────────────
    json_path = Path("/tmp/case_best_pair.json")
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    logger.info("Wrote JSON: %s", json_path)

    # ── write histogram PNG ───────────────────────────────────────────────────
    png_path = Path("/tmp/case_angle_distribution.png")
    if _HAS_MPL and deltas:
        max_d = max(deltas)
        bin_step = 2
        bins = list(range(0, int(max_d) + bin_step + 1, bin_step))
        if len(bins) < 3:
            bins = [0, 5, 10, max(15, int(max_d) + 5)]

        fig, ax = plt.subplots(figsize=(10, 5))
        n_vals, bin_edges, patches = ax.hist(deltas, bins=bins, edgecolor="white", linewidth=0.6)
        for patch, left_edge in zip(patches, bin_edges[:-1]):
            if left_edge < 5:
                patch.set_facecolor("#4caf50")
            elif left_edge < 10:
                patch.set_facecolor("#ff9800")
            else:
                patch.set_facecolor("#f44336")

        ax.axvline(5,  color="#388e3c", linestyle="--", linewidth=1.3, alpha=0.75)
        ax.axvline(10, color="#c62828", linestyle="--", linewidth=1.3, alpha=0.75)
        ax.set_xlabel("Best-pair Euler angle distance (°)", fontsize=12)
        ax.set_ylabel("Case count", fontsize=12)
        ax.set_title(
            f"Case Angle Distribution — n={n_scanned} valid, {skipped} skipped / {n_total} total",
            fontsize=13,
        )

        pct_lt5  = 100 * bucket_lt5  // max(n_scanned, 1)
        pct_5_10 = 100 * bucket_5_10 // max(n_scanned, 1)
        pct_gt10 = 100 * bucket_gt10 // max(n_scanned, 1)

        legend_elements = [
            MPatch(facecolor="#4caf50", label=f"<5°   ({bucket_lt5} cases, {pct_lt5}%)"),
            MPatch(facecolor="#ff9800", label=f"5–10° ({bucket_5_10} cases, {pct_5_10}%)"),
            MPatch(facecolor="#f44336", label=f">10°  ({bucket_gt10} cases, {pct_gt10}%)"),
        ]
        ax.legend(handles=legend_elements, fontsize=11)

        ax.text(
            0.98, 0.95,
            f"median={median_deg:.1f}°  P90={p90_deg:.1f}°",
            transform=ax.transAxes, ha="right", va="top",
            fontsize=10, color="#444",
        )

        plt.tight_layout()
        fig.savefig(str(png_path), dpi=150)
        plt.close(fig)
        logger.info("Wrote PNG: %s", png_path)
    elif _HAS_MPL:
        logger.warning("No valid results — histogram not written")

    # ── console summary ───────────────────────────────────────────────────────
    pct_lt5  = 100 * bucket_lt5  // max(n_scanned, 1)
    pct_5_10 = 100 * bucket_5_10 // max(n_scanned, 1)
    pct_gt10 = 100 * bucket_gt10 // max(n_scanned, 1)

    print(
        f"Total cases scanned: {n_scanned} (skipped {skipped}/{n_total}) / "
        f"<5°: {bucket_lt5} ({pct_lt5}%) / "
        f"5-10°: {bucket_5_10} ({pct_5_10}%) / "
        f">10°: {bucket_gt10} ({pct_gt10}%) / "
        f"median: {median_deg:.1f}° / P90: {p90_deg:.1f}°"
    )
    print(f"Artifacts: {json_path}  {png_path}")


if __name__ == "__main__":
    main()
