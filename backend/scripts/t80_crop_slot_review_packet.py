"""Generate T80 crop reselection/replacement and slot-fill review packets.

The packet is evidence-only. It reads live review_tickets and source images,
copies available assets into a review folder, and writes blank decision drafts.
It never resolves tickets, never locks crop sources, and never creates render
jobs.
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/Users/a1234/Desktop/案例生成器/case-workbench")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import db  # noqa: E402
from backend.scripts.t78_slot_crop_reduction import (  # noqa: E402
    _candidate_rows,
    _crop_ok,
    _json_dict,
    _open_tickets,
    _safe_front_alternative_pairs,
    _sharpness_ok,
)

DEFAULT_DB_PATH = ROOT / "case-workbench.db"
DEFAULT_OUTPUT_DIR = ROOT / "tasks" / "t80_crop_slot_review_packet"
DEFAULT_SUMMARY_JSON = ROOT / "tasks" / "t80_crop_slot_review_packet_summary.json"
DEFAULT_SUMMARY_MD = ROOT / "tasks" / "t80_crop_slot_review_packet_summary.md"
UNVERIFIED = "未验证/无法获取"

CROP_ALLOWED_ACTIONS = [
    "accept_current_pair",
    "needs_reselect_pair",
    "needs_replace_source",
    "defer_no_safe_alternative",
]
SLOT_ALLOWED_ACTIONS = [
    "manual_phase_view_override",
    "restore_or_add_source_photos",
    "bind_or_rescan_real_source",
    "template_policy_review",
    "defer",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    import re

    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-").lower()
    return slug[:90] or "asset"


def _read_case_paths(conn: sqlite3.Connection, case_ids: set[int]) -> dict[int, str]:
    if not case_ids:
        return {}
    placeholders = ",".join("?" * len(case_ids))
    rows = conn.execute(
        f"SELECT id, abs_path FROM cases WHERE id IN ({placeholders})",
        sorted(case_ids),
    ).fetchall()
    return {int(row["id"]): str(row["abs_path"] or "") for row in rows}


def _copy_case_file(
    *,
    case_paths: dict[int, str],
    case_id: int,
    filename: str | None,
    target_dir: Path,
    prefix: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not filename:
        return None, {"case_id": case_id, "filename": filename, "reason": "missing filename"}
    base = Path(case_paths.get(int(case_id), "")).resolve()
    source = (base / filename).resolve()
    try:
        source.relative_to(base)
    except ValueError:
        return None, {"case_id": case_id, "filename": filename, "reason": "invalid path escape"}
    if not source.is_file():
        return None, {"case_id": case_id, "filename": filename, "reason": f"{UNVERIFIED}: source file missing"}
    suffix = source.suffix or ".jpg"
    target = target_dir / f"{prefix}-{_slug(str(case_id))}-{_slug(Path(filename).stem)}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return {
        "case_id": int(case_id),
        "filename": filename,
        "source_path": str(source),
        "asset_path": str(target),
        "asset_relative_path": target.relative_to(target_dir.parent.parent).as_posix(),
    }, None


def _ticket_groups(tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter(
        (str(item.get("ticket_type") or ""), str(item.get("reason_code") or "")) for item in tickets
    )
    return [
        {"ticket_type": ticket_type, "reason_code": reason_code, "count": count}
        for (ticket_type, reason_code), count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _copy_candidate_asset(
    *,
    case_paths: dict[int, str],
    candidate: dict[str, Any],
    target_dir: Path,
    prefix: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    asset, missing = _copy_case_file(
        case_paths=case_paths,
        case_id=int(candidate.get("case_id") or 0),
        filename=str(candidate.get("filename") or ""),
        target_dir=target_dir,
        prefix=prefix,
    )
    out = {
        "case_id": int(candidate.get("case_id") or 0),
        "filename": str(candidate.get("filename") or ""),
        "phase": candidate.get("phase"),
        "view": candidate.get("view"),
        "selection_score": candidate.get("selection_score"),
        "sharpness_score": candidate.get("sharpness_score"),
        "crop_margin": candidate.get("crop_margin"),
        "face_crop_margin": candidate.get("face_crop_margin"),
        "crop_touches_frame": bool(candidate.get("crop_touches_frame") or candidate.get("face_crop_touches_frame")),
        "crop_ok": _crop_ok(candidate),
        "sharpness_ok": _sharpness_ok(candidate),
        "risk_level": candidate.get("risk_level"),
        "asset_relative_path": (asset or {}).get("asset_relative_path"),
    }
    return out, missing


def _crop_review_units(
    *,
    conn: sqlite3.Connection,
    tickets: list[dict[str, Any]],
    output_dir: Path,
    case_paths: dict[int, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    units: list[dict[str, Any]] = []
    missing_assets: list[dict[str, Any]] = []
    copied_count = 0
    by_case: dict[int, list[dict[str, Any]]] = {}
    for ticket in tickets:
        by_case.setdefault(int(ticket["case_id"]), []).append(ticket)
    for case_id, group in sorted(by_case.items()):
        first = group[0]
        evidence = first.get("evidence") if isinstance(first.get("evidence"), dict) else {}
        before = evidence.get("before") if isinstance(evidence.get("before"), dict) else {}
        after = evidence.get("after") if isinstance(evidence.get("after"), dict) else {}
        unit_dir = output_dir / "assets" / f"crop-case{case_id}"
        before_asset, missing = _copy_case_file(
            case_paths=case_paths,
            case_id=int(before.get("case_id") or case_id),
            filename=str(before.get("filename") or ""),
            target_dir=unit_dir,
            prefix="current-before",
        )
        if before_asset:
            copied_count += 1
        if missing:
            missing_assets.append({"unit": f"crop-case{case_id}", "role": "before", **missing})
        after_asset, missing = _copy_case_file(
            case_paths=case_paths,
            case_id=int(after.get("case_id") or case_id),
            filename=str(after.get("filename") or ""),
            target_dir=unit_dir,
            prefix="current-after",
        )
        if after_asset:
            copied_count += 1
        if missing:
            missing_assets.append({"unit": f"crop-case{case_id}", "role": "after", **missing})

        try:
            candidates = _candidate_rows(conn, case_id)
            safe_pairs = _safe_front_alternative_pairs(conn, case_id)
            readiness = "verified"
            error = None
        except Exception as exc:  # noqa: BLE001
            candidates = []
            safe_pairs = []
            readiness = UNVERIFIED
            error = f"{type(exc).__name__}: {str(exc)[:400]}"

        candidate_assets: dict[str, list[dict[str, Any]]] = {"before": [], "after": []}
        for phase in ("before", "after"):
            pool = [
                item for item in candidates
                if item.get("phase") == phase and item.get("view") == "front"
            ]
            pool.sort(
                key=lambda item: (
                    0 if _crop_ok(item) else 1,
                    0 if _sharpness_ok(item) else 1,
                    -(int(item.get("selection_score") or 0)),
                    str(item.get("filename") or ""),
                )
            )
            for index, candidate in enumerate(pool[:8], start=1):
                asset, missing = _copy_candidate_asset(
                    case_paths=case_paths,
                    candidate=candidate,
                    target_dir=unit_dir,
                    prefix=f"candidate-{phase}-{index}",
                )
                if asset.get("asset_relative_path"):
                    copied_count += 1
                if missing:
                    missing_assets.append({"unit": f"crop-case{case_id}", "role": f"candidate-{phase}", **missing})
                candidate_assets[phase].append(asset)

        unit = {
            "unit_id": f"crop-case{case_id}",
            "case_id": case_id,
            "ticket_ids": [int(item["id"]) for item in group],
            "reason_code": "crop_touches_frame",
            "message": first.get("message"),
            "current_pair": {
                "before": {**before, **(before_asset or {})},
                "after": {**after, **(after_asset or {})},
            },
            "candidate_assets": candidate_assets,
            "safe_alternative_pair_count": len(safe_pairs),
            "safe_alternative_pairs": safe_pairs[:5],
            "recommended_action": "needs_reselect_pair" if safe_pairs else "needs_replace_source",
            "allowed_actions": CROP_ALLOWED_ACTIONS,
            "readiness": readiness,
            "error": error,
            "auto_lock_applied": False,
            "blocks_render": True,
            "blocks_publish": True,
        }
        units.append(unit)
    return units, missing_assets, copied_count


def _slot_action(evidence: dict[str, Any]) -> str:
    profile = evidence.get("source_profile") if isinstance(evidence.get("source_profile"), dict) else {}
    source_kind = str(profile.get("source_kind") or "")
    source_count = int(profile.get("source_count") or 0)
    unlabeled = int(profile.get("unlabeled_source_count") or 0)
    renderable = [str(item) for item in (evidence.get("renderable_slots") or []) if item]
    if source_kind in {"generated_output_collection", "manual_not_case_source_directory", "missing_source_files"}:
        return "bind_or_rescan_real_source"
    if source_count < 2:
        return "restore_or_add_source_photos"
    if unlabeled > 0 or source_kind == "missing_before_after_pair":
        return "manual_phase_view_override"
    if renderable:
        return "template_policy_review"
    return "restore_or_add_source_photos"


def _slot_fill_units(
    *,
    tickets: list[dict[str, Any]],
    output_dir: Path,
    case_paths: dict[int, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    units: list[dict[str, Any]] = []
    missing_assets: list[dict[str, Any]] = []
    copied_count = 0
    by_case: dict[int, list[dict[str, Any]]] = {}
    for ticket in tickets:
        by_case.setdefault(int(ticket["case_id"]), []).append(ticket)
    for case_id, group in sorted(by_case.items()):
        first = group[0]
        evidence = first.get("evidence") if isinstance(first.get("evidence"), dict) else {}
        profile = evidence.get("source_profile") if isinstance(evidence.get("source_profile"), dict) else {}
        sample_assets: list[dict[str, Any]] = []
        unit_dir = output_dir / "assets" / f"slot-case{case_id}"
        for index, filename in enumerate([str(item) for item in (profile.get("source_samples") or []) if item][:8], start=1):
            asset, missing = _copy_case_file(
                case_paths=case_paths,
                case_id=case_id,
                filename=filename,
                target_dir=unit_dir,
                prefix=f"source-sample-{index}",
            )
            if asset:
                copied_count += 1
                sample_assets.append(asset)
            if missing:
                missing_assets.append({"unit": f"slot-case{case_id}", "role": "source_sample", **missing})
        action = _slot_action(evidence)
        units.append(
            {
                "unit_id": f"slot-case{case_id}",
                "case_id": case_id,
                "ticket_ids": [int(item["id"]) for item in group],
                "reason_code": "missing_render_slots",
                "message": first.get("message"),
                "missing_slots": evidence.get("missing_slots") or [],
                "required_slots": evidence.get("effective_required_slots") or evidence.get("required_slots") or [],
                "renderable_slots": evidence.get("renderable_slots") or [],
                "source_profile": profile,
                "source_sample_assets": sample_assets,
                "recommended_action": action,
                "allowed_actions": SLOT_ALLOWED_ACTIONS,
                "blocks_render": True,
                "blocks_publish": True,
            }
        )
    return units, missing_assets, copied_count


def _decision_draft(crop_units: list[dict[str, Any]], slot_units: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "generated_at": _now(),
        "scope": "t80_crop_slot_review_decisions_draft_v1",
        "instructions": "填写 reviewer/action/note 后再由导入脚本写回；空 action 不计入修复证据。",
        "crop_allowed_actions": CROP_ALLOWED_ACTIONS,
        "slot_allowed_actions": SLOT_ALLOWED_ACTIONS,
        "crop_decisions": [
            {
                "unit_id": item["unit_id"],
                "case_id": item["case_id"],
                "ticket_ids": item["ticket_ids"],
                "reviewer": None,
                "action": None,
                "note": None,
                "selected_before": None,
                "selected_after": None,
            }
            for item in crop_units
        ],
        "slot_decisions": [
            {
                "unit_id": item["unit_id"],
                "case_id": item["case_id"],
                "ticket_ids": item["ticket_ids"],
                "reviewer": None,
                "action": None,
                "note": None,
            }
            for item in slot_units
        ],
    }


def _render_html(manifest: dict[str, Any]) -> str:
    def img(asset: str | None, alt: str) -> str:
        if not asset:
            return '<div class="missing">无法获取真实图片</div>'
        return f'<img src="{html.escape(asset)}" alt="{html.escape(alt)}">'

    crop_sections: list[str] = []
    for unit in manifest.get("crop_review_units") or []:
        before = (unit.get("current_pair") or {}).get("before") or {}
        after = (unit.get("current_pair") or {}).get("after") or {}
        candidates = unit.get("candidate_assets") or {}
        before_candidates = "".join(
            f'<figure>{img(item.get("asset_relative_path"), str(item.get("filename") or ""))}'
            f'<figcaption>{html.escape(str(item.get("filename") or ""))}<br>crop_ok={item.get("crop_ok")} score={item.get("selection_score")}</figcaption></figure>'
            for item in candidates.get("before") or []
        )
        after_candidates = "".join(
            f'<figure>{img(item.get("asset_relative_path"), str(item.get("filename") or ""))}'
            f'<figcaption>{html.escape(str(item.get("filename") or ""))}<br>crop_ok={item.get("crop_ok")} score={item.get("selection_score")}</figcaption></figure>'
            for item in candidates.get("after") or []
        )
        crop_sections.append(
            '<section class="unit">'
            f'<h2>{html.escape(unit["unit_id"])}</h2>'
            f'<p>case {unit["case_id"]} · tickets {html.escape(str(unit["ticket_ids"]))} · safe pairs {unit["safe_alternative_pair_count"]} · action {html.escape(unit["recommended_action"])}</p>'
            '<h3>当前阻断配对</h3><div class="pair">'
            f'<figure>{img(before.get("asset_relative_path"), str(before.get("filename") or "before"))}<figcaption>before · {html.escape(str(before.get("filename") or ""))}</figcaption></figure>'
            f'<figure>{img(after.get("asset_relative_path"), str(after.get("filename") or "after"))}<figcaption>after · {html.escape(str(after.get("filename") or ""))}</figcaption></figure>'
            '</div><h3>候选 before</h3><div class="grid">'
            f'{before_candidates or "<p>无候选</p>"}</div><h3>候选 after</h3><div class="grid">'
            f'{after_candidates or "<p>无候选</p>"}</div>'
            '</section>'
        )
    slot_sections: list[str] = []
    for unit in manifest.get("slot_fill_units") or []:
        samples = "".join(
            f'<figure>{img(item.get("asset_relative_path"), str(item.get("filename") or ""))}'
            f'<figcaption>{html.escape(str(item.get("filename") or ""))}</figcaption></figure>'
            for item in unit.get("source_sample_assets") or []
        )
        slot_sections.append(
            '<section class="unit">'
            f'<h2>{html.escape(unit["unit_id"])}</h2>'
            f'<p>case {unit["case_id"]} · action {html.escape(unit["recommended_action"])} · missing {html.escape(json.dumps(unit.get("missing_slots") or [], ensure_ascii=False))}</p>'
            f'<div class="grid">{samples or "<p>无真实源图样本</p>"}</div>'
            '</section>'
        )
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>T80 Crop / Slot Review Packet</title>"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:24px;background:#f6f7f8;color:#20242a;}"
        "h1{margin:0 0 8px;font-size:24px}h2{font-size:17px;margin:0 0 6px}h3{font-size:13px;margin:14px 0 8px}"
        ".unit{background:#fff;border:1px solid #d9dde3;border-radius:8px;margin:0 0 18px;padding:14px}"
        ".pair{display:grid;grid-template-columns:repeat(2,minmax(220px,1fr));gap:10px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}"
        "figure{margin:0;border:1px solid #e1e5ea;background:#eef1f4;border-radius:6px;padding:6px}img{width:100%;height:180px;object-fit:contain;display:block}"
        "figcaption{font-size:11px;color:#4c5565;line-height:1.35;word-break:break-all;margin-top:4px}.missing{height:180px;display:flex;align-items:center;justify-content:center;color:#9a3412;font-weight:700}"
        "p{margin:0 0 10px;color:#596273;font-size:13px;line-height:1.5}"
        "</style></head><body>"
        "<header><h1>T80 Crop / Slot Review Packet</h1>"
        "<p>只展示真实 open review tickets 和真实源图。填写 review_decisions_draft.json 后再回写；本包不会自动放行。</p></header>"
        f"<h1>Crop 重选/替换</h1>{''.join(crop_sections) or '<p>无 crop unit</p>'}"
        f"<h1>槽位缺口</h1>{''.join(slot_sections) or '<p>无 slot unit</p>'}"
        "</body></html>"
    )


def _render_markdown(summary: dict[str, Any], manifest: dict[str, Any]) -> str:
    lines = [
        "# T80 Crop / Slot Review Packet",
        "",
        f"- generated_at: `{summary.get('generated_at')}`",
        f"- used_mock_data: `{summary.get('used_mock_data')}`",
        f"- crop_review_unit_count: `{summary.get('crop_review_unit_count')}`",
        f"- slot_fill_unit_count: `{summary.get('slot_fill_unit_count')}`",
        f"- crop_safe_alternative_pair_count: `{summary.get('crop_safe_alternative_pair_count')}`",
        f"- crop_asset_copy_count: `{summary.get('crop_asset_copy_count')}`",
        f"- slot_asset_copy_count: `{summary.get('slot_asset_copy_count')}`",
        f"- missing_asset_count: `{summary.get('missing_asset_count')}`",
        f"- html_path: `{summary.get('html_path')}`",
        f"- decision_draft_path: `{summary.get('decision_draft_path')}`",
        "",
        "## Slot Action Counts",
        "",
    ]
    for key, count in (summary.get("slot_action_counts") or {}).items():
        lines.append(f"- `{key}`: `{count}`")
    lines.extend(["", "## Crop Units", ""])
    for unit in (manifest.get("crop_review_units") or [])[:80]:
        lines.append(
            f"- case `{unit.get('case_id')}` tickets `{unit.get('ticket_ids')}` "
            f"safe_pairs `{unit.get('safe_alternative_pair_count')}` action `{unit.get('recommended_action')}`"
        )
    lines.extend(["", "## Slot Units", ""])
    for unit in (manifest.get("slot_fill_units") or [])[:120]:
        lines.append(
            f"- case `{unit.get('case_id')}` tickets `{unit.get('ticket_ids')}` "
            f"action `{unit.get('recommended_action')}` missing `{json.dumps(unit.get('missing_slots') or [], ensure_ascii=False)}`"
        )
    return "\n".join(lines) + "\n"


def build_review_packet(*, output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    db.init_schema()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with db.connect() as conn:
        open_before = _open_tickets(conn)
        crop_tickets = [
            item for item in open_before
            if item.get("ticket_type") == "source_quality_review" and item.get("reason_code") == "crop_touches_frame"
        ]
        slot_tickets = [
            item for item in open_before
            if item.get("ticket_type") == "slot_fill" and item.get("reason_code") == "missing_render_slots"
        ]
        ids: set[int] = {int(item["case_id"]) for item in [*crop_tickets, *slot_tickets]}
        for ticket in crop_tickets:
            evidence = ticket.get("evidence") if isinstance(ticket.get("evidence"), dict) else {}
            for role in ("before", "after"):
                candidate = evidence.get(role)
                if isinstance(candidate, dict) and candidate.get("case_id") is not None:
                    ids.add(int(candidate["case_id"]))
        case_paths = _read_case_paths(conn, ids)
        crop_units, crop_missing, crop_copied = _crop_review_units(
            conn=conn,
            tickets=crop_tickets,
            output_dir=output_dir,
            case_paths=case_paths,
        )
        slot_units, slot_missing, slot_copied = _slot_fill_units(
            tickets=slot_tickets,
            output_dir=output_dir,
            case_paths=case_paths,
        )
        open_after = _open_tickets(conn)

    slot_action_counts = Counter(str(item.get("recommended_action") or "") for item in slot_units)
    summary = {
        "generated_at": _now(),
        "scope": "t80_crop_slot_review_packet_summary_v1",
        "used_mock_data": False,
        "open_ticket_count_before": len(open_before),
        "open_ticket_count_after": len(open_after),
        "open_ticket_delta": len(open_after) - len(open_before),
        "open_ticket_groups_before": _ticket_groups(open_before),
        "open_ticket_groups_after": _ticket_groups(open_after),
        "crop_review_unit_count": len(crop_units),
        "slot_fill_unit_count": len(slot_units),
        "crop_asset_copy_count": crop_copied,
        "slot_asset_copy_count": slot_copied,
        "missing_asset_count": len(crop_missing) + len(slot_missing),
        "crop_safe_alternative_pair_count": sum(int(unit.get("safe_alternative_pair_count") or 0) for unit in crop_units),
        "slot_action_counts": dict(sorted(slot_action_counts.items())),
        "html_path": str(output_dir / "index.html"),
        "manifest_path": str(output_dir / "manifest.json"),
        "decision_draft_path": str(output_dir / "review_decisions_draft.json"),
    }
    manifest = {
        "generated_at": _now(),
        "scope": "t80_crop_slot_review_packet_manifest_v1",
        "used_mock_data": False,
        "summary": summary,
        "crop_allowed_actions": CROP_ALLOWED_ACTIONS,
        "slot_allowed_actions": SLOT_ALLOWED_ACTIONS,
        "crop_review_units": crop_units,
        "slot_fill_units": slot_units,
        "missing_assets": [*crop_missing, *slot_missing],
        "notes": [
            "本包只复制真实源图，不生成占位图。",
            "crop_touches_frame 继续 fail-closed，未人工重选/替换前不得正式 render。",
            "missing_render_slots 需要人工补槽位、改 phase/view override、绑定真实源目录或补拍/恢复源图。",
        ],
    }
    decision_draft = _decision_draft(crop_units, slot_units)
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "review_decisions_draft.json").write_text(
        json.dumps(decision_draft, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output_dir / "index.html").write_text(_render_html(manifest), encoding="utf-8")
    return {**summary, "summary": summary, "manifest": manifest}


def write_summary(report: dict[str, Any], *, json_output: Path, markdown_output: Path) -> None:
    summary = report["summary"]
    manifest = report["manifest"]
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    markdown_output.write_text(_render_markdown(summary, manifest), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--json-output", default=str(DEFAULT_SUMMARY_JSON))
    parser.add_argument("--markdown-output", default=str(DEFAULT_SUMMARY_MD))
    args = parser.parse_args(argv)
    db.DB_PATH = Path(args.db_path).expanduser().resolve()
    report = build_review_packet(output_dir=Path(args.output_dir))
    write_summary(report, json_output=Path(args.json_output), markdown_output=Path(args.markdown_output))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
