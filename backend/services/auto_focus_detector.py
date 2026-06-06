"""Auto-detect treatment focus targets and regions — three-tier cascade.

Tier 1: Parse case directory name for treatment keywords → atlas region names
Tier 2: CV before/after per-region pixel diff using MediaPipe 478 landmarks
Tier 3: Full-face fallback (empty targets/regions → prompt builder handles it)

Called by simulate-after endpoint when operator provides no manual focus.
All heavy dependencies (mediapipe, numpy, cv2) are lazy-imported so the
module loads instantly even when CV stack is absent.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .facial_region_atlas import (
    FACIAL_REGION_ATLAS,
    _all_idx,
    extract_regions,
)

LOGGER = logging.getLogger(__name__)

_DIFF_THRESHOLD = 18.0  # mean abs pixel diff (0-255) — high to cut pose/lighting noise
_PADDING = 0.03  # bbox padding around landmark cluster (normalized 0-1)

_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "face_landmarker/face_landmarker/float16/1/face_landmarker.task"
)
_MODEL_CACHE = Path("/tmp/face_landmarker.task")
_MODEL_FALLBACK = Path.home() / ".cache" / "mediapipe" / "face_landmarker.task"


@dataclass
class AutoFocusResult:
    focus_targets: list[str]
    focus_regions: list[dict[str, Any]]
    detection_method: str  # "metadata" | "cv" | "metadata+cv" | "fallback"
    confidence: float  # 0.0 - 1.0
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tier 1 — metadata extraction from directory name
# ---------------------------------------------------------------------------

def detect_from_metadata(abs_path: str) -> list[str]:
    """Extract treatment region names from case directory path.

    Returns atlas-standard region keys (e.g. ["面颊", "下巴"]).
    Uses ``facial_region_atlas.extract_regions`` which matches atlas keys
    + aliases against the input text.
    """
    dirname = os.path.basename(abs_path.rstrip("/"))
    parent = os.path.basename(os.path.dirname(abs_path.rstrip("/")))
    combined = f"{parent} {dirname}"
    targets = extract_regions(combined)
    if targets:
        LOGGER.info("Tier 1 metadata: extracted %s from '%s'", targets, dirname)
    return targets


# ---------------------------------------------------------------------------
# Landmark utilities (lazy mediapipe)
# ---------------------------------------------------------------------------

def _ensure_model() -> str:
    """Return path to face_landmarker.task, downloading if needed."""
    for p in (_MODEL_CACHE, _MODEL_FALLBACK):
        if p.exists() and p.stat().st_size > 1_000_000:
            return str(p)
    import urllib.request
    target = _MODEL_CACHE
    target.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Downloading face_landmarker model to %s ...", target)
    urllib.request.urlretrieve(_MODEL_URL, str(target))
    return str(target)


def _detect_landmarks_478(image_path: str | Path) -> list[tuple[float, float]] | None:
    """Detect 478 face landmarks, returning normalized (x, y) in [0, 1].

    Returns None if mediapipe is unavailable or no face detected.
    """
    try:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision
    except ImportError:
        LOGGER.warning("mediapipe/cv2 not available — Tier 2 CV detection disabled")
        return None

    img = cv2.imread(str(image_path))
    if img is None:
        LOGGER.warning("Cannot read image: %s", image_path)
        return None

    # EXIF auto-orient
    try:
        from PIL import Image as PILImage
        pil = PILImage.open(str(image_path))
        from PIL import ImageOps
        pil = ImageOps.exif_transpose(pil)
        import numpy as np
        img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    except Exception:
        pass

    model_path = _ensure_model()
    base = mp_python.BaseOptions(model_asset_path=model_path)
    opts = vision.FaceLandmarkerOptions(
        base_options=base,
        num_faces=1,
        min_face_detection_confidence=0.5,
        min_face_presence_confidence=0.5,
    )

    with vision.FaceLandmarker.create_from_options(opts) as det:
        mp_image = mp.Image(
            image_format=mp.ImageFormat.SRGB,
            data=cv2.cvtColor(img, cv2.COLOR_BGR2RGB),
        )
        result = det.detect(mp_image)

    if not result.face_landmarks:
        LOGGER.warning("No face detected in %s", image_path)
        return None

    lms = result.face_landmarks[0]
    return [(lm.x, lm.y) for lm in lms]


# ---------------------------------------------------------------------------
# Landmark → bounding box
# ---------------------------------------------------------------------------

def _region_bbox(
    landmarks: list[tuple[float, float]],
    region_key: str,
    padding: float = _PADDING,
) -> dict[str, Any] | None:
    """Compute normalized bounding box for an atlas region from 478 landmarks."""
    spec = FACIAL_REGION_ATLAS.get(region_key)
    if not spec:
        return None
    indices = _all_idx(spec)
    if not indices:
        return None

    valid = [(landmarks[i][0], landmarks[i][1]) for i in indices if i < len(landmarks)]
    if not valid:
        return None

    xs = [p[0] for p in valid]
    ys = [p[1] for p in valid]

    x_min = max(0.0, min(xs) - padding)
    y_min = max(0.0, min(ys) - padding)
    x_max = min(1.0, max(xs) + padding)
    y_max = min(1.0, max(ys) + padding)

    return {
        "x": round(x_min, 4),
        "y": round(y_min, 4),
        "width": round(x_max - x_min, 4),
        "height": round(y_max - y_min, 4),
        "label": region_key,
    }


def targets_to_regions(
    targets: list[str],
    image_path: str | Path,
) -> list[dict[str, Any]]:
    """Convert target region names to bounding boxes using face landmarks.

    Falls back to empty list if landmarks cannot be detected.
    """
    if not targets:
        return []
    landmarks = _detect_landmarks_478(image_path)
    if not landmarks:
        return []

    regions: list[dict[str, Any]] = []
    for t in targets:
        bbox = _region_bbox(landmarks, t)
        if bbox:
            regions.append(bbox)
    return regions


# ---------------------------------------------------------------------------
# Tier 2 — CV before/after per-region pixel diff
# ---------------------------------------------------------------------------

def _crop_region_pixels(
    img_array: Any,  # numpy ndarray (H, W, 3)
    bbox: dict[str, Any],
) -> Any | None:
    """Crop a normalized bbox from an image array, return pixel patch."""
    import numpy as np
    h, w = img_array.shape[:2]
    x1 = int(bbox["x"] * w)
    y1 = int(bbox["y"] * h)
    x2 = int((bbox["x"] + bbox["width"]) * w)
    y2 = int((bbox["y"] + bbox["height"]) * h)
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return None
    return img_array[y1:y2, x1:x2].astype(np.float32)


def detect_from_cv(
    after_path: str | Path,
    before_path: str | Path,
    *,
    threshold: float = _DIFF_THRESHOLD,
) -> tuple[list[str], list[dict[str, Any]], dict[str, float]]:
    """Tier 2: detect changed regions by per-atlas-region pixel diff.

    Returns (targets, regions, per_region_scores).
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return [], [], {}

    after_lms = _detect_landmarks_478(after_path)
    before_lms = _detect_landmarks_478(before_path)
    if not after_lms or not before_lms:
        return [], [], {}

    # Load images as RGB arrays
    def _load_oriented(path: str | Path) -> Any:
        from PIL import Image as PILImage, ImageOps
        pil = PILImage.open(str(path))
        pil = ImageOps.exif_transpose(pil)
        return np.array(pil.convert("RGB"))

    after_img = _load_oriented(after_path)
    before_img = _load_oriented(before_path)

    scores: dict[str, float] = {}
    targets: list[str] = []
    regions: list[dict[str, Any]] = []

    for region_key in FACIAL_REGION_ATLAS:
        after_bbox = _region_bbox(after_lms, region_key)
        before_bbox = _region_bbox(before_lms, region_key)
        if not after_bbox or not before_bbox:
            continue

        after_crop = _crop_region_pixels(after_img, after_bbox)
        before_crop = _crop_region_pixels(before_img, before_bbox)
        if after_crop is None or before_crop is None:
            continue

        # Resize to common size for comparison
        target_h = min(after_crop.shape[0], before_crop.shape[0], 128)
        target_w = min(after_crop.shape[1], before_crop.shape[1], 128)
        if target_h < 4 or target_w < 4:
            continue

        after_resized = cv2.resize(after_crop, (target_w, target_h))
        before_resized = cv2.resize(before_crop, (target_w, target_h))

        diff = np.abs(after_resized - before_resized).mean()
        scores[region_key] = round(float(diff), 2)

        if diff >= threshold:
            targets.append(region_key)
            regions.append(after_bbox)

    if targets:
        LOGGER.info(
            "Tier 2 CV: detected %d changed regions (threshold=%.1f): %s",
            len(targets), threshold, targets,
        )
    return targets, regions, scores


