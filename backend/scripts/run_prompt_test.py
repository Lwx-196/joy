"""Single-shot prompt-v2 test on case 88.

Goal: validate the rewritten _POSE_ALIGNMENT_CLAUSE alone (no Path A/C wrapping,
no face composite, no two-pass). Just one direct enhance.js call with:
  - image: 原术后 (AFTER)
  - pose-ref: BEFORE (the only ref)
  - prompt: build_after_enhancement_prompt(focus_targets, [], model)

If this succeeds (角度对术前 + 不照搬细节 + 医美增强保持), we win on the
prompt path. Otherwise we move on (other model / pre-warp).

Usage:
  python3 backend/scripts/run_prompt_test.py [--label v2]

Outputs:
  /tmp/case88_prompt_<label>/generated.png
  /tmp/case88_prompt_<label>/audit.json
  /tmp/case88_prompt_<label>/compare.jpg
  /tmp/case88_prompt_<label>/summary.json
  /tmp/case88_prompt_<label>/prompt.txt    (final prompt sent to enhance.js)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
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


def load_env() -> dict[str, str]:
    env = os.environ.copy()
    if PS_ENV_FILE.is_file():
        for raw in PS_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if k.strip() and k.strip() not in env:
                env[k.strip()] = v
    return env


def get_prompt_from_backend() -> str:
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


def run_enhance(prompt: str, out_dir: Path, max_attempts: int = 3) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "node", str(ENHANCE_SCRIPT),
        "--image", str(AFTER_IMAGE),
        "--pose-ref", str(BEFORE_IMAGE),
        "--prompt", prompt,
        "--quality", QUALITY,
        "--model", MODEL,
    ]
    last_raw: dict[str, Any] | None = None
    elapsed = 0.0
    for attempt in range(1, max_attempts + 1):
        t0 = time.time()
        print(f"\n=== enhance.js attempt {attempt}/{max_attempts} ===")
        proc = subprocess.run(
            cmd, text=True, capture_output=True, env=load_env(),
            timeout=TIMEOUT_SEC, check=False,
        )
        elapsed = time.time() - t0
        print(f"=== attempt {attempt} done in {elapsed:.1f}s rc={proc.returncode} ===")
        if proc.returncode != 0:
            print(proc.stderr[-1500:])
            if attempt == max_attempts:
                raise RuntimeError(f"enhance.js exit {proc.returncode}")
            time.sleep(5); continue

        text = proc.stdout.strip()
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            s, e = text.find("{"), text.rfind("}")
            if s < 0 or e <= s:
                if attempt == max_attempts:
                    raise
                time.sleep(5); continue
            raw = json.loads(text[s:e+1])
        last_raw = raw
        if raw.get("success") and raw.get("imagePath"):
            break
        deg = raw.get("degradations") or []
        deg_msg = ", ".join(f"{d.get('step')}:{d.get('error','')}" for d in deg if isinstance(d, dict))
        print(f"  attempt {attempt} success=false  degradations={deg_msg or 'n/a'}")
        if attempt == max_attempts:
            print(json.dumps(raw, ensure_ascii=False, indent=2)[:1500])
            raise RuntimeError(f"enhance returned success=false after {attempt} attempts")
        time.sleep(8)

    raw = last_raw
    assert raw is not None and raw.get("success")
    src = Path(raw["imagePath"])
    saved = out_dir / f"generated{src.suffix or '.png'}"
    saved.write_bytes(src.read_bytes())
    audit = {
        "elapsed_sec": round(elapsed, 1),
        "imagePath": str(src),
        "savedPath": str(saved),
        "model": raw.get("model"),
        "quality": raw.get("quality"),
        "poseRefCount": raw.get("poseRefCount"),
        "stabilization": raw.get("stabilization"),
        "elapsedSeconds": raw.get("elapsedSeconds"),
        "editPrompt": raw.get("editPrompt"),
        "plannedTasks": raw.get("plannedTasks"),
    }
    (out_dir / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return audit


def build_compare(before: Path, after: Path, generated: Path, out: Path, label: str) -> None:
    import cv2, numpy as np
    panels = []
    labels = ["术前 (target angle)", "原术后", f"新 prompt 输出 ({label})"]
    for p in (before, after, generated):
        img = cv2.imread(str(p))
        if img is None:
            raise RuntimeError(f"compare: cannot read {p}")
        panels.append(img)
    target_h = 1200
    resized = []
    for img in panels:
        h, w = img.shape[:2]
        scale = target_h / h
        resized.append(cv2.resize(img, (int(round(w*scale)), target_h), interpolation=cv2.INTER_AREA))
    band_h = 60
    composed = []
    for img, lab in zip(resized, labels):
        h, w = img.shape[:2]
        canvas = np.zeros((h+band_h, w, 3), dtype=np.uint8)
        canvas[band_h:, :] = img
        cv2.rectangle(canvas, (0, 0), (w, band_h), (40, 40, 40), -1)
        cv2.putText(canvas, lab, (16, band_h-18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255, 255, 255), 2, cv2.LINE_AA)
        composed.append(canvas)
    total_w = sum(p.shape[1] for p in composed)
    big = np.zeros((composed[0].shape[0], total_w, 3), dtype=np.uint8)
    x = 0
    for p in composed:
        w = p.shape[1]; big[:, x:x+w] = p; x += w
    cv2.imwrite(str(out), big, [cv2.IMWRITE_JPEG_QUALITY, 90])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="v2", help="output dir suffix")
    args = ap.parse_args()

    out_dir = Path(f"/tmp/case88_prompt_{args.label}")
    out_dir.mkdir(parents=True, exist_ok=True)

    prompt = get_prompt_from_backend()
    (out_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    print(f"prompt length: {len(prompt)} chars")

    audit = run_enhance(prompt, out_dir)
    generated = Path(audit["savedPath"])

    compare = out_dir / "compare.jpg"
    build_compare(BEFORE_IMAGE, AFTER_IMAGE, generated, compare, args.label)

    summary = {
        "case_id": 88,
        "label": args.label,
        "after": str(AFTER_IMAGE),
        "before": str(BEFORE_IMAGE),
        "model": MODEL,
        "quality": QUALITY,
        "audit": audit,
        "compare": str(compare),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nDone: {out_dir}")
    print(f"Compare: {compare}")


if __name__ == "__main__":
    main()
