#!/usr/bin/env python3
"""
面部对齐对比图生成器
- MediaPipe 面部关键点检测
- 基于双眼位置做仿射变换（旋转+缩放+平移）
- 统一裁切，以面部中心为锚点
- 色彩归一化（亮度+色温匹配）
- 拼合 + 顶部标签条
"""
from __future__ import annotations

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision
import sys
import os
import math
import urllib.request
from pathlib import Path

# ── 模型路径 ──────────────────────────────────────────────
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
DEFAULT_MODEL_CACHE_DIR = Path.home() / ".cache" / "feishu-claude" / "mediapipe"
MODEL_PATH = os.environ.get("FACE_LANDMARKER_TASK_PATH") or str(
    Path("/tmp/face_landmarker.task") if Path("/tmp/face_landmarker.task").exists() else DEFAULT_MODEL_CACHE_DIR / "face_landmarker.task"
)

# ── MediaPipe 面部关键点索引 ──────────────────────────────
# Face Mesh 478 landmarks (含虹膜)
LEFT_EYE_INNER = 133
LEFT_EYE_OUTER = 33
RIGHT_EYE_INNER = 362
RIGHT_EYE_OUTER = 263
NOSE_TIP = 1
CHIN = 152
FOREHEAD = 10


def ensure_model():
    """确保 face landmarker 模型存在；缺失时自动下载到本地缓存。"""
    model_path = Path(os.environ.get("FACE_LANDMARKER_TASK_PATH") or MODEL_PATH)
    if model_path.exists() and model_path.stat().st_size > 0:
        return str(model_path)

    model_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = model_path.with_suffix(model_path.suffix + ".download")
    urllib.request.urlretrieve(MODEL_URL, tmp_path)
    tmp_path.replace(model_path)
    return str(model_path)


def estimate_pose_angles(transform_matrix):
    """从 MediaPipe 4x4 变换矩阵提取 pitch / yaw / roll（度）。"""
    rot = np.asarray(transform_matrix, dtype=np.float64)[:3, :3]
    sy = math.sqrt(rot[0, 0] ** 2 + rot[1, 0] ** 2)
    singular = sy < 1e-6

    if not singular:
        pitch = math.degrees(math.atan2(rot[2, 1], rot[2, 2]))
        yaw = math.degrees(math.atan2(-rot[2, 0], sy))
        roll = math.degrees(math.atan2(rot[1, 0], rot[0, 0]))
    else:
        pitch = math.degrees(math.atan2(-rot[1, 2], rot[1, 1]))
        yaw = math.degrees(math.atan2(-rot[2, 0], sy))
        roll = 0.0

    return {
        "pitch": float(pitch),
        "yaw": float(yaw),
        "roll": float(roll),
    }


def classify_view_from_yaw(yaw_deg):
    """按 yaw 粗分正面 / 45侧 / 侧面，并附带方向与置信度。"""
    abs_yaw = abs(float(yaw_deg))
    if abs_yaw < 10:
        bucket = "front"
        confidence = max(0.55, 1.0 - abs_yaw / 20)
        direction = "center"
    elif abs_yaw < 35:
        bucket = "oblique"
        confidence = max(0.55, 1.0 - abs(abs_yaw - 22.5) / 25)
        direction = "right" if yaw_deg > 0 else "left"
    else:
        bucket = "side"
        confidence = min(0.98, 0.7 + min(abs_yaw - 35, 20) / 30)
        direction = "right" if yaw_deg > 0 else "left"

    return {
        "bucket": bucket,
        "direction": direction,
        "confidence": float(round(confidence, 4)),
    }


