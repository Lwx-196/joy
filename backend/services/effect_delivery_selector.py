"""Effect-projection delivery lane — case selection + evidence-anchored effect_pairs.

anchored-sim Phase 4 生产 selector。两层职责，明确分开：

1. ``resolve_effect_pairs`` — 把品牌标注的 case 文件夹名映射成 evidence-anchored
   ``(project, region)`` pairs（反臆造 fail-closed：只保留有 registered effect_row
   的部位，无循证依据的 drop）。**从 calibration harness 物理迁移而来**，builder 反向
   import 它（避免两份漂移）。calibration 用它测全部效果类型（含鼻/颏侧脸），**不套
   下面的 scope gate**。

2. ``scope_gate`` / ``select_effect_eligible`` — 上线 scope 过滤，**仅 effect 投影交付
   lane 用**。只放行 owner greenlight 的正脸清晰类型（泪沟/苹果肌/唇/法令纹/卧蚕）；
   侧脸主导（鼻/颏）、纯动态纹（川字/额/鱼尾）、其它已注册但未greenlight（太阳穴/咬肌）
   全部 skip + 记明确原因（fail-closed 透明，不静默丢）。
"""
from __future__ import annotations

from typing import Any

from backend.services import procedure_region_mappings as prm

# 上线 scope（owner 2026-06-02 拍板）：正脸清晰类型。侧脸主导（鼻背/鼻基底/下巴）+
# 纯动态纹（川字/额纹/鱼尾纹）+ 其它已注册但未greenlight（太阳穴/咬肌）暂不上线。
LAUNCH_SCOPE_REGIONS: frozenset[str] = frozenset({"泪沟", "苹果肌", "唇", "法令纹", "卧蚕"})


def resolve_effect_pairs(
    case_name: str,
) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    """case 文件夹名 → evidence-anchored ``(project, region)`` pairs（反臆造 fail-closed）。

    ``parse_procedures`` 把品牌标注的文件夹名映射成结构化术式；只保留有 registered
    ``effect_row`` 的 ``(project, region)`` pair——无循证依据的部位 drop（绝不编造效果）。
    返回 ``(pairs, parsed)``，``parsed`` 含 ``needs_human_review`` / ``unknown_segments``
    供调用方透明上报。
    """
    parsed = prm.parse_procedures(case_name)
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for proc in parsed.get("procedures", []):
        project = str(proc.get("project") or "").strip()
        if not project:
            continue
        for region in proc.get("regions", []):
            key = (project, str(region))
            if key in seen:
                continue
            if prm.effect_row(project, str(region)) is not None:
                pairs.append(key)
                seen.add(key)
    return pairs, parsed


def scope_skip_reason(region: str) -> str | None:
    """None = 在上线 scope 内（eligible）；否则返回明确 skip 原因。"""
    if region in LAUNCH_SCOPE_REGIONS:
        return None
    view = prm.REGION_EFFECT_VIEW.get(region)
    if view == "profile":
        return f"profile_only:{region}（侧脸主战场，正脸价值有限，上线不投）"
    if view == "expression":
        return f"expression_only:{region}（肉毒动态纹，静态正脸≈无变化，上线不投）"
    return f"out_of_launch_scope:{region}（未在 owner greenlight 的正脸清晰类型）"


def scope_gate(
    pairs: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[str]]:
    """把 effect_pairs 按上线 scope 过滤；返回 ``(in_scope_pairs, skip_reasons)``。"""
    in_scope: list[tuple[str, str]] = []
    skipped: list[str] = []
    for project, region in pairs:
        reason = scope_skip_reason(region)
        if reason is None:
            in_scope.append((project, region))
        else:
            skipped.append(reason)
    return in_scope, skipped


# 源图质量门（owner 2026-06-02 retry）：effect 投影要术前「单张干净照」做 baseline。
# 案例库 discover 可能把「术前｜术后」双拼板/多拼板当 baseline（如康巧佳唇案）→ AI 是在
# 编辑成品板 = garbage-in，judge 还可能 flip-flop 误 pass。真信号 = 人脸计数：干净单图恰
# 1 张脸，双拼板/多人源 ≥2 张脸（aspect-ratio 分不开——唇板 1.54 ≈ 干净 landscape 单图 1.50）。
SOURCE_MULTIFACE_REASON = "source_quality_suspect:multi_face_board(术前｜术后双拼板/多人源非干净单图)"
_FACE_MODEL_ENV = "CASE_WORKBENCH_FACE_LANDMARKER_TASK"
_FACE_MODEL_DEFAULT = "~/.cache/feishu-claude/mediapipe/face_landmarker.task"


def count_baseline_faces(baseline_path: Any) -> int | None:
    """术前图人脸数（mediapipe FaceLandmarker）。CV 栈不可用 / 模型缺 / 解码错 → 返回
    ``None``（fail-open）。**懒加载** mediapipe，保模块 import（及 CI collection）不依赖重 CV
    栈——与 ``precise_face_mask`` / ``test_board_annotator`` importorskip 约定一致。
    """
    import os
    from pathlib import Path

    try:
        import mediapipe as mp
        import numpy as np
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision
        from PIL import Image, ImageOps
    except Exception:  # noqa: BLE001 - CI venv 故意无 mediapipe → fail-open
        return None

    model = Path(os.path.expanduser(os.environ.get(_FACE_MODEL_ENV, _FACE_MODEL_DEFAULT)))
    if not model.is_file():
        return None
    try:
        options = vision.FaceLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(model)),
            num_faces=5,
            min_face_detection_confidence=0.4,
            min_face_presence_confidence=0.4,
        )
        with vision.FaceLandmarker.create_from_options(options) as landmarker:
            pil = ImageOps.exif_transpose(Image.open(baseline_path)).convert("RGB")
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.asarray(pil))
            return len(landmarker.detect(mp_image).face_landmarks)
    except Exception:  # noqa: BLE001 - any detection error → fail-open (held backstop)
        return None


def source_quality_suspect(baseline_path: Any) -> str | None:
    """干净术前单图恰 1 张脸；「术前｜术后」双拼板 / 多人源 ≥2 张脸 → 非有效投影源
    （编辑成品板 = garbage-in）。可疑返回 held 原因，否则 ``None``。**Fail-OPEN**：人脸数
    不可测（``None``）时返回 ``None`` 放行——强制人工 held 队列是真兜底，测不出的源不该
    静默拦掉可能干净的 case。0 脸（纯色/检测不到）也放行（不是「板」信号）。
    """
    faces = count_baseline_faces(baseline_path)
    if faces is not None and faces >= 2:
        return SOURCE_MULTIFACE_REASON
    return None


def select_effect_eligible(case_names: list[str]) -> list[dict[str, Any]]:
    """对一组 case 名做 effect-eligibility 判定（lane discover 用）。

    每 case 返回 dict：``case_name`` / ``effect_pairs``（in-scope）/ ``eligible``（bool）/
    ``skip_reasons``（fail-closed 透明，不静默）/ ``parsed``。``eligible`` 仅当有 in-scope
    pair 才 True。
    """
    results: list[dict[str, Any]] = []
    for name in case_names:
        pairs, parsed = resolve_effect_pairs(name)
        in_scope, scope_skips = scope_gate(pairs)
        reasons: list[str] = list(scope_skips)
        if not pairs:
            reasons.append(
                "no_evidence_anchored_pairs "
                f"(needs_human_review={parsed.get('needs_human_review')})"
            )
        results.append(
            {
                "case_name": name,
                "effect_pairs": in_scope,
                "eligible": bool(in_scope),
                "skip_reasons": reasons,
                "parsed": parsed,
            }
        )
    return results
