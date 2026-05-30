"""成品 board 后处理标注器 —— 内部 QA 版（owner 2026-05-30 钦定）.

成品 final-board.jpg 给客户保持干净；本模块在其副本上叠治疗区标注 → final-board.annotated.jpg
供 operator / 术式核对。**不动 case-layout-board skill**：直接对成品 board 重跑 facemesh 定位。

board 布局 = tri-compare 两列：术前(左) | 术后(右)，每行一个角度(正面/45°/侧面)。
- 多脸 facemesh → 按中线 x 分术前(左半)/术后(右半)，按 yaw 分角度。
- 只标术前脸（术后是对照，不标）；斜位只标近侧（复用 render_panel near_side_only）。
- region 来自术式文本（extract_regions），术后/客户成品零改动。
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np

from backend.services import case_material_coverage as cov
from backend.services import facial_region_atlas as atlas
from backend.services import treatment_zone_panel as tzp

# 生产须 vendor 模型并设此 env；缺省指开发期 asset（ephemeral /tmp，world-writable）。
DEFAULT_MODEL_ENV = "CASE_WORKBENCH_FACEMESH_MODEL"
_DEV_MODEL_FALLBACK = "/tmp/focal-p4-asset/face_landmarker.task"
# /tmp dev asset 是 world-writable（可被本地投毒）→ 仅显式 opt-in 才纳入回落链。
ALLOW_DEV_MODEL_ENV = "CASE_WORKBENCH_ALLOW_DEV_MODEL"


def multiface_landmarks(image_bgr: np.ndarray, model_path: str,
                        *, max_faces: int = 6) -> list[tuple[np.ndarray, float | None]]:
    """成品 board 多脸 FaceLandmarker → [(pts(478,2), yaw_deg)]。lazy import mediapipe。

    pose-upgrade Phase 2 决策：**本点（yaw 计算点 #2）显式不纳入 pose_backend 抽象，维持 FaceMesh**。
    理由：tzp.render_panel 渲染需要 478 个 landmark，6DRepNet 只出姿态不出 landmark，无法替代；
    且这里的 yaw 仅用于 annotate_board 的 near_side_only 装饰判定（斜位只标近侧），6D 边际价值低。
    若日后"入库分类(classify_views 走 6D) vs board 标注(走 FaceMesh)"对同一物理角度产生口径漂移
    需消除，再在独立 phase 把这里的 yaw 也接 backend（landmark 仍走 FaceMesh）。
    """
    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    base = mp_python.BaseOptions(model_asset_path=model_path)
    opts = vision.FaceLandmarkerOptions(base_options=base, num_faces=max_faces,
                                        output_facial_transformation_matrixes=True)
    det = vision.FaceLandmarker.create_from_options(opts)
    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    res = det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
    if not res.face_landmarks:
        return []
    h, w = image_bgr.shape[:2]
    out: list[tuple[np.ndarray, float | None]] = []
    mats = res.facial_transformation_matrixes or []
    for i, lms in enumerate(res.face_landmarks):
        pts = np.array([[p.x * w, p.y * h] for p in lms], dtype=np.float32)
        yaw = None
        if i < len(mats):
            R = np.array(mats[i])[:3, :3]
            yaw = abs(math.degrees(math.atan2(-R[2, 0], math.hypot(R[0, 0], R[1, 0]))))
        out.append((pts, yaw))
    return out


def _is_before(pts: np.ndarray, board_w: int) -> bool:
    """术前=左列：脸中心 x 在 board 左半。"""
    return float(pts[:, 0].mean()) < board_w / 2.0


def annotate_board(board_bgr: np.ndarray, focus_text: str, model_path: str,
                   *, max_faces: int = 6) -> tuple[np.ndarray, list[dict]]:
    """成品 board → 在术前脸叠治疗区标注。返回 (annotated BGR, 标注明细)。

    无 region(术式抽不出已知部位) 或无脸 → 原图返回 + 空明细。
    """
    detail: list[dict] = []
    regions = atlas.extract_regions(focus_text)
    if not regions:   # 术式抽不出已知部位 → 不跑 facemesh，原图返回
        return board_bgr, detail
    faces = multiface_landmarks(board_bgr, model_path, max_faces=max_faces)
    board_w = board_bgr.shape[1]
    befores = [(pts, yaw) for pts, yaw in faces if _is_before(pts, board_w)]
    if not befores:
        return board_bgr, detail

    out = board_bgr
    for pts, yaw in befores:
        view, _certain = cov.classify_angle(yaw, True)
        # board 已对齐裁切 → crop_to_face=False；斜位只标近侧
        out = tzp.render_panel(out, pts, regions, substrate_bgr=out,
                               crop_to_face=False,
                               near_side_only=(view == atlas.VIEW_OBLIQUE))
        detail.append({"view": view, "yaw": None if yaw is None else round(yaw, 1),
                       "regions": list(regions)})
    return out, detail


def resolve_model_path(explicit: str | None = None) -> str | None:
    """模型路径：显式 > env CASE_WORKBENCH_FACEMESH_MODEL > 开发 fallback（仅 opt-in）；不存在返回 None。

    /tmp dev asset 是 world-writable，生产不该静默回落到可被本地投毒的路径 →
    仅当显式 CASE_WORKBENCH_ALLOW_DEV_MODEL=1（本地 dev）才把它纳入候选链。
    """
    cands: list[str | None] = [explicit, os.environ.get(DEFAULT_MODEL_ENV)]
    if os.environ.get(ALLOW_DEV_MODEL_ENV, "").strip().lower() in ("1", "true", "yes"):
        cands.append(_DEV_MODEL_FALLBACK)
    for cand in cands:
        if cand and os.path.isfile(cand):
            return cand
    return None


def _focus_from_manifest(manifest: dict, out_root: Path) -> str:
    """术式文本：focus_targets 优先，否则 case_dir 目录名（含整段术式描述）。"""
    targets = manifest.get("focus_targets") or []
    if targets:
        return " ".join(str(t) for t in targets)
    case_dir = manifest.get("case_dir") or ""
    return os.path.basename(case_dir.rstrip("/")) if case_dir else ""


def _atomic_imwrite(out_path: Path, image: np.ndarray) -> bool:
    """imencode → temp → os.replace 原子落地，避免 max_workers>1 下同 out_root 并发交叠写半截 JPEG。

    格式由 ".jpg" 参数定（非 tmp 文件名扩展名）。返回是否成功
    （False = 编码失败 / 磁盘满 / 权限，调用方应转 status=error）。
    """
    import cv2

    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        return False
    tmp = out_path.with_name(out_path.name + f".tmp.{os.getpid()}")
    try:
        tmp.write_bytes(buf.tobytes())
        os.replace(str(tmp), str(out_path))
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def annotate_render_output(out_root: str | Path, *, model_path: str | None = None,
                           board_name: str = "final-board.jpg") -> dict:
    """读 render 产出目录(final-board.jpg + manifest.final.json) → 写 final-board.annotated.jpg。

    内部 QA 件：成品 board 不动，另存带标注副本。返回 {status, annotated_path?, detail?, reason?}。
    任何缺失/失败都不抛（含 cv2 缺失/检测异常/写盘失败），用 status 表达——直接调用方
    （CLI / focal_p4 工具）无需自带 try/except，渲染流程不会被打断。
    """
    out_root = Path(out_root)
    board_path = out_root / board_name
    manifest_path = out_root / "manifest.final.json"
    if not board_path.is_file():
        return {"status": "skipped", "reason": "no final-board.jpg"}
    model = resolve_model_path(model_path)
    if not model:
        return {"status": "skipped", "reason": "facemesh model unavailable"}
    try:
        import cv2

        focus = ""
        if manifest_path.is_file():
            try:
                focus = _focus_from_manifest(json.loads(manifest_path.read_text("utf-8")), out_root)
            except (json.JSONDecodeError, OSError):
                focus = ""
        if not focus:
            focus = os.path.basename(str(out_root))
        board = cv2.imread(str(board_path))
        if board is None:
            return {"status": "skipped", "reason": "board unreadable"}
        annotated, detail = annotate_board(board, focus, model)
        if not detail:
            return {"status": "no-annotation", "reason": "no known region / no before-face",
                    "focus": focus}
        out_path = out_root / "final-board.annotated.jpg"
        if not _atomic_imwrite(out_path, annotated):
            return {"status": "error", "reason": "imwrite failed (disk/permission?)", "focus": focus}
        return {"status": "ok", "annotated_path": str(out_path), "detail": detail, "focus": focus}
    except Exception as e:  # noqa: BLE001 — 内部 QA 件：任何失败转 status，绝不抛断渲染
        return {"status": "error", "reason": str(e)[:200]}


__all__ = ["multiface_landmarks", "annotate_board", "annotate_render_output",
           "resolve_model_path"]
