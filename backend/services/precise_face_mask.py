from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Sequence


_MODEL_ENV_VARS = (
    "PRECISE_FACE_MASK_MODEL",
    "MEDIAPIPE_FACE_LANDMARKER_MODEL",
)
_DEFAULT_MODEL_PATH = "~/.cache/feishu-claude/mediapipe/face_landmarker.task"

_LIPS = (
    61,
    146,
    91,
    181,
    84,
    17,
    314,
    405,
    321,
    375,
    291,
    409,
    270,
    269,
    267,
    0,
    37,
    39,
    40,
    185,
    308,
    78,
)
_CHIN = (
    152,
    148,
    176,
    149,
    150,
    377,
    400,
    378,
    379,
    365,
    175,
    199,
    200,
    18,
    83,
    313,
    201,
    421,
    208,
    428,
    169,
    394,
)
_GLABELLA = (9, 8, 107, 55, 285, 336, 66, 296, 105, 334, 65, 295)
_FOREHEAD = (
    10,
    67,
    69,
    104,
    108,
    151,
    9,
    337,
    299,
    333,
    297,
    338,
    105,
    334,
    66,
    296,
    107,
    336,
    71,
    301,
    63,
    293,
    109,
    338,
)

_REGION_LANDMARKS: dict[str, Sequence[int]] = {
    "lips": _LIPS,
    "chin": _CHIN,
    "glabella": _GLABELLA,
    "forehead": _FOREHEAD,
}
_REGION_DILATE_FRAC = {
    "lips": 0.018,
    "chin": 0.022,
    "glabella": 0.014,
    "forehead": 0.010,
}
_REGION_ALIASES = {
    "lip": "lips",
    "lips": "lips",
    "mouth": "lips",
    "唇": "lips",
    "嘴": "lips",
    "嘴唇": "lips",
    "口唇": "lips",
    "丰唇": "lips",
    "海魅": "lips",
    "chin": "chin",
    "jaw": "chin",
    "jawline": "chin",
    "下巴": "chin",
    "下颌": "chin",
    "下颌线": "chin",
    "颏": "chin",
    "glabella": "glabella",
    "frown": "glabella",
    "frown_lines": "glabella",
    "brow": "glabella",
    "眉间": "glabella",
    "眉间纹": "glabella",
    "川字": "glabella",
    "川字纹": "glabella",
    "forehead": "forehead",
    "forehead_lines": "forehead",
    "额头": "forehead",
    "额纹": "forehead",
    "抬头": "forehead",
    "抬头纹": "forehead",
}


def generate_precise_mask(
    image_path: str | Path,
    region_keys: Iterable[str],
    output_path: str | Path,
    *,
    feather_frac: float = 0.018,
) -> Path:
    """Generate a MediaPipe landmark-union face treatment mask.

    The output is an L-mode PNG with white treatment pixels and black preserved
    intervals between independently generated region hulls.
    """
    regions = _normalise_region_keys(region_keys)
    if feather_frac < 0:
        raise ValueError("feather_frac must be non-negative")

    mp, mp_python, mp_vision = _lazy_import_mediapipe()

    try:
        import cv2
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise ImportError(
            "precise face mask generation requires cv2, numpy, and Pillow"
        ) from exc

    src_path = Path(image_path)
    img_bgr = cv2.imread(str(src_path), cv2.IMREAD_COLOR)
    if img_bgr is None:
        raise FileNotFoundError(f"cannot read image for precise face mask: {src_path}")

    h, w = img_bgr.shape[:2]
    landmarks = _landmarks_px(img_bgr, mp, mp_python, mp_vision, cv2, np)
    if landmarks is None:
        raise RuntimeError(f"no face detected for precise face mask: {src_path}")
    _validate_landmarks(landmarks)

    face_w = float(np.linalg.norm(landmarks[454] - landmarks[234]))
    if face_w <= 0:
        raise RuntimeError("invalid MediaPipe face width from landmarks 454 and 234")

    union = np.zeros((h, w), dtype=np.uint8)
    for region in regions:
        region_mask = _region_mask(
            (h, w),
            landmarks,
            _REGION_LANDMARKS[region],
            _REGION_DILATE_FRAC[region],
            face_w,
            cv2,
            np,
        )
        union = np.maximum(union, region_mask)

    if feather_frac > 0:
        sigma = max(1.0, feather_frac * face_w)
        ksize = int(sigma * 4) | 1
        union = cv2.GaussianBlur(union, (ksize, ksize), sigma)

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(union, mode="L").save(out_path, format="PNG", optimize=True)
    return out_path


def precise_region_for(key: str) -> str | None:
    """closeup region key → precise landmark region 名（无精确 landmark 支持则 None）。

    用于近景分流：有 landmark 的部位（眉间/颏/唇/额）用真实 landmark mask 锚定，
    无的（泪沟/苹果肌/法令纹…）走 face_bbox 相对粗椭圆。映射表 = ``_REGION_ALIASES``
    （唯一 SSoT，避免在 board_closeup_section 重复维护一份硬编码集）。
    """
    lookup = str(key).strip().lower().replace("-", "_").replace(" ", "_")
    return _REGION_ALIASES.get(lookup)


