"""Compare Direction A (face-region composite) vs Direction C (two-pass) for case 88.

Direction A:
  1. Enhance after image with the full medical-aesthetic prompt, NO pose-ref.
     The model is free to enhance at the original after-image angle.
  2. Composite the generated face back onto the original after-image, so hair,
     ears, earrings, hairband, neck, and background all come from the real
     after photo.

Direction C:
  1. Pass 1: same as A step 1 (enhance, no pose-ref) -> enhanced_no_pose.
  2. Pass 2: feed pass-1 output as the primary image and the before image as
     pose-ref, with a strict "only realign angle, do not change anything else"
     prompt. The hope is the model only rotates/recomposes without re-copying
     pixels from the before image.

Both branches use gpt-image-2-vip with the same focus_targets from job 19's
audit so we are comparing apples to apples.

Outputs:
  /tmp/case88_a_vs_c/<branch>/...
  /tmp/case88_a_vs_c/compare.jpg

Real subprocess calls go to:
  /Users/a1234/Desktop/飞书Claude/skills/case-layout-board/scripts/case_layout_enhance.js
  /Users/a1234/Desktop/案例生成器/case-workbench/backend/scripts/face_composite.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path("/Users/a1234/Desktop/案例生成器/case-workbench")
VENV_PY = ROOT / ".venv" / "bin" / "python"

ENHANCE_SCRIPT = Path("/Users/a1234/Desktop/飞书Claude/skills/case-layout-board/scripts/case_layout_enhance.js")
FACE_COMPOSITE = Path("/Users/a1234/Desktop/案例生成器/case-workbench/backend/scripts/face_composite.py")
PS_ENV_FILE = Path("/Users/a1234/Desktop/飞书Claude/claude-feishu-bridge/.env")

CASE_DIR = Path("/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/小绿/25.7.2隆鼻，卧蚕，泪沟，唇")
AFTER_IMAGE = CASE_DIR / "07041738_29.jpg"
BEFORE_IMAGE = CASE_DIR / "07041738_04.jpg"
STYLE_REF = Path("/Users/a1234/Downloads/对比照模板.jpg")

MODEL = "gpt-image-2-vip"
QUALITY = "4k"
TIMEOUT_SEC = 600

FOCUS_TARGETS = [
    "鼻山根明显抬高 5-7mm，鼻梁线条立体清晰，侧光下可见明显高光带",
    "卧蚕宽度增加 60%，呈饱满立体弧形，笑起来能看到光影段",
    "泪沟凹陷减少 80%，眼下区域几乎平整，无明显阴影",
    "唇部体积增加 50%，唇珠唇峰饱满立体，唇形更圆润，保持原唇色",
]

ANGLE_ONLY_PROMPT = (
    "任务：只调整第一张图的【人物大姿态、头部转角和俯仰、镜头视角与高度、构图裁切】，对齐到第二张参考图的拍摄角度。\n"
    "硬性约束（必须严格遵守）：\n"
    "- 不要改变第一张图里已有的医美治疗效果（鼻山根、鼻梁、卧蚕、泪沟、唇部都保持现状不动）。\n"
    "- 不要改变第一张图的人物身份、五官比例、发型颜色长度、肤色、皮肤纹理。\n"
    "- 不要从参考图复制任何像素：发丝、皮肤光斑、毛孔、衣服褶皱、首饰位置、背景，全部保留第一张图本身。\n"
    "- 参考图只用于告诉你目标角度和构图，不是视觉模板。\n"
    "输出要求：与第一张图同一个人在另一次拍摄中按参考图角度被拍到的样子，画面像素细节继续来自第一张图本身。"
)


def load_env_for_subprocess() -> dict[str, str]:
    env = os.environ.copy()
    if PS_ENV_FILE.is_file():
        for raw in PS_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in env:
                env[k] = v
    return env


def run_enhance(
    *,
    label: str,
    image_path: Path,
    pose_refs: list[Path],
    prompt: str,
    out_dir: Path,
    max_attempts: int = 3,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "node",
        str(ENHANCE_SCRIPT),
        "--image", str(image_path),
        "--prompt", prompt,
        "--quality", QUALITY,
        "--model", MODEL,
    ]
    for ref in pose_refs:
        cmd.extend(["--pose-ref", str(ref)])

    last_raw: dict[str, Any] | None = None
    elapsed = 0.0
    for attempt in range(1, max_attempts + 1):
        t0 = time.time()
        print(f"\n=== [{label}] enhance.js attempt {attempt}/{max_attempts} (refs={len(pose_refs)}) ===")
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            env=load_env_for_subprocess(),
            timeout=TIMEOUT_SEC,
            check=False,
        )
        elapsed = time.time() - t0
        print(f"=== [{label}] attempt {attempt} done in {elapsed:.1f}s rc={proc.returncode} ===")
        if proc.returncode != 0:
            print(f"stderr tail:\n{proc.stderr[-1500:]}")
            if attempt == max_attempts:
                raise RuntimeError(f"{label}: enhance.js exit {proc.returncode} after {attempt} attempts")
            time.sleep(5)
            continue

        text = proc.stdout.strip()
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            s, e = text.find("{"), text.rfind("}")
            if s < 0 or e <= s:
                if attempt == max_attempts:
                    raise
                time.sleep(5)
                continue
            raw = json.loads(text[s : e + 1])

        last_raw = raw
        if raw.get("success") and raw.get("imagePath"):
            break

        deg = raw.get("degradations") or []
        deg_msg = ", ".join(f"{d.get('step')}:{d.get('error','')}" for d in deg if isinstance(d, dict))
        print(f"  attempt {attempt} success=false  degradations={deg_msg or 'n/a'}")
        if attempt == max_attempts:
            print(json.dumps(raw, ensure_ascii=False, indent=2)[:1500])
            raise RuntimeError(f"{label}: enhance returned success=false after {attempt} attempts")
        time.sleep(8)

    raw = last_raw
    assert raw is not None and raw.get("success")

    src_path = Path(raw["imagePath"])
    if not src_path.is_file():
        raise RuntimeError(f"{label}: imagePath not on disk: {src_path}")

    saved = out_dir / f"generated{src_path.suffix or '.png'}"
    saved.write_bytes(src_path.read_bytes())

    summary = {
        "label": label,
        "elapsed_sec": round(elapsed, 1),
        "imagePath": str(src_path),
        "savedPath": str(saved),
        "model": raw.get("model"),
        "quality": raw.get("quality"),
        "poseRefCount": raw.get("poseRefCount"),
        "stabilization": raw.get("stabilization"),
        "elapsedSeconds": raw.get("elapsedSeconds"),
    }
    (out_dir / "audit.json").write_text(
        json.dumps({**summary, "editPrompt": raw.get("editPrompt"), "plannedTasks": raw.get("plannedTasks")},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def run_face_composite(*, base: Path, face: Path, out: Path, label: str) -> dict[str, Any]:
    cmd = [
        "python3", str(FACE_COMPOSITE),
        "--base", str(base),
        "--face", str(face),
        "--out", str(out),
        "--include-neck",
    ]
    print(f"\n=== [{label}] face_composite start ===")
    t0 = time.time()
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=120, check=False)
    elapsed = time.time() - t0
    print(f"=== [{label}] face_composite done in {elapsed:.1f}s rc={proc.returncode} ===")
    if proc.returncode != 0:
        print(proc.stderr[-2000:])
        raise RuntimeError(f"{label}: face_composite exit {proc.returncode}")
    print(proc.stdout.strip()[-500:])
    return {"label": label, "elapsed_sec": round(elapsed, 1), "out": str(out)}


def build_compare_image(
    *,
    base_after: Path,
    before: Path,
    a_final: Path,
    c_final: Path,
    out_path: Path,
) -> None:
    """Stitch a 4-panel comparison image labeled 术前 / 原术后 / 方向A / 方向C."""
    import cv2
    import numpy as np

    panels = []
    labels = ["术前 (pose-ref)", "原术后", "方向 A: face composite", "方向 C: 双 pass"]
    for img_path in [before, base_after, a_final, c_final]:
        img = cv2.imread(str(img_path))
        if img is None:
            raise RuntimeError(f"compare: cannot read {img_path}")
        panels.append(img)

    target_h = 1200
    resized = []
    for img in panels:
        h, w = img.shape[:2]
        scale = target_h / h
        nw = int(round(w * scale))
        resized.append(cv2.resize(img, (nw, target_h), interpolation=cv2.INTER_AREA))

    band_h = 60
    composed = []
    for img, label in zip(resized, labels):
        h, w = img.shape[:2]
        canvas = np.zeros((h + band_h, w, 3), dtype=np.uint8)
        canvas[band_h:, :] = img
        cv2.rectangle(canvas, (0, 0), (w, band_h), (40, 40, 40), -1)
        cv2.putText(
            canvas, label, (16, band_h - 18),
            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2, cv2.LINE_AA,
        )
        composed.append(canvas)

    total_w = sum(p.shape[1] for p in composed)
    out = np.zeros((composed[0].shape[0], total_w, 3), dtype=np.uint8)
    x = 0
    for p in composed:
        w = p.shape[1]
        out[:, x : x + w] = p
        x += w
    cv2.imwrite(str(out_path), out, [cv2.IMWRITE_JPEG_QUALITY, 90])
    print(f"\nCompare image saved: {out_path}")


def get_base_prompt_from_backend() -> str:
    """Invoke backend venv python to build the medical-aesthetic prompt.

    backend.ai_generation_adapter pulls in fastapi via stress.py, so we cannot
    import it from system python. Round-trip via the project venv keeps the
    prompt source-of-truth in the backend module.
    """
    inline = (
        "import sys, json\n"
        f"sys.path.insert(0, '{ROOT}')\n"
        "from backend.ai_generation_adapter import build_after_enhancement_prompt\n"
        f"focus = {json.dumps(FOCUS_TARGETS, ensure_ascii=False)}\n"
        f"print(build_after_enhancement_prompt(focus, [], '{MODEL}'))\n"
    )
    proc = subprocess.run(
        [str(VENV_PY), "-c", inline],
        text=True, capture_output=True, timeout=30, check=True,
    )
    return proc.stdout.rstrip("\n")


def main() -> None:
    skip_a = "--skip-a" in sys.argv
    out_root = Path("/tmp/case88_a_vs_c")
    out_root.mkdir(parents=True, exist_ok=True)

    base_prompt = get_base_prompt_from_backend()
    angle_only = ANGLE_ONLY_PROMPT

    # ----- Direction A -----
    a_dir = out_root / "A"
    a_final = a_dir / "final_face_composite.jpg"
    if skip_a and a_final.is_file() and (a_dir / "audit.json").is_file():
        print(f"\n=== [A] reusing existing artifacts at {a_dir} ===")
        a_enhance = json.loads((a_dir / "audit.json").read_text(encoding="utf-8"))
        a_composite = {"label": "A.composite", "elapsed_sec": 0.0, "out": str(a_final), "reused": True}
    else:
        a_enhance = run_enhance(
            label="A.enhance",
            image_path=AFTER_IMAGE,
            pose_refs=[STYLE_REF],  # only style ref, no before pose-ref
            prompt=base_prompt,
            out_dir=a_dir,
        )
        a_composite = run_face_composite(
            base=AFTER_IMAGE,
            face=Path(a_enhance["savedPath"]),
            out=a_final,
            label="A.composite",
        )

    # ----- Direction C -----
    c_dir = out_root / "C"
    c_pass1 = run_enhance(
        label="C.pass1",
        image_path=AFTER_IMAGE,
        pose_refs=[STYLE_REF],
        prompt=base_prompt,
        out_dir=c_dir / "pass1",
    )
    c_pass2 = run_enhance(
        label="C.pass2",
        image_path=Path(c_pass1["savedPath"]),
        pose_refs=[BEFORE_IMAGE],
        prompt=angle_only,
        out_dir=c_dir / "pass2",
    )
    c_final = c_dir / "final_two_pass.jpg"
    Path(c_pass2["savedPath"]).replace(c_final)

    # ----- compare image -----
    compare_path = out_root / "compare.jpg"
    build_compare_image(
        base_after=AFTER_IMAGE,
        before=BEFORE_IMAGE,
        a_final=a_final,
        c_final=c_final,
        out_path=compare_path,
    )

    summary = {
        "case_id": 88,
        "after": str(AFTER_IMAGE),
        "before": str(BEFORE_IMAGE),
        "style_ref": str(STYLE_REF),
        "model": MODEL,
        "quality": QUALITY,
        "A": {
            "enhance": a_enhance,
            "composite": a_composite,
            "final": str(a_final),
        },
        "C": {
            "pass1": c_pass1,
            "pass2": c_pass2,
            "final": str(c_final),
        },
        "compare": str(compare_path),
    }
    (out_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nDone. Summary: {out_root / 'summary.json'}")


if __name__ == "__main__":
    main()
