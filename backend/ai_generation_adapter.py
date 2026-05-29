"""AI image generation adapters for auditable case simulations."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import hashlib
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import parse, request

from . import stress
from .services.promotion_manifest_loader import load_manifest, should_promote

LOGGER = logging.getLogger(__name__)

SIMULATION_ROOT = stress.simulation_root(
    Path(__file__).resolve().parent.parent / "case-workbench-ai" / "simulation_jobs"
)
PS_ENHANCE_SCRIPT = Path(
    os.environ.get(
        "CASE_WORKBENCH_PS_ENHANCE_SCRIPT",
        "/Users/a1234/Desktop/飞书Claude/skills/case-layout-board/scripts/case_layout_enhance.js",
    )
)
DEFAULT_PROVIDER = "ps_model_router"
DEFAULT_QUALITY = os.environ.get("CASE_WORKBENCH_AI_QUALITY", "4k")
DEFAULT_TIMEOUT_SEC = int(os.environ.get("CASE_WORKBENCH_AI_TIMEOUT_SEC", "240"))
PS_AUTOMATION_ENV = Path(
    os.environ.get(
        "CASE_WORKBENCH_PS_ENV_FILE",
        "/Users/a1234/Desktop/飞书Claude/claude-feishu-bridge/.env",
    )
)
DEFAULT_PRIMARY_IMAGE_MODEL = "image-2"
DEFAULT_FALLBACK_IMAGE_MODEL = "gemini-3-pro-image-preview-4k"
KNOWN_TUZI_IMAGE_MODELS = [
    "gemini-3-pro-image-preview-4k",
    "gemini-3-pro-image-preview-2k-vip",
    "gemini-3-pro-image-preview-vip",
    "nano-banana",
    "gpt-image-2",
    "gpt-image-2-vip",
]
COMFYUI_PROVIDER = "comfyui_local"
COMFYUI_BASE_URL = os.environ.get(
    "CASE_WORKBENCH_COMFYUI_BASE_URL",
    os.environ.get("COMFYUI_BASE_URL", "http://127.0.0.1:8188"),
).rstrip("/")
COMFYUI_WORKFLOW_DIR = Path(
    os.environ.get(
        "CASE_WORKBENCH_COMFYUI_WORKFLOW_DIR",
        str(Path(__file__).resolve().parent.parent / "comfyui-workflows"),
    )
)
COMFYUI_ALLOWED_CANDIDATE_WORKFLOWS = {
    "local_region_enhance_v1@conservative",
    "local_region_enhance_v2@conservative",
    "local_region_enhance_v3@conservative",
}
COMFYUI_DEFAULT_CANDIDATE_WORKFLOW = "local_region_enhance_v1@conservative"

# C0.5.4 (Data Contract Freeze) — GA-approved workflow scope.
# Locked to ``portrait_focal_enhance_v1`` to match the W11 production focal
# runtime. Sub-plan examples that referenced ``local_region_enhance_v1`` are
# obsolete; the gate's ``approved_workflows`` check uses this constant as
# the canonical workflow name. See ``docs/contracts/ga-workflow-scope.md``.
GA_APPROVED_WORKFLOW: str = "portrait_focal_enhance_v1"
COMFYUI_MAX_CONCURRENCY = int(os.environ.get("CASE_WORKBENCH_COMFYUI_MAX_CONCURRENCY", "1"))
COMFYUI_MIN_FREE_MEMORY_MB = int(os.environ.get("CASE_WORKBENCH_COMFYUI_MIN_FREE_MEMORY_MB", "1024"))
COMFYUI_TIMEOUT_SEC = int(os.environ.get("CASE_WORKBENCH_COMFYUI_TIMEOUT_SEC", "300"))
_COMFYUI_GATE = threading.BoundedSemaphore(COMFYUI_MAX_CONCURRENCY)
_DEFAULT_COMFYUI_AB_VALIDATION_REPORT_PATH = (
    Path(__file__).resolve().parent.parent / "case-workbench-ai" / "ab_runs" / "t47_comfyui_ab_report.json"
)
COMFYUI_AB_VALIDATION_REPORT_PATH = Path(
    os.environ.get(
        "CASE_WORKBENCH_COMFYUI_AB_REPORT_PATH",
        str(_DEFAULT_COMFYUI_AB_VALIDATION_REPORT_PATH),
    )
)
COMFYUI_VLM_GUARDRAIL_REPORT_PATH = Path(
    os.environ.get(
        "CASE_WORKBENCH_COMFYUI_VLM_GUARDRAIL_REPORT_PATH",
        str(Path(__file__).resolve().parent.parent / "case-workbench-ai" / "ab_runs" / "vlm_guardrail_report.json"),
    )
)
COMFYUI_PRODUCTION_GATE_REPORT_PATH = Path(
    os.environ.get(
        "CASE_WORKBENCH_COMFYUI_PRODUCTION_GATE_REPORT_PATH",
        str(Path(__file__).resolve().parent.parent / "case-workbench-ai" / "ab_runs" / "comfyui_production_gate.json"),
    )
)
_COMFYUI_WORKFLOW_PARAMETERS: dict[str, dict[str, Any]] = {
    "conservative": {"steps": 6, "cfg": 1.5, "denoise": 0.05, "control_strength": 0.45, "control_end": 0.58},
    "balanced": {"steps": 12, "cfg": 2.8, "denoise": 0.14, "control_strength": 0.55, "control_end": 0.72},
    "strong": {"steps": 16, "cfg": 3.4, "denoise": 0.20, "control_strength": 0.65, "control_end": 0.78},
    # 2026-05-28 Step 2 of 4-mode plan: FOCAL strength for M3 portrait_focal_enhance_v1.
    # Aggressive denoise inside the SAM-refined focus mask only; preservation
    # outside is enforced by InpaintModelConditioning + negative prompt.
    "focal": {"steps": 20, "cfg": 4.0, "denoise": 0.40, "control_strength": 0.55, "control_end": 0.72},
}
_COMFYUI_TONE_DETAIL_POSTPROCESS: dict[str, Any] = {
    "enabled": True,
    "strategy": "clinical_candidate_fidelity_guard_v11",
    "candidate_only": True,
    "mask_mode": "focus_mask_feathered",
    "midtone_lift": 1.01,
    "highlight_lift": 1.0,
    "local_contrast": 1.0,
    "detail_sharpness": 1.0,
    "shadow_lift": 1.06,
    "shadow_detail_contrast": 1.0,
    "shadow_threshold": 128,
    "global_luma_lift": 1.005,
    "global_max_delta": 3,
    "max_delta": 8,
    "preserve_chroma": True,
    "reference_chroma_match_strength": 1.0,
    "max_chroma_shift_delta": 4,
    "reference_luma_floor_delta": -4,
    "reference_luma_floor_max_lift": 32,
    "max_shadow_contrast_delta": 8,
    "shadow_floor_lift_max": 36,
    "max_highlight_p95_delta": 3,
    "max_highlight_p99_delta": 4,
    "highlight_guard_max_darken": 128,
    "specular_threshold": 228,
    "max_specular_ratio_delta": 0.006,
    "reference_blend_strength": 0.55,
    "face_tone_guard_enabled": True,
    "face_luma_target_delta": 5,
    "face_luma_max_lift": 14,
    "face_background_contrast_target_delta": 4,
    "face_contrast_max_lift": 6,
    "face_tone_highlight_protect_threshold": 190,
    "semantic_fidelity_guard_enabled": True,
    "background_preserve_blend_strength": 1.0,
    "feature_protect_blend_strength": 1.0,
    "feature_protect_min_delta": 4.0,
    "feature_protect_min_excess_delta": 0.5,
    "face_chroma_guard_enabled": True,
    "face_chroma_max_delta": 2.5,
    "face_chroma_blend_strength": 0.9,
    "face_texture_guard_enabled": True,
    "face_texture_min_loss": 0.4,
    "face_texture_blend_strength": 0.9,
    "face_identity_guard_enabled": True,
    "face_identity_min_delta": 1.5,
    "face_identity_blend_strength": 0.95,
    "local_chroma_guard_enabled": True,
    "local_chroma_max_delta": 2.5,
    "local_chroma_blend_strength": 0.95,
    "local_chroma_tile_rows": 4,
    "local_chroma_tile_cols": 4,
    "local_texture_guard_enabled": True,
    "local_texture_min_loss": 0.4,
    "local_texture_blend_strength": 0.95,
    "local_texture_tile_rows": 4,
    "local_texture_tile_cols": 4,
    "lip_feature_guard_enabled": True,
    "lip_feature_max_delta": 1.5,
    "lip_feature_blend_strength": 1.0,
    "edge_halo_guard_enabled": True,
    "edge_halo_min_delta": 2.0,
    "edge_halo_blend_strength": 1.0,
}
_COMFYUI_WORKFLOW_GROUPS = {
    "background_cleanup_v1": ("comfyui_background_cleanup", "ComfyUI background cleanup"),
    "local_region_enhance_v1": ("comfyui_local_region", "ComfyUI local region"),
    "local_region_enhance_v2": ("comfyui_local_region", "ComfyUI local region"),
    "local_region_enhance_v3": ("comfyui_local_region", "ComfyUI local patch"),
    "portrait_focal_enhance_v1": ("comfyui_focal_enhance", "ComfyUI focal enhance (M3)"),
    "portrait_front_compare_v1": ("comfyui_pose_alignment", "ComfyUI pose alignment"),
    "portrait_45_compare_v1": ("comfyui_pose_alignment", "ComfyUI pose alignment"),
    "portrait_side_compare_v1": ("comfyui_pose_alignment", "ComfyUI pose alignment"),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def simulation_job_dir(job_id: int) -> Path:
    return SIMULATION_ROOT / str(job_id)


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    except OSError:
        return values
    return values


def _ps_env_value(name: str, fallback: str = "") -> str:
    if os.environ.get(name):
        return str(os.environ[name]).strip()
    return _read_env_file(PS_AUTOMATION_ENV).get(name, fallback).strip()


def _split_model_list(raw: str, fallback: str) -> list[str]:
    items = [
        item.strip()
        for item in str(raw or fallback).replace("\n", ",").split(",")
        if item.strip()
    ]
    return list(dict.fromkeys(items))


def get_ps_image_model_options() -> dict[str, Any]:
    """Expose PS automation image models without leaking API keys.

    Mirrors `scripts/providers/tuzi-image.js`: primary models come from
    TUZI_IMAGE_PRIMARY_MODELS, then the fallback model is tried by the router.
    Known Gemini quality variants are also shown so the UI can explicitly
    choose them when needed.
    """
    primary = _split_model_list(
        _ps_env_value("TUZI_IMAGE_PRIMARY_MODELS", DEFAULT_PRIMARY_IMAGE_MODEL),
        DEFAULT_PRIMARY_IMAGE_MODEL,
    )
    fallback = _ps_env_value("TUZI_IMAGE_FALLBACK_MODEL", DEFAULT_FALLBACK_IMAGE_MODEL) or DEFAULT_FALLBACK_IMAGE_MODEL
    ordered = list(dict.fromkeys([*primary, fallback, *KNOWN_TUZI_IMAGE_MODELS]))
    options: list[dict[str, Any]] = []
    for model in ordered:
        if model in primary:
            source = "primary"
            group = "ps_router"
            group_label = "PS model router"
            desc = "PS 自动化主模型；优先走 TUZI_IMAGE_PRIMARY_* 配置"
        elif model == fallback:
            source = "fallback"
            group = "ps_router"
            group_label = "PS model router"
            desc = "PS 自动化 fallback；主模型失败时使用"
        else:
            source = "tuzi_builtin"
            group = "ps_router"
            group_label = "PS model router"
            desc = "兔子图像模型质量档位；用于手动指定本次增强"
        options.append(
            {
                "value": model,
                "label": model,
                "source": source,
                "group": group,
                "group_label": group_label,
                "description": desc,
                "is_default": model == primary[0],
            }
        )
    for profile_name in [
        "local_region_enhance_v1@conservative",
        "local_region_enhance_v2@conservative",
        "local_region_enhance_v3@conservative",
        "local_region_enhance_v1@balanced",
        "local_region_enhance_v1@strong",
        "background_cleanup_v1@conservative",
        "portrait_front_compare_v1@conservative",
        "portrait_45_compare_v1@conservative",
        "portrait_side_compare_v1@conservative",
        COMFYUI_PROVIDER,
    ]:
        if profile_name == COMFYUI_PROVIDER:
            workflow_name = "local_region_enhance_v1"
            label = "comfyui_local"
        else:
            workflow_name = _resolve_comfyui_workflow_name(profile_name)
            label = profile_name
        group, group_label = _COMFYUI_WORKFLOW_GROUPS.get(
            workflow_name,
            ("comfyui_candidate", "ComfyUI candidate"),
        )
        options.append(
            {
                "value": profile_name,
                "label": label,
                "source": "comfyui_candidate",
                "group": group,
                "group_label": group_label,
                "description": "ComfyUI candidate；只能走受控复测，不进入默认正式生产。",
                "is_default": False,
                "candidate_only": True,
                "promotion_note": "T90 gate 未通过，不能 promote default。",
            }
        )
    return {
        "provider": DEFAULT_PROVIDER,
        "default_model": primary[0] if primary else None,
        "fallback_model": fallback,
        "options": options,
    }


# 某些模型本身风格更激进，需要在 prompt 末尾追加幅度回拉提示。
# 值是百分比（10 = 减弱 10%）。当 focus_targets 里写「60%」这类字面值时，
# 模型应理解为要在该字面值上再回拉对应百分比，避免过饱和。
_MODEL_STRENGTH_DAMPING: dict[str, int] = {
    "gpt-image-2-vip": 20,
    "image-2": 20,
}


_POSE_ALIGNMENT_CLAUSE = (
    "术前参考图的使用范围（关键，请严格遵守）：术前参考图（pose-ref / 第二张及之后辅助图中、"
    "标注为「术前」或「before」的那张）仅作为【角度/姿态/构图】参考使用。除此之外，术前图的所有像素细节"
    "（发丝位置和走向、皮肤光斑、毛孔分布、衣服褶皱位置、光影微噪点、首饰位置等）都不得直接复用到术后图，"
    "否则术后图会看起来像是在术前图上做 P 图，而不是另一次独立实拍。\n"
    "姿态对齐规范（必须执行）：把术后图的【人物大姿态、头部转角和俯仰、镜头视角与高度、构图裁切】对齐到术前参考图，"
    "即使原始术后图是另一个角度也要按术前角度重出，方便做术前/术后并排对比。\n"
    "差异化要求（必须执行，体现「另一次独立实拍」感）：术后图的下列项目都要与术前参考图自然不同，"
    "不要照搬术前的细节像素分布——\n"
    "  · 表情/神态：眼神、嘴角弧度、微表情、肌肉松紧（例：术前抿嘴/平视，术后可嘴角自然微抬/眼神更松弛或更聚焦）；\n"
    "  · 头发：单根发丝的具体走向、左右两侧的散落位置、刘海或鬓发的微卷/微翘、与脸颊接触的位置都要换一种自然分布"
    "（保持同一个发型/颜色/长度/分缝侧，但具体哪一根丝 in 什么位置必须不同，模拟不同快门瞬间）；\n"
    "  · 皮肤微细节：高光位置、毛孔/纹理的微噪点、可见瑕疵（雀斑/痘印/痣）的反光，要按一次新的拍摄重新生成，"
    "保留原本的真实分布但允许微位移；\n"
    "  · 衣服/首饰：领口/褶皱/纽扣阴影的微变化要自然不同，但款式、颜色、配饰本身保持一致。\n"
    "差异化幅度要克制——只动上述微观层级，不得破坏已经对齐好的姿态、角度和构图，也不得改变同一人身份、五官比例、骨相、服装、发饰和背景。"
)

# --- MD Persona & Clinical Keywords ---

MD_DIRECTOR_PROMPT = """
你是专业的人像局部精修导演。目标：在保留同一个人身份的前提下，基于医学解剖逻辑进行高保真调整。
质感要求：真实 iPhone 手机直出感，保留皮肤原始纹理、毛孔、痣及微小瑕疵。杜绝任何磨皮、影棚感、美白或 AI 滤镜感。
硬性要求：不换人、不换脸、不改年龄感、不改发型、不改衣服、不改背景；只有已选部位允许发生变化，其他未选区域保持稳定。
遮罩要求：绝对不能出现任何遮罩残留、红色/品红色边线或半透明蒙层。效果要自然、细、稳。
"""

MD_CONSISTENCY_LOCKS = [
    "PIXEL-PERFECT IDENTITY: Maintain 100% identical facial structure, bone structure, and feature placement.",
    "NO RE-SHAPING: Do not change the person's identity, face shape, eye shape, or nose bridge height.",
    "REAL SKIN: Keep all original skin texture, pores, and micro-imperfections; ZERO smoothing or blur.",
    "NATIVE LIGHTING: Use flat, clinical, or native smartphone lighting (iPhone 13/14/15) only; NO studio lighting.",
    "TRUE ANATOMY: The result must look like a real photo of the SAME PERSON after a procedure, not a simulated portrait.",
]

MD_ANATOMICAL_KEYWORDS = {
    "苹果肌": "饱满度调整、笑肌位置优化、面中支撑感提升",
    "法令纹": "鼻唇沟深浅优化、转折感自然、面中平整度提升",
    "面颊": "脸颊平整度、软组织过渡自然、纠正轻微凹陷",
    "鼻尖": "鼻头精致度提升、圆润感优化、微翘翘度调整",
    "鼻基底": "鼻翼根部支撑力、鼻唇衔接优化、缓解凹陷感",
    "鼻翼": "鼻翼收窄、鼻孔精致度、视觉对称性增强",
    "泪沟": "泪沟深浅优化、眼下过渡顺滑、消除疲态感",
    "卧蚕": "卧蚕存在感提升、边缘柔和度、眼下层次感",
    "眼袋": "下睑平整度提升、消除眼袋浮肿感",
    "下颌线": "下颌缘线条清晰度、轮廓转折利落、过渡平整",
    "颧骨": "缓解颧区外扩感、颧弓过渡衔接、侧面轮廓内收",
    "下巴": "下庭比例微调、尖圆度自然优化",
}

# --- Clinical-First Fidelity Enhancements ---
MD_CINEMATIC_ENHANCEMENTS = [
    "High-fidelity clinical restoration of the treated areas only.",
    "Preserve original camera lens characteristics (approx 26mm-35mm smartphone lens).",
    "Ensure zero-drift identity preservation even in shadow areas.",
    "Neutral, consistent clinical background with original room lighting tones.",
]


def _finalize_prompt(prompt_body: str, model_name: str | None = None) -> str:
    """Apply damping, pose alignment, and common clinical constraints."""
    damping_pct = 0
    if model_name and model_name in _MODEL_STRENGTH_DAMPING:
        damping_pct = _MODEL_STRENGTH_DAMPING[model_name]

    damping_clause = ""
    if damping_pct > 0:
        damping_clause = (
            f"\n模型强度调节（Damping）：检测到当前模型为 {model_name}，其默认表现较激进。"
            f"请在理解所有提示词目标的基础上，将最终输出的变化幅度【回拉 {damping_pct}%】。"
            "这意味着目标里要求的 30% 变化应实际呈现为约 24% 的视觉体感，以维持真实感。\n"
        )

    return (
        f"{prompt_body}\n\n"
        f"{damping_clause}\n"
        f"--- 姿态对齐与差异化规范 ---\n{_POSE_ALIGNMENT_CLAUSE}\n\n"
        "--- 通用约束 ---\n"
        "- 保持同一人身份 (Identity Preservation)\n"
        "- 严禁生成文字、水印、Logo\n"
        "- 严禁过度磨皮"
    )


def build_after_enhancement_prompt(
    focus_targets: list[str],
    focus_regions: list[dict[str, Any]],
    model_name: str | None = None,
    brand: str = "fumei",
) -> str:
    """Build a detailed prompt for post-simulation image enhancement."""
    is_md = (brand == "meiji_ai" or brand == "md_ai")
    persona = MD_DIRECTOR_PROMPT if is_md else ""

    focus_text = ", ".join(focus_targets) if focus_targets else "整体肤质与光影"
    region_text = json.dumps(focus_regions, ensure_ascii=False) if focus_regions else "未指定具体区域"

    # Inject anatomical keywords if MD brand
    if is_md:
        anatomical_hints = []
        for target in focus_targets:
            for key, hint in MD_ANATOMICAL_KEYWORDS.items():
                if key in target:
                    anatomical_hints.append(f"{key}: {hint}")
        if anatomical_hints:
            focus_text += f" (医学解剖提示：{'; '.join(anatomical_hints)})"

    region_policy = (
        "框内区域 (Focus Regions)：这是主要变动区，允许进行符合治疗目标的解剖学优化。\n"
        "框外区域：严禁改动。即使是肤色提亮也应尽量局限在框内或其自然过渡带，确保框外像素的高保真度。"
        if focus_regions else ""
    )

    if focus_regions:
        prompt_lines = [
            "任务：对第一张术后照片做医美案例展示用的轻量局部增强。",
            f"只允许处理这些目标部位 and 效果：{focus_text}。",
            f"用户在术后图上提供的辅助框选区域坐标如下，坐标为相对整张图片的 0-1 归一化比例：{region_text}。",
            "增强必须限制在框选区域内；框外仅允许自然羽化过渡，不得主动改动。",
            region_policy,
            "硬性约束：必须保持同一人身份、五官结构、脸型/身体主要轮廓、服装、发饰和背景关系（姿态/镜头视角/构图见后文姿态对齐规范）。",
            "非目标区域严禁做任何主动改动：不得调亮、不得统一肤色 or 色温、不得磨皮、不得锐化、不得改变毛孔/纹理/斑点、不得改变眼睛/鼻子/眉毛/额头/头发/服装/背景。",
            "目标区域也只能做轻量、局部、可审计的自然优化，不得改变真实治疗效果，不得新增文字、Logo、二维码或装饰元素。",
            "输出后会做差异热区 and 全脸变化评分；请优先降低框外变化。",
        ]
    elif focus_targets:
        prompt_lines = [
            "任务：对第一张术后照片做医美案例展示用的整张图片轻量级画质增强与局部精修。",
            f"处理目标：{focus_text}。",
            "硬性约束：必须保持同一人身份、五官结构、脸型/身体主要轮廓、服装、发饰和背景。不得过度磨皮，保留皮肤细节。",
            "非目标区域严禁做任何主动改动。输出图像必须看起来像原始拍摄的照片，而非AI生成的图片。",
        ]
    else:
        prompt_lines = [
            "任务：对提供的整张术后照片进行轻量级画质修复与保真度增强。",
            "处理目标：在保留原始五官和皮肤纹理的前提下，优化光影质感。",
            "硬性约束：不换人、不改脸、不改服装背景、不磨皮。保持 iPhone 原生实拍感。",
        ]

    # Add cinematic enhancements for MD brands if applicable
    if is_md:
        prompt_lines.append("\n医学美学增强建议（Cinematic Enhancements）：")
        for enh in MD_CINEMATIC_ENHANCEMENTS:
            prompt_lines.append(f"- {enh}")

    prompt_body = "\n".join(prompt_lines)
    final_prompt = _finalize_prompt(prompt_body, model_name)

    if is_md:
        locks = "\n".join([f"- {l}" for l in MD_CONSISTENCY_LOCKS])
        cinematic = "\n".join([f"- {c}" for c in MD_CINEMATIC_ENHANCEMENTS])
        final_prompt = (
            f"{persona}\n{final_prompt}\n\n"
            f"--- 临床保真锁 (Clinical Consistency Locks) ---\n{locks}\n\n"
            f"--- 电影感增强词 (Cinematic Enhancements) ---\n{cinematic}"
        )
    return final_prompt
def _parse_json_stdout(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise ValueError("PS model router returned empty stdout")
    try:
        return json.loads(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return str(left) == str(right)


def _resolve_ps_generated_path(raw: dict[str, Any], after_image_path: Path) -> tuple[Path, str]:
    """Pick a real generated image, never the original source fallback."""
    candidates = [
        ("imagePath", raw.get("imagePath")),
        ("generatedImagePath", raw.get("generatedImagePath")),
    ]
    saw_source_fallback = False
    missing: list[str] = []
    for source, value in candidates:
        if not value:
            continue
        path = Path(str(value))
        if not path.is_file():
            missing.append(f"{source}={path}")
            continue
        if _same_path(path, after_image_path):
            saw_source_fallback = True
            continue
        return path, source
    if saw_source_fallback:
        detail = "; ".join(missing)
        suffix = f"; missing generated candidate: {detail}" if detail else ""
        raise RuntimeError(f"PS model router returned original source image instead of generated output{suffix}")
    detail = "; ".join(missing) or "no imagePath/generatedImagePath"
    raise RuntimeError(f"PS model router did not return a local generated image: {detail}")


def _copy_reference(src: Path, output_dir: Path, stem: str) -> str:
    dest = output_dir / f"{stem}{src.suffix.lower() or '.jpg'}"
    shutil.copyfile(src, dest)
    return str(dest)


def _copy_with_watermark(src: Path, dest: Path) -> tuple[bool, str | None]:
    try:
        from PIL import Image, ImageDraw, ImageFont

        with Image.open(src) as image:
            base = image.convert("RGBA")
            overlay = Image.new("RGBA", base.size, (255, 255, 255, 0))
            draw = ImageDraw.Draw(overlay)
            text = "AI SIMULATION"
            font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), text, font=font)
            width = bbox[2] - bbox[0]
            height = bbox[3] - bbox[1]
            pad = max(8, int(min(base.size) * 0.015))
            x = max(pad, base.size[0] - width - pad * 2)
            y = max(pad, base.size[1] - height - pad * 2)
            draw.rectangle(
                (x - pad, y - pad // 2, x + width + pad, y + height + pad // 2),
                fill=(0, 0, 0, 96),
            )
            draw.text((x, y), text, fill=(255, 255, 255, 220), font=font)
            watermarked = Image.alpha_composite(base, overlay).convert("RGB")
            watermarked.save(dest)
        return True, None
    except Exception as exc:  # noqa: BLE001 - fallback preserves the generated artifact for review
        shutil.copyfile(src, dest)
        return False, str(exc)


def _resolve_comfyui_workflow_name(model_name: str | None) -> str:
    value = str(model_name or "").strip()
    workflow = value.split("@", 1)[0] if value else ""
    if workflow in _COMFYUI_WORKFLOW_GROUPS:
        return workflow
    return "portrait_front_compare_v1"


def _resolve_comfyui_workflow_profile(model_name: str | None) -> dict[str, Any]:
    value = str(model_name or COMFYUI_DEFAULT_CANDIDATE_WORKFLOW).strip()
    workflow_name = _resolve_comfyui_workflow_name(value)
    strength = value.split("@", 1)[1] if "@" in value else "conservative"
    if strength not in _COMFYUI_WORKFLOW_PARAMETERS:
        strength = "conservative"
    profile_name = f"{workflow_name}@{strength}"
    postprocess = (
        dict(_COMFYUI_TONE_DETAIL_POSTPROCESS)
        if _is_local_region_workflow(workflow_name) and strength == "conservative"
        else {"enabled": False, "strategy": "none", "candidate_only": True}
    )
    return {
        "workflow_name": workflow_name,
        "strength": strength,
        "profile_name": profile_name,
        "parameters": dict(_COMFYUI_WORKFLOW_PARAMETERS[strength]),
        "postprocess": postprocess,
    }


def is_t90_allowed_comfyui_candidate(model_name: str | None) -> bool:
    return str(model_name or "").strip() in COMFYUI_ALLOWED_CANDIDATE_WORKFLOWS


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _load_json_dict(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _approved_workflows(approval: dict[str, Any]) -> list[str]:
    raw = approval.get("approved_workflows") or approval.get("workflows") or approval.get("approved_workflow")
    if raw is None:
        raw = approval.get("workflow")
    if isinstance(raw, str):
        values = [item.strip() for item in raw.replace("\n", ",").split(",")]
    elif isinstance(raw, list):
        values = [str(item).strip() for item in raw]
    else:
        values = []
    return [item for item in dict.fromkeys(values) if item]


def _approval_hashes(approval: dict[str, Any]) -> dict[str, str]:
    raw = approval.get("approved_evidence_sha256") or approval.get("evidence_sha256") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if value}


def _vlm_promotion_blockers(report: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    nested_guardrail = report.get("vlm_guardrail") if isinstance(report.get("vlm_guardrail"), dict) else {}
    calibration_status = str(
        report.get("calibration_status") or nested_guardrail.get("calibration_status") or ""
    ).strip()
    if calibration_status != "calibrated_for_fail_closed_review":
        blockers.append(
            "vlm_not_calibrated_fail_closed"
            if calibration_status == "not_calibrated_fail_closed"
            else "vlm_calibration_status_not_approved"
        )

    accepted_count = _int_value(report.get("accepted_judgment_count"))
    required_count = _int_value(report.get("required_judgment_count_min"), 50)
    if accepted_count < required_count:
        blockers.append("vlm_accepted_judgments_below_threshold")

    agreement_rate = _float_value(report.get("agreement_rate"))
    required_agreement = _float_value(report.get("required_agreement_rate_min"), 0.9)
    if agreement_rate < required_agreement:
        blockers.append("vlm_agreement_below_threshold")

    false_candidate_count = _int_value(
        report.get("false_candidate_promotion_count", nested_guardrail.get("false_candidate_promotion_count"))
    )
    if false_candidate_count > 0:
        blockers.append("vlm_false_candidate_promotion")

    guardrail = report.get("candidate_promotion_guardrail")
    guardrail = guardrail if isinstance(guardrail, dict) else {}
    guardrail_status = str(
        guardrail.get("guardrail_status")
        or nested_guardrail.get("guardrail_status")
        or report.get("candidate_promotion_guardrail_status")
        or ""
    ).strip()
    if guardrail_status == "hard_veto":
        blockers.append("vlm_candidate_guardrail_hard_veto")
    if guardrail_status == "manual_review_required" or _int_value(guardrail.get("manual_review_required_count")) > 0:
        blockers.append("vlm_candidate_manual_review_required")
    return list(dict.fromkeys(blockers))


def _production_gate_blockers(report: dict[str, Any]) -> list[str]:
    gate = report.get("production_gate") if isinstance(report.get("production_gate"), dict) else report
    blockers: list[str] = []
    reason_code = str(gate.get("reason_code") or "").strip()
    if reason_code != "promotion_approval_required":
        blockers.append(f"production_gate_{reason_code or 'missing_reason_code'}")
    if gate.get("hard_defect_codes"):
        blockers.append("production_gate_hard_defects")
    candidate_wins = _int_value(gate.get("candidate_win_count"))
    required_wins = _int_value(gate.get("required_candidate_wins_min"), 20)
    if candidate_wins < required_wins:
        blockers.append("production_gate_candidate_wins_below_threshold")
    return blockers


def _manifest_binding_blockers(
    *,
    ab_validation_report_path: Path | None,
    vlm_guardrail_report_path: Path | None,
    production_gate_report_path: Path | None,
) -> list[str]:
    """C0.5.3 — second-layer evidence-hash check against the promotion manifest.

    The approval signature inside ``t47_comfyui_ab_report.json`` carries an
    ``approved_evidence_sha256`` map that pins the VLM + production gate
    reports. ``_evidence_hash_blockers`` enforces that pin. This function
    enforces the **mirror** check on the operator-controlled side: every
    canonical evidence file must also match the corresponding
    ``bindings.{ab_validation,vlm_guardrail,production_gate}_report_hash``
    slot in ``case-workbench-ai/promotion/manifest.json``.

    Both checks must pass before ``promote_to_default`` is allowed; either
    side drifting (e.g. a tampered report swapped under a stale approval,
    or a manifest written without re-signing) emits a blocker and the gate
    fails closed.
    """
    manifest = load_manifest()
    if manifest is None:
        return ["manifest_unavailable_for_binding_check"]
    bindings = manifest.get("bindings") if isinstance(manifest.get("bindings"), dict) else {}
    checks: tuple[tuple[str, Path | None], ...] = (
        ("ab_validation_report_hash", ab_validation_report_path),
        ("vlm_guardrail_report_hash", vlm_guardrail_report_path),
        ("production_gate_report_hash", production_gate_report_path),
    )
    blockers: list[str] = []
    for binding_name, path in checks:
        expected = bindings.get(binding_name)
        if not isinstance(expected, str) or not expected.strip():
            blockers.append(f"manifest_binding_{binding_name}_missing")
            continue
        if not path or not path.is_file():
            blockers.append(f"manifest_binding_{binding_name}_file_missing")
            continue
        try:
            actual = _hash_file(path)
        except OSError:
            blockers.append(f"manifest_binding_{binding_name}_file_unreadable")
            continue
        if actual != expected:
            blockers.append(f"manifest_binding_{binding_name}_drift")
    return blockers


def _evidence_hash_blockers(
    approval: dict[str, Any],
    *,
    vlm_guardrail_report_path: Path | None,
    production_gate_report_path: Path | None,
) -> list[str]:
    expected_hashes = _approval_hashes(approval)
    checks = {
        "vlm_guardrail_report": vlm_guardrail_report_path,
        "production_gate_report": production_gate_report_path,
    }
    blockers: list[str] = []
    for key, path in checks.items():
        if not path or not path.is_file():
            blockers.append(f"{key}_missing")
            continue
        if expected_hashes.get(key) != _hash_file(path):
            blockers.append(f"{key}_hash_mismatch")
    return blockers


def _comfyui_ab_validation_gate(
    report_path: Path | str | None = None,
    *,
    vlm_guardrail_report_path: Path | str | None = None,
    production_gate_report_path: Path | str | None = None,
    model_name: str | None = None,
) -> dict[str, Any]:
    """Check A/B validation report and apply promotion safety gates."""
    rp = Path(report_path or COMFYUI_AB_VALIDATION_REPORT_PATH)
    if not rp.is_file():
        return {
            "validation_status": "missing_report",
            "ready_for_human_review": False,
            "promote_to_default": False,
            "default_promotion_ready": False,
            "default_promotion_blockers": ["ab_validation_report_missing"],
        }
    report = _load_json_dict(rp)
    ready_for_review = bool(report.get("ready_for_human_review")) or report.get("validation_status") == "ready_for_human_review"
    raw_promote = bool(report.get("promote_to_default"))
    approval = report.get("promotion_approval") if isinstance(report.get("promotion_approval"), dict) else {}
    blockers: list[str] = []
    approved_workflows = _approved_workflows(approval)
    vlm_report: dict[str, Any] = {}
    production_gate_report: dict[str, Any] = {}
    vlm_path = Path(vlm_guardrail_report_path or COMFYUI_VLM_GUARDRAIL_REPORT_PATH)
    production_path = Path(production_gate_report_path or COMFYUI_PRODUCTION_GATE_REPORT_PATH)

    if raw_promote:
        if not ready_for_review:
            blockers.append("ab_validation_not_ready")
        if (
            approval.get("status") != "approved"
            or not bool(approval.get("approved"))
            or approval.get("scope") != "comfyui_default_promotion_v1"
            or approval.get("decision") != "approve_default_promotion"
            or not str(approval.get("approver") or approval.get("approved_by") or "").strip()
        ):
            blockers.append("promotion_approval_invalid")
        if not approved_workflows:
            blockers.append("approved_workflow_scope_missing")
        elif model_name and str(model_name).strip() not in approved_workflows:
            blockers.append("workflow_not_approved_for_default")

        if vlm_path.is_file():
            try:
                vlm_report = _load_json_dict(vlm_path)
                blockers.extend(_vlm_promotion_blockers(vlm_report))
            except (ValueError, OSError):
                blockers.append("vlm_guardrail_report_unreadable")
        else:
            blockers.append("vlm_guardrail_report_missing")

        if production_path.is_file():
            try:
                production_gate_report = _load_json_dict(production_path)
                blockers.extend(_production_gate_blockers(production_gate_report))
            except (ValueError, OSError):
                blockers.append("production_gate_report_unreadable")
        else:
            blockers.append("production_gate_report_missing")

        blockers.extend(
            _evidence_hash_blockers(
                approval,
                vlm_guardrail_report_path=vlm_path,
                production_gate_report_path=production_path,
            )
        )
        # C0.5.3 — mirror check against the manifest binding side.
        blockers.extend(
            _manifest_binding_blockers(
                ab_validation_report_path=rp,
                vlm_guardrail_report_path=vlm_path,
                production_gate_report_path=production_path,
            )
        )

    safe_promote = raw_promote and ready_for_review and not blockers
    return {
        "validation_status": report.get("validation_status", "unknown"),
        "ready_for_human_review": ready_for_review,
        "promote_to_default": safe_promote,
        "default_promotion_ready": safe_promote,
        "comparable_pair_count": report.get("comparable_pair_count", 0),
        "winner_evidence_count": report.get("winner_evidence_count", 0),
        "candidate_win_count": report.get("candidate_win_count", 0),
        "approved_workflows": approved_workflows,
        "default_promotion_blockers": list(dict.fromkeys(blockers)),
        "vlm_guardrail": vlm_report.get("candidate_promotion_guardrail") or vlm_report.get("vlm_guardrail") or {},
        "production_gate": production_gate_report.get("production_gate") or {},
    }


def _is_local_region_workflow(workflow_name: str) -> bool:
    return workflow_name.startswith("local_region_enhance_v")


def _replace_workflow_placeholders(value: Any, replacements: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _replace_workflow_placeholders(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_workflow_placeholders(item, replacements) for item in value]
    if isinstance(value, str):
        text = value
        if text.startswith("{{") and text.endswith("}}"):
            key = text[2:-2].strip()
            if key in replacements:
                return replacements[key]
        for key, replacement in replacements.items():
            text = text.replace("{{" + key + "}}", str(replacement))
        return text
    return value


def _load_comfyui_workflow(
    workflow_name: str,
    input_image: str,
    filename_prefix: str,
    *,
    extra_uploads: dict[str, str] | None = None,
    seed: int | None = None,
    workflow_parameters: dict[str, Any] | None = None,
    positive_prompt: str | None = None,
    negative_prompt: str | None = None,
) -> dict[str, Any]:
    workflow_path = COMFYUI_WORKFLOW_DIR / f"{workflow_name}.json"
    if not workflow_path.is_file():
        raise FileNotFoundError(f"ComfyUI workflow not found: {workflow_path}")
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    params = dict(_COMFYUI_WORKFLOW_PARAMETERS["conservative"])
    params.update(workflow_parameters or {})
    replacements: dict[str, Any] = {
        "input_image": input_image,
        "seed": int(seed if seed is not None else time.time_ns() % 2_147_483_647),
        "steps": int(params["steps"]),
        "cfg": float(params["cfg"]),
        "denoise": float(params["denoise"]),
        "control_strength": float(params.get("control_strength", 0.45)),
        "control_end": float(params.get("control_end", 0.68)),
        "filename_prefix": filename_prefix,
        "focus_mask": (extra_uploads or {}).get("focus_mask", "focus-mask.png"),
        "subject_mask": (extra_uploads or {}).get("subject_mask", "subject-mask.png"),
        "edge_mask": (extra_uploads or {}).get("edge_mask", "edge-mask.png"),
        # 2026-05-28 Step 2 of 4-mode plan: per-target prompts for M3 FOCAL.
        # Default to empty strings so workflows without these placeholders are unaffected.
        "positive_prompt": positive_prompt or "",
        "negative_prompt": negative_prompt or "",
    }
    return _replace_workflow_placeholders(workflow, replacements)


def _evaluate_comfyui_workflow_readiness(workflow: dict[str, Any], *, workflow_name: str) -> dict[str, Any]:
    class_types = {str(node.get("class_type") or "") for node in workflow.values() if isinstance(node, dict)}
    local_patch_composite = "ImageCompositeMasked" in class_types and "ImageSharpen" in class_types
    uses = {
        "inpaint": "VAEEncodeForInpaint" in class_types or "InpaintModelConditioning" in class_types,
        "background_removal": "LoadBackgroundRemovalModel" in class_types or "RemoveBackground" in class_types,
        "control_pose": "ControlNetLoader" in class_types or "ControlNetApply" in class_types or "ControlNetApplyAdvanced" in class_types,
        "sampler": "KSampler" in class_types,
        "sam_mask_refine": "SAMLoader" in class_types or "SAMDetectorCombined" in class_types,
        "local_patch_composite": local_patch_composite,
        "final_upscale": "ImageUpscaleWithModel" in class_types or "UpscaleModelLoader" in class_types,
    }
    reasons: list[str] = []
    if "LoadImage" in class_types and "SaveImage" in class_types and len(class_types) <= 2:
        reasons.append("workflow is pass-through and has no generation step")
    local_region_patch_workflow = _is_local_region_workflow(workflow_name) and local_patch_composite
    if not uses["sampler"] and not local_region_patch_workflow:
        reasons.append("workflow missing KSampler")
    if not uses["inpaint"] and not local_region_patch_workflow:
        reasons.append("workflow missing inpaint path")
    if workflow_name.startswith("portrait_") and not uses["control_pose"]:
        reasons.append("portrait workflow missing pose control")
    if _is_local_region_workflow(workflow_name) and not uses["sam_mask_refine"] and not local_region_patch_workflow:
        reasons.append("local-region workflow missing SAM mask refine")
    return {
        "workflow_name": workflow_name,
        "production_ready": not reasons,
        "readiness_reasons": reasons,
        "uses": uses,
    }


def _object_info_options(object_info: dict[str, Any], node: str, input_name: str) -> set[str]:
    try:
        raw = object_info[node]["input"]["required"][input_name]
    except Exception:
        return set()
    stack = [raw]
    options: set[str] = set()
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            options.add(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return options


def _build_comfyui_model_profile(
    registry: dict[str, Any],
    *,
    model_root: Path,
    object_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    object_info = object_info or {}
    models: dict[str, dict[str, Any]] = {}
    capabilities: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    for model in registry.get("models") or []:
        if not isinstance(model, dict):
            continue
        model_id = str(model.get("id") or "")
        capability = str(model.get("capability") or model_id)
        relative_path = str(model.get("relative_path") or "")
        filename = Path(relative_path).name
        file_exists = bool(relative_path) and (model_root / relative_path).is_file()
        object_ready = True
        if capability == "background_removal":
            object_ready = filename in _object_info_options(object_info, "LoadBackgroundRemovalModel", "bg_removal_name")
        elif capability == "final_upscale":
            object_ready = filename in _object_info_options(object_info, "UpscaleModelLoader", "model_name")
        elif capability == "sam_mask_refine":
            object_ready = filename in _object_info_options(object_info, "SAMLoader", "model_name")
        elif capability == "inpaint_checkpoint":
            object_ready = filename in _object_info_options(object_info, "CheckpointLoaderSimple", "ckpt_name")
        elif capability == "control_pose":
            object_ready = filename in _object_info_options(object_info, "ControlNetLoader", "control_net_name")
        elif capability == "identity_candidate":
            object_ready = filename in _object_info_options(object_info, "InstantIDModelLoader", "instantid_file")
        ready = bool(file_exists and object_ready)
        required = bool(model.get("required_for_production"))
        entry = {
            **model,
            "file_exists": file_exists,
            "object_info_ready": object_ready,
            "ready": ready,
            "required_for_production": required,
        }
        models[model_id] = entry
        capabilities[capability] = {"ready": ready, "model_id": model_id, "required_for_production": required}
        if required and not ready:
            reasons.append(f"{capability} not ready")
    candidate_gaps: list[dict[str, Any]] = []
    for model in registry.get("models") or []:
        if not isinstance(model, dict):
            continue
        if not model.get("production_candidate"):
            continue
        model_id = str(model.get("id") or "")
        cap = str(model.get("capability") or model_id)
        cap_entry = capabilities.get(cap)
        if cap_entry and not cap_entry["ready"]:
            hint = str(model.get("source_model") or model.get("relative_path") or model_id)
            candidate_gaps.append({
                "capability": cap,
                "model_id": model_id,
                "required_for_production": bool(model.get("required_for_production")),
                "install_hint": f"Install {hint} to enable {cap}",
            })
    return {
        "models": models,
        "capabilities": capabilities,
        "production_ready": not reasons,
        "readiness_reasons": reasons,
        "candidate_capability_gaps": candidate_gaps,
    }


def _comfyui_json(path: str, payload: dict[str, Any] | None = None, *, timeout: int = 30) -> dict[str, Any]:
    data = None
    headers: dict[str, str] = {}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = request.Request(f"{COMFYUI_BASE_URL}{path}", data=data, headers=headers, method=method)
    with request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local ComfyUI operator API.
        body = resp.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def _comfyui_interrupt(prompt_id: str | None = None) -> None:
    payload = {"prompt_id": prompt_id} if prompt_id else {}
    try:
        _comfyui_json("/interrupt", payload=payload, timeout=10)
    except Exception:
        pass


def _comfyui_preflight(*, workflow_name: str | None = None) -> dict[str, Any]:
    """C0.5.4 — the gate now needs to know which workflow we plan to route
    so it can enforce the GA approval scope (``portrait_focal_enhance_v1``).

    ``workflow_name`` defaults to ``GA_APPROVED_WORKFLOW`` so callers that
    do not have a runtime override (preflight probes, status endpoints)
    still hit the GA scope check. Runtime renderers should pass the
    actually-resolved workflow name so a routing mistake fails the gate.
    """
    system_stats = _comfyui_json("/system_stats", timeout=10)
    try:
        object_info = _comfyui_json("/object_info", timeout=20)
    except Exception:  # noqa: BLE001 - old ComfyUI installs may omit object_info details.
        object_info = {}
    core_required = {"LoadImage", "SaveImage", "KSampler", "VAEEncodeForInpaint"}
    core_missing = sorted(node for node in core_required if object_info and node not in object_info)
    registry_path = Path(__file__).resolve().parent.parent / "comfyui-model-registry.json"
    registry: dict[str, Any] = {}
    if registry_path.is_file():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    model_root = Path(os.environ.get("CASE_WORKBENCH_COMFYUI_MODEL_ROOT", "/Users/a1234/Desktop/飞书Claude/ComfyUI/models"))
    model_profile = _build_comfyui_model_profile(registry, model_root=model_root, object_info=object_info) if registry else {
        "production_ready": False,
        "readiness_reasons": ["missing model registry"],
        "capabilities": {},
        "models": {},
    }
    effective_workflow = (workflow_name or "").strip() or GA_APPROVED_WORKFLOW
    ab_validation = _comfyui_ab_validation_gate(model_name=effective_workflow)
    ab_reasons: list[str] = []
    if ab_validation["validation_status"] != "ready_for_human_review":
        ab_reasons.append(f"A/B validation not ready ({ab_validation['validation_status']})")
    for blocker in ab_validation.get("default_promotion_blockers") or []:
        ab_reasons.append(f"default promotion blocked ({blocker})")
    vlm_guardrail: dict[str, Any] = {}
    vlm_veto = False
    if COMFYUI_VLM_GUARDRAIL_REPORT_PATH.is_file():
        try:
            vlm_data = json.loads(COMFYUI_VLM_GUARDRAIL_REPORT_PATH.read_text(encoding="utf-8"))
            vlm_guardrail = vlm_data.get("candidate_promotion_guardrail") or {}
            if vlm_guardrail.get("guardrail_status") == "hard_veto":
                vlm_veto = True
                ab_reasons.append("VLM guardrail hard_veto")
        except (ValueError, OSError):
            pass
    all_reasons = [*core_missing, *(model_profile.get("readiness_reasons") or []), *ab_reasons]
    return {
        **system_stats,
        "object_info_available": bool(object_info),
        "node_status": {"core_missing": core_missing},
        "model_stack_ready": bool(model_profile.get("production_ready")),
        "model_profile": model_profile,
        "ab_validation": ab_validation,
        "vlm_guardrail": vlm_guardrail,
        "default_promotion_ready": ab_validation.get("default_promotion_ready", False),
        "production_ready": not core_missing and bool(model_profile.get("production_ready")) and not ab_reasons and not vlm_veto,
        "readiness_reasons": all_reasons,
        # C0.5.4 — surface which workflow the gate evaluated so operators
        # can confirm the GA scope binding from the status endpoint.
        "gated_workflow": effective_workflow,
    }


def _multipart_upload_body(image_path: Path) -> tuple[bytes, str]:
    boundary = f"----case-workbench-{uuid.uuid4().hex}"
    content_type = "image/png" if image_path.suffix.lower() == ".png" else "application/octet-stream"
    parts = [
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{image_path.name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ]
    body = bytearray("".join(parts).encode("utf-8"))
    body.extend(image_path.read_bytes())
    body.extend(f"\r\n--{boundary}\r\n".encode("utf-8"))
    body.extend(b'Content-Disposition: form-data; name="type"\r\n\r\ninput\r\n')
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), boundary


def _comfyui_upload_image(image_path: Path) -> dict[str, Any]:
    body, boundary = _multipart_upload_body(Path(image_path))
    req = request.Request(
        f"{COMFYUI_BASE_URL}/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with request.urlopen(req, timeout=60) as resp:  # noqa: S310 - local ComfyUI operator API.
        return json.loads(resp.read().decode("utf-8"))


def _download_comfyui_output(image_ref: dict[str, Any], output_path: Path) -> None:
    params = parse.urlencode(
        {
            "filename": str(image_ref.get("filename") or ""),
            "subfolder": str(image_ref.get("subfolder") or ""),
            "type": str(image_ref.get("type") or "output"),
        }
    )
    with request.urlopen(f"{COMFYUI_BASE_URL}/view?{params}", timeout=120) as resp:  # noqa: S310
        output_path.write_bytes(resp.read())


def _first_comfyui_output_image(history_item: dict[str, Any]) -> dict[str, Any] | None:
    outputs = history_item.get("outputs") if isinstance(history_item, dict) else None
    if not isinstance(outputs, dict):
        return None
    for output in outputs.values():
        if not isinstance(output, dict):
            continue
        images = output.get("images")
        if isinstance(images, list) and images:
            image_ref = images[0]
            if isinstance(image_ref, dict):
                return image_ref
    return None


def _mask_nonzero_ratio(mask: "Image.Image") -> float:
    hist = mask.convert("L").histogram()
    nonzero = sum(hist[1:])
    total = sum(hist) or 1
    return round(nonzero / total, 6)


def _prepare_comfyui_input_layers(
    source_path: Path,
    *,
    output_dir: Path,
    workflow_name: str,
    focus_regions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from PIL import Image, ImageDraw, ImageFilter, ImageOps

    output_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(source_path) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB")
    normalized = output_dir / "comfyui-normalized.png"
    image.save(normalized)
    width, height = image.size
    subject_mask = Image.new("L", image.size, 255)
    focus_mask = Image.new("L", image.size, 0)
    draw = ImageDraw.Draw(focus_mask)
    requested = 0.0
    regions = focus_regions or []
    if regions:
        for region in regions:
            left = int(round(float(region.get("x", 0)) * width))
            top = int(round(float(region.get("y", 0)) * height))
            right = int(round((float(region.get("x", 0)) + float(region.get("width", 0))) * width))
            bottom = int(round((float(region.get("y", 0)) + float(region.get("height", 0))) * height))
            left = max(0, min(width, left))
            right = max(0, min(width, right))
            top = max(0, min(height, top))
            bottom = max(0, min(height, bottom))
            if right > left and bottom > top:
                draw.rectangle((left, top, right, bottom), fill=255)
                requested += ((right - left) * (bottom - top)) / max(1, width * height)
    else:
        focus_mask = Image.new("L", image.size, 255)
        requested = 1.0
    if _is_local_region_workflow(workflow_name):
        focus_mask = focus_mask.filter(ImageFilter.GaussianBlur(radius=max(2, min(width, height) // 160)))
    edge_mask = focus_mask.filter(ImageFilter.FIND_EDGES)
    subject_mask_path = output_dir / "subject-mask.png"
    focus_mask_path = output_dir / "focus-mask.png"
    edge_mask_path = output_dir / "edge-mask.png"
    subject_mask.convert("RGB").save(subject_mask_path)
    focus_mask.convert("RGB").save(focus_mask_path)
    edge_mask.convert("RGB").save(edge_mask_path)
    return {
        "normalized_image_path": str(normalized),
        "subject_mask_path": str(subject_mask_path),
        "focus_mask_path": str(focus_mask_path),
        "edge_mask_path": str(edge_mask_path),
        "canvas_slot": {"width": width, "height": height},
        "subject_scale": {"height_ratio": 1.0},
        "mask_metrics": {
            "focus_strategy": "local_region_tight" if _is_local_region_workflow(workflow_name) else "full_subject",
            "focus_requested_coverage": round(requested, 6),
            "focus_coverage": _mask_nonzero_ratio(focus_mask),
            "focus_shrink_px": 0,
            "focus_grow_px": 0,
            "focus_feather_px": max(2, min(width, height) // 160) if _is_local_region_workflow(workflow_name) else 0,
            "subject_coverage": _mask_nonzero_ratio(subject_mask),
        },
    }


def _score_comfyui_output_quality(
    original_path: Path,
    generated_path: Path,
    subject_mask_path: Path,
    canvas_slot: dict[str, Any],
    subject_scale: dict[str, Any],
    target_mask_path: Path | None = None,
) -> dict[str, Any]:
    from PIL import Image, ImageChops, ImageOps, ImageStat

    with Image.open(original_path) as original_raw, Image.open(generated_path) as generated_raw:
        original = ImageOps.exif_transpose(original_raw).convert("RGB")
        generated = ImageOps.exif_transpose(generated_raw).convert("RGB")
        if generated.size != original.size:
            generated = generated.resize(original.size)
        diff = ImageChops.difference(original, generated)
        gray = ImageOps.grayscale(diff)
        mean = ImageStat.Stat(gray).mean[0] / 255 * 100

    with Image.open(subject_mask_path) as mask_raw:
        mask = mask_raw.convert("L")
        if mask.size != original.size:
            mask = mask.resize(original.size)
    quality_mask = mask
    if target_mask_path and target_mask_path.is_file():
        with Image.open(target_mask_path) as target_mask_raw:
            quality_mask = target_mask_raw.convert("L")
            if quality_mask.size != original.size:
                quality_mask = quality_mask.resize(original.size)
    mask_inv = ImageChops.invert(mask)
    outside_diff = ImageChops.multiply(gray, mask_inv)
    outside_pixels = sum(1 for p in mask_inv.getdata() if p > 0)
    mask_outside_delta = float(ImageStat.Stat(outside_diff).sum[0]) / max(1, outside_pixels) / 255 * 100 if outside_pixels > 0 else 0.0
    mask_pixels = sum(1 for p in mask.getdata() if p > 0)
    masked_luma_delta = 0.0
    color_cast_delta = 0.0
    red_green_delta = 0.0
    blue_yellow_delta = 0.0
    masked_texture_before = 0.0
    masked_texture_after = 0.0
    masked_shadow_p10_delta = 0.0
    masked_shadow_contrast_before = 0.0
    masked_shadow_contrast_after = 0.0
    masked_shadow_contrast_delta = 0.0
    masked_highlight_p95_before = 0.0
    masked_highlight_p95_after = 0.0
    masked_highlight_p95_delta = 0.0
    masked_highlight_p99_before = 0.0
    masked_highlight_p99_after = 0.0
    masked_highlight_p99_delta = 0.0
    masked_specular_ratio_before = 0.0
    masked_specular_ratio_after = 0.0
    masked_specular_ratio_delta = 0.0
    face_luma_before = 0.0
    face_luma_after = 0.0
    face_luma_delta = 0.0
    face_background_contrast_before = 0.0
    face_background_contrast_after = 0.0
    face_background_contrast_delta = 0.0
    quality_mask_pixels = sum(1 for p in quality_mask.getdata() if p > 0)
    original_gray = ImageOps.grayscale(original)
    generated_gray = ImageOps.grayscale(generated)
    face_mask = _portrait_face_tone_mask(original.size)
    background_mask = _portrait_background_tone_mask(original.size)
    if sum(face_mask.histogram()[1:]) > 0 and sum(background_mask.histogram()[1:]) > 0:
        original_face_luma = ImageStat.Stat(original_gray, face_mask).mean[0]
        generated_face_luma = ImageStat.Stat(generated_gray, face_mask).mean[0]
        original_background_luma = ImageStat.Stat(original_gray, background_mask).mean[0]
        generated_background_luma = ImageStat.Stat(generated_gray, background_mask).mean[0]
        face_luma_before = original_face_luma
        face_luma_after = generated_face_luma
        face_luma_delta = generated_face_luma - original_face_luma
        face_background_contrast_before = original_face_luma - original_background_luma
        face_background_contrast_after = generated_face_luma - generated_background_luma
        face_background_contrast_delta = face_background_contrast_after - face_background_contrast_before
    if quality_mask_pixels > 0:
        original_luma = ImageStat.Stat(original_gray, quality_mask).mean[0]
        generated_luma = ImageStat.Stat(generated_gray, quality_mask).mean[0]
        masked_texture_before = ImageStat.Stat(original_gray, quality_mask).stddev[0]
        masked_texture_after = ImageStat.Stat(generated_gray, quality_mask).stddev[0]
        original_rgb = ImageStat.Stat(original, quality_mask).mean
        generated_rgb = ImageStat.Stat(generated, quality_mask).mean
        original_p10 = _masked_luma_percentile(original_gray, quality_mask, 10)
        original_p50 = _masked_luma_percentile(original_gray, quality_mask, 50)
        original_p95 = _masked_luma_percentile(original_gray, quality_mask, 95)
        original_p99 = _masked_luma_percentile(original_gray, quality_mask, 99)
        generated_p10 = _masked_luma_percentile(generated_gray, quality_mask, 10)
        generated_p50 = _masked_luma_percentile(generated_gray, quality_mask, 50)
        generated_p95 = _masked_luma_percentile(generated_gray, quality_mask, 95)
        generated_p99 = _masked_luma_percentile(generated_gray, quality_mask, 99)
        original_red_green = original_rgb[0] - original_rgb[1]
        generated_red_green = generated_rgb[0] - generated_rgb[1]
        original_blue_yellow = original_rgb[2] - ((original_rgb[0] + original_rgb[1]) / 2)
        generated_blue_yellow = generated_rgb[2] - ((generated_rgb[0] + generated_rgb[1]) / 2)
        masked_luma_delta = generated_luma - original_luma
        red_green_delta = generated_red_green - original_red_green
        blue_yellow_delta = generated_blue_yellow - original_blue_yellow
        color_cast_delta = max(abs(red_green_delta), abs(blue_yellow_delta))
        masked_shadow_p10_delta = generated_p10 - original_p10
        masked_shadow_contrast_before = original_p50 - original_p10
        masked_shadow_contrast_after = generated_p50 - generated_p10
        masked_shadow_contrast_delta = masked_shadow_contrast_after - masked_shadow_contrast_before
        masked_highlight_p95_before = original_p95
        masked_highlight_p95_after = generated_p95
        masked_highlight_p95_delta = generated_p95 - original_p95
        masked_highlight_p99_before = original_p99
        masked_highlight_p99_after = generated_p99
        masked_highlight_p99_delta = generated_p99 - original_p99
        masked_specular_ratio_before = _masked_luma_ratio_at_or_above(original_gray, quality_mask, 230)
        masked_specular_ratio_after = _masked_luma_ratio_at_or_above(generated_gray, quality_mask, 230)
        masked_specular_ratio_delta = masked_specular_ratio_after - masked_specular_ratio_before

    target_cx = float(canvas_slot.get("target_center_x") or 0.5)
    target_cy = float(canvas_slot.get("target_center_y") or 0.5)
    actual_cx = 0.5
    actual_cy = 0.5
    slot_center_delta = round(((actual_cx - target_cx) ** 2 + (actual_cy - target_cy) ** 2) ** 0.5, 4)

    height_ratio = float(subject_scale.get("height_ratio") or 1.0)
    target_height_ratio = float(subject_scale.get("target_height_ratio") or height_ratio)
    scale_delta = round(abs(height_ratio - target_height_ratio), 3)

    return {
        "output_width": generated.size[0],
        "output_height": generated.size[1],
        "halo_score": round(mean, 3),
        "subject_scale_delta": scale_delta,
        "slot_center_delta": slot_center_delta,
        "mask_outside_delta": round(mask_outside_delta, 3),
        "masked_luma_delta": round(masked_luma_delta, 3),
        "color_cast_delta": round(color_cast_delta, 3),
        "red_green_delta": round(red_green_delta, 3),
        "blue_yellow_delta": round(blue_yellow_delta, 3),
        "masked_texture_before": round(masked_texture_before, 3),
        "masked_texture_after": round(masked_texture_after, 3),
        "texture_detail_delta": round(masked_texture_after - masked_texture_before, 3),
        "masked_shadow_p10_delta": round(masked_shadow_p10_delta, 3),
        "masked_shadow_contrast_before": round(masked_shadow_contrast_before, 3),
        "masked_shadow_contrast_after": round(masked_shadow_contrast_after, 3),
        "masked_shadow_contrast_delta": round(masked_shadow_contrast_delta, 3),
        "masked_highlight_p95_before": round(masked_highlight_p95_before, 3),
        "masked_highlight_p95_after": round(masked_highlight_p95_after, 3),
        "masked_highlight_p95_delta": round(masked_highlight_p95_delta, 3),
        "masked_highlight_p99_before": round(masked_highlight_p99_before, 3),
        "masked_highlight_p99_after": round(masked_highlight_p99_after, 3),
        "masked_highlight_p99_delta": round(masked_highlight_p99_delta, 3),
        "masked_specular_ratio_before": round(masked_specular_ratio_before, 6),
        "masked_specular_ratio_after": round(masked_specular_ratio_after, 6),
        "masked_specular_ratio_delta": round(masked_specular_ratio_delta, 6),
        "face_luma_before": round(face_luma_before, 3),
        "face_luma_after": round(face_luma_after, 3),
        "face_luma_delta": round(face_luma_delta, 3),
        "face_background_contrast_before": round(face_background_contrast_before, 3),
        "face_background_contrast_after": round(face_background_contrast_after, 3),
        "face_background_contrast_delta": round(face_background_contrast_delta, 3),
        "canvas_width": canvas_slot.get("width"),
        "canvas_height": canvas_slot.get("height"),
    }


def _free_memory_mb(preflight: dict[str, Any]) -> float | None:
    values: list[float] = []
    system = preflight.get("system") if isinstance(preflight.get("system"), dict) else {}
    for key in ("ram_free", "free_memory"):
        value = system.get(key)
        if isinstance(value, (int, float)):
            values.append(float(value) / 1024 / 1024)
    devices = preflight.get("devices")
    if isinstance(devices, list):
        for device in devices:
            if not isinstance(device, dict):
                continue
            for key in ("vram_free", "free_memory"):
                value = device.get(key)
                if isinstance(value, (int, float)):
                    values.append(float(value) / 1024 / 1024)
    return max(values) if values else None


_COMFYUI_MPS_RETRYABLE_ERRORS = (
    "convolution_overrideable not implemented",
    "NotImplementedError",
    "MPS backend",
)
COMFYUI_MAX_RETRIES = int(os.environ.get("CASE_WORKBENCH_COMFYUI_MAX_RETRIES", "2"))


def _comfyui_free_memory() -> None:
    """Ask ComfyUI to unload models and free MPS/CUDA memory between retries."""
    try:
        _comfyui_json(
            "/free",
            payload={"unload_models": True, "free_memory": True},
            timeout=30,
        )
        time.sleep(3)
    except Exception:
        pass


def _is_mps_retryable(error: BaseException) -> bool:
    msg = str(error)
    return any(needle in msg for needle in _COMFYUI_MPS_RETRYABLE_ERRORS)


def _run_comfyui_workflow(
    input_path: Path,
    *,
    output_dir: Path,
    workflow_name: str,
    seed: int | None = None,
    focus_mask_path: Path | None = None,
    subject_mask_path: Path | None = None,
    edge_mask_path: Path | None = None,
    workflow_parameters: dict[str, Any] | None = None,
    positive_prompt: str | None = None,
    negative_prompt: str | None = None,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    started_wait = time.monotonic()
    _COMFYUI_GATE.acquire()
    wait_seconds = time.monotonic() - started_wait
    try:
        last_error: BaseException | None = None
        for attempt in range(1 + COMFYUI_MAX_RETRIES):
            if attempt > 0:
                LOGGER.info("ComfyUI retry %d/%d after MPS error, freeing memory first", attempt, COMFYUI_MAX_RETRIES)
                _comfyui_free_memory()
            try:
                return _run_comfyui_workflow_once(
                    input_path,
                    output_dir=output_dir,
                    workflow_name=workflow_name,
                    seed=seed,
                    focus_mask_path=focus_mask_path,
                    subject_mask_path=subject_mask_path,
                    edge_mask_path=edge_mask_path,
                    workflow_parameters=workflow_parameters,
                    positive_prompt=positive_prompt,
                    negative_prompt=negative_prompt,
                    timeout_seconds=timeout_seconds,
                    wait_seconds=wait_seconds,
                )
            except (RuntimeError, TimeoutError) as exc:
                last_error = exc
                if not _is_mps_retryable(exc) or attempt >= COMFYUI_MAX_RETRIES:
                    raise
                LOGGER.warning("ComfyUI MPS-retryable error (attempt %d): %s", attempt + 1, exc)
        raise last_error  # unreachable but satisfies type checker
    finally:
        _COMFYUI_GATE.release()


def _run_comfyui_workflow_once(
    input_path: Path,
    *,
    output_dir: Path,
    workflow_name: str,
    seed: int | None = None,
    focus_mask_path: Path | None = None,
    subject_mask_path: Path | None = None,
    edge_mask_path: Path | None = None,
    workflow_parameters: dict[str, Any] | None = None,
    positive_prompt: str | None = None,
    negative_prompt: str | None = None,
    timeout_seconds: int = 900,
    wait_seconds: float = 0.0,
) -> dict[str, Any]:
    # S1: thread the actually-resolved workflow name so the GA scope check
    # (`workflow_not_approved_for_default`) and `gated_workflow` reflect the
    # real runtime workflow. Calling preflight with no arg made
    # `effective_workflow` fall back to GA_APPROVED_WORKFLOW unconditionally —
    # a tautology that let a routing mistake bypass the gate.
    preflight = _comfyui_preflight(workflow_name=workflow_name)
    missing = ((preflight.get("node_status") or {}).get("core_missing") or [])
    if missing:
        raise RuntimeError(f"ComfyUI missing required nodes: {', '.join(missing)}")
    free_mb = _free_memory_mb(preflight)
    if free_mb is not None and free_mb < COMFYUI_MIN_FREE_MEMORY_MB:
        raise RuntimeError(f"ComfyUI free memory too low for candidate run: {free_mb:.0f} MB")
    uploaded_input = _comfyui_upload_image(Path(input_path))
    uploads: dict[str, str] = {}
    if focus_mask_path is not None:
        uploads["focus_mask"] = str(_comfyui_upload_image(Path(focus_mask_path)).get("name") or Path(focus_mask_path).name)
    if subject_mask_path is not None:
        uploads["subject_mask"] = str(_comfyui_upload_image(Path(subject_mask_path)).get("name") or Path(subject_mask_path).name)
    if edge_mask_path is not None:
        uploads["edge_mask"] = str(_comfyui_upload_image(Path(edge_mask_path)).get("name") or Path(edge_mask_path).name)
    filename_prefix = f"case-workbench/{output_dir.name}/{workflow_name}"
    workflow = _load_comfyui_workflow(
        workflow_name,
        str(uploaded_input.get("name") or Path(input_path).name),
        filename_prefix,
        extra_uploads=uploads,
        seed=seed,
        workflow_parameters=workflow_parameters,
        positive_prompt=positive_prompt,
        negative_prompt=negative_prompt,
    )
    workflow_hash = "sha256:" + hashlib.sha256(json.dumps(workflow, sort_keys=True).encode("utf-8")).hexdigest()
    prompt_response = _comfyui_json(
        "/prompt",
        payload={"prompt": workflow, "client_id": f"case-workbench-{uuid.uuid4().hex}"},
        timeout=60,
    )
    prompt_id = str(prompt_response.get("prompt_id") or "")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI did not return prompt_id: {prompt_response}")
    deadline = time.monotonic() + timeout_seconds
    image_ref: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        history = _comfyui_json(f"/history/{prompt_id}", timeout=30)
        item = history.get(prompt_id) if isinstance(history, dict) else None
        if isinstance(item, dict):
            image_ref = _first_comfyui_output_image(item)
            if image_ref:
                break
            status = item.get("status") if isinstance(item.get("status"), dict) else {}
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI prompt failed: {status}")
        time.sleep(1.0)
    if not image_ref:
        _comfyui_interrupt(prompt_id)
        raise TimeoutError(f"ComfyUI prompt timed out: {prompt_id}")
    output_path = output_dir / "comfyui-generated.png"
    _download_comfyui_output(image_ref, output_path)
    return {
        "generated_path": str(output_path),
        "prompt_id": prompt_id,
        "workflow_name": workflow_name,
        "workflow_hash": workflow_hash,
        "preflight": preflight,
        "concurrency": {
            "max_concurrency": COMFYUI_MAX_CONCURRENCY,
            "wait_seconds": round(wait_seconds, 3),
        },
        "image_ref": image_ref,
    }


def _score_from_histogram(histogram: list[int], total: int, percentile: float) -> float:
    if total <= 0:
        return 0.0
    target = total * percentile
    seen = 0
    for value, count in enumerate(histogram):
        seen += count
        if seen >= target:
            return round(value / 255 * 100, 3)
    return 100.0


def _region_mask(size: tuple[int, int], focus_regions: list[dict[str, Any]]) -> "Image.Image":
    from PIL import Image, ImageChops, ImageDraw

    width, height = size
    if not focus_regions:
        return Image.new("L", size, 255)
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for region in focus_regions:
        x = max(0.0, min(1.0, float(region.get("x", 0))))
        y = max(0.0, min(1.0, float(region.get("y", 0))))
        w = max(0.0, min(1.0, float(region.get("width", 0))))
        h = max(0.0, min(1.0, float(region.get("height", 0))))
        left = int(round(x * width))
        top = int(round(y * height))
        right = int(round((x + w) * width))
        bottom = int(round((y + h) * height))
        if right > left and bottom > top:
            draw.rectangle((left, top, min(width, right), min(height, bottom)), fill=255)
    return mask


def _portrait_face_tone_mask(size: tuple[int, int]) -> "Image.Image":
    from PIL import Image, ImageDraw, ImageFilter

    width, height = size
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse(
        (
            int(round(width * 0.31)),
            int(round(height * 0.15)),
            int(round(width * 0.69)),
            int(round(height * 0.70)),
        ),
        fill=255,
    )
    return mask.filter(ImageFilter.GaussianBlur(radius=max(1, min(width, height) // 160)))


def _portrait_background_tone_mask(size: tuple[int, int]) -> "Image.Image":
    from PIL import Image, ImageDraw

    width, height = size
    mask = Image.new("L", size, 255)
    draw = ImageDraw.Draw(mask)
    draw.rectangle(
        (
            int(round(width * 0.18)),
            int(round(height * 0.05)),
            int(round(width * 0.82)),
            int(round(height * 0.90)),
        ),
        fill=0,
    )
    return mask


def _portrait_feature_protect_mask(size: tuple[int, int]) -> "Image.Image":
    from PIL import Image, ImageDraw

    width, height = size
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    boxes = (
        (0.30, 0.32, 0.45, 0.42),
        (0.55, 0.32, 0.70, 0.42),
        (0.43, 0.38, 0.57, 0.58),
        (0.36, 0.55, 0.64, 0.66),
    )
    for left, top, right, bottom in boxes:
        draw.rectangle(
            (
                int(round(width * left)),
                int(round(height * top)),
                int(round(width * right)),
                int(round(height * bottom)),
            ),
            fill=255,
        )
    return mask


def _portrait_lip_feature_mask(size: tuple[int, int]) -> "Image.Image":
    from PIL import Image, ImageDraw

    width, height = size
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rectangle(
        (
            int(round(width * 0.34)),
            int(round(height * 0.53)),
            int(round(width * 0.66)),
            int(round(height * 0.68)),
        ),
        fill=255,
    )
    return mask


def _portrait_edge_halo_guard_mask(size: tuple[int, int]) -> "Image.Image":
    from PIL import Image, ImageChops, ImageDraw

    width, height = size
    outer = Image.new("L", size, 0)
    inner = Image.new("L", size, 0)
    outer_pad_x = max(3, int(round(width * 0.07)))
    outer_pad_y = max(3, int(round(height * 0.06)))
    inner_pad_x = max(2, int(round(width * 0.02)))
    inner_pad_y = max(2, int(round(height * 0.02)))
    face_box = (
        int(round(width * 0.31)),
        int(round(height * 0.15)),
        int(round(width * 0.69)),
        int(round(height * 0.70)),
    )
    ImageDraw.Draw(outer).ellipse(
        (
            max(0, face_box[0] - outer_pad_x),
            max(0, face_box[1] - outer_pad_y),
            min(width, face_box[2] + outer_pad_x),
            min(height, face_box[3] + outer_pad_y),
        ),
        fill=255,
    )
    ImageDraw.Draw(inner).ellipse(
        (
            max(0, face_box[0] + inner_pad_x),
            max(0, face_box[1] + inner_pad_y),
            min(width, face_box[2] - inner_pad_x),
            min(height, face_box[3] - inner_pad_y),
        ),
        fill=255,
    )
    return ImageChops.subtract(outer, inner)


def _masked_mean(gray: "Image.Image", mask: "Image.Image", include_mask: bool) -> float:
    from PIL import ImageChops, ImageStat

    active = mask if include_mask else ImageChops.invert(mask)
    stat = ImageStat.Stat(gray, active)
    count = sum(active.histogram()[1:])
    if count <= 0:
        return 0.0
    return round(float(stat.mean[0]) / 255 * 100, 3)


def _masked_luma_percentile(gray: "Image.Image", mask: "Image.Image", percentile: float) -> float:
    hist = gray.histogram(mask=mask)
    total = sum(hist)
    if total <= 0:
        return 0.0
    threshold = max(1.0, total * max(0.0, min(100.0, float(percentile))) / 100.0)
    cumulative = 0
    for value, count in enumerate(hist):
        cumulative += count
        if cumulative >= threshold:
            return float(value)
    return 255.0


def _masked_luma_ratio_at_or_above(gray: "Image.Image", mask: "Image.Image", threshold: float) -> float:
    hist = gray.histogram(mask=mask)
    total = sum(hist)
    if total <= 0:
        return 0.0
    start = int(max(0, min(255, round(float(threshold)))))
    return float(sum(hist[start:]) / total)


def _apply_comfyui_tone_detail_postprocess(
    generated_path: Path,
    output_path: Path,
    *,
    mask_path: Path | None,
    reference_path: Path | None = None,
    settings: dict[str, Any],
) -> dict[str, Any]:
    from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps, ImageStat

    strategy = str(settings.get("strategy") or "clinical_tone_highlight_detail_v1")
    midtone_lift = float(settings.get("midtone_lift") or 1.0)
    highlight_lift = float(settings.get("highlight_lift") or 1.0)
    local_contrast = float(settings.get("local_contrast") or 1.0)
    detail_sharpness = float(settings.get("detail_sharpness") or 1.0)
    shadow_lift = float(settings.get("shadow_lift") or 1.0)
    shadow_detail_contrast = float(settings.get("shadow_detail_contrast") or 1.0)
    shadow_threshold = int(settings.get("shadow_threshold") or 112)
    global_luma_lift = float(settings.get("global_luma_lift") or 1.0)
    global_max_delta = int(settings.get("global_max_delta") or 0)
    max_delta = int(settings.get("max_delta") or 0)
    preserve_chroma = bool(settings.get("preserve_chroma"))
    reference_chroma_match_strength = float(settings.get("reference_chroma_match_strength") or 0.0)
    max_chroma_shift_delta = int(settings.get("max_chroma_shift_delta") or 0)
    reference_luma_floor_delta = float(settings.get("reference_luma_floor_delta", -4.0))
    reference_luma_floor_max_lift = float(settings.get("reference_luma_floor_max_lift", 0.0))
    max_shadow_contrast_delta = float(settings.get("max_shadow_contrast_delta", 0.0))
    shadow_floor_lift_max = float(settings.get("shadow_floor_lift_max", 0.0))
    max_highlight_p95_delta = float(settings.get("max_highlight_p95_delta", 0.0))
    max_highlight_p99_delta = float(settings.get("max_highlight_p99_delta", 0.0))
    highlight_guard_max_darken = float(settings.get("highlight_guard_max_darken", 0.0))
    specular_threshold = float(settings.get("specular_threshold", 230.0))
    max_specular_ratio_delta = float(settings.get("max_specular_ratio_delta", 0.0))
    reference_blend_strength = max(0.0, min(1.0, float(settings.get("reference_blend_strength", 0.0))))
    face_tone_guard_enabled = bool(settings.get("face_tone_guard_enabled"))
    face_luma_target_delta = float(settings.get("face_luma_target_delta", 0.0))
    face_luma_max_lift = float(settings.get("face_luma_max_lift", 0.0))
    face_background_contrast_target_delta = float(settings.get("face_background_contrast_target_delta", 0.0))
    face_contrast_max_lift = float(settings.get("face_contrast_max_lift", 0.0))
    face_tone_highlight_protect_threshold = float(settings.get("face_tone_highlight_protect_threshold", 190.0))
    semantic_fidelity_guard_enabled = bool(settings.get("semantic_fidelity_guard_enabled"))
    background_preserve_blend_strength = max(
        0.0,
        min(1.0, float(settings.get("background_preserve_blend_strength", 0.0))),
    )
    feature_protect_blend_strength = max(0.0, min(1.0, float(settings.get("feature_protect_blend_strength", 0.0))))
    feature_protect_min_delta = max(0.0, float(settings.get("feature_protect_min_delta", 0.0)))
    feature_protect_min_excess_delta = max(0.0, float(settings.get("feature_protect_min_excess_delta", 0.0)))
    face_chroma_guard_enabled = bool(settings.get("face_chroma_guard_enabled"))
    face_chroma_max_delta = max(0.0, float(settings.get("face_chroma_max_delta", 0.0)))
    face_chroma_blend_strength = max(0.0, min(1.0, float(settings.get("face_chroma_blend_strength", 0.0))))
    face_texture_guard_enabled = bool(settings.get("face_texture_guard_enabled"))
    face_texture_min_loss = max(0.0, float(settings.get("face_texture_min_loss", 0.0)))
    face_texture_blend_strength = max(0.0, min(1.0, float(settings.get("face_texture_blend_strength", 0.0))))
    face_identity_guard_enabled = bool(settings.get("face_identity_guard_enabled"))
    face_identity_min_delta = max(0.0, float(settings.get("face_identity_min_delta", 0.0)))
    face_identity_blend_strength = max(0.0, min(1.0, float(settings.get("face_identity_blend_strength", 0.0))))
    local_chroma_guard_enabled = bool(settings.get("local_chroma_guard_enabled"))
    local_chroma_max_delta = max(0.0, float(settings.get("local_chroma_max_delta", 0.0)))
    local_chroma_blend_strength = max(0.0, min(1.0, float(settings.get("local_chroma_blend_strength", 0.0))))
    local_chroma_tile_rows = max(1, int(settings.get("local_chroma_tile_rows") or 1))
    local_chroma_tile_cols = max(1, int(settings.get("local_chroma_tile_cols") or 1))
    local_texture_guard_enabled = bool(settings.get("local_texture_guard_enabled"))
    local_texture_min_loss = max(0.0, float(settings.get("local_texture_min_loss", 0.0)))
    local_texture_blend_strength = max(0.0, min(1.0, float(settings.get("local_texture_blend_strength", 0.0))))
    local_texture_tile_rows = max(1, int(settings.get("local_texture_tile_rows") or 1))
    local_texture_tile_cols = max(1, int(settings.get("local_texture_tile_cols") or 1))
    lip_feature_guard_enabled = bool(settings.get("lip_feature_guard_enabled"))
    lip_feature_max_delta = max(0.0, float(settings.get("lip_feature_max_delta", 0.0)))
    lip_feature_blend_strength = max(0.0, min(1.0, float(settings.get("lip_feature_blend_strength", 0.0))))
    edge_halo_guard_enabled = bool(settings.get("edge_halo_guard_enabled"))
    edge_halo_min_delta = max(0.0, float(settings.get("edge_halo_min_delta", 0.0)))
    edge_halo_blend_strength = max(0.0, min(1.0, float(settings.get("edge_halo_blend_strength", 0.0))))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(generated_path) as raw:
        base = ImageOps.exif_transpose(raw).convert("RGB")
    reference: Image.Image | None = None
    if reference_path and reference_path.is_file():
        with Image.open(reference_path) as reference_raw:
            reference = ImageOps.exif_transpose(reference_raw).convert("RGB")
            if reference.size != base.size:
                reference = reference.resize(base.size, Image.Resampling.BILINEAR)
    if mask_path and mask_path.is_file():
        with Image.open(mask_path) as mask_raw:
            mask = ImageOps.exif_transpose(mask_raw).convert("L")
            if mask.size != base.size:
                mask = mask.resize(base.size, Image.Resampling.BILINEAR)
    else:
        mask = Image.new("L", base.size, 255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=max(0.0, min(base.size) / 320)))

    def clamp_delta(candidate: Image.Image, reference: Image.Image, delta: int) -> Image.Image:
        if delta <= 0:
            return candidate
        upper = reference.point(lambda value: min(255, int(value + delta)))
        lower = reference.point(lambda value: max(0, int(value - delta)))
        return ImageChops.lighter(ImageChops.darker(candidate, upper), lower)

    def clamp_channel_delta(value: float) -> int:
        if max_chroma_shift_delta <= 0:
            return 0
        return int(round(max(-max_chroma_shift_delta, min(max_chroma_shift_delta, value))))

    def masked_channel_mean(image: Image.Image, channel_index: int, active_mask: Image.Image) -> float:
        channel = image.convert("YCbCr").split()[channel_index]
        count = sum(active_mask.histogram()[1:])
        if count <= 0:
            return 0.0
        return float(ImageStat.Stat(channel, active_mask).mean[0])

    def apply_chroma_guard(luma_source: Image.Image) -> tuple[Image.Image, dict[str, float | bool]]:
        if not preserve_chroma:
            return luma_source, {
                "preserve_chroma": False,
                "chroma_cb_delta": 0.0,
                "chroma_cr_delta": 0.0,
            }
        y = luma_source.convert("YCbCr").split()[0]
        _, cb, cr = base.convert("YCbCr").split()
        chroma_target = reference or base
        cb_delta = 0
        cr_delta = 0
        if reference_chroma_match_strength > 0 and reference is not None:
            cb_delta = clamp_channel_delta(
                (masked_channel_mean(chroma_target, 1, mask) - masked_channel_mean(base, 1, mask))
                * reference_chroma_match_strength
            )
            cr_delta = clamp_channel_delta(
                (masked_channel_mean(chroma_target, 2, mask) - masked_channel_mean(base, 2, mask))
                * reference_chroma_match_strength
            )
        if cb_delta:
            cb = cb.point(lambda value: max(0, min(255, int(value + cb_delta))))
        if cr_delta:
            cr = cr.point(lambda value: max(0, min(255, int(value + cr_delta))))
        guarded = Image.merge("YCbCr", (y, cb, cr)).convert("RGB")
        guarded = Image.composite(guarded, luma_source, mask)
        return guarded, {
            "preserve_chroma": True,
            "chroma_cb_delta": float(cb_delta),
            "chroma_cr_delta": float(cr_delta),
        }

    canvas = base
    if global_luma_lift > 1.0:
        canvas = ImageEnhance.Brightness(base).enhance(global_luma_lift)
        canvas = clamp_delta(canvas, base, global_max_delta)

    enhanced = ImageEnhance.Brightness(canvas).enhance(midtone_lift)
    enhanced = ImageEnhance.Contrast(enhanced).enhance(local_contrast)
    if highlight_lift > 1.0:
        highlight = enhanced.point(
            lambda value: min(255, int(round(value * highlight_lift))) if value >= 150 else value
        )
        enhanced = Image.blend(enhanced, highlight, 0.45)
    active_shadow_mask: Image.Image | None = None
    if shadow_lift > 1.0 or shadow_detail_contrast > 1.0:
        gray = base.convert("L")
        threshold = max(1, min(255, shadow_threshold))
        shadow_mask = gray.point(
            lambda value: int(round(255 * (1 - (value / threshold)))) if value < threshold else 0
        )
        active_shadow_mask = ImageChops.multiply(shadow_mask, mask)
        shadow_lifted = ImageEnhance.Brightness(enhanced).enhance(shadow_lift)
        shadow_detail = ImageEnhance.Contrast(shadow_lifted).enhance(shadow_detail_contrast)
        shadow_detail = ImageEnhance.Sharpness(shadow_detail).enhance(max(detail_sharpness, shadow_detail_contrast))
        shadow_detail = ImageChops.lighter(shadow_detail, shadow_lifted)
        enhanced = Image.composite(shadow_detail, enhanced, active_shadow_mask)
    enhanced = ImageEnhance.Sharpness(enhanced).enhance(detail_sharpness)
    enhanced = clamp_delta(enhanced, base, max_delta)

    merged = Image.composite(enhanced, canvas, mask)
    merged, chroma_report = apply_chroma_guard(merged)
    reference_luma_floor_applied = False
    reference_luma_floor_target = 0.0
    reference_luma_floor_lift = 0.0
    if reference is not None and reference_luma_floor_max_lift > 0 and sum(mask.histogram()[1:]) > 0:
        reference_luma = ImageStat.Stat(reference.convert("L"), mask).mean[0]
        current_luma = ImageStat.Stat(merged.convert("L"), mask).mean[0]
        reference_luma_floor_target = reference_luma + reference_luma_floor_delta
        if current_luma < reference_luma_floor_target:
            reference_luma_floor_lift = min(reference_luma_floor_max_lift, reference_luma_floor_target - current_luma)
            lift_ratio = (current_luma + reference_luma_floor_lift) / max(1.0, current_luma)
            lifted = ImageEnhance.Brightness(merged).enhance(lift_ratio)
            lifted = clamp_delta(lifted, merged, int(round(reference_luma_floor_lift + 2)))
            merged = Image.composite(lifted, merged, mask)
            reference_luma_floor_applied = True
    shadow_contrast_guard_applied = False
    shadow_floor_lift = 0.0
    masked_shadow_p10_reference = 0.0
    masked_shadow_p10_after = 0.0
    masked_shadow_p10_delta = 0.0
    masked_shadow_contrast_reference = 0.0
    masked_shadow_contrast_after = 0.0
    masked_shadow_contrast_delta = 0.0
    highlight_guard_applied = False
    highlight_guard_darken = 0.0
    masked_highlight_p95_reference = 0.0
    masked_highlight_p95_after = 0.0
    masked_highlight_p95_delta = 0.0
    masked_highlight_p99_reference = 0.0
    masked_highlight_p99_after = 0.0
    masked_highlight_p99_delta = 0.0
    masked_specular_ratio_reference = 0.0
    masked_specular_ratio_after = 0.0
    masked_specular_ratio_delta = 0.0
    reference_blend_applied = False
    face_tone_guard_applied = False
    background_preserve_applied = False
    feature_protect_applied = False
    feature_protect_delta_before = 0.0
    feature_protect_context_delta = 0.0
    feature_protect_excess_delta = 0.0
    face_chroma_guard_applied = False
    face_chroma_delta_before = 0.0
    face_chroma_delta_after = 0.0
    face_texture_guard_applied = False
    face_texture_reference = 0.0
    face_texture_before_guard = 0.0
    face_texture_after_guard = 0.0
    face_identity_guard_applied = False
    face_identity_delta_before = 0.0
    face_identity_delta_after = 0.0
    local_chroma_guard_applied = False
    local_chroma_delta_before = 0.0
    local_chroma_delta_after = 0.0
    local_chroma_tiles_applied = 0
    local_texture_guard_applied = False
    local_texture_loss_before = 0.0
    local_texture_loss_after = 0.0
    local_texture_tiles_applied = 0
    lip_feature_guard_applied = False
    lip_feature_delta_before = 0.0
    lip_feature_delta_after = 0.0
    edge_halo_guard_applied = False
    edge_halo_delta_before = 0.0
    edge_halo_delta_after = 0.0
    face_luma_lift = 0.0
    face_luma_input = 0.0
    face_luma_reference = 0.0
    face_luma_after = 0.0
    face_luma_delta = 0.0
    face_background_contrast_input = 0.0
    face_background_contrast_reference = 0.0
    face_background_contrast_after = 0.0
    face_background_contrast_delta = 0.0
    if reference is not None and max_shadow_contrast_delta > 0 and sum(mask.histogram()[1:]) > 0:
        reference_gray = reference.convert("L")
        merged_gray = merged.convert("L")
        reference_p10 = _masked_luma_percentile(reference_gray, mask, 10)
        reference_p50 = _masked_luma_percentile(reference_gray, mask, 50)
        merged_p10 = _masked_luma_percentile(merged_gray, mask, 10)
        merged_p50 = _masked_luma_percentile(merged_gray, mask, 50)
        reference_gap = reference_p50 - reference_p10
        current_gap = merged_p50 - merged_p10
        target_gap = max(0.0, reference_gap + max_shadow_contrast_delta - 6.0)
        if shadow_floor_lift_max > 0 and (current_gap > target_gap or merged_p10 < reference_p10):
            target_p10 = max(reference_p10, merged_p50 - target_gap)
            shadow_floor_lift = min(shadow_floor_lift_max, max(0.0, target_p10 - merged_p10))
            if shadow_floor_lift > 0:
                floor_threshold = int(round(max(0.0, min(255.0, target_p10))))
                dark_mask = merged_gray.point(lambda value: 255 if value <= floor_threshold else 0)
                active_floor_mask = ImageChops.multiply(dark_mask, mask)
                lifted = merged.point(lambda value: max(0, min(255, int(round(value + shadow_floor_lift)))))
                merged = Image.composite(lifted, merged, active_floor_mask)
                shadow_contrast_guard_applied = True
                merged_gray = merged.convert("L")
                merged_p10 = _masked_luma_percentile(merged_gray, mask, 10)
                merged_p50 = _masked_luma_percentile(merged_gray, mask, 50)
                current_gap = merged_p50 - merged_p10
        masked_shadow_p10_reference = reference_p10
        masked_shadow_p10_after = merged_p10
        masked_shadow_p10_delta = merged_p10 - reference_p10
        masked_shadow_contrast_reference = reference_gap
        masked_shadow_contrast_after = current_gap
        masked_shadow_contrast_delta = current_gap - reference_gap
    has_mask_pixels = sum(mask.histogram()[1:]) > 0
    if (
        reference is not None
        and has_mask_pixels
        and (max_highlight_p95_delta > 0 or max_highlight_p99_delta > 0 or max_specular_ratio_delta > 0)
    ):
        reference_gray = reference.convert("L")
        merged_gray = merged.convert("L")
        reference_p95 = _masked_luma_percentile(reference_gray, mask, 95)
        reference_p99 = _masked_luma_percentile(reference_gray, mask, 99)
        merged_p95 = _masked_luma_percentile(merged_gray, mask, 95)
        merged_p99 = _masked_luma_percentile(merged_gray, mask, 99)
        reference_specular_ratio = _masked_luma_ratio_at_or_above(reference_gray, mask, specular_threshold)
        merged_specular_ratio = _masked_luma_ratio_at_or_above(merged_gray, mask, specular_threshold)
        target_p95 = min(255.0, reference_p95 + max_highlight_p95_delta) if max_highlight_p95_delta > 0 else 255.0
        target_p99 = min(255.0, reference_p99 + max_highlight_p99_delta) if max_highlight_p99_delta > 0 else 255.0
        p95_excess = max(0.0, merged_p95 - target_p95) if max_highlight_p95_delta > 0 else 0.0
        p99_excess = max(0.0, merged_p99 - target_p99) if max_highlight_p99_delta > 0 else 0.0
        specular_excess = (
            max(0.0, merged_specular_ratio - reference_specular_ratio - max_specular_ratio_delta)
            if max_specular_ratio_delta > 0
            else 0.0
        )
        if highlight_guard_max_darken > 0 and (p95_excess > 0 or p99_excess > 0 or specular_excess > 0):
            target_luma = 255.0
            peak_luma = 0.0
            if p95_excess > 0:
                target_luma = min(target_luma, target_p95)
                peak_luma = max(peak_luma, merged_p95)
            if p99_excess > 0:
                target_luma = min(target_luma, target_p99)
                peak_luma = max(peak_luma, merged_p99)
            if specular_excess > 0:
                target_luma = min(target_luma, specular_threshold - 1)
                peak_luma = max(peak_luma, specular_threshold)
            target_luma = max(target_luma, peak_luma - highlight_guard_max_darken)
            target_luma = max(0.0, min(255.0, target_luma))
            highlight_mask = ImageChops.multiply(
                merged_gray.point(lambda value: 255 if value > target_luma else 0),
                mask,
            )
            if sum(highlight_mask.histogram()[1:]) > 0:
                y, cb, cr = merged.convert("YCbCr").split()
                clamp_to = int(round(target_luma))
                highlight_guard_darken = min(highlight_guard_max_darken, max(0.0, peak_luma - target_luma))
                clamped_y = y.point(lambda value: min(value, clamp_to))
                clamped = Image.merge("YCbCr", (clamped_y, cb, cr)).convert("RGB")
                merged = Image.composite(clamped, merged, highlight_mask)
                highlight_guard_applied = True
                merged_gray = merged.convert("L")
                merged_p95 = _masked_luma_percentile(merged_gray, mask, 95)
                merged_p99 = _masked_luma_percentile(merged_gray, mask, 99)
                merged_specular_ratio = _masked_luma_ratio_at_or_above(merged_gray, mask, specular_threshold)
    if reference is not None and reference_blend_strength > 0 and has_mask_pixels:
        blended = Image.blend(merged, reference, reference_blend_strength)
        merged = Image.composite(blended, merged, mask)
        reference_blend_applied = True
    if reference is not None and face_tone_guard_enabled and face_luma_max_lift > 0:
        face_mask = _portrait_face_tone_mask(merged.size)
        background_mask = _portrait_background_tone_mask(merged.size)
        if sum(face_mask.histogram()[1:]) > 0 and sum(background_mask.histogram()[1:]) > 0:
            reference_gray = reference.convert("L")
            merged_gray = merged.convert("L")
            base_gray = base.convert("L")
            base_face_luma = ImageStat.Stat(base_gray, face_mask).mean[0]
            base_background_luma = ImageStat.Stat(base_gray, background_mask).mean[0]
            reference_face_luma = ImageStat.Stat(reference_gray, face_mask).mean[0]
            current_face_luma = ImageStat.Stat(merged_gray, face_mask).mean[0]
            reference_background_luma = ImageStat.Stat(reference_gray, background_mask).mean[0]
            current_background_luma = ImageStat.Stat(merged_gray, background_mask).mean[0]
            base_face_contrast = base_face_luma - base_background_luma
            reference_face_contrast = reference_face_luma - reference_background_luma
            current_face_contrast = current_face_luma - current_background_luma
            target_face_luma = reference_face_luma + face_luma_target_delta
            target_face_contrast = reference_face_contrast + face_background_contrast_target_delta
            required_lift = max(0.0, target_face_luma - current_face_luma, target_face_contrast - current_face_contrast)
            allowed_lift = min(face_luma_max_lift, required_lift)
            if face_contrast_max_lift > 0:
                contrast_needed = max(0.0, target_face_contrast - current_face_contrast)
                allowed_lift = min(allowed_lift, face_luma_max_lift, contrast_needed + face_contrast_max_lift)
            if allowed_lift > 0.1:
                y, cb, cr = merged.convert("YCbCr").split()
                lift = int(round(allowed_lift))
                threshold = max(1.0, min(255.0, face_tone_highlight_protect_threshold))
                rolloff = 48.0
                lifted_y = y.point(
                    lambda value: max(
                        0,
                        min(
                            255,
                            int(round(value + lift * max(0.0, min(1.0, (threshold - value) / rolloff)))),
                        ),
                    )
                    if value < threshold
                    else value
                )
                lifted = Image.merge("YCbCr", (lifted_y, cb, cr)).convert("RGB")
                merged = Image.composite(lifted, merged, face_mask)
                face_tone_guard_applied = True
                face_luma_lift = float(lift)
                merged_gray = merged.convert("L")
                current_face_luma = ImageStat.Stat(merged_gray, face_mask).mean[0]
                current_background_luma = ImageStat.Stat(merged_gray, background_mask).mean[0]
                current_face_contrast = current_face_luma - current_background_luma
            face_luma_input = base_face_luma
            face_luma_reference = reference_face_luma
            face_luma_after = current_face_luma
            face_luma_delta = current_face_luma - base_face_luma
            face_background_contrast_input = base_face_contrast
            face_background_contrast_reference = reference_face_contrast
            face_background_contrast_after = current_face_contrast
            face_background_contrast_delta = current_face_contrast - base_face_contrast
    if (
        reference is not None
        and has_mask_pixels
        and highlight_guard_max_darken > 0
        and (max_highlight_p95_delta > 0 or max_highlight_p99_delta > 0 or max_specular_ratio_delta > 0)
    ):
        reference_gray = reference.convert("L")
        merged_gray = merged.convert("L")
        reference_p95 = _masked_luma_percentile(reference_gray, mask, 95)
        reference_p99 = _masked_luma_percentile(reference_gray, mask, 99)
        merged_p95 = _masked_luma_percentile(merged_gray, mask, 95)
        merged_p99 = _masked_luma_percentile(merged_gray, mask, 99)
        reference_specular_ratio = _masked_luma_ratio_at_or_above(reference_gray, mask, specular_threshold)
        merged_specular_ratio = _masked_luma_ratio_at_or_above(merged_gray, mask, specular_threshold)
        target_p95 = min(255.0, reference_p95 + max_highlight_p95_delta) if max_highlight_p95_delta > 0 else 255.0
        target_p99 = min(255.0, reference_p99 + max_highlight_p99_delta) if max_highlight_p99_delta > 0 else 255.0
        p95_excess = max(0.0, merged_p95 - target_p95) if max_highlight_p95_delta > 0 else 0.0
        p99_excess = max(0.0, merged_p99 - target_p99) if max_highlight_p99_delta > 0 else 0.0
        specular_excess = (
            max(0.0, merged_specular_ratio - reference_specular_ratio - max_specular_ratio_delta)
            if max_specular_ratio_delta > 0
            else 0.0
        )
        if p95_excess > 0 or p99_excess > 0 or specular_excess > 0:
            target_luma = 255.0
            peak_luma = 0.0
            if p95_excess > 0:
                target_luma = min(target_luma, target_p95)
                peak_luma = max(peak_luma, merged_p95)
            if p99_excess > 0:
                target_luma = min(target_luma, target_p99)
                peak_luma = max(peak_luma, merged_p99)
            if specular_excess > 0:
                target_luma = min(target_luma, specular_threshold - 1)
                peak_luma = max(peak_luma, specular_threshold)
            target_luma = max(0.0, min(255.0, max(target_luma, peak_luma - highlight_guard_max_darken)))
            highlight_mask = ImageChops.multiply(
                merged_gray.point(lambda value: 255 if value > target_luma else 0),
                mask,
            )
            if sum(highlight_mask.histogram()[1:]) > 0:
                y, cb, cr = merged.convert("YCbCr").split()
                clamp_to = int(round(target_luma))
                clamped_y = y.point(lambda value: min(value, clamp_to))
                clamped = Image.merge("YCbCr", (clamped_y, cb, cr)).convert("RGB")
                merged = Image.composite(clamped, merged, highlight_mask)
                highlight_guard_applied = True
                highlight_guard_darken = max(
                    highlight_guard_darken,
                    min(highlight_guard_max_darken, max(0.0, peak_luma - target_luma)),
                )
    if reference is not None and has_mask_pixels:
        face_guard_mask = ImageChops.multiply(_portrait_face_tone_mask(merged.size), mask)
        if sum(face_guard_mask.histogram()[1:]) > 0:
            def mask_count(active_mask: Image.Image) -> int:
                return sum(active_mask.histogram()[1:])

            def color_offsets(image: Image.Image, active_mask: Image.Image) -> tuple[float, float]:
                rgb = ImageStat.Stat(image, active_mask).mean
                red_green = float(rgb[0] - rgb[1])
                blue_yellow = float(rgb[2] - ((rgb[0] + rgb[1]) / 2))
                return red_green, blue_yellow

            def chroma_delta(image: Image.Image, active_mask: Image.Image) -> float:
                reference_red_green, reference_blue_yellow = color_offsets(reference, active_mask)
                current_red_green, current_blue_yellow = color_offsets(image, active_mask)
                return max(
                    abs(current_red_green - reference_red_green),
                    abs(current_blue_yellow - reference_blue_yellow),
                )

            def tiled_masks(active_mask: Image.Image, rows: int, cols: int) -> list[Image.Image]:
                from PIL import ImageDraw

                width, height = active_mask.size
                masks: list[Image.Image] = []
                for row in range(rows):
                    top = int(round(height * row / rows))
                    bottom = int(round(height * (row + 1) / rows))
                    for col in range(cols):
                        left = int(round(width * col / cols))
                        right = int(round(width * (col + 1) / cols))
                        tile = Image.new("L", active_mask.size, 0)
                        ImageDraw.Draw(tile).rectangle((left, top, max(left, right - 1), max(top, bottom - 1)), fill=255)
                        active_tile = ImageChops.multiply(active_mask, tile)
                        if mask_count(active_tile) >= 12:
                            masks.append(active_tile)
                return masks

            if local_chroma_guard_enabled and local_chroma_blend_strength > 0 and local_chroma_max_delta > 0:
                local_chroma_delta_before = 0.0
                candidate_masks: list[Image.Image] = []
                for tile_mask in tiled_masks(face_guard_mask, local_chroma_tile_rows, local_chroma_tile_cols):
                    tile_delta = chroma_delta(merged, tile_mask)
                    local_chroma_delta_before = max(local_chroma_delta_before, tile_delta)
                    if tile_delta > local_chroma_max_delta:
                        candidate_masks.append(tile_mask)
                for tile_mask in candidate_masks:
                    recovered = Image.blend(merged, reference, local_chroma_blend_strength)
                    merged = Image.composite(recovered, merged, tile_mask)
                local_chroma_tiles_applied = len(candidate_masks)
                local_chroma_guard_applied = local_chroma_tiles_applied > 0
                local_chroma_delta_after = 0.0
                for tile_mask in tiled_masks(face_guard_mask, local_chroma_tile_rows, local_chroma_tile_cols):
                    local_chroma_delta_after = max(local_chroma_delta_after, chroma_delta(merged, tile_mask))

            reference_red_green, reference_blue_yellow = color_offsets(reference, face_guard_mask)
            current_red_green, current_blue_yellow = color_offsets(merged, face_guard_mask)
            face_chroma_delta_before = max(
                abs(current_red_green - reference_red_green),
                abs(current_blue_yellow - reference_blue_yellow),
            )
            if (
                face_chroma_guard_enabled
                and face_chroma_blend_strength > 0
                and face_chroma_delta_before > face_chroma_max_delta
            ):
                chroma_recovered = Image.blend(merged, reference, face_chroma_blend_strength)
                merged = Image.composite(chroma_recovered, merged, face_guard_mask)
                face_chroma_guard_applied = True
                current_red_green, current_blue_yellow = color_offsets(merged, face_guard_mask)
            face_chroma_delta_after = max(
                abs(current_red_green - reference_red_green),
                abs(current_blue_yellow - reference_blue_yellow),
            )

            reference_gray = reference.convert("L")
            merged_gray = merged.convert("L")
            if local_texture_guard_enabled and local_texture_blend_strength > 0:
                texture_masks: list[Image.Image] = []
                for tile_mask in tiled_masks(face_guard_mask, local_texture_tile_rows, local_texture_tile_cols):
                    reference_texture = float(ImageStat.Stat(reference_gray, tile_mask).stddev[0])
                    current_texture = float(ImageStat.Stat(merged_gray, tile_mask).stddev[0])
                    texture_loss = reference_texture - current_texture
                    local_texture_loss_before = max(local_texture_loss_before, texture_loss)
                    if texture_loss >= local_texture_min_loss:
                        texture_masks.append(tile_mask)
                for tile_mask in texture_masks:
                    recovered = Image.blend(merged, reference, local_texture_blend_strength)
                    merged = Image.composite(recovered, merged, tile_mask)
                local_texture_tiles_applied = len(texture_masks)
                local_texture_guard_applied = local_texture_tiles_applied > 0
                merged_gray = merged.convert("L")
                for tile_mask in tiled_masks(face_guard_mask, local_texture_tile_rows, local_texture_tile_cols):
                    reference_texture = float(ImageStat.Stat(reference_gray, tile_mask).stddev[0])
                    current_texture = float(ImageStat.Stat(merged_gray, tile_mask).stddev[0])
                    local_texture_loss_after = max(local_texture_loss_after, reference_texture - current_texture)

            face_texture_reference = float(ImageStat.Stat(reference_gray, face_guard_mask).stddev[0])
            face_texture_before_guard = float(ImageStat.Stat(merged_gray, face_guard_mask).stddev[0])
            texture_loss = face_texture_reference - face_texture_before_guard
            if (
                face_texture_guard_enabled
                and face_texture_blend_strength > 0
                and texture_loss >= face_texture_min_loss
            ):
                texture_recovered = Image.blend(merged, reference, face_texture_blend_strength)
                merged = Image.composite(texture_recovered, merged, face_guard_mask)
                face_texture_guard_applied = True
                merged_gray = merged.convert("L")
            face_texture_after_guard = float(ImageStat.Stat(merged_gray, face_guard_mask).stddev[0])

            if lip_feature_guard_enabled and lip_feature_blend_strength > 0:
                lip_mask = ImageChops.multiply(_portrait_lip_feature_mask(merged.size), mask)
                if mask_count(lip_mask) > 0:
                    lip_diff = ImageChops.difference(merged, reference).convert("L")
                    lip_feature_delta_before = float(ImageStat.Stat(lip_diff, lip_mask).mean[0])
                    if lip_feature_delta_before >= lip_feature_max_delta:
                        lip_recovered = Image.blend(merged, reference, lip_feature_blend_strength)
                        merged = Image.composite(lip_recovered, merged, lip_mask)
                        lip_feature_guard_applied = True
                        lip_diff = ImageChops.difference(merged, reference).convert("L")
                    lip_feature_delta_after = float(ImageStat.Stat(lip_diff, lip_mask).mean[0])

            if edge_halo_guard_enabled and edge_halo_blend_strength > 0:
                edge_halo_mask = ImageChops.multiply(_portrait_edge_halo_guard_mask(merged.size), mask)
                if mask_count(edge_halo_mask) > 0:
                    halo_diff = ImageChops.difference(merged, reference).convert("L")
                    edge_halo_delta_before = float(ImageStat.Stat(halo_diff, edge_halo_mask).mean[0])
                    if edge_halo_delta_before >= edge_halo_min_delta:
                        halo_recovered = Image.blend(merged, reference, edge_halo_blend_strength)
                        merged = Image.composite(halo_recovered, merged, edge_halo_mask)
                        edge_halo_guard_applied = True
                        halo_diff = ImageChops.difference(merged, reference).convert("L")
                    edge_halo_delta_after = float(ImageStat.Stat(halo_diff, edge_halo_mask).mean[0])

            face_diff = ImageChops.difference(merged, reference).convert("L")
            face_identity_delta_before = float(ImageStat.Stat(face_diff, face_guard_mask).mean[0])
            if (
                face_identity_guard_enabled
                and face_identity_blend_strength > 0
                and face_identity_delta_before >= face_identity_min_delta
            ):
                identity_recovered = Image.blend(merged, reference, face_identity_blend_strength)
                merged = Image.composite(identity_recovered, merged, face_guard_mask)
                face_identity_guard_applied = True
                face_diff = ImageChops.difference(merged, reference).convert("L")
            face_identity_delta_after = float(ImageStat.Stat(face_diff, face_guard_mask).mean[0])
    if reference is not None and has_mask_pixels and semantic_fidelity_guard_enabled:
        if background_preserve_blend_strength > 0:
            face_edit_mask = _portrait_face_tone_mask(merged.size)
            preserve_mask = ImageChops.multiply(ImageChops.invert(face_edit_mask), mask)
            if sum(preserve_mask.histogram()[1:]) > 0:
                preserved = Image.blend(merged, reference, background_preserve_blend_strength)
                merged = Image.composite(preserved, merged, preserve_mask)
                background_preserve_applied = True
        if feature_protect_blend_strength > 0:
            feature_mask = ImageChops.multiply(_portrait_feature_protect_mask(merged.size), mask)
            if sum(feature_mask.histogram()[1:]) > 0:
                feature_diff = ImageChops.difference(merged, reference).convert("L")
                feature_protect_delta_before = float(ImageStat.Stat(feature_diff, feature_mask).mean[0])
                context_mask = ImageChops.multiply(
                    ImageChops.multiply(_portrait_face_tone_mask(merged.size), ImageChops.invert(feature_mask)),
                    mask,
                )
                if sum(context_mask.histogram()[1:]) > 0:
                    feature_protect_context_delta = float(ImageStat.Stat(feature_diff, context_mask).mean[0])
                feature_protect_excess_delta = feature_protect_delta_before - feature_protect_context_delta
                if (
                    feature_protect_delta_before >= feature_protect_min_delta
                    and feature_protect_excess_delta >= feature_protect_min_excess_delta
                ):
                    protected = Image.blend(merged, reference, feature_protect_blend_strength)
                    merged = Image.composite(protected, merged, feature_mask)
                    feature_protect_applied = True
    if reference is not None and has_mask_pixels:
        reference_gray = reference.convert("L")
        merged_gray = merged.convert("L")
        if max_shadow_contrast_delta > 0:
            reference_p10 = _masked_luma_percentile(reference_gray, mask, 10)
            reference_p50 = _masked_luma_percentile(reference_gray, mask, 50)
            merged_p10 = _masked_luma_percentile(merged_gray, mask, 10)
            merged_p50 = _masked_luma_percentile(merged_gray, mask, 50)
            masked_shadow_p10_reference = reference_p10
            masked_shadow_p10_after = merged_p10
            masked_shadow_p10_delta = merged_p10 - reference_p10
            masked_shadow_contrast_reference = reference_p50 - reference_p10
            masked_shadow_contrast_after = merged_p50 - merged_p10
            masked_shadow_contrast_delta = masked_shadow_contrast_after - masked_shadow_contrast_reference
        if max_highlight_p95_delta > 0 or max_highlight_p99_delta > 0 or max_specular_ratio_delta > 0:
            reference_p95 = _masked_luma_percentile(reference_gray, mask, 95)
            reference_p99 = _masked_luma_percentile(reference_gray, mask, 99)
            merged_p95 = _masked_luma_percentile(merged_gray, mask, 95)
            merged_p99 = _masked_luma_percentile(merged_gray, mask, 99)
            reference_specular_ratio = _masked_luma_ratio_at_or_above(reference_gray, mask, specular_threshold)
            merged_specular_ratio = _masked_luma_ratio_at_or_above(merged_gray, mask, specular_threshold)
            masked_highlight_p95_reference = reference_p95
            masked_highlight_p95_after = merged_p95
            masked_highlight_p95_delta = merged_p95 - reference_p95
            masked_highlight_p99_reference = reference_p99
            masked_highlight_p99_after = merged_p99
            masked_highlight_p99_delta = merged_p99 - reference_p99
            masked_specular_ratio_reference = reference_specular_ratio
            masked_specular_ratio_after = merged_specular_ratio
            masked_specular_ratio_delta = merged_specular_ratio - reference_specular_ratio
    merged.save(output_path)
    before_mean = ImageStat.Stat(base.convert("L"), mask).mean[0]
    after_mean = ImageStat.Stat(merged.convert("L"), mask).mean[0]
    global_before_mean = ImageStat.Stat(base.convert("L")).mean[0]
    global_canvas_mean = ImageStat.Stat(canvas.convert("L")).mean[0]
    shadow_before_mean = 0.0
    shadow_after_mean = 0.0
    if active_shadow_mask is not None and sum(active_shadow_mask.histogram()[1:]) > 0:
        shadow_before_mean = ImageStat.Stat(base.convert("L"), active_shadow_mask).mean[0]
        shadow_after_mean = ImageStat.Stat(merged.convert("L"), active_shadow_mask).mean[0]
    return {
        "applied": True,
        "strategy": strategy,
        "candidate_only": bool(settings.get("candidate_only", True)),
        "mask_mode": str(settings.get("mask_mode") or "focus_mask_feathered"),
        "midtone_lift": midtone_lift,
        "highlight_lift": highlight_lift,
        "local_contrast": local_contrast,
        "detail_sharpness": detail_sharpness,
        "shadow_lift": shadow_lift,
        "shadow_detail_contrast": shadow_detail_contrast,
        "shadow_threshold": shadow_threshold,
        "global_luma_lift": global_luma_lift,
        "global_max_delta": global_max_delta,
        "max_delta": max_delta,
        "preserve_chroma": bool(chroma_report["preserve_chroma"]),
        "reference_chroma_match_strength": reference_chroma_match_strength,
        "max_chroma_shift_delta": max_chroma_shift_delta,
        "chroma_cb_delta": chroma_report["chroma_cb_delta"],
        "chroma_cr_delta": chroma_report["chroma_cr_delta"],
        "reference_luma_floor_delta": reference_luma_floor_delta,
        "reference_luma_floor_max_lift": reference_luma_floor_max_lift,
        "reference_luma_floor_target": round(reference_luma_floor_target, 3),
        "reference_luma_floor_lift": round(reference_luma_floor_lift, 3),
        "reference_luma_floor_applied": reference_luma_floor_applied,
        "max_shadow_contrast_delta": max_shadow_contrast_delta,
        "shadow_floor_lift_max": shadow_floor_lift_max,
        "shadow_contrast_guard_applied": shadow_contrast_guard_applied,
        "shadow_floor_lift": round(shadow_floor_lift, 3),
        "masked_shadow_p10_reference": round(masked_shadow_p10_reference, 3),
        "masked_shadow_p10_after": round(masked_shadow_p10_after, 3),
        "masked_shadow_p10_delta": round(masked_shadow_p10_delta, 3),
        "masked_shadow_contrast_reference": round(masked_shadow_contrast_reference, 3),
        "masked_shadow_contrast_after": round(masked_shadow_contrast_after, 3),
        "masked_shadow_contrast_delta": round(masked_shadow_contrast_delta, 3),
        "max_highlight_p95_delta": max_highlight_p95_delta,
        "max_highlight_p99_delta": max_highlight_p99_delta,
        "highlight_guard_max_darken": highlight_guard_max_darken,
        "specular_threshold": specular_threshold,
        "max_specular_ratio_delta": max_specular_ratio_delta,
        "reference_blend_strength": reference_blend_strength,
        "reference_blend_applied": reference_blend_applied,
        "face_tone_guard_enabled": face_tone_guard_enabled,
        "face_luma_target_delta": face_luma_target_delta,
        "face_luma_max_lift": face_luma_max_lift,
        "face_background_contrast_target_delta": face_background_contrast_target_delta,
        "face_contrast_max_lift": face_contrast_max_lift,
        "face_tone_highlight_protect_threshold": face_tone_highlight_protect_threshold,
        "face_tone_guard_applied": face_tone_guard_applied,
        "semantic_fidelity_guard_enabled": semantic_fidelity_guard_enabled,
        "background_preserve_blend_strength": background_preserve_blend_strength,
        "background_preserve_applied": background_preserve_applied,
        "feature_protect_blend_strength": feature_protect_blend_strength,
        "feature_protect_min_delta": feature_protect_min_delta,
        "feature_protect_min_excess_delta": feature_protect_min_excess_delta,
        "feature_protect_delta_before": round(feature_protect_delta_before, 3),
        "feature_protect_context_delta": round(feature_protect_context_delta, 3),
        "feature_protect_excess_delta": round(feature_protect_excess_delta, 3),
        "feature_protect_applied": feature_protect_applied,
        "face_chroma_guard_enabled": face_chroma_guard_enabled,
        "face_chroma_max_delta": face_chroma_max_delta,
        "face_chroma_blend_strength": face_chroma_blend_strength,
        "face_chroma_delta_before": round(face_chroma_delta_before, 3),
        "face_chroma_delta_after": round(face_chroma_delta_after, 3),
        "face_chroma_guard_applied": face_chroma_guard_applied,
        "face_texture_guard_enabled": face_texture_guard_enabled,
        "face_texture_min_loss": face_texture_min_loss,
        "face_texture_blend_strength": face_texture_blend_strength,
        "face_texture_reference": round(face_texture_reference, 3),
        "face_texture_before_guard": round(face_texture_before_guard, 3),
        "face_texture_after_guard": round(face_texture_after_guard, 3),
        "face_texture_guard_applied": face_texture_guard_applied,
        "face_identity_guard_enabled": face_identity_guard_enabled,
        "face_identity_min_delta": face_identity_min_delta,
        "face_identity_blend_strength": face_identity_blend_strength,
        "face_identity_delta_before": round(face_identity_delta_before, 3),
        "face_identity_delta_after": round(face_identity_delta_after, 3),
        "face_identity_guard_applied": face_identity_guard_applied,
        "local_chroma_guard_enabled": local_chroma_guard_enabled,
        "local_chroma_max_delta": local_chroma_max_delta,
        "local_chroma_blend_strength": local_chroma_blend_strength,
        "local_chroma_tile_rows": local_chroma_tile_rows,
        "local_chroma_tile_cols": local_chroma_tile_cols,
        "local_chroma_delta_before": round(local_chroma_delta_before, 3),
        "local_chroma_delta_after": round(local_chroma_delta_after, 3),
        "local_chroma_tiles_applied": local_chroma_tiles_applied,
        "local_chroma_guard_applied": local_chroma_guard_applied,
        "local_texture_guard_enabled": local_texture_guard_enabled,
        "local_texture_min_loss": local_texture_min_loss,
        "local_texture_blend_strength": local_texture_blend_strength,
        "local_texture_tile_rows": local_texture_tile_rows,
        "local_texture_tile_cols": local_texture_tile_cols,
        "local_texture_loss_before": round(local_texture_loss_before, 3),
        "local_texture_loss_after": round(local_texture_loss_after, 3),
        "local_texture_tiles_applied": local_texture_tiles_applied,
        "local_texture_guard_applied": local_texture_guard_applied,
        "lip_feature_guard_enabled": lip_feature_guard_enabled,
        "lip_feature_max_delta": lip_feature_max_delta,
        "lip_feature_blend_strength": lip_feature_blend_strength,
        "lip_feature_delta_before": round(lip_feature_delta_before, 3),
        "lip_feature_delta_after": round(lip_feature_delta_after, 3),
        "lip_feature_guard_applied": lip_feature_guard_applied,
        "edge_halo_guard_enabled": edge_halo_guard_enabled,
        "edge_halo_min_delta": edge_halo_min_delta,
        "edge_halo_blend_strength": edge_halo_blend_strength,
        "edge_halo_delta_before": round(edge_halo_delta_before, 3),
        "edge_halo_delta_after": round(edge_halo_delta_after, 3),
        "edge_halo_guard_applied": edge_halo_guard_applied,
        "face_luma_lift": round(face_luma_lift, 3),
        "face_luma_input": round(face_luma_input, 3),
        "face_luma_reference": round(face_luma_reference, 3),
        "face_luma_after": round(face_luma_after, 3),
        "face_luma_delta": round(face_luma_delta, 3),
        "face_background_contrast_input": round(face_background_contrast_input, 3),
        "face_background_contrast_reference": round(face_background_contrast_reference, 3),
        "face_background_contrast_after": round(face_background_contrast_after, 3),
        "face_background_contrast_delta": round(face_background_contrast_delta, 3),
        "highlight_guard_applied": highlight_guard_applied,
        "highlight_guard_darken": round(highlight_guard_darken, 3),
        "masked_highlight_p95_reference": round(masked_highlight_p95_reference, 3),
        "masked_highlight_p95_after": round(masked_highlight_p95_after, 3),
        "masked_highlight_p95_delta": round(masked_highlight_p95_delta, 3),
        "masked_highlight_p99_reference": round(masked_highlight_p99_reference, 3),
        "masked_highlight_p99_after": round(masked_highlight_p99_after, 3),
        "masked_highlight_p99_delta": round(masked_highlight_p99_delta, 3),
        "masked_specular_ratio_reference": round(masked_specular_ratio_reference, 6),
        "masked_specular_ratio_after": round(masked_specular_ratio_after, 6),
        "masked_specular_ratio_delta": round(masked_specular_ratio_delta, 6),
        "masked_luma_before": round(before_mean, 3),
        "masked_luma_after": round(after_mean, 3),
        "masked_luma_delta": round(after_mean - before_mean, 3),
        "global_luma_before": round(global_before_mean, 3),
        "global_luma_after": round(global_canvas_mean, 3),
        "global_luma_delta": round(global_canvas_mean - global_before_mean, 3),
        "shadow_luma_before": round(shadow_before_mean, 3),
        "shadow_luma_after": round(shadow_after_mean, 3),
        "shadow_luma_delta": round(shadow_after_mean - shadow_before_mean, 3),
        "input_path": str(generated_path),
        "reference_path": str(reference_path) if reference_path else None,
        "output_path": str(output_path),
    }


def _create_difference_heatmap(
    original_path: Path,
    generated_path: Path,
    output_path: Path,
    focus_regions: list[dict[str, Any]],
) -> dict[str, Any]:
    from PIL import Image, ImageChops, ImageEnhance, ImageOps, ImageStat

    with Image.open(original_path) as original_img, Image.open(generated_path) as generated_img:
        original = ImageOps.exif_transpose(original_img).convert("RGB")
        generated = ImageOps.exif_transpose(generated_img).convert("RGB")
        if generated.size != original.size:
            generated = generated.resize(original.size, Image.Resampling.LANCZOS)
        diff = ImageChops.difference(original, generated)
        gray = ImageOps.grayscale(diff)
        heat = ImageOps.colorize(ImageEnhance.Contrast(gray).enhance(2.4), black="#101828", white="#ff2d55")
        overlay = Image.blend(original, heat, 0.42)
        mask = _region_mask(original.size, focus_regions)
        # Draw the target boxes on top so reviewers can separate intentional
        # edits from spillover in the heatmap.
        draw_img = overlay.convert("RGB")
        from PIL import ImageDraw

        draw = ImageDraw.Draw(draw_img)
        width, height = original.size
        for region in focus_regions:
            left = int(round(float(region.get("x", 0)) * width))
            top = int(round(float(region.get("y", 0)) * height))
            right = int(round((float(region.get("x", 0)) + float(region.get("width", 0))) * width))
            bottom = int(round((float(region.get("y", 0)) + float(region.get("height", 0))) * height))
            draw.rectangle((left, top, right, bottom), outline="#12b76a", width=max(2, width // 220))
        draw_img.save(output_path)

        histogram = gray.histogram()
        total_pixels = original.size[0] * original.size[1]
        mean_abs = ImageStat.Stat(gray).mean[0] / 255 * 100
        changed_pixels = sum(histogram[20:])
        target_score = _masked_mean(gray, mask, include_mask=True)
        non_target_score = _masked_mean(gray, mask, include_mask=False)
        return {
            "full_frame_change_score": round(mean_abs, 3),
            "target_region_change_score": target_score,
            "non_target_change_score": non_target_score,
            "p95_change_score": _score_from_histogram(histogram, total_pixels, 0.95),
            "changed_pixel_ratio_8pct": round(changed_pixels / max(1, total_pixels), 4),
            "heatmap_path": str(output_path),
            "heatmap_kind": "difference_heatmap",
        }


def _run_stress_stub_after_simulation(
    *,
    job_id: int,
    after_image_path: Path,
    before_image_path: Path | None,
    focus_targets: list[str],
    focus_regions: list[dict[str, Any]],
    model_name: str | None,
    note: str | None,
) -> dict[str, Any]:
    """Local no-external-call simulation for stress runs.

    This keeps the AI review/file/audit chain hot while guaranteeing that real
    patient images are not sent to a generation provider during pressure tests.
    """
    output_dir = simulation_job_dir(job_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt = build_after_enhancement_prompt(focus_targets, focus_regions, model_name=model_name, brand="meiji_ai")
    started = _now_iso()
    generated = output_dir / f"generated-stress-stub{after_image_path.suffix.lower() or '.jpg'}"
    shutil.copyfile(after_image_path, generated)
    original_after = _copy_reference(after_image_path, output_dir, "after-original")
    original_before = _copy_reference(before_image_path, output_dir, "before-reference") if before_image_path else None
    final_path = output_dir / f"after-ai-enhanced{generated.suffix.lower() or '.jpg'}"
    watermarked, watermark_error = _copy_with_watermark(generated, final_path)
    heatmap_path = output_dir / "difference-heatmap.png"
    difference_analysis = _create_difference_heatmap(
        after_image_path,
        generated,
        heatmap_path,
        focus_regions,
    )
    finished = _now_iso()
    audit = {
        "provider": DEFAULT_PROVIDER,
        "model_name": model_name or "stress-stub-local",
        "quality": "stress-local-copy",
        "focus_targets": focus_targets,
        "focus_regions": focus_regions,
        "prompt": prompt,
        "edit_prompt": "Stress mode local stub: copied the original after image without external generation.",
        "planned_tasks": [],
        "planner_used": False,
        "degradations": [],
        "elapsed_seconds": {"total": 0},
        "started_at": started,
        "finished_at": finished,
        "note": note,
        "watermark_applied": watermarked,
        "watermark_error": watermark_error,
        "difference_analysis": difference_analysis,
        "stress_stub": True,
        "policy": {
            "artifact_mode": "ai_after_simulation",
            "focus_scope": "region-locked-light" if focus_regions else "whole-image-light",
            "focus_region_required": False,
            "target_input_requirement": "focus_regions_optional",
            "non_target_policy": "preserve-no-global-retouch" if focus_regions else "whole-image-light-retouch",
            "mix_with_real_case": False,
            "can_publish_default": False,
            "external_model_called": False,
            "stress_run_id": stress.stress_run_id(),
        },
    }
    (output_dir / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "done" if watermarked else "done_with_issues",
        "output_refs": [
            {"kind": "ai_after_simulation", "path": str(final_path), "watermarked": watermarked},
            {"kind": "generated_raw", "path": str(generated), "watermarked": False},
            {"kind": "original_after", "path": original_after, "watermarked": False},
            *(
                [{"kind": "before_reference", "path": original_before, "watermarked": False}]
                if original_before
                else []
            ),
            {"kind": "difference_heatmap", "path": str(heatmap_path), "watermarked": False},
            {"kind": "audit", "path": str(output_dir / "audit.json"), "watermarked": False},
        ],
        "audit": audit,
        "raw": {"success": True, "model": model_name or "stress-stub-local", "stressStub": True},
        "watermarked": watermarked,
        "error_message": watermark_error,
    }


def run_direct_clinical_enhancement(
    image_path: Path,
    brand: str,
    focus_targets: list[str] | None = None,
    *,
    retry_count: int = 1,
) -> Path:
    """Trigger AI clinical enhancement directly on a specific image (M1 POLISH).

    Used by the rendering queue to automate 'md_ai' clinical post-processing.

    Step 3 of 4-mode plan adds:
      * Identity-preservation reinforcement at prompt prefix (mitigates the
        over-polish behaviour observed in L2 quality validation case 129).
      * Single retry on subprocess failure (mitigates the 1/5 silent_fail
        rate observed on L2 case 140 — transient API errors, timeouts).
      * LOGGER.warning surfaces stderr on terminal failure so bulk runs can
        be grepped for root cause (was previously silent).
    """
    if stress.is_stress_mode() and not stress.allow_external_ai():
        return image_path

    if not PS_ENHANCE_SCRIPT.is_file():
        return image_path

    focus_targets = focus_targets or []
    if not focus_targets:
        focus_targets = ["面部"]

    # Build prompt with explicit anti-polish reinforcement prefix (Step 3 polish improvement).
    # Earlier prompt put identity locks AT THE END which some models down-weight; lifting the
    # safety clause to the front gives consistent identity preservation across calls.
    body_prompt = build_after_enhancement_prompt(focus_targets, [], brand=brand)
    prompt = (
        "CRITICAL: Preserve patient identity exactly. NO over-smoothing, NO over-brightening, "
        "NO over-enhancement. The output must look like a real photograph of the SAME PERSON, "
        "not an AI-generated portrait. Preserve all original skin texture, pores, freckles, "
        "blemishes, eye shape, and facial structure.\n\n"
        + body_prompt
    )
    cmd = [
        "node",
        str(PS_ENHANCE_SCRIPT),
        "--image",
        str(image_path),
        "--prompt",
        prompt,
        "--quality",
        DEFAULT_QUALITY,
    ]

    # Subprocess attempt loop: original attempt + (retry_count) retries.
    # Total subprocess calls = 1 + retry_count.
    last_error_summary: str | None = None
    for attempt in range(1 + retry_count):
        try:
            proc = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=DEFAULT_TIMEOUT_SEC,
                check=False,
            )
            if proc.returncode != 0:
                stderr_tail = (proc.stderr or "")[-400:]
                last_error_summary = (
                    f"returncode={proc.returncode} stderr_tail={stderr_tail!r}"
                )
                if attempt < retry_count:
                    LOGGER.warning(
                        "P1 PS direct enhance non-zero exit for %s (attempt %d/%d): %s — retrying",
                        image_path.name, attempt + 1, 1 + retry_count, last_error_summary,
                    )
                    continue
                break
            raw = _parse_json_stdout(proc.stdout)
            generated = Path(str(raw.get("imagePath") or raw.get("generatedImagePath") or ""))
            if raw.get("success") and generated.is_file():
                return generated
            # success=False or missing imagePath — bookkeeping-level failure, retry.
            last_error_summary = (
                f"raw.success={raw.get('success')} imagePath={raw.get('imagePath')!r}"
            )
            if attempt < retry_count:
                LOGGER.warning(
                    "P1 PS direct enhance returned no image for %s (attempt %d/%d): %s — retrying",
                    image_path.name, attempt + 1, 1 + retry_count, last_error_summary,
                )
                continue
        except subprocess.TimeoutExpired as exc:
            last_error_summary = f"TimeoutExpired after {exc.timeout}s"
            if attempt < retry_count:
                LOGGER.warning(
                    "P1 PS direct enhance timed out for %s (attempt %d/%d): %s — retrying",
                    image_path.name, attempt + 1, 1 + retry_count, last_error_summary,
                )
                continue
        except Exception as exc:  # noqa: BLE001
            last_error_summary = f"{type(exc).__name__}: {exc}"
            if attempt < retry_count:
                LOGGER.warning(
                    "P1 PS direct enhance raised for %s (attempt %d/%d): %s — retrying",
                    image_path.name, attempt + 1, 1 + retry_count, last_error_summary,
                )
                continue

    if last_error_summary:
        LOGGER.warning(
            "P1 PS direct enhance terminal failure for %s after %d attempts: %s",
            image_path.name, 1 + retry_count, last_error_summary,
        )
    return image_path


def run_clinical_archive_pipeline(
    image_path: Path,
    *,
    output_dir: Path | None = None,
    apply_white_balance: bool = False,
) -> Path:
    """M2 — Clinical archive raw pipeline (NO AI).

    Per the 4-mode dispatch plan (~/.claude/plans/md-ai-4-mode-router.md), this
    is the "preserve clinical reality" path. It performs ONLY non-destructive
    corrections — never alters facial features, skin texture, or anatomy.

    Operations:
      1. EXIF orientation transpose (``PIL.ImageOps.exif_transpose``) — fixes
         photos shot in landscape that should display portrait (case 140 was
         a real example from L2 validation).
      2. Optional gray-world LAB white balance (env-gated via
         ``apply_white_balance``) — normalises clinic lighting drift WITHOUT
         touching pixel-level facial detail.

    Silent-fail contract: on any error, returns input ``image_path`` unchanged
    so the caller dispatch can fall back to logging a warning. Same contract as
    ``run_direct_clinical_enhancement`` and ``run_comfyui_inline_enhance``.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover  — defensive; PIL is a hard dep
        LOGGER.warning("Pillow not importable; clinical archive pipeline disabled")
        return image_path

    if not image_path.is_file():
        return image_path

    caller_provided_output_dir = output_dir is not None
    if not caller_provided_output_dir:
        import tempfile
        output_dir = Path(tempfile.mkdtemp(prefix=".archive-pipeline-", dir=str(image_path.parent)))
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    try:
        with Image.open(image_path) as raw:
            # PIL's exif_transpose returns a NEW image with orientation
            # baked in (and the orientation tag stripped) — exactly what we
            # want for an archive that's safe to consume by downstream pipelines
            # that may not honour EXIF rotation tags.
            transposed = ImageOps.exif_transpose(raw)

            if apply_white_balance:
                transposed = _archive_apply_white_balance(transposed)

            if transposed.mode != "RGB":
                transposed = transposed.convert("RGB")

            # Output PNG (lossless) to preserve byte-identicality if no edits
            # actually fired, while still allowing diff inspection. Suffix
            # ``.archive.png`` distinguishes from raw inputs.
            output_path = output_dir / f"{image_path.stem}.archive.png"
            transposed.save(output_path, format="PNG", optimize=True)

        if not caller_provided_output_dir:
            # Promote to stable path next to input (mirrors K-1 contract for
            # ``run_comfyui_inline_enhance``); auto-cleaned tempdir below.
            stable_path = image_path.parent / f".archive-out-{int(time.time())}-{os.getpid()}-{image_path.stem}.png"
            shutil.copyfile(output_path, stable_path)
            return stable_path
        return output_path
    except Exception as exc:  # noqa: BLE001 — silent-fail contract
        LOGGER.warning(
            "Clinical archive pipeline failed for %s (%s); returning input",
            image_path, exc,
        )
        return image_path
    finally:
        if not caller_provided_output_dir and output_dir is not None and output_dir.is_dir():
            shutil.rmtree(output_dir, ignore_errors=True)