def detect_face_landmarks(image_path):
    """检测面部关键点，返回关键坐标"""
    img = cv2.imread(image_path)
    if img is None:
        raise FileNotFoundError(f"无法读取图片: {image_path}")

    # 自动修正 EXIF 旋转
    img = auto_orient(img, image_path)

    h, w = img.shape[:2]

    # 使用 Tasks API
    base_options = mp_python.BaseOptions(model_asset_path=ensure_model())
    options = vision.FaceLandmarkerOptions(
        base_options=base_options,
        output_face_blendshapes=False,
        output_facial_transformation_matrixes=True,
        num_faces=1,
        min_face_detection_confidence=0.5,
        min_face_presence_confidence=0.5,
    )

    with vision.FaceLandmarker.create_from_options(options) as landmarker:
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB,
                            data=cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        result = landmarker.detect(mp_image)

    if not result.face_landmarks:
        raise ValueError(f"未检测到面部: {image_path}")

    landmarks = result.face_landmarks[0]

    def to_px(idx):
        lm = landmarks[idx]
        return np.array([lm.x * w, lm.y * h])

    # 取眼角中点作为眼睛中心
    left_eye = (to_px(LEFT_EYE_INNER) + to_px(LEFT_EYE_OUTER)) / 2
    right_eye = (to_px(RIGHT_EYE_INNER) + to_px(RIGHT_EYE_OUTER)) / 2

    # 如果有虹膜点 (index 468, 473)，优先使用
    if len(landmarks) > 473:
        try:
            iris_left = to_px(468)
            iris_right = to_px(473)
            iris_dist = np.linalg.norm(iris_right - iris_left)
            if iris_dist > w * 0.05:
                left_eye = iris_left
                right_eye = iris_right
        except (IndexError, ValueError):
            pass

    nose = to_px(NOSE_TIP)
    chin = to_px(CHIN)
    forehead = to_px(FOREHEAD)

    pose = {}
    view = {}
    transform_matrix = None
    if result.facial_transformation_matrixes:
        transform_matrix = np.asarray(result.facial_transformation_matrixes[0], dtype=np.float64)
        pose = estimate_pose_angles(transform_matrix)
        view = classify_view_from_yaw(pose["yaw"])

    return {
        'image': img,
        'left_eye': left_eye,
        'right_eye': right_eye,
        'nose': nose,
        'chin': chin,
        'forehead': forehead,
        'eye_center': (left_eye + right_eye) / 2,
        'eye_distance': np.linalg.norm(right_eye - left_eye),
        'face_height': np.linalg.norm(chin - forehead),
        'size': (w, h),
        'transform_matrix': transform_matrix,
        'pose': pose,
        'view': view,
    }


