"""Generate a human-review packet for real ComfyUI A/B outputs.

The packet is intentionally evidence-only: it copies existing output images
from review_assets and leaves all winner fields blank for a human reviewer.
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

PREFERRED_OUTPUT_KINDS = ("generated_raw", "ai_after_simulation")
REQUIRED_ROLES = ("baseline", "candidate")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "").strip()).strip("-").lower()
    return slug or "ab-unit"


def _role_assets(decision: dict[str, Any], role: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for asset in decision.get("review_assets") or []:
        if isinstance(asset, dict) and str(asset.get("role") or "").strip().lower() == role:
            out.append(asset)
    return out


def _find_existing_preferred_ref(asset: dict[str, Any]) -> dict[str, Any] | None:
    refs = [ref for ref in asset.get("output_refs") or [] if isinstance(ref, dict)]
    for kind in PREFERRED_OUTPUT_KINDS:
        preferred = [ref for ref in refs if str(ref.get("kind") or "") == kind]
        for ref in preferred:
            path = Path(str(ref.get("path") or ""))
            if kind == "generated_raw" and ".case-workbench-simulation-inputs" in path.parts:
                continue
            if path.is_file():
                return ref
    return None


def _copy_selected_asset(
    *,
    decision: dict[str, Any],
    asset: dict[str, Any],
    role: str,
    output_dir: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    unit_id = str(decision.get("ab_unit_id") or decision.get("unit_id") or decision.get("case_id") or "").strip()
    selected_ref = _find_existing_preferred_ref(asset)
    if not selected_ref:
        return None, {
            "ab_unit_id": unit_id,
            "role": role,
            "variant": asset.get("variant"),
            "reason": f"无法获取现有 {'/'.join(PREFERRED_OUTPUT_KINDS)} 输出图",
            "checked_paths": [
                str(ref.get("path") or "")
                for ref in asset.get("output_refs") or []
                if isinstance(ref, dict) and str(ref.get("kind") or "") in PREFERRED_OUTPUT_KINDS
            ],
        }

    source = Path(str(selected_ref.get("path") or ""))
    unit_dir = output_dir / "assets" / _slug(unit_id)
    suffix = source.suffix or ".img"
    target = unit_dir / f"{role}{suffix}"
    unit_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    copied = {
        "role": role,
        "variant": asset.get("variant"),
        "source_path": str(source),
        "packet_path": str(target),
        "packet_relative_path": target.relative_to(output_dir).as_posix(),
        "kind": selected_ref.get("kind"),
        "watermarked": selected_ref.get("watermarked"),
        "simulation_job_id": asset.get("simulation_job_id"),
        "status": asset.get("status"),
    }
    return copied, None


def _build_decision_draft(decision: dict[str, Any], copied_assets: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ab_unit_id": decision.get("ab_unit_id") or decision.get("unit_id") or str(decision.get("case_id") or ""),
        "case_id": decision.get("case_id"),
        "view": decision.get("view"),
        "workflow": decision.get("workflow"),
        "variants": decision.get("variants") or [],
        "packet_assets": copied_assets,
        "winner_role": None,
        "winner_variant": None,
        "reviewer": None,
        "review_note": None,
        "decision_required": True,
    }


def _render_html(manifest: dict[str, Any]) -> str:
    rows: list[str] = []
    for unit in manifest.get("review_units") or []:
        unit_id = html.escape(str(unit.get("ab_unit_id") or ""))
        case_id = html.escape(str(unit.get("case_id") or ""))
        view = html.escape(str(unit.get("view") or ""))
        workflow = html.escape(str(unit.get("workflow") or ""))
        assets_by_role = {
            str(asset.get("role") or ""): asset
            for asset in unit.get("packet_assets") or []
            if isinstance(asset, dict)
        }
        image_cells: list[str] = []
        for role in REQUIRED_ROLES:
            asset = assets_by_role.get(role)
            if asset:
                rel_path = html.escape(str(asset.get("packet_relative_path") or ""))
                variant = html.escape(str(asset.get("variant") or ""))
                image_cells.append(
                    f"<figure><figcaption>{html.escape(role)}<br><span>{variant}</span></figcaption>"
                    f'<img src="{rel_path}" alt="{html.escape(role)} output for {unit_id}"></figure>'
                )
            else:
                image_cells.append(f"<figure><figcaption>{html.escape(role)}</figcaption><div class=\"missing\">无法获取</div></figure>")
        rows.append(
            "<section class=\"unit\">"
            f"<h2>{unit_id}</h2>"
            f"<p>case {case_id} · {view} · {workflow}</p>"
            f"<div class=\"images\">{''.join(image_cells)}</div>"
            "</section>"
        )
    return (
        "<!doctype html>\n"
        "<html lang=\"zh-CN\">\n"
        "<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>ComfyUI A/B Human Review Packet</title>\n"
        "<style>\n"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:24px;background:#f7f7f5;color:#1f2328;}"
        "header{margin-bottom:20px;}h1{font-size:24px;margin:0 0 8px;}h2{font-size:16px;margin:0 0 6px;}"
        ".unit{background:#fff;border:1px solid #d8d8d0;border-radius:8px;margin:0 0 18px;padding:14px;}"
        ".unit p{margin:0 0 12px;color:#5b616e;font-size:13px;}.images{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;}"
        "figure{margin:0;border:1px solid #e3e3dc;border-radius:6px;padding:8px;background:#fbfbf9;}"
        "figcaption{font-weight:600;font-size:13px;margin-bottom:8px;}figcaption span{font-weight:400;color:#5b616e;}"
        "img{display:block;width:100%;height:auto;max-height:720px;object-fit:contain;background:#ecece7;}"
        ".missing{display:flex;align-items:center;justify-content:center;min-height:220px;background:#ecece7;color:#8a4b15;font-weight:600;}"
        "@media(max-width:900px){.images{grid-template-columns:1fr;}body{margin:12px;}}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        "<header><h1>ComfyUI A/B Human Review Packet</h1>"
        "<p>人工决策请填写 review_decisions_draft.json；本页面只展示已存在的真实输出图。</p></header>\n"
        f"{''.join(rows)}\n"
        "</body>\n"
        "</html>\n"
    )


def build_review_packet(template: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    review_units: list[dict[str, Any]] = []
    decision_drafts: list[dict[str, Any]] = []
    missing_assets: list[dict[str, Any]] = []
    copied_asset_count = 0

    for decision in template.get("decisions") or []:
        if not isinstance(decision, dict):
            continue
        unit_copied_assets: list[dict[str, Any]] = []
        for role in REQUIRED_ROLES:
            role_asset = next(iter(_role_assets(decision, role)), None)
            if not role_asset:
                missing_assets.append(
                    {
                        "ab_unit_id": decision.get("ab_unit_id") or decision.get("unit_id") or str(decision.get("case_id") or ""),
                        "role": role,
                        "reason": "无法获取 review_assets role",
                    }
                )
                continue
            copied, missing = _copy_selected_asset(
                decision=decision,
                asset=role_asset,
                role=role,
                output_dir=output_dir,
            )
            if copied:
                unit_copied_assets.append(copied)
                copied_asset_count += 1
            if missing:
                missing_assets.append(missing)

        unit = {
            "ab_unit_id": decision.get("ab_unit_id") or decision.get("unit_id") or str(decision.get("case_id") or ""),
            "case_id": decision.get("case_id"),
            "view": decision.get("view"),
            "workflow": decision.get("workflow"),
            "variants": decision.get("variants") or [],
            "packet_assets": unit_copied_assets,
            "ready_for_review": len({asset.get("role") for asset in unit_copied_assets}) == len(REQUIRED_ROLES),
        }
        review_units.append(unit)
        decision_drafts.append(_build_decision_draft(decision, unit_copied_assets))

    manifest = {
        "generated_at": _now(),
        "scope": "t46_comfyui_human_review_packet_manifest_v1",
        "source_template_scope": template.get("scope"),
        "review_unit_count": len(review_units),
        "copied_asset_count": copied_asset_count,
        "missing_asset_count": len(missing_assets),
        "ready_for_review": bool(review_units) and not missing_assets and all(unit["ready_for_review"] for unit in review_units),
        "review_units": review_units,
        "missing_assets": missing_assets,
        "notes": [
            "优先复制 review_assets 中已存在的真实 generated_raw 清洁输出图；缺失时才回退 ai_after_simulation 水印图。",
            "不会生成占位图，不会填充 winner/reviewer。",
        ],
    }
    decision_draft = {
        "generated_at": _now(),
        "scope": "t46_comfyui_ab_review_decisions_draft_v1",
        "instructions": "人工 reviewer 必须填写 reviewer、winner_role 或 winner_variant；空草稿不计入 winner evidence。",
        "decisions": decision_drafts,
    }

    _write_json(output_dir / "manifest.json", manifest)
    _write_json(output_dir / "review_decisions_draft.json", decision_draft)
    (output_dir / "index.html").write_text(_render_html(manifest), encoding="utf-8")

    return {
        "generated_at": _now(),
        "scope": "t46_comfyui_human_review_packet_summary_v1",
        "output_dir": str(output_dir),
        "html_path": str(output_dir / "index.html"),
        "manifest_path": str(output_dir / "manifest.json"),
        "decision_draft_path": str(output_dir / "review_decisions_draft.json"),
        "review_unit_count": manifest["review_unit_count"],
        "copied_asset_count": manifest["copied_asset_count"],
        "missing_asset_count": manifest["missing_asset_count"],
        "ready_for_review": manifest["ready_for_review"],
        "notes": manifest["notes"],
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a ComfyUI A/B human-review packet from a review decision template.")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    template = json.loads(args.template.read_text(encoding="utf-8"))
    summary = build_review_packet(template, args.output_dir)
    if args.summary_output:
        _write_json(args.summary_output, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