def _archive_apply_white_balance(image):
    """Conservative gray-world LAB white balance.

    Computes the mean of the a* and b* channels and shifts them toward the
    neutral point (128 in LAB 8-bit encoding). Does NOT modify the L channel
    so brightness is preserved. Safe for clinical record photos because the
    operation is global + linear — no localised pixel content alteration.

    Returns the image unchanged on any error (silent-fail).

    Implementation note: uses a 256-element LUT passed to ``point()`` instead
    of a per-pixel lambda. The lambda form is ~1000x slower on 480p images
    (~95s vs ~0.05s) because it triggers a Python call per pixel.
    """
    try:
        from PIL import Image
        if image.mode != "RGB":
            image = image.convert("RGB")
        lab = image.convert("LAB")
        from PIL.ImageStat import Stat
        stat = Stat(lab)
        a_mean, b_mean = stat.mean[1], stat.mean[2]
        l_band, a_band, b_band = lab.split()
        # Build 256-element LUTs for each channel shift — point() applies the
        # LUT in C, no Python callbacks per pixel.
        a_shift = int(round(a_mean - 128))
        b_shift = int(round(b_mean - 128))
        a_lut = [max(0, min(255, v - a_shift)) for v in range(256)]
        b_lut = [max(0, min(255, v - b_shift)) for v in range(256)]
        a_band = a_band.point(a_lut)
        b_band = b_band.point(b_lut)
        return Image.merge("LAB", (l_band, a_band, b_band)).convert("RGB")
    except Exception:  # noqa: BLE001
        return image


