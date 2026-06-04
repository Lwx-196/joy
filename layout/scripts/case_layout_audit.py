#!/usr/bin/env python3
"""case_layout_audit.py

批量扫描案例库，复用 case-layout-board 的 inspect 逻辑做体检分类。
输出：
- audit-summary.json
- audit-summary.csv
- audit-summary.md
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASE_LAYOUT_PATH = Path(__file__).resolve().parent / "case_layout_board.py"
CASE_LAYOUT_SPEC = importlib.util.spec_from_file_location("case_layout_board", CASE_LAYOUT_PATH)
if CASE_LAYOUT_SPEC is None or CASE_LAYOUT_SPEC.loader is None:
    raise RuntimeError(f"无法加载 case_layout_board.py: {CASE_LAYOUT_PATH}")
CASE_LAYOUT = importlib.util.module_from_spec(CASE_LAYOUT_SPEC)
CASE_LAYOUT_SPEC.loader.exec_module(CASE_LAYOUT)


def contains_labeled_images(case_dir: Path, recursive: bool = True) -> bool:
    iterator = case_dir.rglob("*") if recursive else case_dir.iterdir()
    phase_hint_count = 0
    phase_image_count = 0
    phase_set = set()
    for item in iterator:
        if not CASE_LAYOUT.is_image_file(item):
            continue
        if "术前" in item.stem or "术后" in item.stem:
            return True
        phase, _source = CASE_LAYOUT.infer_phase_from_path(item, case_dir)
        if phase:
            phase_image_count += 1
            phase_set.add(phase)
            if CASE_LAYOUT.parse_angle_hint(item.stem):
                phase_hint_count += 1
    if {"before", "after"}.issubset(phase_set) and phase_hint_count >= 2 and phase_image_count <= 24:
        return True
    return False


def discover_case_dirs(root_dir: Path) -> list[dict]:
    root_dir = root_dir.resolve()
    cases: list[dict] = []

    if contains_labeled_images(root_dir, recursive=False):
        cases.append({
            "customer": root_dir.parent.name,
            "case_name": root_dir.name,
            "case_dir": root_dir,
        })
        return cases

    for customer_dir in sorted(root_dir.iterdir()):
        if not customer_dir.is_dir() or customer_dir.name.startswith("."):
            continue
        direct_case = contains_labeled_images(customer_dir, recursive=False)
        has_child_cases = False
        for case_dir in sorted(customer_dir.iterdir()):
            if not case_dir.is_dir() or case_dir.name.startswith("."):
                continue
            if contains_labeled_images(case_dir):
                has_child_cases = True
                cases.append({
                    "customer": customer_dir.name,
                    "case_name": case_dir.name,
                    "case_dir": case_dir.resolve(),
                })
        if direct_case and not has_child_cases:
            cases.append({
                "customer": customer_dir.name,
                "case_name": customer_dir.name,
                "case_dir": customer_dir.resolve(),
            })
    return cases


def summarize_selected_slots(manifest: dict) -> list[str]:
    slots = set()
    for group in manifest.get("groups", []):
        slots.update(group.get("selected_slots", {}).keys())
    slot_order = list(CASE_LAYOUT.ANGLE_SLOTS)
    if "back" not in slot_order:
        slot_order.append("back")
    return [slot for slot in slot_order if slot in slots]


def summarize_effective_templates(manifest: dict) -> list[str]:
    templates = []
    for group in manifest.get("groups", []):
        template = group.get("effective_template")
        if template and template not in templates:
            templates.append(template)
    return templates


def classify_manifest(manifest: dict) -> tuple[str, list[str]]:
    selected_slots = summarize_selected_slots(manifest)
    effective_templates = summarize_effective_templates(manifest)
    blockers = manifest.get("blocking_issues", [])
    flags: list[str] = []

    if any("面部检测失败" in item for item in blockers):
        flags.append("face_detection_failure")
    if any("命中过多显式候选" in item for item in blockers):
        flags.append("ambiguous_candidates")
    if any("未找到带术前/术后命名的源图" in item for item in blockers):
        flags.append("no_labeled_sources")
    if "front" in selected_slots:
        flags.append("has_front")
    if "oblique" in selected_slots:
        flags.append("has_oblique")
    if "side" in selected_slots:
        flags.append("has_side")
    if "back" in selected_slots:
        flags.append("has_back")
    if manifest.get("status") == "ok":
        flags.append("enhance_after_supported")
        if "body-dual-compare" in effective_templates:
            return "ready_body_dual_compare", flags
        if "tri-compare" in effective_templates:
            return "ready_tri_compare", flags
        if "bi-compare" in effective_templates:
            return "ready_bi_compare", flags
        if "single-compare" in effective_templates:
            return "ready_single_compare", flags
        return "ready_unknown", flags

    if selected_slots == ["front"]:
        return "front_only", flags
    if selected_slots and ("oblique" not in selected_slots or "side" not in selected_slots):
        return "missing_nonfront", flags
    if "no_labeled_sources" in flags:
        return "no_labeled_sources", flags
    if "ambiguous_candidates" in flags:
        return "ambiguous_candidates", flags
    if "face_detection_failure" in flags:
        return "face_detection_failure", flags
    return "other_blocked", flags


def make_record(case_meta: dict, manifest: dict) -> dict:
    selected_slots = summarize_selected_slots(manifest)
    effective_templates = summarize_effective_templates(manifest)
    primary_category, flags = classify_manifest(manifest)
    first_blocker = manifest.get("blocking_issues", [""])[0] if manifest.get("blocking_issues") else ""
    return {
        "customer": case_meta["customer"],
        "case_name": case_meta["case_name"],
        "case_dir": str(case_meta["case_dir"]),
        "status": manifest.get("status"),
        "primary_category": primary_category,
        "flags": flags,
        "selected_slots": selected_slots,
        "effective_templates": effective_templates,
        "blocking_issue_count": manifest.get("blocking_issue_count", 0),
        "warning_count": manifest.get("warning_count", 0),
        "group_count": len(manifest.get("groups", [])),
        "first_blocker": first_blocker,
        "blocking_issues": manifest.get("blocking_issues", []),
        "warnings": manifest.get("warnings", []),
    }


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "customer",
        "case_name",
        "case_dir",
        "status",
        "primary_category",
        "selected_slots",
        "effective_templates",
        "flags",
        "blocking_issue_count",
        "warning_count",
        "group_count",
        "first_blocker",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            out = dict(row)
            out["selected_slots"] = ",".join(out["selected_slots"])
            out["effective_templates"] = ",".join(out["effective_templates"])
            out["flags"] = ",".join(out["flags"])
            writer.writerow({field: out.get(field, "") for field in fields})


def build_markdown(summary: dict) -> str:
    lines = [
        "# case-layout-board 案例库体检",
        "",
        f"- 时间: `{summary['created_at']}`",
        f"- 根目录: `{summary['root_dir']}`",
        f"- 品牌: `{summary['brand']}`",
        f"- 总案例数: `{summary['case_count']}`",
        "",
        "## 分类汇总",
        "",
    ]

    for category, count in sorted(summary["category_counts"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- `{category}`: {count}")

    lines.extend(["", "## 可直接三角度出板", ""])
    ready_rows = [row for row in summary["records"] if row["primary_category"].startswith("ready_")]
    if ready_rows:
        for row in ready_rows[:20]:
            lines.append(f"- `{row['customer']} / {row['case_name']}` → `{row['primary_category']}`")
    else:
        lines.append("- 无")

    lines.extend(["", "## 需人工关注", ""])
    focus_rows = [row for row in summary["records"] if not row["primary_category"].startswith("ready_")]
    if focus_rows:
        for row in focus_rows[:30]:
            lines.append(
                f"- `{row['customer']} / {row['case_name']}` → `{row['primary_category']}`"
                + (f" | {row['first_blocker']}" if row["first_blocker"] else "")
            )
    else:
        lines.append("- 无")

    return "\n".join(lines).strip() + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量体检医美案例库")
    parser.add_argument("root_dir", help="案例库根目录，例如 医美资料/陈院案例(1)")
    parser.add_argument("--brand", default="fumei", choices=sorted(CASE_LAYOUT.BRANDS.keys()))
    parser.add_argument("--template", default="tri-compare", choices=["tri-compare"])
    parser.add_argument("--out", help="输出目录，默认 <root_dir>/.case-layout-audit/<brand>")
    parser.add_argument("--limit", type=int, default=0, help="仅扫描前 N 个案例，0 表示全部")
    parser.add_argument("--semantic-judge", default="auto", choices=sorted(CASE_LAYOUT.SEMANTIC_JUDGE_MODES), help="语义判官模式，默认 auto")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root_dir = Path(args.root_dir).resolve()
    if not root_dir.exists():
        raise FileNotFoundError(f"根目录不存在: {root_dir}")

    brand = CASE_LAYOUT.resolve_brand(args.brand)
    case_dirs = discover_case_dirs(root_dir)
    if args.limit > 0:
        case_dirs = case_dirs[:args.limit]
    if not case_dirs:
        raise ValueError(f"未在 {root_dir} 发现可体检案例目录")

    records = []
    for case_meta in case_dirs:
        manifest = CASE_LAYOUT.build_manifest(
            case_meta["case_dir"],
            brand,
            args.template,
            semantic_judge_mode=args.semantic_judge,
        )
        records.append(make_record(case_meta, manifest))

    category_counts = Counter(record["primary_category"] for record in records)
    summary = {
        "created_at": CASE_LAYOUT.now_iso(),
        "root_dir": str(root_dir),
        "brand": args.brand,
        "template": args.template,
        "semantic_judge_mode": args.semantic_judge,
        "case_count": len(records),
        "category_counts": dict(category_counts),
        "records": records,
    }

    out_dir = Path(args.out).resolve() if args.out else root_dir / ".case-layout-audit" / args.brand
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "audit-summary.json"
    csv_path = out_dir / "audit-summary.csv"
    md_path = out_dir / "audit-summary.md"

    write_json(json_path, summary)
    write_csv(csv_path, records)
    md_path.write_text(build_markdown(summary), encoding="utf-8")

    print(json.dumps({
        "case_count": len(records),
        "json_path": str(json_path.resolve()),
        "csv_path": str(csv_path.resolve()),
        "md_path": str(md_path.resolve()),
        "category_counts": dict(category_counts),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
