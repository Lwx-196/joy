"""出框源图实时 gate：面部保护框未 clamp 越界比例检测.

为什么存在（2026-06-10 黄靖榕 case 365 实锤）：
- 治疗床即刻近景源图半脸贴边/出框，但既有信号全部失效——VLM angle_confidence
  反而高（0.88-0.99）、T69 enrichment 弱信号全 None、layout 历史证据只降权不剔除。
- 本模块在 selection plan 构建期实时计算 case_layout_board.build_crop_box 同款
  保护框的**未 clamp** 越界比例（truncation），供 render_queue 剔除超阈值候选。

标定（/tmp/probe_face_frame_gate.py，51 张真实源图）：
- 黄靖榕坏照 trunc 0.181-0.470 全触发；好 case 47 张 max trunc=0.071 零误杀
- 分隔带 [0.071, 0.181] → 阈值定 0.12

纪律：
- fail-open：cv2/mediapipe 不可用、模型缺失、读图失败、no-face（非面部照合法）
  → 一律不剔除（verdict.exceeded=False），与拼接图过滤双层防御先例一致。
- 性能：face_align_compare.detect_face_landmarks 每次新建 landmarker（~1-2s/张），
  本模块复用进程级 landmarker 实例 + (path, mtime) 结果缓存，否则 plan build 卡死。
- 提取逻辑必须与 face_align_compare.detect_face_landmarks 逐行一致
  （标定数据来自该路径），仅 landmarker 生命周期不同。
"""
from __future__ import annotations

import importlib.util
import logging
import os
import threading
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

# 标定阈值：好 case max 0.071 / 坏照 min 0.181，取分隔带内 0.12
FACE_FRAME_TRUNCATION_THRESHOLD = 0.12

FACE_ALIGN_PATH = Path(__file__).resolve().parents[1] / "layout" / "scripts" / "face_align_compare.py"

_LOCK = threading.Lock()
_FA_MODULE: Any = None  # None=未加载 / False=加载失败（缓存失败避免反复 exec） / module
_LANDMARKER: Any = None
_VERDICT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def protection_box_truncation(face_info: dict[str, Any], size: tuple[float, float]) -> tuple[float, dict[str, float]]:
    """照 case_layout_board.build_crop_box 公式（未 clamp），量化保护框越界比例.

    返回 (truncation, edge_overflow)：truncation = 1 - clamp 后保留面积比；
    edge_overflow = 各边越界长度占框宽/高的比例。纯数学，无 I/O。
    """
    eye_center = face_info["eye_center"]
    eye_d = float(face_info["eye_distance"])
    face_h = float(face_info["face_height"])
    w, h = float(size[0]), float(size[1])
    box_w = max(eye_d * 3.1, face_h * 1.25)
    box_h = max(face_h * 1.18, eye_d * 3.0)
    if box_w <= 0 or box_h <= 0:
        # 退化 face_info（眼距/脸高全 0）无法定义保护框 → fail-open
        return 0.0, {"left": 0.0, "top": 0.0, "right": 0.0, "bottom": 0.0}
    x1 = float(eye_center[0]) - box_w / 2
    y1 = float(eye_center[1]) - box_h * 0.36
    x2, y2 = x1 + box_w, y1 + box_h
    cx1, cy1 = max(0.0, x1), max(0.0, y1)
    cx2, cy2 = min(w, x2), min(h, y2)
    kept = max(0.0, cx2 - cx1) * max(0.0, cy2 - cy1)
    # max 钳掉浮点误差导致的 -0.0（kept ≈ 全框面积时）
    truncation = max(0.0, 1.0 - kept / (box_w * box_h))
    edge_overflow = {
        "left": max(0.0, -x1) / box_w,
        "top": max(0.0, -y1) / box_h,
        "right": max(0.0, x2 - w) / box_w,
        "bottom": max(0.0, y2 - h) / box_h,
    }
    return truncation, edge_overflow