def auto_orient(img, image_path):
    """根据 EXIF 信息自动旋转图片"""
    try:
        from PIL import Image

        with Image.open(image_path) as pil:
            exif = pil.getexif()
        orientation = exif.get(274)  # 0x0112 = Orientation
        if orientation not in {3, 6, 8}:
            return img

        raw = cv2.imread(str(image_path), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if raw is not None:
            img = raw
        if orientation == 3:
            img = cv2.rotate(img, cv2.ROTATE_180)
        elif orientation == 6:
            img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
        elif orientation == 8:
            img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    except Exception:
        pass
    return img


def estimate_background_color(img):
    """从四角采样估算背景色，过滤皮肤/头发高彩度像素，避免仿射出界时产生色偏填充。"""
    h, w = img.shape[:2]
    patch = max(12, int(min(h, w) * 0.06))
    corners = [
        img[:patch, :patch],
        img[:patch, w - patch:w],
        img[h - patch:h, :patch],
        img[h - patch:h, w - patch:w],
    ]
    all_samples = np.concatenate([corner.reshape(-1, 3) for corner in corners], axis=0).astype(np.float64)
    gray = all_samples.mean(axis=1)
    channel_spread = all_samples.max(axis=1) - all_samples.min(axis=1)
    wall_mask = (channel_spread < 40) & (gray > 120)
    wall_pixels = all_samples[wall_mask]
    if len(wall_pixels) >= 50:
        median = np.median(wall_pixels, axis=0)
        return tuple(int(round(v)) for v in median.tolist())
    best_corner = None
    best_spread = 999.0
    for corner in corners:
        flat = corner.reshape(-1, 3).astype(np.float64)
        med = np.median(flat, axis=0)
        spread = float(med.max() - med.min())
        if spread < best_spread:
            best_spread = spread
            best_corner = med
    if best_corner is not None and best_spread < 50:
        return tuple(int(round(v)) for v in best_corner.tolist())
    bright_mask = gray >= np.percentile(gray, 75)
    bright_pixels = all_samples[bright_mask]
    if len(bright_pixels) > 0:
        median = np.median(bright_pixels, axis=0)
    else:
        median = np.median(all_samples, axis=0)
    return tuple(int(round(v)) for v in median.tolist())


# ── 面部对齐 ──────────────────────────────────────────────

def align_face(img, landmarks, target_eye_distance, target_eye_center, output_size, return_mask=False):
    """
    仿射变换：旋转 + 缩放 + 平移，使面部对齐到目标位置
    """
    left_eye = landmarks['left_eye']
    right_eye = landmarks['right_eye']

    # 当前眼间距和角度
    dx = right_eye[0] - left_eye[0]
    dy = right_eye[1] - left_eye[1]
    current_angle = np.degrees(np.arctan2(dy, dx))
    current_distance = np.linalg.norm(right_eye - left_eye)

    # 缩放比例
    scale = target_eye_distance / current_distance

    # 仿射变换矩阵：绕双眼中心旋转 + 缩放
    eye_center = landmarks['eye_center']
    M = cv2.getRotationMatrix2D(
        center=tuple(eye_center.astype(float)),
        angle=current_angle,  # 旋转到水平
        scale=scale
    )

    # 调整平移，使双眼中心移到目标位置
    M[0, 2] += target_eye_center[0] - eye_center[0]
    M[1, 2] += target_eye_center[1] - eye_center[1]

    aligned = cv2.warpAffine(
        img, M, output_size,
        flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=estimate_background_color(img),
    )
    if not return_mask:
        return aligned

    src_mask = np.full((img.shape[0], img.shape[1]), 255, dtype=np.uint8)
    valid_mask = cv2.warpAffine(
        src_mask, M, output_size,
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    ) > 0
    return aligned, valid_mask


def normalize_color(source, reference):
    """
    色彩归一化：将 source 的亮度和色温匹配到 reference
    使用 LAB 色彩空间做均值/标准差匹配
    """
    src_lab = cv2.cvtColor(source, cv2.COLOR_BGR2LAB).astype(np.float64)
    ref_lab = cv2.cvtColor(reference, cv2.COLOR_BGR2LAB).astype(np.float64)

    # 只在面部区域计算统计量（中心 60% 区域）
    h, w = source.shape[:2]
    y1, y2 = int(h * 0.2), int(h * 0.8)
    x1, x2 = int(w * 0.2), int(w * 0.8)

    for ch in range(3):
        src_mean = src_lab[y1:y2, x1:x2, ch].mean()
        src_std = src_lab[y1:y2, x1:x2, ch].std()
        ref_mean = ref_lab[y1:y2, x1:x2, ch].mean()
        ref_std = ref_lab[y1:y2, x1:x2, ch].std()

        if src_std < 1e-6:
            src_std = 1e-6

        src_lab[:, :, ch] = (src_lab[:, :, ch] - src_mean) * (ref_std / src_std) + ref_mean

    src_lab = np.clip(src_lab, 0, 255).astype(np.uint8)
    return cv2.cvtColor(src_lab, cv2.COLOR_LAB2BGR)


def _region_bounds(image):
    h, w = image.shape[:2]
    y1, y2 = int(h * 0.18), int(h * 0.82)
    x1, x2 = int(w * 0.18), int(w * 0.82)
    return x1, y1, x2, y2


def _face_region_mask(image):
    h, w = image.shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    center = (int(w * 0.5), int(h * 0.49))
    axes = (int(w * 0.24), int(h * 0.31))
    cv2.ellipse(mask, center, axes, 0, 0, 360, 255, -1)
    # 额头到下巴做更宽容的柔和过渡，但尽量不碰发际线和耳后背景。
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=max(w, h) * 0.06, sigmaY=max(w, h) * 0.06)
    return mask.astype(np.float64) / 255.0


