"""CLI: case 目录 → 角度路由三联拼图（正面|45°|侧面）治疗区标注。

用法：
  python -m backend.scripts.render_triptych --case-dir <患者/术式> \
      --model /path/face_landmarker.task --out triptych.jpg \
      [--provider tuzi,code77] [--env-file <.env>]
"""
from __future__ import annotations

import argparse
import os
import sys

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from backend.services import image_providers as ip  # noqa: E402
from backend.services import treatment_panel_triptych as tri  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--focus", default="")
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--provider", default="")
    ap.add_argument("--env-file", default=ip.DEFAULT_ENV_FILE)
    args = ap.parse_args()

    env = ip.load_env_file(args.env_file)
    explicit = [x.strip() for x in args.provider.split(",") if x.strip()] or None
    providers = ip.resolve_chain(env, explicit)
    if not providers:
        print("no ready image provider (check --env-file / PANEL_IMG_* / TUZI_IMAGE_PRIMARY_*)",
              file=sys.stderr)
        return 2
    print(f"providers={[p.name for p in providers]}")

    img, cc = tri.build_triptych(args.case_dir, args.model, providers,
                                 focus_text=args.focus or None)
    for r in cc.regions:
        ch = os.path.basename(r.chosen.path) if r.chosen else "-"
        print(f"  {r.region:5s} [{r.status:8s}] {r.note}  ← {ch}")
    cv2.imwrite(args.out, img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"wrote {args.out} ({img.shape[1]}x{img.shape[0]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