def run_comfyui_inline_enhance(
    image_path: Path,
    *,
    focus_targets: list[str] | None = None,
    brand: str = "fumei",
    case_id: int | None = None,
    model_name: str | None = None,
    output_dir: Path | None = None,
) -> Path:
    """G1.A.i — inline ComfyUI enhancement (lightweight P2 swap path).

    Calls _run_comfyui_workflow directly (the same core HTTP flow used by
    run_comfyui_local_after_simulation) but **skips watermark / qa_scores /
    difference_heatmap / audit.json / simulation_jobs DB row**. Designed for
    pre-render staging inline replacement where the heavier simulation-after
    pipeline is overkill.

    Returns:
        - On success: Path to the ComfyUI-generated PNG (caller os.replace into staging)
        - On any failure: returns input `image_path` unchanged (silent — caller
          detects success by checking `result != image_path`)

    Failure modes covered (per G1.A.i contract — render done + ComfyUI fail audit only):
        - _run_comfyui_workflow raises (ConnectionError / TimeoutError / RuntimeError)
        - run_result missing `generated_path` key
        - generated_path string returned but file doesn't exist on disk
        - LOGGER.warning emitted in all silent-fail branches for grep audit

    Args:
        image_path: source image (typically an "after" image in staging dir)
        focus_targets: anatomical keywords (e.g. ["面部", "下颌线"]); informational
            only at this layer — the workflow itself controls focus regions
        brand: render brand (md_ai / meiji_ai); informational
        case_id: render case_id (for LOGGER context); informational
        model_name: ComfyUI workflow identifier (e.g.
            "local_region_enhance_v1@conservative"). Defaults to
            COMFYUI_DEFAULT_CANDIDATE_WORKFLOW.
        output_dir: optional temp dir for ComfyUI output. Defaults to a
            timestamp+pid-namespaced subdir under image_path.parent.
    """
    # K-1 hardening: use mkdtemp + try/finally + rmtree to prevent staging dir
    # accumulation (cross-reviewer Critical: 100 case x 3 after x 1MB = 300MB/round 累积).
    # output_dir caller-provided path is honored (no cleanup) for testability;
    # auto-generated tempdir is always cleaned. We copy generated.png to a
    # caller-readable path before cleanup so the helper's Path return remains stable.
    caller_provided_output_dir = output_dir is not None
    if not caller_provided_output_dir:
        import tempfile
        # K-2 hardening: tempdir under image_path.parent with explicit prefix lets
        # render_queue iterdir filter (`.` + `enhanced_`) reliably skip residue.
        output_dir = Path(tempfile.mkdtemp(prefix=".comfyui-inline-", dir=str(image_path.parent)))
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
    try:
        profile = _resolve_comfyui_workflow_profile(model_name)
        workflow_name = str(profile["workflow_name"])
        run_result = _run_comfyui_workflow(
            image_path,
            output_dir=output_dir,
            workflow_name=workflow_name,
            workflow_parameters=profile.get("parameters") or {},
        )
        generated_path_str = run_result.get("generated_path")
        if not generated_path_str:
            LOGGER.warning(
                "ComfyUI inline enhance: no generated_path in run_result for %s "
                "(case_id=%s brand=%s, run_result keys=%s)",
                image_path.name, case_id, brand, list(run_result.keys()),
            )
            return image_path
        generated_path = Path(generated_path_str)
        if not generated_path.is_file():
            LOGGER.warning(
                "ComfyUI inline enhance: generated_path %s does not exist on disk "
                "(case_id=%s brand=%s)",
                generated_path, case_id, brand,
            )
            return image_path
        if not caller_provided_output_dir:
            # K-1: stable Path outside the soon-to-be-rmtree'd tempdir.
            # Use a sibling file next to image_path; caller os.replace it onto staging.
            stable_path = image_path.parent / f".comfyui-out-{int(time.time())}-{os.getpid()}-{image_path.name}.png"
            shutil.copyfile(generated_path, stable_path)
            return stable_path
        return generated_path
    except Exception as exc:  # noqa: BLE001 - silent fail per G1.A.i contract (render done + audit only)
        LOGGER.warning(
            "ComfyUI inline enhance failed for %s (case_id=%s brand=%s): %s: %s",
            image_path.name, case_id, brand, type(exc).__name__, exc,
        )
        return image_path
    finally:
        # K-1: always cleanup auto-generated tempdir (caller-provided dir is owned by caller)
        if not caller_provided_output_dir and output_dir.is_dir():
            shutil.rmtree(output_dir, ignore_errors=True)