def _load_face_align() -> Any:
    """importlib 按路径加载 face_align_compare（照 case_layout_board L33-38 先例）.

    重依赖（cv2/mediapipe/numpy）在 exec_module 时才进内存；失败缓存为 False
    避免每张图反复重试 import。
    """
    global _FA_MODULE
    if _FA_MODULE is not None:
        return _FA_MODULE
    try:
        spec = importlib.util.spec_from_file_location("face_align_compare", FACE_ALIGN_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载 face_align_compare.py: {FACE_ALIGN_PATH}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _FA_MODULE = module
    except Exception as exc:  # fail-open：CV 依赖缺失的环境照常跑选图，只是 gate 失效
        LOGGER.warning("face frame gate 不可用（face_align_compare 加载失败）: %s", exc)
        _FA_MODULE = False
    return _FA_MODULE


def _get_landmarker(fa_module: Any) -> Any:
    """进程级复用 FaceLandmarker（原 detect_face_landmarks 每次新建 ~1-2s/张）.

    选项与 face_align_compare.detect_face_landmarks 完全一致，保证与标定同路径。
    """
    global _LANDMARKER
    if _LANDMARKER is None:
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision

        base_options = mp_python.BaseOptions(model_asset_path=fa_module.ensure_model())
        options = vision.FaceLandmarkerOptions(
            base_options=base_options,
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=True,
            num_faces=1,
            min_face_detection_confidence=0.5,
            min_face_presence_confidence=0.5,
        )
        _LANDMARKER = vision.FaceLandmarker.create_from_options(options)
    return _LANDMARKER


def _detect_face_geometry(image_path: str, fa_module: Any) -> dict[str, Any]:
    """复刻 face_align_compare.detect_face_landmarks 的眼点/脸高提取（landmarker 复用版）.

    只取保护框公式需要的字段；眼角中点 + 虹膜优先逻辑与原函数逐行一致。
    no-face 时与原函数一样抛 ValueError。
    """
    import cv2
    import mediapipe as mp
    import numpy as np

    img = cv2.imread(image_path)
    if img is None:
        raise FileNotFoundError(f"无法读取图片: {image_path}")
    img = fa_module.auto_orient(img, image_path)
    h, w = img.shape[:2]

    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    result = _get_landmarker(fa_module).detect(mp_image)
    if not result.face_landmarks:
        raise ValueError(f"未检测到面部: {image_path}")
    landmarks = result.face_landmarks[0]

    def to_px(idx: int) -> Any:
        lm = landmarks[idx]
        return np.array([lm.x * w, lm.y * h])

    left_eye = (to_px(fa_module.LEFT_EYE_INNER) + to_px(fa_module.LEFT_EYE_OUTER)) / 2
    right_eye = (to_px(fa_module.RIGHT_EYE_INNER) + to_px(fa_module.RIGHT_EYE_OUTER)) / 2
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
    chin = to_px(fa_module.CHIN)
    forehead = to_px(fa_module.FOREHEAD)
    return {
        "eye_center": (left_eye + right_eye) / 2,
        "eye_distance": float(np.linalg.norm(right_eye - left_eye)),
        "face_height": float(np.linalg.norm(chin - forehead)),
        "size": (w, h),
    }


def _evaluate(image_path: str) -> dict[str, Any]:
    fa_module = _load_face_align()
    if not fa_module:
        return {"status": "unavailable", "truncation": None, "edge_overflow": None, "exceeded": False}
    try:
        face_info = _detect_face_geometry(image_path, fa_module)
    except ValueError:
        # no-face：陈艺琼式非面部源图合法 → 不剔除
        return {"status": "no_face", "truncation": None, "edge_overflow": None, "exceeded": False}
    except Exception as exc:  # 读图失败 / 模型下载失败 / mediapipe 运行时异常 → fail-open
        LOGGER.warning("face frame gate 检测异常（fail-open）: %s: %s", image_path, exc)
        return {"status": "unavailable", "truncation": None, "edge_overflow": None, "exceeded": False}
    truncation, edge_overflow = protection_box_truncation(face_info, face_info["size"])
    return {
        "status": "evaluated",
        "truncation": round(truncation, 4),
        "edge_overflow": {edge: round(value, 4) for edge, value in edge_overflow.items()},
        "exceeded": truncation >= FACE_FRAME_TRUNCATION_THRESHOLD,
    }


def evaluate_face_frame(image_path: str | Path) -> dict[str, Any]:
    """评估单张源图的保护框出框情况，返回 verdict dict.

    verdict: {status: evaluated|no_face|unavailable, truncation, edge_overflow, exceeded}
    只有 status=evaluated 且 truncation>=0.12 时 exceeded=True；其余一律 fail-open。
    (path, mtime) 进程内缓存，文件未变不重测。
    """
    path = str(image_path)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return {"status": "unavailable", "truncation": None, "edge_overflow": None, "exceeded": False}
    with _LOCK:
        cached = _VERDICT_CACHE.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        verdict = _evaluate(path)
        _VERDICT_CACHE[path] = (mtime, verdict)
        return verdict
