"""Face-region composite tool.

Take a generated/enhanced image and an original after-image, composite the face
region from the generated image back onto the original. The original photo
provides hair, ears, jewelry, neck, clothing, and background; the generated
image provides the face. This is the practical fix for image-edit models that
treat a pose-reference image as a visual template and copy non-face pixels
verbatim.

Usage:
  python3 face_composite.py --base ORIGINAL_AFTER --face GENERATED \
      --out COMPOSITED [--feather PX] [--include-neck]

Uses MediaPipe Tasks FaceLandmarker (the SRGB Tasks API), which is the path
that works on Python 3.12 with mediapipe>=0.10.x.
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision


# Standard MediaPipe FaceMesh face-oval indices (36 points around the face).
FACE_OVAL_INDICES = [
    10, 338, 297, 332, 284, 251, 389, 356, 454, 323,
    361, 288, 397, 365, 379, 378, 400, 377, 152, 148,
    176, 149, 150, 136, 172, 58, 132, 93, 234, 127,
    162, 21, 54, 103, 67, 109,
]

MODEL_URL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
DEFAULT_MODEL_CACHE_DIR = Path.home() / ".cache" / "feishu-claude" / "mediapipe"


def ensure_model() -> str:
    env = os.environ.get("FACE_LANDMARKER_TASK_PATH")
    if env:
        p = Path(env)
        if p.exists() and p.stat().st_size > 0:
            return str(p)

    candidates = [Path("/tmp/face_landmarker.task"), DEFAULT_MODEL_CACHE_DIR / "face_landmarker.task"]
    for c in candidates:
        if c.exists() and c.stat().st_size > 0:
            return str(c)

    target = candidates[-1]
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".download")
    urllib.request.urlretrieve(MODEL_URL, tmp)
    tmp.replace(target)
    return str(target)


def detect_face_polygon(rgb: np.ndarray, include_neck: bool = False) -> np.ndarray | None:
    h, w = rgb.shape[:2]
    base_options = mp_python.BaseOptions(model_asset_path=ensure_model())
    options = vision.FaceLandmarkerOptions(
        base_options=base_options,
        output_face_blendshapes=False,
        output_facial_transformation_matrixes=False,
        num_faces=1,
        min_face_detection_confidence=0.5,
        min_face_presence_confidence=0.5,
    )
    with vision.FaceLandmarker.create_from_options(options) as landmarker:
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        res = landmarker.detect(mp_image)

    if not res.face_landmarks:
        return None
    lm = res.face_landmarks[0]

    pts = np.array(
        [[lm[i].x * w, lm[i].y * h] for i in FACE_OVAL_INDICES if 0 <= i < len(lm)],
        dtype=np.float32,
    )
    if pts.shape[0] < 6:
        return None

    cx, cy = pts.mean(axis=0)
    order = np.argsort(np.arctan2(pts[:, 1] - cy, pts[:, 0] - cx))
    pts = pts[order]

    if include_neck:
        # extend mask down to cover upper neck so jaw blending is smoother
        chin_y = float(pts[:, 1].max())
        chin_x = float(pts[pts[:, 1].argmax()][0])
        neck_left = chin_x - 0.20 * w
        neck_right = chin_x + 0.20 * w
        neck_bottom = min(h - 1, chin_y + 0.18 * h)
        neck_pts = np.array([
            [neck_right, chin_y + 5],
            [neck_right, neck_bottom],
            [neck_left, neck_bottom],
            [neck_left, chin_y + 5],
        ], dtype=np.float32)
        pts = np.vstack([pts, neck_pts])

    hull = cv2.convexHull(pts.astype(np.int32))
    return hull


def soft_mask_from_polygon(polygon: np.ndarray, shape: tuple[int, int], feather_px: int) -> np.ndarray:
    mask = np.zeros(shape, dtype=np.uint8)
    cv2.fillPoly(mask, [polygon], 255)
    if feather_px > 0:
        kernel = max(3, feather_px * 2 + 1)
        mask = cv2.GaussianBlur(mask, (kernel, kernel), feather_px)
    return mask.astype(np.float32) / 255.0


def color_match(face_bgr: np.ndarray, base_bgr: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Pull face region's mean LAB color partway toward the base's color."""
    m = (alpha > 0.05).astype(np.uint8)
    if m.sum() < 100:
        return face_bgr
    face_lab = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    base_lab = cv2.cvtColor(base_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    out = face_lab.copy()
    for c in range(3):
        fm = float(face_lab[..., c][m == 1].mean())
        bm = float(base_lab[..., c][m == 1].mean())
        out[..., c] += (bm - fm) * 0.55  # partial pull, keep generated tint
    out = np.clip(out, 0, 255).astype(np.uint8)
    return cv2.cvtColor(out, cv2.COLOR_LAB2BGR)


def composite(
    base_path: Path,
    face_path: Path,
    out_path: Path,
    feather_px: int,
    include_neck: bool,
) -> dict:
    base = cv2.imread(str(base_path))
    face = cv2.imread(str(face_path))
    if base is None:
        raise RuntimeError(f"failed to read base image: {base_path}")
    if face is None:
        raise RuntimeError(f"failed to read face image: {face_path}")

    bh, bw = base.shape[:2]
    if face.shape[:2] != (bh, bw):
        face = cv2.resize(face, (bw, bh), interpolation=cv2.INTER_LANCZOS4)

    base_rgb = cv2.cvtColor(base, cv2.COLOR_BGR2RGB)
    polygon = detect_face_polygon(base_rgb, include_neck=include_neck)
    if polygon is None:
        face_rgb = cv2.cvtColor(face, cv2.COLOR_BGR2RGB)
        polygon = detect_face_polygon(face_rgb, include_neck=include_neck)
    if polygon is None:
        raise RuntimeError("no face detected on base or face image")

    alpha = soft_mask_from_polygon(polygon, (bh, bw), feather_px)
    face_matched = color_match(face, base, alpha)

    a = alpha[..., None]
    blended = (face_matched.astype(np.float32) * a + base.astype(np.float32) * (1.0 - a)).astype(np.uint8)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() in {".jpg", ".jpeg"}:
        cv2.imwrite(str(out_path), blended, [cv2.IMWRITE_JPEG_QUALITY, 95])
    else:
        cv2.imwrite(str(out_path), blended)
    return {
        "base_size": [bw, bh],
        "feather_px": feather_px,
        "include_neck": include_neck,
        "polygon_points": int(polygon.shape[0]),
        "out_path": str(out_path),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="original after-image (provides hair/ears/clothing/background)")
    ap.add_argument("--face", required=True, help="generated/enhanced image (provides the face region)")
    ap.add_argument("--out", required=True, help="output image path")
    ap.add_argument("--feather", type=int, default=21, help="feather radius in pixels (default 21)")
    ap.add_argument("--include-neck", action="store_true", help="extend mask down to cover neck region")
    args = ap.parse_args()

    info = composite(
        Path(args.base),
        Path(args.face),
        Path(args.out),
        feather_px=max(0, args.feather),
        include_neck=args.include_neck,
    )
    print(info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
