"""Generate a formal render human-review packet from real final-board outputs.

T65 is evidence-only: it copies existing final-board and manifest artifacts
from the T64 warning queue and leaves reviewer decisions blank. Missing
artifacts are reported as missing; no placeholder images are generated.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/Users/a1234/Desktop/案例生成器/case-workbench")
DEFAULT_INPUT = ROOT / "tasks" / "t64_quality_warning_repair_queue_final.json"
DEFAULT_OUTPUT_DIR = ROOT / "tasks" / "t65_formal_review_packet"
DEFAULT_SUMMARY_OUTPUT = ROOT / "tasks" / "t65_formal_review_packet_summary.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "tasks" / "t65_formal_review_packet_summary.md"
UNVERIFIED = "未验证/无法获取"
ALLOWED_DECISIONS = [
    "accept_template_downgrade",
    "needs_slot_fill",
    "needs_reselect",
    "needs_rerender",
    "reject",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-").lower()
    return slug or "review-unit"


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _unit_id(item: dict[str, Any]) -> str:
    case_id = int(item.get("case_id") or 0)
    job_id = int(item.get("job_id") or 0)
    return f"case{case_id}-job{job_id}"


def _copy_if_file(source_path: str | None, target: Path) -> tuple[str | None, dict[str, Any] | None]:
    source = Path(str(source_path or ""))
    if not source.is_file():
        return None, {"source_path": str(source), "reason": f"{UNVERIFIED}: source artifact missing"}
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return str(target), None


def _decision_draft(unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "unit_id": unit["unit_id"],
        "case_id": unit["case_id"],
        "job_id": unit["job_id"],
        "reviewer": None,
        "decision": None,
        "allowed_decisions": ALLOWED_DECISIONS,
        "review_note": None,
        "decision_required": True,
    }


def _render_html(manifest: dict[str, Any]) -> str:
    sections: list[str] = []
    for unit in manifest.get("review_units") or []:
        if not isinstance(unit, dict):
            continue
        unit_id = html.escape(str(unit.get("unit_id") or ""))
        case_id = html.escape(str(unit.get("case_id") or ""))
        job_id = html.escape(str(unit.get("job_id") or ""))
        customer = html.escape(str(unit.get("customer_raw") or ""))
        template = html.escape(str(unit.get("template") or ""))
        score = html.escape(str(unit.get("quality_score") or ""))
        rel = html.escape(str(unit.get("packet_final_board_relative_path") or ""))
        image = (
            f'<img src="{rel}" alt="final board {unit_id}">'
            if rel
            else '<div class="missing">无法获取真实 final-board</div>'
        )
        warnings = "".join(
            f"<li>{html.escape(str(item))}</li>"
            for item in _as_list(unit.get("warning_samples"))
        )
        decision_rows = "".join(f"<code>{html.escape(item)}</code> " for item in ALLOWED_DECISIONS)
        sections.append(
            "<section class=\"unit\">"
            f"<h2>{unit_id}</h2>"
            f"<p>case {case_id} · job {job_id} · {customer} · template {template} · score {score}</p>"
            f"<figure>{image}</figure>"
            f"<h3>Warnings</h3><ul>{warnings or '<li>无 warning 样本</li>'}</ul>"
            f"<h3>Decision</h3><p>{decision_rows}</p>"
            "</section>"
        )
    return (
        "<!doctype html>\n"
        "<html lang=\"zh-CN\">\n"
        "<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>T65 Formal Render Human Review</title>\n"
        "<style>\n"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:24px;background:#f6f7f8;color:#20242a;}"
        "header{margin-bottom:18px;}h1{font-size:24px;margin:0 0 8px;}h2{font-size:17px;margin:0 0 6px;}h3{font-size:13px;margin:14px 0 6px;}"
        ".unit{background:#fff;border:1px solid #d9dde3;border-radius:8px;margin:0 0 20px;padding:14px;}"
        ".unit p{margin:0 0 10px;color:#596273;font-size:13px;}figure{margin:0;border:1px solid #e1e5ea;background:#eef1f4;border-radius:6px;padding:8px;}"
        "img{display:block;width:100%;height:auto;max-height:900px;object-fit:contain;background:#eef1f4;}"
        "ul{margin:0 0 0 18px;padding:0;color:#4c5565;font-size:13px;line-height:1.6;}"
        "code{display:inline-block;margin:0 5px 5px 0;padding:3px 6px;border:1px solid #ccd3dd;border-radius:4px;background:#f8fafc;color:#8a1f11;}"
        ".missing{display:flex;align-items:center;justify-content:center;min-height:260px;color:#9a3412;font-weight:700;}"
        "@media(max-width:900px){body{margin:12px;}.unit{padding:10px;}}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        "<header><h1>T65 Formal Render Human Review</h1>"
        "<p>请在 review_decisions_draft.json 中为每个 unit 填写 reviewer 和 decision；本页面只展示真实 final-board。</p></header>\n"
        f"{''.join(sections)}\n"
        "</body>\n"
        "</html>\n"
    )


def build_review_packet(warning_queue: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    review_units: list[dict[str, Any]] = []
    decision_drafts: list[dict[str, Any]] = []
    missing_assets: list[dict[str, Any]] = []
    copied_asset_count = 0

    for item in _as_list(warning_queue.get("action_items")):
        if not isinstance(item, dict):
            continue
        unit_id = _unit_id(item)
        unit_dir = output_dir / "assets" / _slug(unit_id)
        final_target = unit_dir / "final-board.jpg"
        manifest_target = unit_dir / "manifest.final.json"
        packet_final, missing_final = _copy_if_file(item.get("final_board_path"), final_target)
        packet_manifest, missing_manifest = _copy_if_file(item.get("manifest_path"), manifest_target)
        if packet_final:
            copied_asset_count += 1
        if packet_manifest:
            copied_asset_count += 1
        for missing in (missing_final, missing_manifest):
            if missing:
                missing_assets.append({"unit_id": unit_id, **missing})
        unit = {
            "unit_id": unit_id,
            "case_id": int(item.get("case_id") or 0),
            "job_id": int(item.get("job_id") or 0),
            "case_path": item.get("case_path"),
            "customer_raw": item.get("customer_raw"),
            "template": item.get("template"),
            "quality_score": item.get("quality_score"),
            "categories": _as_list(item.get("categories")),
            "primary_category": item.get("primary_category"),
            "warning_samples": _as_list(item.get("warning_samples")),
            "blocking_issue_samples": _as_list(item.get("blocking_issue_samples")),
            "recommended_next_step": item.get("recommended_next_step"),
            "source_final_board_path": item.get("final_board_path"),
            "source_manifest_path": item.get("manifest_path"),
            "packet_final_board_path": packet_final,
            "packet_manifest_path": packet_manifest,
            "packet_final_board_relative_path": (
                Path(packet_final).relative_to(output_dir).as_posix() if packet_final else None
            ),
            "packet_manifest_relative_path": (
                Path(packet_manifest).relative_to(output_dir).as_posix() if packet_manifest else None
            ),
            "ready_for_review": bool(packet_final and packet_manifest),
            "blocks_publish": True,
            "can_publish_without_human_review": False,
        }
        review_units.append(unit)
        decision_drafts.append(_decision_draft(unit))

    manifest = {
        "generated_at": _now(),
        "scope": "t65_formal_render_human_review_packet_manifest_v1",
        "source_scope": warning_queue.get("scope"),
        "source_run_status": warning_queue.get("run_status"),
        "used_mock_data": False,
        "review_unit_count": len(review_units),
        "ready_review_unit_count": sum(1 for unit in review_units if unit.get("ready_for_review")),
        "copied_asset_count": copied_asset_count,
        "missing_asset_count": len(missing_assets),
        "ready_for_review": bool(review_units) and not missing_assets and all(unit["ready_for_review"] for unit in review_units),
        "allowed_decisions": ALLOWED_DECISIONS,
        "review_units": review_units,
        "missing_assets": missing_assets,
        "notes": [
            "只复制 T64 warning queue 中已存在的真实 final-board 和 manifest。",
            "不会生成占位图，不会填充 reviewer/decision。",
            "review decision 导入前，所有 done_with_issues 继续 blocks_publish=true。",
        ],
    }
    draft = {
        "generated_at": _now(),
        "scope": "t65_formal_render_human_review_decisions_draft_v1",
        "instructions": "每个 decision 必须填写 reviewer 与 decision；空决策不计入正式交付证据。",
        "allowed_decisions": ALLOWED_DECISIONS,
        "decisions": decision_drafts,
    }
    _write_json(output_dir / "manifest.json", manifest)
    _write_json(output_dir / "review_decisions_draft.json", draft)
    (output_dir / "index.html").write_text(_render_html(manifest), encoding="utf-8")
    return {
        "generated_at": _now(),
        "scope": "t65_formal_render_human_review_packet_summary_v1",
        "run_status": "completed_real_formal_review_packet" if manifest["review_unit_count"] else "blocked_missing_warning_queue",
        "decision": (
            "T65 正式 render 人工复核包已生成；未导入真实 reviewer 决策前不放行。"
            if manifest["ready_for_review"]
            else f"{UNVERIFIED}：复核包缺少真实 final-board 或 manifest。"
        ),
        "output_dir": str(output_dir),
        "html_path": str(output_dir / "index.html"),
        "manifest_path": str(output_dir / "manifest.json"),
        "decision_draft_path": str(output_dir / "review_decisions_draft.json"),
        "review_unit_count": manifest["review_unit_count"],
        "ready_review_unit_count": manifest["ready_review_unit_count"],
        "copied_asset_count": manifest["copied_asset_count"],
        "missing_asset_count": manifest["missing_asset_count"],
        "ready_for_review": manifest["ready_for_review"],
        "used_mock_data": False,
    }


def render_summary_markdown(summary: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# T65 Formal Render Human Review Packet",
            "",
            f"- run_status: `{summary.get('run_status')}`",
            f"- decision: {summary.get('decision')}",
            f"- review_unit_count: `{summary.get('review_unit_count')}`",
            f"- ready_review_unit_count: `{summary.get('ready_review_unit_count')}`",
            f"- missing_asset_count: `{summary.get('missing_asset_count')}`",
            f"- html_path: `{summary.get('html_path')}`",
            f"- decision_draft_path: `{summary.get('decision_draft_path')}`",
            "",
        ]
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate T65 formal render human-review packet.")
    parser.add_argument("--warning-queue-json", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--summary-output", type=Path, default=DEFAULT_SUMMARY_OUTPUT)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    summary = build_review_packet(_load_json(args.warning_queue_json), args.output_dir)
    _write_json(args.summary_output, summary)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.write_text(render_summary_markdown(summary), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