def _lab_stats(image, mask=None):
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float64)
    x1, y1, x2, y2 = _region_bounds(image)
    region = lab[y1:y2, x1:x2]
    if mask is None:
        means = region.reshape(-1, 3).mean(axis=0)
        stds = region.reshape(-1, 3).std(axis=0)
    else:
        weights = mask[y1:y2, x1:x2].reshape(-1, 1)
        flat = region.reshape(-1, 3)
        weight_sum = max(float(weights.sum()), 1e-6)
        means = (flat * weights).sum(axis=0) / weight_sum
        var = (((flat - means) ** 2) * weights).sum(axis=0) / weight_sum
        stds = np.sqrt(np.maximum(var, 1e-6))
    stds = np.maximum(stds, 1e-6)
    return lab, means, stds


def _apply_lab_target(image, target_means, target_stds, mask=None,
                      max_std_ratio=1.35, max_mean_shift=12.0):
    lab, means, stds = _lab_stats(image, mask=mask)
    for ch in range(3):
        ratio = np.clip(target_stds[ch] / stds[ch], 1.0 / max_std_ratio, max_std_ratio)
        shift = np.clip(target_means[ch] - means[ch], -max_mean_shift, max_mean_shift)
        lab[:, :, ch] = (lab[:, :, ch] - means[ch]) * ratio + means[ch] + shift
    lab = np.clip(lab, 0, 255).astype(np.uint8)
    converted = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    if mask is None:
        return converted
    alpha = np.clip(mask[..., None], 0.0, 1.0)
    blended = converted.astype(np.float64) * alpha + image.astype(np.float64) * (1.0 - alpha)
    return np.clip(blended, 0, 255).astype(np.uint8)


def _region_sharpness(image, mask=None):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    x1, y1, x2, y2 = _region_bounds(image)
    region = gray[y1:y2, x1:x2]
    if mask is None:
        return float(cv2.Laplacian(region, cv2.CV_64F).var())
    weights = mask[y1:y2, x1:x2]
    lap = cv2.Laplacian(region, cv2.CV_64F)
    weight_sum = max(float(weights.sum()), 1e-6)
    mean = float((lap * weights).sum() / weight_sum)
    var = float((((lap - mean) ** 2) * weights).sum() / weight_sum)
    return var


def _unsharp_mask(image, sigma=1.1, amount=0.55, mask=None):
    blurred = cv2.GaussianBlur(image, (0, 0), sigma)
    sharpened = cv2.addWeighted(image, 1.0 + amount, blurred, -amount, 0)
    sharpened = np.clip(sharpened, 0, 255).astype(np.uint8)
    if mask is None:
        return sharpened
    alpha = np.clip(mask[..., None], 0.0, 1.0)
    blended = sharpened.astype(np.float64) * alpha + image.astype(np.float64) * (1.0 - alpha)
    return np.clip(blended, 0, 255).astype(np.uint8)


def _soft_ellipse_mask(shape, center, axes):
    h, w = shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.ellipse(mask, center, axes, 0, 0, 360, 255, -1)
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=max(w, h) * 0.05, sigmaY=max(w, h) * 0.05)
    return mask.astype(np.float64) / 255.0


