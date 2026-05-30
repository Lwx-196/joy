"""可插拔头部姿态估计后端 —— FaceMesh（现状默认）vs BlazeFace 门控 + 6DRepNet-ONNX（侧脸更准）。

owner 2026-05-30：**影子模式 + flag，默认 facemesh，生产零行为变化**。
保留 `case_material_coverage` 的"区分不出就降级"哲学：no-face / 低置信检测 → has_face=False，
让下游 `classify_angle` 走 unknown 降级，绝不硬判侧脸。

flag `CASE_WORKBENCH_POSE_BACKEND`：
- `facemesh`（默认）：现状，FaceLandmarker 旋转矩阵 → yaw（与 case_material_coverage:145-149 逐字一致）。
- `sixdrep`：BlazeFace full-range 门控 → 有脸裁剪 → 6DRepNet ONNX 出 yaw（Phase 5 才翻默认）。
- `shadow`：两 backend 都跑、都写日志 `pose_shadow_compare`，**生产值仍用 facemesh**（字节不变）。

重依赖（numpy/cv2/mediapipe/onnxruntime/PIL）全部 **lazy import**：本模块在无 CV 依赖的
CI pytest venv 里能裸导入；真实推理只在 CV-capable 环境发生。

6DRepNet ONNX 两个生产必踩坑（Phase 1 实测，已 bake 进本实现）：
1. onnxruntime 必须 `ORT_ENABLE_BASIC`——默认 `ORT_ENABLE_ALL` 把 RepVGG 分组卷积优化坏 →
   6D 向量爆 1e17 → 所有输入塌成同一姿态（**不报错，静默全错**）。
2. 预处理用 PIL（torchvision-exact），不用 cv2.resize（抗锯齿对不上，边界角度会misbucket）。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

ENV_BACKEND = "CASE_WORKBENCH_POSE_BACKEND"
ENV_FACEDETECT_MODEL = "CASE_WORKBENCH_FACEDETECT_MODEL"   # BlazeFace full-range .tflite
ENV_SIXDREP_ONNX = "CASE_WORKBENCH_SIXDREP_ONNX"           # 6DRepNet 导出的 .onnx

MODE_FACEMESH, MODE_SIXDREP, MODE_SHADOW = "facemesh", "sixdrep", "shadow"
_DEFAULT_MODE = MODE_FACEMESH

# Phase 1 benchmark：BlazeFace full-range 在医美真实分布上的甜点阈值
# （profile .74–.88 vs 误检 .37，gap 干净；0.5 同时拿 profile 5/5 + non-face 4/4）。
_DETECT_SCORE_THR = 0.5
# 6D 训练用偏紧的人脸裁剪；检测器 bbox 加少量 margin（Phase 3 随阈值重标定一起复测）。
_CROP_MARGIN = 0.25

_shadow_logger = logging.getLogger("pose_shadow_compare")

# ImageNet 归一化（torchvision 默认；6DRepNet 训练沿用）
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)


@dataclass(frozen=True)
class PoseResult:
    """单脸姿态估计结果。yaw=None 表示未检到脸（→ 下游降级）。"""
    has_face: bool
    yaw: float | None                 # 绝对偏航角度
    pitch: float | None = None
    roll: float | None = None
    certain: bool = False             # 该 backend 是否给出可靠读数（边界软判仍由 classify_angle 决定）
    source: str = ""                  # 哪个 backend 产的（审计/影子日志）
    score: float | None = None        # 检测器置信度（6D 路径），facemesh 为 None


def pose_backend_mode() -> str:
    """读 flag，未知值安全回落 facemesh。"""
    mode = os.environ.get(ENV_BACKEND, _DEFAULT_MODE).strip().lower()
    return mode if mode in (MODE_FACEMESH, MODE_SIXDREP, MODE_SHADOW) else _DEFAULT_MODE


# ----------------------- FaceMesh backend（现状，默认）-----------------------

class FaceMeshPoseBackend:
    """FaceLandmarker 旋转矩阵 → yaw。与 case_material_coverage.classify_views:145-149 逐字一致，
    保证 mode=facemesh 时 PhotoView 输出字节不变。detector 实例化一次复用。"""

    name = MODE_FACEMESH

    def __init__(self, model_path: str):
        self._model_path = model_path
        self._det = None

    def _detector(self):
        if self._det is None:
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision

            base = mp_python.BaseOptions(model_asset_path=self._model_path)
            opts = vision.FaceLandmarkerOptions(base_options=base, num_faces=1,
                                                output_facial_transformation_matrixes=True)
            self._det = vision.FaceLandmarker.create_from_options(opts)
        return self._det

    def estimate(self, image_bgr) -> PoseResult:
        import math

        import cv2
        import mediapipe as mp
        import numpy as np

        r = self._detector().detect(mp.Image(image_format=mp.ImageFormat.SRGB,
                                              data=cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)))
        has_face = bool(r.face_landmarks)
        yaw = None
        if has_face and r.facial_transformation_matrixes:
            R = np.array(r.facial_transformation_matrixes[0])[:3, :3]
            yaw = abs(math.degrees(math.atan2(-R[2, 0], math.hypot(R[0, 0], R[1, 0]))))
        return PoseResult(has_face=has_face, yaw=yaw,
                          certain=has_face and yaw is not None, source=self.name)


# ----------------------- 6DRepNet backend（BlazeFace 门控 + ONNX）-----------------------

def _euler_from_rotation_matrix(R):
    """6DRepNet 旋转矩阵(batch,3,3) → (pitch,yaw,roll) 度。与 sixdrepnet 原版逐位等价
    （Phase 1 ONNX agent 验证 max diff 0.00014°）。"""
    import numpy as np

    sy = np.sqrt(R[:, 0, 0] ** 2 + R[:, 1, 0] ** 2)
    singular = sy < 1e-6
    x = np.arctan2(R[:, 2, 1], R[:, 2, 2])
    y = np.arctan2(-R[:, 2, 0], sy)
    z = np.arctan2(R[:, 1, 0], R[:, 0, 0])
    xs = np.arctan2(-R[:, 1, 2], R[:, 1, 1])
    ys = np.arctan2(-R[:, 2, 0], sy)
    zs = np.zeros_like(R[:, 1, 0])
    s = singular.astype(np.float64)
    deg = 180.0 / np.pi
    return ((x * (1 - s) + xs * s) * deg,
            (y * (1 - s) + ys * s) * deg,
            (z * (1 - s) + zs * s) * deg)


class SixDRepPoseBackend:
    """BlazeFace full-range 门控 → 取最高分脸 → 裁剪 → 6DRepNet ONNX 出 yaw。
    无脸 / 低置信 → has_face=False（走降级，不硬判侧脸）。模型缺失时 _ensure 抛错
    （shadow 模式会 catch；sixdrep 模式则硬失败，因为是显式 opt-in）。"""

    name = MODE_SIXDREP

    def __init__(self, *, detect_model: str | None = None, onnx_path: str | None = None,
                 score_thr: float = _DETECT_SCORE_THR, crop_margin: float = _CROP_MARGIN):
        self._detect_model = detect_model or os.environ.get(ENV_FACEDETECT_MODEL)
        self._onnx_path = onnx_path or os.environ.get(ENV_SIXDREP_ONNX)
        self._score_thr = score_thr
        self._crop_margin = crop_margin
        self._detector = None
        self._session = None
        self._in_name = None

    def _ensure(self):
        if self._detector is None:
            if not self._detect_model or not os.path.isfile(self._detect_model):
                raise RuntimeError(
                    f"BlazeFace 模型不可用: {self._detect_model!r}（设 {ENV_FACEDETECT_MODEL}）")
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision

            base = mp_python.BaseOptions(model_asset_path=self._detect_model)
            opts = vision.FaceDetectorOptions(base_options=base, min_detection_confidence=0.3)
            self._detector = vision.FaceDetector.create_from_options(opts)
        if self._session is None:
            if not self._onnx_path or not os.path.isfile(self._onnx_path):
                raise RuntimeError(
                    f"6DRepNet onnx 不可用: {self._onnx_path!r}（设 {ENV_SIXDREP_ONNX}）")
            import onnxruntime as ort

            so = ort.SessionOptions()
            # 坑1：默认 ORT_ENABLE_ALL 把 RepVGG 分组卷积优化坏 → 静默全错。必须 BASIC。
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
            self._session = ort.InferenceSession(self._onnx_path, so,
                                                 providers=["CPUExecutionProvider"])
            self._in_name = self._session.get_inputs()[0].name

    def estimate(self, image_bgr) -> PoseResult:
        import cv2
        import mediapipe as mp
        import numpy as np

        self._ensure()
        h, w = image_bgr.shape[:2]
        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        res = self._detector.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
        dets = res.detections or []
        if not dets:
            return PoseResult(has_face=False, yaw=None, certain=False, source=self.name)
        best = max(dets, key=lambda d: (d.categories[0].score if d.categories else 0.0))
        score = float(best.categories[0].score) if best.categories else 0.0
        if score < self._score_thr:
            # 低置信（旋转宏观 / 非脸）→ 区分不出就降级
            return PoseResult(has_face=False, yaw=None, certain=False,
                              source=self.name, score=score)
        crop = self._crop(rgb, best.bounding_box, w, h)
        if crop is None or crop.size == 0:
            return PoseResult(has_face=False, yaw=None, certain=False,
                              source=self.name, score=score)
        pre = self._preprocess(crop)
        R = np.asarray(self._session.run(None, {self._in_name: pre})[0])
        pitch, yaw, roll = _euler_from_rotation_matrix(R)
        return PoseResult(has_face=True, yaw=abs(float(yaw[0])),
                          pitch=float(pitch[0]), roll=float(roll[0]),
                          certain=True, source=self.name, score=score)

    def _crop(self, rgb, bbox, w, h):
        """检测器 bbox + margin 裁剪（RGB）。坐标越界 clamp。"""
        bw, bh = int(bbox.width), int(bbox.height)
        mx, my = int(bw * self._crop_margin), int(bh * self._crop_margin)
        x0 = max(0, int(bbox.origin_x) - mx)
        y0 = max(0, int(bbox.origin_y) - my)
        x1 = min(w, int(bbox.origin_x) + bw + mx)
        y1 = min(h, int(bbox.origin_y) + bh + my)
        if x1 <= x0 or y1 <= y0:
            return None
        return rgb[y0:y1, x0:x1]

    @staticmethod
    def _preprocess(rgb_crop):
        """torchvision Resize(224, BILINEAR)+CenterCrop(224)+ToTensor+Normalize，PIL bit-match。"""
        import numpy as np
        from PIL import Image

        mean = np.array(_MEAN, dtype=np.float32)
        std = np.array(_STD, dtype=np.float32)
        img = Image.fromarray(np.ascontiguousarray(rgb_crop).astype(np.uint8))
        iw, ih = img.size
        if iw < ih:
            nw, nh = 224, int(round(224 * ih / iw))
        else:
            nw, nh = int(round(224 * iw / ih)), 224
        img = img.resize((nw, nh), Image.BILINEAR)
        left, top = (nw - 224) // 2, (nh - 224) // 2
        img = img.crop((left, top, left + 224, top + 224))
        arr = np.asarray(img).astype(np.float32) / 255.0
        arr = (arr - mean) / std
        arr = np.transpose(arr, (2, 0, 1))[None, :]
        return arr.astype(np.float32)


# ----------------------- 会话：按 flag 路由 + 影子模式 -----------------------

def _result_brief(r: PoseResult | None) -> dict | None:
    if r is None:
        return None
    return {"has_face": r.has_face,
            "yaw": None if r.yaw is None else round(r.yaw, 2),
            "certain": r.certain, "source": r.source,
            "score": None if r.score is None else round(r.score, 3)}


def _emit_shadow(fm: PoseResult, sd: PoseResult | None) -> None:
    """影子日志：两 backend 读数 + 桶级分歧（侧脸纠偏信号）。"""
    rec: dict = {"facemesh": _result_brief(fm), "sixdrep": _result_brief(sd)}
    if fm is not None and sd is not None:
        from backend.services.case_material_coverage import classify_angle
        fmv, _ = classify_angle(fm.yaw, fm.has_face)
        sdv, _ = classify_angle(sd.yaw, sd.has_face)
        rec["fm_view"], rec["sd_view"], rec["view_diff"] = fmv, sdv, (fmv != sdv)
    _shadow_logger.info(json.dumps(rec, ensure_ascii=False))


class PoseSession:
    """按 `CASE_WORKBENCH_POSE_BACKEND` 选 backend，缓存 detector，封装影子模式。
    classify_views 对一个 case 目录建一个 session，循环 estimate。"""

    def __init__(self, model_path: str, *, sixdrep_kwargs: dict | None = None):
        self.mode = pose_backend_mode()
        self._model_path = model_path
        self._sixdrep_kwargs = sixdrep_kwargs or {}
        self._fm: FaceMeshPoseBackend | None = None
        self._sd: SixDRepPoseBackend | None = None

    def _facemesh(self) -> FaceMeshPoseBackend:
        if self._fm is None:
            self._fm = FaceMeshPoseBackend(self._model_path)
        return self._fm

    def _sixdrep(self) -> SixDRepPoseBackend:
        if self._sd is None:
            self._sd = SixDRepPoseBackend(**self._sixdrep_kwargs)
        return self._sd

    def estimate(self, image_bgr) -> PoseResult:
        if self.mode == MODE_SIXDREP:
            return self._sixdrep().estimate(image_bgr)
        if self.mode == MODE_SHADOW:
            fm = self._facemesh().estimate(image_bgr)
            sd = None
            try:
                sd = self._sixdrep().estimate(image_bgr)
            except Exception as e:  # noqa: BLE001 — 6D 缺依赖/模型 → 影子臂降级，绝不影响 facemesh 生产值
                _shadow_logger.warning("sixdrep shadow arm failed: %s", str(e)[:200])
            _emit_shadow(fm, sd)
            return fm  # 影子模式：生产路径恒用 facemesh 值（字节不变）
        # facemesh（默认）或未知 flag → facemesh
        return self._facemesh().estimate(image_bgr)


__all__ = [
    "PoseResult", "PoseSession", "FaceMeshPoseBackend", "SixDRepPoseBackend",
    "pose_backend_mode", "ENV_BACKEND", "ENV_FACEDETECT_MODEL", "ENV_SIXDREP_ONNX",
    "MODE_FACEMESH", "MODE_SIXDREP", "MODE_SHADOW",
]