def detect_face_bbox(image_path: str | Path) -> tuple[int, int, int, int] | None:
    """检测人脸包围盒（像素 ``left, top, right, bottom``），失败一律返回 None。

    用 MediaPipe FaceLandmarker 478 点取 min/max 包围盒并 clamp 进图界。

    用途：把人脸归一的 ROI 分数（``focal_mask_generator._FOCAL_REGIONS``）映射到
    **真实人脸位置/尺度**而非整图——使 before/after 近景按人脸解剖对齐，根治
    closeup 各自独立裁剪导致的术前/术后错位（③ 川字术后偏上）。

    注意：用 ``cv2.imread`` 读 raw 像素（**不**应用 EXIF）。调用方须传**已 EXIF
    归一**的图（如 board_closeup_section 的 norm_src），与 ``generate_focus_mask``
    同坐标系。fail-open：依赖缺失 / 读图失败 / 无脸 / 任何异常都返回 None
    （近景是增益不是门槛，永不挡板）。
    """
    try:
        mp, mp_python, mp_vision = _lazy_import_mediapipe()
        import cv2
        import numpy as np
    except ImportError:
        return None

    try:
        img_bgr = cv2.imread(str(Path(image_path)), cv2.IMREAD_COLOR)
        if img_bgr is None:
            return None
        h, w = img_bgr.shape[:2]
        landmarks = _landmarks_px(img_bgr, mp, mp_python, mp_vision, cv2, np)
        if landmarks is None or len(landmarks) == 0:
            return None
        xs, ys = landmarks[:, 0], landmarks[:, 1]
        left = int(max(0, np.floor(float(xs.min()))))
        top = int(max(0, np.floor(float(ys.min()))))
        right = int(min(w, np.ceil(float(xs.max()))))
        bottom = int(min(h, np.ceil(float(ys.max()))))
        if right <= left or bottom <= top:
            return None
        return (left, top, right, bottom)
    except Exception:  # noqa: BLE001 — fail-open
        return None


def _normalise_region_keys(region_keys: Iterable[str]) -> list[str]:
    regions: list[str] = []
    unknown: list[str] = []

    for raw_key in region_keys:
        key = str(raw_key).strip()
        if not key:
            continue
        lookup_key = key.lower().replace("-", "_").replace(" ", "_")
        region = _REGION_ALIASES.get(lookup_key)
        if region is None:
            unknown.append(key)
            continue
        if region not in regions:
            regions.append(region)

    if unknown:
        joined = ", ".join(unknown)
        raise ValueError(f"unknown precise face mask region(s): {joined}")
    if not regions:
        raise ValueError("no precise face mask regions requested")
    return regions


def _lazy_import_mediapipe():
    try:
        import mediapipe as mp
    except ImportError as exc:
        raise ImportError(
            "mediapipe is required for precise face mask generation; "
            "install mediapipe or use the coarse mask backend"
        ) from exc

    try:
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision
    except ImportError:
        mp_python = None
        mp_vision = None

    return mp, mp_python, mp_vision


def _landmarks_px(img_bgr, mp, mp_python, mp_vision, cv2, np):
    model_path = _face_landmarker_model_path()
    if model_path is not None and mp_python is not None and mp_vision is not None:
        return _landmarks_px_with_tasks(
            img_bgr, model_path, mp, mp_python, mp_vision, cv2, np
        )
    return _landmarks_px_with_solution(img_bgr, mp, cv2, np)


def _landmarks_px_with_tasks(img_bgr, model_path: Path, mp, mp_python, mp_vision, cv2, np):
    base_options = mp_python.BaseOptions(model_asset_path=str(model_path))
    options = mp_vision.FaceLandmarkerOptions(
        base_options=base_options,
        num_faces=1,
    )
    detector = mp_vision.FaceLandmarker.create_from_options(options)
    try:
        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = detector.detect(mp_image)
    finally:
        detector.close()

    if not result.face_landmarks:
        return None

    h, w = img_bgr.shape[:2]
    return np.array(
        [[point.x * w, point.y * h] for point in result.face_landmarks[0]],
        dtype=np.float32,
    )


def _landmarks_px_with_solution(img_bgr, mp, cv2, np):
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    with mp.solutions.face_mesh.FaceMesh(
        static_image_mode=True,
        max_num_faces=1,
        refine_landmarks=True,
    ) as face_mesh:
        result = face_mesh.process(rgb)

    if not result.multi_face_landmarks:
        return None

    h, w = img_bgr.shape[:2]
    return np.array(
        [[point.x * w, point.y * h] for point in result.multi_face_landmarks[0].landmark],
        dtype=np.float32,
    )


def _face_landmarker_model_path() -> Path | None:
    candidates: list[Path] = []
    for env_var in _MODEL_ENV_VARS:
        value = os.environ.get(env_var)
        if value:
            candidates.append(Path(value).expanduser())
    candidates.append(Path(_DEFAULT_MODEL_PATH).expanduser())

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _validate_landmarks(landmarks) -> None:
    if len(landmarks) < 478:
        raise RuntimeError(
            "MediaPipe face landmarks do not contain required 478-landmark indices"
        )


def _region_mask(shape, landmarks, indices, dilate_frac, face_w, cv2, np):
    mask = np.zeros(shape, dtype=np.uint8)
    polygon = landmarks[list(indices)].astype(np.int32)
    hull = cv2.convexHull(polygon)
    cv2.fillConvexPoly(mask, hull, 255)

    kernel_size = max(3, int(dilate_frac * face_w) | 1)
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (kernel_size, kernel_size),
    )
    return cv2.dilate(mask, kernel)


__all__ = ["generate_precise_mask", "detect_face_bbox", "precise_region_for"]
