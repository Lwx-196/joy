"""CLI: 成品 board 渲染目录 → 内部 QA 标注副本 final-board.annotated.jpg.

成品 final-board.jpg 给客户保持干净；本工具另存带治疗区标注的副本供 operator 核对。
术式从 manifest.final.json(focus_targets / case_dir) 自动取。零 AI、零成本（本地 facemesh）。

用法：
  python -m backend.scripts.annotate_board \
      --out-root <case>/.case-layout-output/<brand>/tri-compare/render \
      [--model /path/face_landmarker.task]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from backend.services import board_annotator as ba  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-root", required=True, help="render 产出目录（含 final-board.jpg）")
    ap.add_argument("--model", default=None, help="face_landmarker.task（缺省用 env / 开发 fallback）")
    args = ap.parse_args()

    res = ba.annotate_render_output(args.out_root, model_path=args.model)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("status") in ("ok", "no-annotation") else 1


if __name__ == "__main__":
    raise SystemExit(main())