def _build_comfyui_policy(
    *,
    case_id: int | None,
    focus_regions: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Plan §P2.3 runtime guard — turn promotion manifest into per-case policy.

    When `case_id` is None or the manifest says shadow / rolled_back / missing,
    behavior matches the legacy hardcoded `candidate_only=True` defaults so
    deployments without a manifest are unchanged (BC).
    """
    promoted = bool(case_id is not None and should_promote(int(case_id)))
    return {
        "artifact_mode": "ai_after_simulation",
        "candidate_provider": COMFYUI_PROVIDER,
        "candidate_only": not promoted,
        "t90_gate_scope": "local_region_repair_retest",
        "focus_scope": "region-locked-light" if focus_regions else "whole-image-light",
        "watermark_required": True,
        "mix_with_real_case": promoted,
        "can_publish_default": promoted,
        "promote_to_default": promoted,
    }


_FOCAL_MAX_LONG_EDGE = 1280
_FOCAL_TIMEOUT_BASE = 600
_FOCAL_TIMEOUT_PER_MEGAPIXEL = 800
_FOCAL_TIMEOUT_CAP = 1800
_FOCAL_PIXEL_FLOOR = 256_000


def _focal_compute_timeout(width: int, height: int) -> int:
    """Wave 11 W11-2: dynamic timeout from resized image dimensions.

    Base ``_FOCAL_TIMEOUT_BASE`` seconds, plus a linear pixel-area component
    above ``_FOCAL_PIXEL_FLOOR`` (256K pixels ≈ 512×500). Capped at
    ``_FOCAL_TIMEOUT_CAP`` so we never wait forever even on a 4K input.
    """
    pixels = max(0, int(width) * int(height))
    extra_pixels = max(0, pixels - _FOCAL_PIXEL_FLOOR)
    extra_seconds = (extra_pixels * _FOCAL_TIMEOUT_PER_MEGAPIXEL) // 1_000_000
    return min(_FOCAL_TIMEOUT_CAP, _FOCAL_TIMEOUT_BASE + int(extra_seconds))


def _focal_resize_if_needed(
    image_path: Path,
    output_dir: Path,
    max_long_edge: int = _FOCAL_MAX_LONG_EDGE,
) -> tuple[Path, int, int, int, int]:
    """Wave 11 W11-3: resize input so long edge ≤ ``max_long_edge``.

    Returns ``(work_path, work_w, work_h, original_w, original_h)``.
    ``work_path == image_path`` (no I/O) if no resize is needed.
    """
    from PIL import Image, ImageOps

    with Image.open(image_path) as im:
        im = ImageOps.exif_transpose(im)
        orig_w, orig_h = im.size
        if max(orig_w, orig_h) <= max_long_edge:
            return image_path, orig_w, orig_h, orig_w, orig_h
        scale = max_long_edge / max(orig_w, orig_h)
        new_w = max(1, int(round(orig_w * scale)))
        new_h = max(1, int(round(orig_h * scale)))
        resized = im.resize((new_w, new_h), Image.LANCZOS)
        if resized.mode != "RGB":
            resized = resized.convert("RGB")
        resized_path = output_dir / f"resized_{image_path.stem}.jpg"
        resized.save(resized_path, format="JPEG", quality=92)
        return resized_path, new_w, new_h, orig_w, orig_h


def run_comfyui_focal_enhance(
    image_path: Path,
    *,
    focus_targets: list[str] | None = None,
    brand: str = "fumei",
    case_id: int | None = None,
    output_dir: Path | None = None,
) -> Path:
    """M3 FOCAL — region-aware SDXL inpaint with per-target prompts.

    Wave 11 update: the v1 workflow has been simplified to consume the
    Python-side ellipse focus mask directly (no MediaPipe whole-face SEGS +
    SAM bbox-expansion chain that was eating the entire face in case 134),
    and inputs are auto-resized to ≤_FOCAL_MAX_LONG_EDGE long-edge before
    submission with a pixel-area-derived timeout (W11-1 / W11-2 / W11-3).

    Steps:
      1. Auto-resize input to ≤_FOCAL_MAX_LONG_EDGE long-edge.
      2. Generate a coarse focus mask at the resized resolution via
         ``focal_mask_generator.generate_focus_mask``.
      3. Build per-target positive + negative prompts via
         ``focal_prompt_library.build_focal_prompts``.
      4. Compute dynamic timeout from resized dimensions
         (``_focal_compute_timeout``).
      5. Run the ``portrait_focal_enhance_v1`` workflow (focal strength:
         denoise=0.40, 20 steps, cfg=4.0).
      6. Promote generated PNG to a stable path next to the input. If the
         input was resized, upscale the output back to the original
         resolution with Lanczos before saving.

    K-1 contract: returns ``image_path`` on any silent failure.
    """
    from .services.focal_mask_generator import generate_focus_mask
    from .services.focal_prompt_library import build_focal_prompts

    focus_targets = focus_targets or []
    caller_provided_output_dir = output_dir is not None
    if not caller_provided_output_dir:
        import tempfile
        output_dir = Path(tempfile.mkdtemp(prefix=".comfyui-focal-", dir=str(image_path.parent)))
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    mask_path: Path | None = None
    try:
        # 1. Auto-resize input if needed (W11-3)
        try:
            workflow_input, work_w, work_h, orig_w, orig_h = _focal_resize_if_needed(
                image_path, output_dir,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "ComfyUI focal enhance: resize failed for %s (%s); silent-fail",
                image_path.name, exc,
            )
            return image_path
        needs_upscale = (work_w, work_h) != (orig_w, orig_h)
        if needs_upscale:
            LOGGER.info(
                "ComfyUI focal enhance: resized input %dx%d -> %dx%d (long-edge cap %d)",
                orig_w, orig_h, work_w, work_h, _FOCAL_MAX_LONG_EDGE,
            )

        # 2. Generate focus mask (at resized resolution so dimensions match)
        try:
            mask_path = generate_focus_mask(
                workflow_input, focus_targets, output_path=output_dir / "focus_mask.png",
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "ComfyUI focal enhance: focus mask generation failed for %s (%s); silent-fail",
                image_path.name, exc,
            )
            return image_path

        # 3. Build prompts
        positive_prompt, negative_prompt = build_focal_prompts(focus_targets)

        # 4. Compute dynamic timeout (W11-2)
        timeout_s = _focal_compute_timeout(work_w, work_h)
        LOGGER.info(
            "ComfyUI focal enhance: dynamic timeout=%ds for %dx%d (%d pixels)",
            timeout_s, work_w, work_h, work_w * work_h,
        )

        # 5. Run workflow
        try:
            run_result = _run_comfyui_workflow(
                workflow_input,
                output_dir=output_dir,
                workflow_name="portrait_focal_enhance_v1",
                workflow_parameters=dict(_COMFYUI_WORKFLOW_PARAMETERS["focal"]),
                focus_mask_path=mask_path,
                positive_prompt=positive_prompt,
                negative_prompt=negative_prompt,
                timeout_seconds=timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "ComfyUI focal enhance: workflow run failed for %s (case_id=%s brand=%s): %s",
                image_path.name, case_id, brand, exc,
            )
            return image_path

        generated_path_str = run_result.get("generated_path") if isinstance(run_result, dict) else None
        if not generated_path_str:
            LOGGER.warning(
                "ComfyUI focal enhance: no generated_path for %s; silent-fail",
                image_path.name,
            )
            return image_path
        generated_path = Path(generated_path_str)
        if not generated_path.is_file():
            LOGGER.warning(
                "ComfyUI focal enhance: generated_path %s does not exist; silent-fail",
                generated_path,
            )
            return image_path

        # 6. Promote to stable path (K-1 contract). Upscale back to the
        #    original resolution whenever the input was resized.
        #    W11.4: the upscale-back must NOT be gated by the output-destination
        #    check. When the caller supplies output_dir, stable_path ==
        #    generated_path, so the old ``and stable_path != generated_path``
        #    guard made BOTH branches dead and silently returned a
        #    working-resolution image — violating the step-6 docstring contract
        #    and producing eval artifacts that diverge from the production path
        #    (render_queue.py passes no output_dir and DID upscale back).
        if not caller_provided_output_dir:
            stable_path = image_path.parent / f".comfyui-focal-out-{int(time.time())}-{os.getpid()}-{image_path.name}.png"
        else:
            stable_path = generated_path

        # W11.5: restore the output to EXACT source dims whenever the *generated*
        # image differs from the source — not only when W11 resized the input
        # (``needs_upscale``). SDXL/VAE snaps working dims to a multiple of 8
        # (e.g. 853 -> 848), so a non-resized, non-divisible-by-8 source would
        # otherwise emit a few-px-short output (case 134: 853x1280 -> 848x1280,
        # surfaced in C1.2 smoke). Conditioning on the actual generated dims
        # generalizes the W11.4 upscale-back contract to every path.
        try:
            from PIL import Image
            with Image.open(generated_path) as gen_im:
                if gen_im.size != (orig_w, orig_h):
                    restored = gen_im.convert("RGB").resize((orig_w, orig_h), Image.LANCZOS)
                    # Sibling temp + atomic replace so the in-place case
                    # (stable_path == generated_path, caller-supplied output_dir)
                    # never leaves a half-written file.
                    tmp_out = stable_path.with_name(f".restore-{os.getpid()}-{stable_path.name}")
                    restored.save(tmp_out, format="PNG")
                    os.replace(tmp_out, stable_path)
                elif stable_path != generated_path:
                    shutil.copyfile(generated_path, stable_path)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "ComfyUI focal enhance: output dim-restore failed for %s (%s); using raw output",
                image_path.name, exc,
            )
            if stable_path != generated_path:
                shutil.copyfile(generated_path, stable_path)
        return stable_path
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning(
            "ComfyUI focal enhance: unexpected error for %s: %s",
            image_path.name, exc,
        )
        return image_path
    finally:
        if not caller_provided_output_dir and output_dir is not None and output_dir.is_dir():
            shutil.rmtree(output_dir, ignore_errors=True)


def run_comfyui_local_after_simulation(
    *,
    job_id: int,
    after_image_path: Path,
    before_image_path: Path | None,
    focus_targets: list[str],
    focus_regions: list[dict[str, Any]] | None = None,
    model_name: str | None = None,
    note: str | None = None,
    style_reference_image_paths: list[Path] | None = None,
    brand: str = "fumei",
    case_id: int | None = None,
) -> dict[str, Any]:
    profile = _resolve_comfyui_workflow_profile(model_name)
    workflow_name = str(profile["workflow_name"])
    output_dir = simulation_job_dir(job_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    focus_regions = focus_regions or []
    started = _now_iso()
    layers = _prepare_comfyui_input_layers(
        after_image_path,
        output_dir=output_dir,
        workflow_name=workflow_name,
        focus_regions=focus_regions,
    )
    run_result = _run_comfyui_workflow(
        Path(layers["normalized_image_path"]),
        output_dir=output_dir,
        workflow_name=workflow_name,
        focus_mask_path=Path(layers["focus_mask_path"]),
        subject_mask_path=Path(layers["subject_mask_path"]),
        edge_mask_path=Path(layers["edge_mask_path"]),
        workflow_parameters=profile["parameters"],
        timeout_seconds=COMFYUI_TIMEOUT_SEC,
    )
    generated = Path(str(run_result["generated_path"]))
    raw_path = output_dir / "comfyui-generated.png"
    if generated.resolve(strict=False) != raw_path.resolve(strict=False):
        shutil.copyfile(generated, raw_path)
    provider_raw_path: Path | None = None
    postprocess_settings = profile.get("postprocess") if isinstance(profile.get("postprocess"), dict) else {}
    postprocess_report: dict[str, Any] = {"applied": False, "strategy": "none", "candidate_only": True}
    if bool(postprocess_settings.get("enabled")):
        provider_raw_path = output_dir / "comfyui-provider-raw.png"
        shutil.copyfile(raw_path, provider_raw_path)
        tone_detail_path = output_dir / "comfyui-generated-tone-detail.png"
        postprocess_report = _apply_comfyui_tone_detail_postprocess(
            provider_raw_path,
            tone_detail_path,
            mask_path=Path(layers["focus_mask_path"]),
            reference_path=Path(layers["normalized_image_path"]),
            settings=postprocess_settings,
        )
        shutil.copyfile(tone_detail_path, raw_path)
    original_after = _copy_reference(after_image_path, output_dir, "after-original")
    original_before = _copy_reference(before_image_path, output_dir, "before-reference") if before_image_path else None
    final_path = output_dir / "after-ai-enhanced.png"
    watermarked, watermark_error = _copy_with_watermark(raw_path, final_path)
    heatmap_path = output_dir / "difference-heatmap.png"
    difference_analysis = _create_difference_heatmap(
        after_image_path,
        raw_path,
        heatmap_path,
        focus_regions,
    )
    qa_scores = _score_comfyui_output_quality(
        Path(layers["normalized_image_path"]),
        raw_path,
        Path(layers["subject_mask_path"]),
        layers["canvas_slot"],
        layers["subject_scale"],
        target_mask_path=Path(layers["focus_mask_path"]),
    )
    finished = _now_iso()
    audit = {
        "provider": COMFYUI_PROVIDER,
        "model_name": profile["profile_name"],
        "workflow_name": workflow_name,
        "workflow_profile_name": profile["profile_name"],
        "workflow_hash": run_result["workflow_hash"],
        "prompt_id": run_result["prompt_id"],
        "focus_targets": focus_targets,
        "focus_regions": focus_regions,
        "style_reference_paths": [str(p) for p in (style_reference_image_paths or [])],
        "started_at": started,
        "finished_at": finished,
        "note": note,
        "brand": brand,
        "watermark_applied": watermarked,
        "watermark_error": watermark_error,
        "difference_analysis": difference_analysis,
        "qa_scores": qa_scores,
        "postprocess": postprocess_report,
        "candidate_tone_strategy": postprocess_report.get("strategy"),
        "canvas_slot": layers["canvas_slot"],
        "subject_scale": layers["subject_scale"],
        "mask_metrics": layers["mask_metrics"],
        "comfyui_concurrency": run_result["concurrency"],
        "fallback_used": False,
        "preflight": {
            "production_ready": bool((run_result.get("preflight") or {}).get("production_ready")),
            "readiness_reasons": (run_result.get("preflight") or {}).get("readiness_reasons") or [],
        },
        "policy": _build_comfyui_policy(
            case_id=case_id,
            focus_regions=focus_regions,
        ),
    }
    (output_dir / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "done" if watermarked else "done_with_issues",
        "output_refs": [
            {"kind": "ai_after_simulation", "path": str(final_path), "watermarked": watermarked},
            {"kind": "generated_raw", "path": str(raw_path), "watermarked": False},
            *(
                [{"kind": "comfyui_provider_raw", "path": str(provider_raw_path), "watermarked": False}]
                if provider_raw_path is not None
                else []
            ),
            {"kind": "original_after", "path": original_after, "watermarked": False},
            *(
                [{"kind": "before_reference", "path": original_before, "watermarked": False}]
                if original_before
                else []
            ),
            {"kind": "focus_mask", "path": str(layers["focus_mask_path"]), "watermarked": False},
            {"kind": "difference_heatmap", "path": str(heatmap_path), "watermarked": False},
            {"kind": "audit", "path": str(output_dir / "audit.json"), "watermarked": False},
        ],
        "audit": audit,
        "raw": {
            "success": True,
            "provider": COMFYUI_PROVIDER,
            "workflow": profile["profile_name"],
            "postprocess": postprocess_report,
        },
        "watermarked": watermarked,
        "error_message": watermark_error,
    }


def run_ps_model_router_after_simulation(
    *,
    job_id: int,
    after_image_path: Path,
    before_image_path: Path | None,
    focus_targets: list[str],
    focus_regions: list[dict[str, Any]] | None = None,
    model_name: str | None = None,
    note: str | None = None,
    style_reference_image_paths: list[Path] | None = None,
    brand: str = "fumei",
) -> dict[str, Any]:
    if stress.is_stress_mode() and not stress.allow_external_ai():
        return _run_stress_stub_after_simulation(
            job_id=job_id,
            after_image_path=after_image_path,
            before_image_path=before_image_path,
            focus_targets=focus_targets,
            focus_regions=focus_regions or [],
            model_name=model_name,
            note=note,
        )
    if not PS_ENHANCE_SCRIPT.is_file():
        raise FileNotFoundError(f"PS model router script not found: {PS_ENHANCE_SCRIPT}")

    output_dir = simulation_job_dir(job_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    focus_regions = focus_regions or []
    prompt = build_after_enhancement_prompt(focus_targets, focus_regions, model_name, brand=brand)
    cmd = [
        "node",
        str(PS_ENHANCE_SCRIPT),
        "--image",
        str(after_image_path),
        "--prompt",
        prompt,
        "--quality",
        DEFAULT_QUALITY,
    ]
    if before_image_path is not None:
        cmd.extend(["--pose-ref", str(before_image_path)])
    for style_ref in style_reference_image_paths or []:
        cmd.extend(["--pose-ref", str(style_ref)])
    if model_name:
        if model_name in _COMFYUI_WORKFLOW_GROUPS or "@" in model_name and model_name.split("@", 1)[0] in _COMFYUI_WORKFLOW_GROUPS:
            LOGGER.warning("ComfyUI workflow name %r passed to PS model router — ignoring, using default model", model_name)
        else:
            cmd.extend(["--model", model_name])

    started = _now_iso()
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=DEFAULT_TIMEOUT_SEC,
        check=False,
    )
    finished = _now_iso()
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise RuntimeError(err[-4000:])

    raw = _parse_json_stdout(proc.stdout)
    if not raw.get("success"):
        stage = raw.get("stage") or raw.get("pending") or "PS model router did not return a local image"
        degradations = raw.get("degradations") or []
        cause_parts = []
        for entry in degradations:
            if not isinstance(entry, dict):
                continue
            step = str(entry.get("step") or "").strip()
            err_msg = str(entry.get("error") or "").strip()
            if err_msg:
                cause_parts.append(f"{step}: {err_msg}" if step else err_msg)
        if cause_parts:
            err = f"{stage}: {'; '.join(cause_parts)}"
        else:
            err = str(stage)
        raise RuntimeError(err[-4000:])
    generated, generated_source = _resolve_ps_generated_path(raw, after_image_path)

    original_after = _copy_reference(after_image_path, output_dir, "after-original")
    original_before = _copy_reference(before_image_path, output_dir, "before-reference") if before_image_path else None
    ext = generated.suffix.lower() or ".jpg"
    raw_generated_path = output_dir / f"generated-raw{ext}"
    if not _same_path(generated, raw_generated_path):
        shutil.copyfile(generated, raw_generated_path)
    generated = raw_generated_path
    final_path = output_dir / f"after-ai-enhanced{ext}"
    watermarked, watermark_error = _copy_with_watermark(generated, final_path)
    heatmap_path = output_dir / "difference-heatmap.png"
    difference_analysis = _create_difference_heatmap(
        after_image_path,
        generated,
        heatmap_path,
        focus_regions,
    )
    actual_model_name = raw.get("usedModel") or raw.get("model") or model_name
    audit = {
        "provider": DEFAULT_PROVIDER,
        "model_name": actual_model_name,
        "requested_model_name": model_name,
        "quality": raw.get("quality") or DEFAULT_QUALITY,
        "focus_targets": focus_targets,
        "focus_regions": focus_regions,
        "style_reference_paths": [str(p) for p in (style_reference_image_paths or [])],
        "prompt": prompt,
        "edit_prompt": raw.get("editPrompt"),
        "planned_tasks": raw.get("plannedTasks"),
        "planner_used": bool(raw.get("plannerUsed")),
        "degradations": raw.get("degradations") or [],
        "elapsed_seconds": raw.get("elapsedSeconds") or {},
        "router_output_image_path": raw.get("imagePath"),
        "router_generated_image_path": raw.get("generatedImagePath"),
        "router_selected_generated_path": raw.get(generated_source),
        "selected_generated_path": str(generated),
        "selected_generated_source": generated_source,
        "stabilization": raw.get("stabilization"),
        "started_at": started,
        "finished_at": finished,
        "note": note,
        "watermark_applied": watermarked,
        "watermark_error": watermark_error,
        "difference_analysis": difference_analysis,
        "policy": {
            "artifact_mode": "ai_after_simulation",
            "focus_scope": "region-locked-light" if focus_regions else "whole-image-light",
            "focus_region_required": False,
            "target_input_requirement": "focus_regions_optional",
            "non_target_policy": "preserve-no-global-retouch" if focus_regions else "whole-image-light-retouch",
            "mix_with_real_case": False,
            "can_publish_default": False,
        },
    }
    (output_dir / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "done" if watermarked else "done_with_issues",
        "output_refs": [
            {"kind": "ai_after_simulation", "path": str(final_path), "watermarked": watermarked},
            {"kind": "generated_raw", "path": str(generated), "watermarked": False},
            {"kind": "original_after", "path": original_after, "watermarked": False},
            *(
                [{"kind": "before_reference", "path": original_before, "watermarked": False}]
                if original_before
                else []
            ),
            {"kind": "difference_heatmap", "path": str(heatmap_path), "watermarked": False},
            {"kind": "audit", "path": str(output_dir / "audit.json"), "watermarked": False},
        ],
        "audit": audit,
        "raw": raw,
        "watermarked": watermarked,
        "error_message": watermark_error,
    }


def run_after_simulation(
    *,
    provider: str,
    job_id: int,
    after_image_path: Path,
    before_image_path: Path | None,
    focus_targets: list[str],
    focus_regions: list[dict[str, Any]] | None = None,
    model_name: str | None = None,
    note: str | None = None,
    style_reference_image_paths: list[Path] | None = None,
    brand: str = "fumei",
    case_id: int | None = None,
) -> dict[str, Any]:
    if provider == COMFYUI_PROVIDER:
        return run_comfyui_local_after_simulation(
            job_id=job_id,
            after_image_path=after_image_path,
            before_image_path=before_image_path,
            focus_targets=focus_targets,
            focus_regions=focus_regions,
            model_name=model_name,
            note=note,
            style_reference_image_paths=style_reference_image_paths,
            brand=brand,
            case_id=case_id,
        )
    if provider == DEFAULT_PROVIDER:
        return run_ps_model_router_after_simulation(
            job_id=job_id,
            after_image_path=after_image_path,
            before_image_path=before_image_path,
            focus_targets=focus_targets,
            focus_regions=focus_regions,
            model_name=model_name,
            note=note,
            style_reference_image_paths=style_reference_image_paths,
            brand=brand,
        )
    raise ValueError(f"unsupported simulation provider: {provider}")
