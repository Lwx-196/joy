"""Path 1: reverse two-pass with detail swap.

Direction:
  pass1: enhance(原术后 + BEFORE作pose-ref, base_prompt)
         -> 角度对齐到术前 + 医美增强 + 像素照搬术前细节 (=job 19 outcome)
  pass2: enhance(pass1结果 + 原术后照作pose-ref, detail_swap_prompt)
         -> 保持 pass1 的姿态/角度/医美效果，把发丝/耳朵/首饰/衣服/颈部/背景
            从真实术后照替换回来，修正 pass1 的错误像素照搬

The bet: image-edit models do better at "swap these regions from a reference"
than at "rotate the head", so this should preserve angle alignment AND restore
real after-image details.

Usage:
  python3 backend/scripts/run_path1.py [--skip-pass1]

Outputs:
  /tmp/case88_path1/pass1/...
  /tmp/case88_path1/pass2/...
  /tmp/case88_path1/final.jpg
  /tmp/case88_path1/compare.jpg
  /tmp/case88_path1/summary.json
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
PS_ENV_FILE = Path("/Users/a1234/Desktop/飞书Claude/claude-feishu-bridge/.env")

CASE_DIR = Path("/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/小绿/25.7.2隆鼻，卧蚕，泪沟，唇")
AFTER_IMAGE = CASE_DIR / "07041738_29.jpg"
BEFORE_IMAGE = CASE_DIR / "07041738_04.jpg"

MODEL = "gpt-image-2-vip"
QUALITY = "4k"
TIMEOUT_SEC = 600

FOCUS_TARGETS = [
    "鼻山根明显抬高 5-7mm，鼻梁线条立体清晰，侧光下可见明显高光带",
    "卧蚕宽度增加 60%，呈饱满立体弧形，笑起来能看到光影段",
    "泪沟凹陷减少 80%，眼下区域几乎平整，无明显阴影",
    "唇部体积增加 50%，唇珠唇峰饱满立体，唇形更圆润，保持原唇色",
]

DETAIL_SWAP_PROMPT = (
    "任务：第一张图的整体姿态、头部转角、镜头视角、构图，以及面部医美治疗效果（鼻山根、鼻梁、卧蚕、泪沟、唇部）都是正确的，必须严格保留不动。"
    "但第一张图错误地从其他参考照复制了一些非面部细节（发丝、耳朵、首饰、衣服、颈部、背景），需要用第二张图（真实术后原照）作为视觉真相来修正这些错误细节。\n"
    "\n"
    "必须从第二张图（真实术后原照）替换回来的内容：\n"
    "1. 发型轮廓、发丝走向、发色、刘海、所有发饰（头箍、发夹的款式与位置）\n"
    "2. 耳朵形状和位置、耳钉/耳饰的款式\n"
    "3. 衣服款式、衣领、肩线、面料颜色和质感\n"
    "4. 颈部、锁骨、肩膀区域的肤色、光线、皮肤纹理\n"
    "5. 背景（颜色、虚化感、灯光）\n"
    "\n"
    "必须严格保留第一张图的内容（不要被第二张图的角度或姿态拉走）：\n"
    "1. 头部转角、面部朝向、镜头视角与高度、整体构图与裁切——这是核心，第二张图只用于"
    "提供细节质感，不要让第二张图的姿态/角度/构图影响输出\n"
    "2. 面部五官的医美增强效果（鼻山根抬高、鼻梁立体、卧蚕饱满、泪沟填平、唇部圆润）\n"
    "3. 面部表情、面部肤色和妆面\n"
    "\n"
    "输出要求：与第一张图同一个人在同一姿态、同一角度、同一医美效果下被拍到的样子，但发丝、耳朵、首饰、衣服、颈部、背景全部恢复成第二张图（真实术后原照）的真实样貌。"
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
        json.dumps(
            {**summary, "editPrompt": raw.get("editPrompt"), "plannedTasks": raw.get("plannedTasks")},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return summary


def build_compare_image(
    *,
    before: Path,
    base_after: Path,
    pass1_img: Path,
    final: Path,
    out_path: Path,
) -> None:
    import cv2
    import numpy as np

    panels = []
    labels = [
        "术前 (target angle)",
        "原术后 (detail truth)",
        "Path1.pass1 (angle+copy)",
        "Path1.final (detail swap)",
    ]
    for img_path in [before, base_after, pass1_img, final]:
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
            cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255, 255, 255), 2, cv2.LINE_AA,
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
    skip_pass1 = "--skip-pass1" in sys.argv
    out_root = Path("/tmp/case88_path1")
    out_root.mkdir(parents=True, exist_ok=True)

    base_prompt = get_base_prompt_from_backend()

    pass1_dir = out_root / "pass1"
    pass1_audit = pass1_dir / "audit.json"
    if skip_pass1 and pass1_audit.is_file():
        print(f"\n=== [pass1] reusing existing artifacts at {pass1_dir} ===")
        pass1 = json.loads(pass1_audit.read_text(encoding="utf-8"))
    else:
        pass1 = run_enhance(
            label="pass1",
            image_path=AFTER_IMAGE,
            pose_refs=[BEFORE_IMAGE],
            prompt=base_prompt,
            out_dir=pass1_dir,
        )

    pass2 = run_enhance(
        label="pass2",
        image_path=Path(pass1["savedPath"]),
        pose_refs=[AFTER_IMAGE],
        prompt=DETAIL_SWAP_PROMPT,
        out_dir=out_root / "pass2",
    )

    final = out_root / "final.jpg"
    src = Path(pass2["savedPath"])
    if src.suffix.lower() in {".jpg", ".jpeg"}:
        final.write_bytes(src.read_bytes())
    else:
        import cv2
        img = cv2.imread(str(src))
        cv2.imwrite(str(final), img, [cv2.IMWRITE_JPEG_QUALITY, 95])

    compare_path = out_root / "compare.jpg"
    build_compare_image(
        before=BEFORE_IMAGE,
        base_after=AFTER_IMAGE,
        pass1_img=Path(pass1["savedPath"]),
        final=final,
        out_path=compare_path,
    )

    summary = {
        "case_id": 88,
        "after": str(AFTER_IMAGE),
        "before": str(BEFORE_IMAGE),
        "model": MODEL,
        "quality": QUALITY,
        "path": "Path1 reverse two-pass detail swap",
        "pass1": pass1,
        "pass2": pass2,
        "final": str(final),
        "compare": str(compare_path),
    }
    (out_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nDone. Summary: {out_root / 'summary.json'}")


if __name__ == "__main__":
    main()