def lift_face_shadows(image, slot='front'):
    """
    只提亮面中部暗部，避免整脸泛白：
    - front: 眼下 / 鼻旁 / 鼻唇沟区域轻度提亮
    - non-front: 做更轻的面中部阴影提升
    """
    h, w = image.shape[:2]
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float64)

    if slot == 'front':
        center_mask = _soft_ellipse_mask(
            image.shape,
            (int(w * 0.50), int(h * 0.54)),
            (int(w * 0.19), int(h * 0.17)),
        )
        left_under_eye = _soft_ellipse_mask(
            image.shape,
            (int(w * 0.37), int(h * 0.42)),
            (int(w * 0.11), int(h * 0.075)),
        )
        right_under_eye = _soft_ellipse_mask(
            image.shape,
            (int(w * 0.63), int(h * 0.42)),
            (int(w * 0.11), int(h * 0.075)),
        )
        left_nasolabial = _soft_ellipse_mask(
            image.shape,
            (int(w * 0.41), int(h * 0.56)),
            (int(w * 0.08), int(h * 0.10)),
        )
        right_nasolabial = _soft_ellipse_mask(
            image.shape,
            (int(w * 0.59), int(h * 0.56)),
            (int(w * 0.08), int(h * 0.10)),
        )
        nose_bridge = _soft_ellipse_mask(
            image.shape,
            (int(w * 0.50), int(h * 0.47)),
            (int(w * 0.07), int(h * 0.11)),
        )
        mask = np.maximum.reduce([
            center_mask,
            left_under_eye,
            right_under_eye,
            left_nasolabial,
            right_nasolabial,
            nose_bridge,
        ])
        lift_cap = 24.0
    else:
        mask = _soft_ellipse_mask(
            image.shape,
            (int(w * 0.50), int(h * 0.54)),
            (int(w * 0.16), int(h * 0.12)),
        )
        lift_cap = 10.0

    lightness = lab[:, :, 0]
    target_L = 178.0 if slot == 'front' else 165.0
    shadow_gate = np.clip((target_L - lightness) / 78.0, 0.0, 1.0)
    delta = mask * shadow_gate * lift_cap
    lab[:, :, 0] = np.clip(lightness + delta, 0, 255)

    return cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2BGR)


SHARPNESS_ASYMMETRY_RATIO = 1.18


def harmonize_pair(before, after):
    """
    成对统一亮度/色温/清晰度：
    - 不对称（max/min sharpness > SHARPNESS_ASYMMETRY_RATIO）：单向 LAB，把弱侧向强侧拉，强侧统计保留
    - 对称：双向 LAB 拉到中点
    - unsharp 调用对称：要么两侧都加，要么两侧都不加，避免单边补偿破坏强侧高频细节
    - 只在人脸保护区内生效，不触发背景和发际线的大幅变化
    """
    before_mask = _face_region_mask(before)
    after_mask = _face_region_mask(after)
    _, before_means, before_stds = _lab_stats(before, mask=before_mask)
    _, after_means, after_stds = _lab_stats(after, mask=after_mask)

    raw_before_sharp = _region_sharpness(before, mask=before_mask)
    raw_after_sharp = _region_sharpness(after, mask=after_mask)

    asymmetric = False
    after_is_stronger = False
    if raw_before_sharp > 0 and raw_after_sharp > 0:
        if max(raw_before_sharp, raw_after_sharp) / min(raw_before_sharp, raw_after_sharp) > SHARPNESS_ASYMMETRY_RATIO:
            asymmetric = True
            after_is_stronger = raw_after_sharp > raw_before_sharp

    if asymmetric:
        if after_is_stronger:
            before_out = _apply_lab_target(before, after_means, after_stds, mask=before_mask)
            after_out = after
        else:
            before_out = before
            after_out = _apply_lab_target(after, before_means, before_stds, mask=after_mask)
    else:
        target_means = (before_means + after_means) / 2.0
        target_stds = (before_stds + after_stds) / 2.0
        before_out = _apply_lab_target(before, target_means, target_stds, mask=before_mask)
        after_out = _apply_lab_target(after, target_means, target_stds, mask=after_mask)

    before_sharp = _region_sharpness(before_out, mask=before_mask)
    after_sharp = _region_sharpness(after_out, mask=after_mask)
    if before_sharp > 0 and after_sharp > 0:
        ratio = min(before_sharp, after_sharp) / max(before_sharp, after_sharp)
        if ratio < 0.82:
            before_out = _unsharp_mask(before_out, mask=before_mask)
            after_out = _unsharp_mask(after_out, mask=after_mask)

    return before_out, after_out