# ---------------------------------------------------------------------------
# Cascade orchestrator
# ---------------------------------------------------------------------------

def auto_detect_focus(
    *,
    abs_path: str | None = None,
    after_path: str | Path | None = None,
    before_path: str | Path | None = None,
) -> AutoFocusResult:
    """Three-tier cascade: metadata → CV → fallback.

    Tier 1 + Tier 2 complement each other:
    - Tier 1 gives semantic targets (what was treated)
    - Tier 2 gives spatial evidence (where pixels changed)
    When both succeed, combine for best result.
    """
    meta_targets: list[str] = []
    cv_targets: list[str] = []
    cv_regions: list[dict[str, Any]] = []
    cv_scores: dict[str, float] = {}
    method_parts: list[str] = []

    # Tier 1: metadata
    if abs_path:
        meta_targets = detect_from_metadata(abs_path)
        if meta_targets:
            method_parts.append("metadata")

    # Tier 2: CV diff (only if before image available)
    if after_path and before_path and Path(str(before_path)).is_file():
        cv_targets, cv_regions, cv_scores = detect_from_cv(after_path, before_path)
        if cv_targets:
            method_parts.append("cv")

    # Combine results
    if meta_targets and cv_scores:
        # Metadata is authoritative for WHICH targets; CV provides spatial regions
        # and validates via diff scores. Don't add CV-only targets — too noisy
        # from pose/lighting differences between before/after sessions.
        combined_targets = list(meta_targets)
        combined_regions: list[dict[str, Any]] = []

        # Use CV regions for metadata targets when available (landmark-based bbox)
        cv_region_map = {r["label"]: r for r in cv_regions}
        for t in combined_targets:
            if t in cv_region_map:
                combined_regions.append(cv_region_map[t])

        # Fill missing regions from landmarks
        if after_path and len(combined_regions) < len(combined_targets):
            lm_regions = targets_to_regions(
                [t for t in combined_targets if not any(r["label"] == t for r in combined_regions)],
                after_path,
            )
            combined_regions.extend(lm_regions)

        # CV validation: metadata targets that also show high CV diff get higher confidence
        validated = [t for t in combined_targets if cv_scores.get(t, 0) >= _DIFF_THRESHOLD]
        confidence = 0.9 if len(validated) == len(combined_targets) else 0.8

        return AutoFocusResult(
            focus_targets=combined_targets,
            focus_regions=combined_regions,
            detection_method="+".join(method_parts),
            confidence=confidence,
            detail={
                "cv_scores": {t: cv_scores.get(t, 0) for t in combined_targets},
                "cv_validated": validated,
                "meta_source": "directory_name",
            },
        )

    if meta_targets:
        # Tier 1 only — convert targets to regions via landmarks
        regions = targets_to_regions(meta_targets, after_path) if after_path else []
        return AutoFocusResult(
            focus_targets=meta_targets,
            focus_regions=regions,
            detection_method="metadata",
            confidence=0.7,
            detail={"meta_source": "directory_name"},
        )

    if cv_targets:
        # Tier 2 only
        return AutoFocusResult(
            focus_targets=cv_targets,
            focus_regions=cv_regions,
            detection_method="cv",
            confidence=0.6,
            detail={"cv_scores": cv_scores},
        )

    # Tier 3: fallback — empty targets/regions = full-face mode
    LOGGER.info("Tier 3 fallback: no targets detected from metadata or CV")
    return AutoFocusResult(
        focus_targets=[],
        focus_regions=[],
        detection_method="fallback",
        confidence=0.0,
        detail={},
    )
