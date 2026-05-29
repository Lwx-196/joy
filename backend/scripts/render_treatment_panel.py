"""CLI: 真实术前照 + 术式描述 → 独立治疗区标注 panel（cv2 线稿 + atlas 几何）。

用法：
  python -m backend.scripts.render_treatment_panel \
      --image <术前.jpg> --focus "玻尿酸注射面颊，下巴" \
      --model /path/face_landmarker.task --out panel.jpg [--title "林真呈 术前"]

或给 case 目录（自动找 术前*.jpg 最正一张 + 用目录名当术式）：
  python -m backend.scripts.render_treatment_panel --case-dir <患者/术式> --model ... --out ...
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from backend.services import treatment_zone_panel as tzp  # noqa: E402


def _pick_before(case_dir: str) -> str | None:
    befores = sorted(glob.glob(os.path.join(case_dir, "术前*.jpg")) +
                     glob.glob(os.path.join(case_dir, "术前*.jpeg")))
    return befores[0] if befores else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image")
    ap.add_argument("--case-dir")
    ap.add_argument("--focus", default="")
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title")
    args = ap.parse_args()

    image_path = args.image
    focus = args.focus
    title = args.title
    if args.case_dir:
        image_path = _pick_before(args.case_dir)
        if not focus:
            focus = os.path.basename(args.case_dir.rstrip("/"))
        if not title:
            parent = os.path.basename(os.path.dirname(args.case_dir.rstrip("/")))
            title = f"{parent} 术前"
    if not image_path or not os.path.isfile(image_path):
        print(f"image not found: {image_path}", file=sys.stderr)
        return 2

    img = cv2.imread(image_path)
    if img is None:
        print(f"cannot read image: {image_path}", file=sys.stderr)
        return 2

    panel, regions = tzp.panel_for_targets(img, focus, args.model, title=title)
    print(f"focus='{focus}' → regions={regions}")
    if panel is None:
        print("no face detected → no panel", file=sys.stderr)
        return 1
    cv2.imwrite(args.out, panel, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"wrote {args.out} ({panel.shape[1]}x{panel.shape[0]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