def harmonize_group(images: list[np.ndarray], masks: list[np.ndarray | None] | None = None) -> list[np.ndarray]:
    """
    组内统一色温/亮度：
    - 选取第一张（通常是正面）作为基准锚点
    - 将其他所有图片向该锚点对齐
    """
    if not images:
        return images
    if masks is None:
        masks = [None] * len(images)

    # 寻找第一个有效锚点
    anchor_idx = 0
    anchor_mask = _face_region_mask(images[anchor_idx]) if masks[anchor_idx] is None else masks[anchor_idx]
    _, anchor_means, anchor_stds = _lab_stats(images[anchor_idx], mask=anchor_mask)

    outputs = []
    for i, (img, mask) in enumerate(zip(images, masks)):
        if i == anchor_idx:
            outputs.append(img)
            continue
        m = _face_region_mask(img) if mask is None else mask
        out = _apply_lab_target(img, anchor_means, anchor_stds, mask=m)
        outputs.append(out)
    return outputs


# ── 合成对比图 ────────────────────────────────────────────

def create_comparison(before_path, after_path, output_path,
                      canvas_width=1600, canvas_height=1100,
                      label_height=80, bg_color=(240, 235, 228)):
    """
    生成对齐的术前术后对比图

    参数:
        before_path: 术前图片路径
        after_path: 术后图片路径
        output_path: 输出路径
        canvas_width: 画布总宽度
        canvas_height: 画布总高度（含标签）
        label_height: 顶部标签条高度
        bg_color: 背景色 (B,G,R)
    """
    print(f"处理: {os.path.basename(before_path)} vs {os.path.basename(after_path)}")

    # 1. 检测面部关键点
    before = detect_face_landmarks(before_path)
    after = detect_face_landmarks(after_path)

    print(f"  术前: 眼距={before['eye_distance']:.0f}px, 面高={before['face_height']:.0f}px")
    print(f"  术后: 眼距={after['eye_distance']:.0f}px, 面高={after['face_height']:.0f}px")

    # 2. 计算对齐参数
    half_width = canvas_width // 2
    img_height = canvas_height - label_height

    # 目标眼间距：面部占单侧画面的 ~40%
    target_eye_distance = half_width * 0.24

    # 目标眼睛中心位置：水平居中，垂直留足头顶空间
    target_eye_y = img_height * 0.40
    target_eye_center = np.array([half_width / 2, target_eye_y])

    output_size = (half_width, img_height)

    # 3. 对齐两张图
    before_aligned = align_face(before['image'], before, target_eye_distance, target_eye_center, output_size)
    after_aligned = align_face(after['image'], after, target_eye_distance, target_eye_center, output_size)

    # 4. 色彩归一化（术后匹配术前）
    after_aligned = normalize_color(after_aligned, before_aligned)

    # 5. 合成画布
    canvas = np.full((canvas_height, canvas_width, 3), bg_color, dtype=np.uint8)

    # 标签条
    before_label_color = (100, 80, 60)   # 深蓝灰色 (B,G,R)
    after_label_color = (100, 160, 80)   # 绿色 (B,G,R)

    canvas[0:label_height, 0:half_width] = before_label_color
    canvas[0:label_height, half_width:canvas_width] = after_label_color

    # 标签文字（居中）
    put_chinese_text(canvas, "术前",
                     (half_width // 2 - 24, label_height // 2 - 16),
                     color=(255, 255, 255), font_scale=1.3, thickness=2)
    put_chinese_text(canvas, "术后",
                     (half_width + half_width // 2 - 24, label_height // 2 - 16),
                     color=(255, 255, 255), font_scale=1.3, thickness=2)

    # 放置对齐后的图片
    canvas[label_height:canvas_height, 0:half_width] = before_aligned
    canvas[label_height:canvas_height, half_width:canvas_width] = after_aligned

    # 中线分隔
    cv2.line(canvas, (half_width, 0), (half_width, canvas_height), (255, 255, 255), 2)

    # 6. 保存
    cv2.imwrite(output_path, canvas, [cv2.IMWRITE_JPEG_QUALITY, 92])
    file_size = os.path.getsize(output_path) / 1024
    print(f"  输出: {output_path} ({file_size:.0f}KB)")

    return output_path


def put_chinese_text(img, text, position, color=(255, 255, 255), font_scale=0.9, thickness=2):
    """
    使用 PIL 绘制中文文字（OpenCV 不支持中文）
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(pil_img)

        # 尝试加载中文字体
        font_paths = [
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
        ]
        font_size = int(font_scale * 28)
        font = None
        for fp in font_paths:
            if os.path.exists(fp):
                font = ImageFont.truetype(fp, font_size)
                break
        if font is None:
            font = ImageFont.load_default()

        # PIL 颜色是 RGB
        pil_color = (color[2], color[1], color[0])
        draw.text(position, text, font=font, fill=pil_color)

        result = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        img[:] = result[:]
    except Exception:
        # 降级到 OpenCV 英文
        cv2.putText(img, text, position, cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, thickness)


# ── 批量处理 ──────────────────────────────────────────────

def batch_process(case_dir, output_dir, pairs=None):
    """
    批量处理一个案例目录

    参数:
        case_dir: 案例目录路径
        output_dir: 输出目录
        pairs: 手动指定配对列表 [(before, after), ...]，None 则自动匹配
    """
    os.makedirs(output_dir, exist_ok=True)

    if pairs is None:
        # 自动配对：术前N ↔ 术后N
        befores = sorted([f for f in os.listdir(case_dir) if f.startswith('术前') and f.endswith(('.jpg', '.JPG', '.jpeg', '.png'))])
        afters = sorted([f for f in os.listdir(case_dir) if f.startswith('术后') and f.endswith(('.jpg', '.JPG', '.jpeg', '.png'))])

        pairs = []
        for b in befores:
            # 提取编号
            num = ''.join(c for c in b.replace('术前', '') if c.isdigit())
            matching = [a for a in afters if num in a.replace('术后', '')]
            if matching:
                pairs.append((b, matching[0]))

    results = []
    for i, (before_name, after_name) in enumerate(pairs):
        before_path = os.path.join(case_dir, before_name)
        after_path = os.path.join(case_dir, after_name)

        if not os.path.exists(before_path) or not os.path.exists(after_path):
            print(f"  跳过: {before_name} 或 {after_name} 不存在")
            continue

        output_name = f"对比图_{i+1:02d}.jpg"
        output_path = os.path.join(output_dir, output_name)

        try:
            create_comparison(before_path, after_path, output_path)
            results.append(output_path)
        except Exception as e:
            print(f"  失败: {e}")

    return results


# ── CLI 入口 ──────────────────────────────────────────────

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("用法: python face_align_compare.py <术前图片> <术后图片> [输出路径]")
        print("批量: python face_align_compare.py --batch <案例目录> <输出目录>")
        sys.exit(1)

    if sys.argv[1] == '--batch':
        case_dir = sys.argv[2]
        output_dir = sys.argv[3] if len(sys.argv) > 3 else os.path.join(case_dir, 'aligned_compare')
        results = batch_process(case_dir, output_dir)
        print(f"\n完成: 共生成 {len(results)} 张对比图")
    else:
        before = sys.argv[1]
        after = sys.argv[2]
        output = sys.argv[3] if len(sys.argv) > 3 else 'compare_output.jpg'
        create_comparison(before, after, output)
