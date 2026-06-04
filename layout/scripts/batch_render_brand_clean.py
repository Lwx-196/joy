#!/usr/bin/env python3
"""batch_render_brand_clean.py

对 audit summary 中 ready_* 案例批量生成正式品牌版。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
CASE_LAYOUT_PATH = SCRIPT_DIR / "case_layout_board.py"
RENDER_PATH = SCRIPT_DIR / "render_brand_clean.py"

case_spec = importlib.util.spec_from_file_location("case_layout_board", CASE_LAYOUT_PATH)
if case_spec is None or case_spec.loader is None:
    raise RuntimeError("无法加载 case_layout_board.py")
CASE_LAYOUT = importlib.util.module_from_spec(case_spec)
case_spec.loader.exec_module(CASE_LAYOUT)

render_spec = importlib.util.spec_from_file_location("render_brand_clean", RENDER_PATH)
if render_spec is None or render_spec.loader is None:
    raise RuntimeError("无法加载 render_brand_clean.py")
RENDER = importlib.util.module_from_spec(render_spec)
render_spec.loader.exec_module(RENDER)


def slugify(name: str) -> str:
    return (
        name.replace("/", "-")
        .replace(" ", "")
        .replace("，", "-")
        .replace(",", "-")
        .replace("：", "-")
        .replace(":", "-")
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量渲染正式品牌版 ready 案例")
    parser.add_argument("audit_summary", help="audit-summary.json 路径")
    parser.add_argument("--brand", default="fumei", choices=sorted(CASE_LAYOUT.BRANDS.keys()))
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--limit", type=int, default=0, help="限制渲染数量")
    parser.add_argument("--semantic-judge", default="auto", choices=sorted(CASE_LAYOUT.SEMANTIC_JUDGE_MODES), help="语义判官模式，默认 auto")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = json.loads(Path(args.audit_summary).read_text(encoding="utf-8"))
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    ready_rows = [row for row in summary["records"] if row["primary_category"].startswith("ready_")]
    if args.limit > 0:
        ready_rows = ready_rows[: args.limit]

    results = []
    for row in ready_rows:
        case_dir = Path(row["case_dir"]).resolve()
        brand = CASE_LAYOUT.resolve_brand(args.brand)
        manifest = CASE_LAYOUT.build_manifest(
            case_dir,
            brand,
            "tri-compare",
            semantic_judge_mode=args.semantic_judge,
        )
        customer = row["customer"]
        case_name = row["case_name"]
        file_name = f"{slugify(customer)}-{slugify(case_name)}-正式品牌版.jpg"
        out_path = out_dir / file_name
        RENDER.render_from_manifest(manifest, out_path)
        results.append({
            "customer": customer,
            "case_name": case_name,
            "primary_category": row["primary_category"],
            "output_path": str(out_path),
        })

    summary_path = out_dir / "batch-render-summary.json"
    summary_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "count": len(results),
        "summary_path": str(summary_path),
        "out_dir": str(out_dir),
        "semantic_judge_mode": args.semantic_judge,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
