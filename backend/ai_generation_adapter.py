"""AI image generation adapters for auditable case simulations."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import stress

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
DEFAULT_PRIMARY_IMAGE_MODEL = "gpt-image-2-vip"
DEFAULT_FALLBACK_IMAGE_MODEL = "gemini-3-pro-image-preview-4k"
KNOWN_TUZI_IMAGE_MODELS = [
    "gemini-3-pro-image-preview-4k",
    "gemini-3-pro-image-preview-2k-vip",
    "gemini-3-pro-image-preview-vip",
    "nano-banana",
    "gpt-image-2",
]


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
            desc = "PS 自动化主模型；优先走 TUZI_IMAGE_PRIMARY_* 配置"
        elif model == fallback:
            source = "fallback"
            desc = "PS 自动化 fallback；主模型失败时使用"
        else:
            source = "tuzi_builtin"
            desc = "兔子图像模型质量档位；用于手动指定本次增强"
        options.append(
            {
                "value": model,
                "label": model,
                "source": source,
                "description": desc,
                "is_default": model == primary[0],
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
    "（保持同一个发型/颜色/长度/分缝侧，但具体哪一根丝在什么位置必须不同，模拟不同快门瞬间）；\n"
    "  · 皮肤微细节：高光位置、毛孔/纹理的微噪点、可见瑕疵（雀斑/痘印/痣）的反光，要按一次新的拍摄重新生成，"
    "保留原本的真实分布但允许微位移；\n"
    "  · 衣服/首饰：领口/褶皱/纽扣阴影的微变化要自然不同，但款式、颜色、配饰本身保持一致。\n"
    "差异化幅度要克制——只动上述微观层级，不得破坏已经对齐好的姿态、角度和构图，也不得改变同一人身份、五官比例、骨相、服装、发饰和背景。"
)


def _apply_pose_alignment(prompt: str) -> str:
    return prompt + "\n" + _POSE_ALIGNMENT_CLAUSE


def _apply_strength_damping(prompt: str, model_name: str | None) -> str:
    pct = _MODEL_STRENGTH_DAMPING.get((model_name or "").strip())
    if not pct:
        return prompt
    return prompt + "\n" + (
        f"模型幅度校准：当前生成模型本身效果偏强，请把上述每个目标的实际表现幅度统一回拉约 {pct}%"
        f"（在保持目标明显可见的前提下，避免过度饱满、过度填充或失真）。例如目标里写「60%」实际做到约 "
        f"{int(round(60 * (100 - pct) / 100))}% 即可。"
    )


def _finalize_prompt(prompt: str, model_name: str | None) -> str:
    return _apply_strength_damping(_apply_pose_alignment(prompt), model_name)


def build_after_enhancement_prompt(
    focus_targets: list[str],
    focus_regions: list[dict[str, Any]] | None = None,
    model_name: str | None = None,
) -> str:
    focus = "；".join(focus_targets)
    regions = focus_regions or []
    if regions:
        region_lines = []
        for idx, region in enumerate(regions, start=1):
            label = str(region.get("label") or "").strip()
            label_part = f"，目标描述：{label}" if label else ""
            region_lines.append(
                "区域{idx}: x={x:.3f}, y={y:.3f}, width={w:.3f}, height={h:.3f}{label}".format(
                    idx=idx,
                    x=float(region.get("x", 0)),
                    y=float(region.get("y", 0)),
                    w=float(region.get("width", 0)),
                    h=float(region.get("height", 0)),
                    label=label_part,
                )
            )
        focus_text = focus if focus else "用户框选区域内的目标部位；未提供文字目标时，只按框选标签做克制、自然的局部优化"
        region_text = "；".join(region_lines)
        region_policy = (
            "增强必须限制在框选区域内；框外区域按像素语义保持，不做主动修饰。"
        )
        return _finalize_prompt(
            "\n".join(
                [
                    "任务：对第一张术后照片做医美案例展示用的轻量局部增强。",
                    f"只允许处理这些目标部位和效果：{focus_text}。",
                    f"用户在术后图上提供的辅助框选区域坐标如下，坐标为相对整张图片的 0-1 归一化比例：{region_text}。",
                    region_policy,
                    "硬性约束：必须保持同一人身份、五官结构、脸型/身体主要轮廓、服装、发饰和背景关系（姿态/镜头视角/构图见后文姿态对齐规范）。",
                    "非目标区域严禁做任何主动改动：不得调亮、不得统一肤色或色温、不得磨皮、不得锐化、不得改变毛孔/纹理/斑点、不得改变眼睛/鼻子/眉毛/额头/头发/服装/背景。",
                    "目标区域也只能做轻量、局部、可审计的自然优化，不得改变真实治疗效果，不得新增文字、Logo、二维码或装饰元素。",
                    "输出后会做差异热区和全脸变化评分；请优先降低框外变化。",
                ]
            ),
            model_name,
        )
    if focus_targets:
        return _finalize_prompt("\n".join(
            [
                "任务：对第一张术后照片做医美案例展示用的术后增强（无用户框选区域，但有明确医美目标）。",
                f"明确的医美治疗目标：{focus}。这些目标必须在结果中清晰可见、肉眼一眼能看出术前→术后改善，否则视为失败。",
                "参考强度：把目标按数值字面执行（例如目标里写「30%」就要做到 30% 量级的改变，写「3-5mm」就要做到 3-5mm 量级的改变），整体类似两到三次注射叠加后的累积效果，可以做到「客户看完一眼就觉得明显变好」的水平。宁可略微偏强也不要欠强。",
                "强度分级（用来对齐内部预期，禁止输出弱化版本）：",
                "- 弱（禁止）：变化几乎看不出，差异热图低于 5% 像素变化。",
                "- 中（允许下限）：变化可察觉但需要对比观察。",
                "- 偏强（推荐目标）：术前/术后并排时，目标部位差异一眼可见，但仍是同一人。",
                "针对每个目标的允许操作（请按量化幅度真做出来，不要只做象征性微调）：",
                "- 鼻部（山根/鼻梁/鼻尖）：可抬高山根、收窄鼻背、雕刻鼻梁线，让侧光下出现明显高光带。",
                "- 卧蚕：可让下眼睑卧蚕明显饱满，弧形立体，笑起来能看到光影段。",
                "- 泪沟/眼袋：可让泪沟凹陷明显变浅或近乎平，眼下不再有明显阴影。",
                "- 唇部（丰唇/唇形）：可明显增加唇部体积、唇珠和唇峰更饱满立体，唇形更圆润，但保持原唇色与原嘴角弧度。",
                "- 面部轮廓/咬肌：可让对应区域线条更紧致、饱满。",
                "- 整体肤质：保留真实毛孔与纹理；可整体提亮、提升通透度、去除明显瑕疵，可保留少量斑点/痘印不要全清。",
                "硬性约束（这些不能动）：",
                "- 必须保持同一人身份、整体五官比例、脸型骨相、服装、发饰和背景（姿态/镜头视角/构图见后文姿态对齐规范；表情/神态见后文差异化要求，可与术前不同）。",
                "- 不得改动未列入目标的器官形态（目标里没写眉/耳/鼻翼/眼型/下颌时，不要主观改它们）。",
                "- 不得磨皮成塑料感、不得过度锐化、不得统一肤色到失真。",
                "- 不得新增文字、Logo、二维码或装饰元素。",
                "输出要求：每个目标都要能在结果上看出明显但自然的改善（按上述「偏强」档对齐），整体观感像一次成功的真实医美术后照片，而不是只做了 5% 微调的版本。",
            ]
        ), model_name)
    return _finalize_prompt("\n".join(
        [
            "任务：对第一张术后照片做医美案例展示用的整体轻量优化（无用户框选区域，无文字目标）。",
            "整体策略：保留真实治疗效果；只做整张照片的克制级提亮、降噪、可见瑕疵的自然修饰；不得做风格化或改变本来的医美呈现。",
            "硬性约束：必须保持同一人身份、五官结构、脸型/身体主要轮廓、服装、发饰和背景关系（姿态/镜头视角/构图见后文姿态对齐规范）。",
            "严禁：不得磨皮过度、不得统一肤色或色温到脱离原片观感、不得锐化到塑料感、不得改变毛孔/纹理/斑点的真实分布；不得改变眼睛/鼻子/眉毛/额头/头发/服装/背景的形态和颜色。",
            "不得新增文字、Logo、二维码或装饰元素；不得改变真实治疗效果。",
            "输出后会做整张差异评分；请把变化幅度控制在低位，避免被识别为重度修图。",
        ]
    ), model_name)


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
    from PIL import Image, ImageDraw

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


def _masked_mean(gray: "Image.Image", mask: "Image.Image", include_mask: bool) -> float:
    from PIL import ImageChops, ImageStat

    active = mask if include_mask else ImageChops.invert(mask)
    stat = ImageStat.Stat(gray, active)
    count = sum(active.histogram()[1:])
    if count <= 0:
        return 0.0
    return round(float(stat.mean[0]) / 255 * 100, 3)


def _create_difference_heatmap(
    original_path: Path,
    generated_path: Path,
    output_path: Path,
    focus_regions: list[dict[str, Any]],
) -> dict[str, Any]:
    from PIL import Image, ImageChops, ImageEnhance, ImageOps, ImageStat

    with Image.open(original_path) as original_img, Image.open(generated_path) as generated_img:
        original = original_img.convert("RGB")
        generated = generated_img.convert("RGB")
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
    prompt = build_after_enhancement_prompt(focus_targets, focus_regions)
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
    prompt = build_after_enhancement_prompt(focus_targets, focus_regions, model_name)
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
    generated = Path(str(raw.get("imagePath") or raw.get("generatedImagePath") or ""))
    if not raw.get("success") or not generated.is_file():
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

    original_after = _copy_reference(after_image_path, output_dir, "after-original")
    original_before = _copy_reference(before_image_path, output_dir, "before-reference") if before_image_path else None
    ext = generated.suffix.lower() or ".jpg"
    final_path = output_dir / f"after-ai-enhanced{ext}"
    watermarked, watermark_error = _copy_with_watermark(generated, final_path)
    heatmap_path = output_dir / "difference-heatmap.png"
    difference_analysis = _create_difference_heatmap(
        after_image_path,
        generated,
        heatmap_path,
        focus_regions,
    )
    audit = {
        "provider": DEFAULT_PROVIDER,
        "model_name": raw.get("model") or model_name,
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
