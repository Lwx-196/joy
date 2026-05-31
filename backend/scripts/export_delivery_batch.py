#!/usr/bin/env python3
"""Export all deliverable cases to a unified delivery directory.

A fail-closed board QA gate (D6) screens every candidate's rendered board JPG
through single-board VLM assessment BEFORE export: `blocker` / `unavailable`
boards are held out of the shipped set and written to a human-review queue
(`delivery_held_review.json`) instead of silently shipping. Enabled by default;
`--no-qa` (or `CASE_WORKBENCH_DELIVERY_QA=0`) disables it for emergencies.

Usage:
    python -m backend.scripts.export_delivery_batch [--output-dir ./delivery] [--dry-run] [--no-qa]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from backend.services.board_delivery_qa import BoardDeliveryQA
from backend.services.delivery_gate import (
    DeliverableItem,
    DeliveryGate,
    HeldBoard,
    P0_THRESHOLD,
    P1_THRESHOLD,
)
from backend.services.single_image_delivery import (
    EnhancedAfter,
    SingleImageBuildError,
    build_enhanced_after,
    closeup_filename,
)
from backend.services.single_image_delivery_qa import SingleImageDeliveryQA, SingleImageQAVerdict
from backend.services.simulation_delivery_gate import SimulationDeliveryGate
from backend.services.vlm_provider import VLMProvider

DB_PATH = Path(__file__).resolve().parent.parent.parent / "case-workbench.db"
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent.parent / "delivery"


def _load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _build_board_qa(conn: sqlite3.Connection, repo_root: Path) -> BoardDeliveryQA:
    """Construct the fail-closed VLM QA gate, loading t54 ADC config if present."""
    env = dict(os.environ)
    env.update(_load_env_file(repo_root / "tasks" / "t54_vertex_adc.local.env"))
    return BoardDeliveryQA(VLMProvider(env=env), conn)


def _build_single_image_qa(conn: sqlite3.Connection, repo_root: Path) -> SingleImageDeliveryQA:
    env = dict(os.environ)
    env.update(_load_env_file(repo_root / "tasks" / "t54_vertex_adc.local.env"))
    return SingleImageDeliveryQA(conn, env=env)


def _held_row(held: HeldBoard) -> dict:
    return {
        "case_id": held.case_id,
        "customer": held.customer,
        "case_name": held.case_name,
        "job_id": held.job_id,
        "verdict": held.verdict,
        "primary_defect": held.primary_defect,
        "families": list(held.families),
        "confidence": held.confidence,
        "content_hash": held.content_hash,
        "reason": held.reason,
        "source_path": held.source_path,
    }


def _write_held_report(output_dir: Path, held: list[HeldBoard], dry_run: bool) -> Path | None:
    """Persist the human-review queue (held boards) next to the manifest."""
    if dry_run or not held:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "delivery_held_review.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "held_count": len(held),
                "note": (
                    "Fail-closed board QA (D6) held these boards. Review each, then "
                    "clear (ships) or re-render (new bytes → re-assessed)."
                ),
                "boards": [_held_row(hb) for hb in held],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"  🛡  Held-review queue: {path}")
    return path


def _single_image_dest_path(output_dir: Path, item: DeliverableItem) -> Path:
    return output_dir / item.customer / "closeups" / closeup_filename(item)


def _single_image_manifest_row(
    item: DeliverableItem,
    enhanced: EnhancedAfter,
    verdict: SingleImageQAVerdict,
    dest_path: Path,
) -> dict:
    return {
        "case_id": item.case_id,
        "customer": item.customer,
        "case_name": item.case_name,
        "focus_targets": list(enhanced.focus_targets),
        "dest_path": str(dest_path),
        "source_after": str(enhanced.source_after_path),
        "enhanced_path": str(enhanced.enhanced_path),
        "verdict": verdict.verdict,
        "winner_role": verdict.winner_role,
        "confidence": verdict.confidence,
        "content_hash": verdict.content_hash,
    }


def _single_image_held_row(
    item: DeliverableItem,
    *,
    enhanced: EnhancedAfter | None = None,
    verdict: SingleImageQAVerdict | None = None,
    reason: str,
) -> dict:
    return {
        "case_id": item.case_id,
        "customer": item.customer,
        "case_name": item.case_name,
        "quality_score": item.quality_score,
        "source_after": str(enhanced.source_after_path) if enhanced else "",
        "enhanced_path": str(enhanced.enhanced_path) if enhanced else "",
        "focus_targets": list(enhanced.focus_targets) if enhanced else [],
        "verdict": verdict.verdict if verdict else "build_failed",
        "winner_role": verdict.winner_role if verdict else "",
        "hard_veto_reason": verdict.hard_veto_reason if verdict else "",
        "confidence": verdict.confidence if verdict else None,
        "prescreen_reasons": list(verdict.prescreen_reasons) if verdict else [],
        "content_hash": verdict.content_hash if verdict else "",
        "reason": reason,
    }


def _write_single_image_manifest(output_dir: Path, rows: list[dict], dry_run: bool) -> Path | None:
    if dry_run or not rows:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "single_image_manifest.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "total_images": len(rows),
                "images": rows,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"  🖼  Single-image manifest: {path}")
    return path


def _write_single_image_held_report(output_dir: Path, held: list[dict], dry_run: bool) -> Path | None:
    if dry_run or not held:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "single_image_held_review.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "held_count": len(held),
                "note": (
                    "Single-image closeups held by source lookup, enhancement, "
                    "prescreen, or fidelity judge. Boards are unaffected."
                ),
                "images": held,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"  🖼  Single-image held-review queue: {path}")
    return path


def _run_single_image_delivery(
    items: list[DeliverableItem],
    output_dir: Path,
    *,
    dry_run: bool,
    conn: sqlite3.Connection,
    qa: SingleImageDeliveryQA,
) -> tuple[list[dict], list[dict]]:
    scratch_root = output_dir / "_single_image_scratch"
    manifest_rows: list[dict] = []
    held_rows: list[dict] = []

    for item in items:
        try:
            enhanced = build_enhanced_after(item, scratch_root, conn)
        except SingleImageBuildError as exc:
            held_rows.append(_single_image_held_row(item, reason=str(exc)))
            continue

        verdict = qa.assess(
            enhanced.raw_path,
            enhanced.enhanced_path,
            enhanced.mask_path,
            case_id=item.case_id,
            customer=item.customer,
            focus_targets=enhanced.focus_targets,
        )
        if verdict.deliverable:
            dest_path = _single_image_dest_path(output_dir, item)
            if not dry_run:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(enhanced.enhanced_path, dest_path)
            manifest_rows.append(_single_image_manifest_row(item, enhanced, verdict, dest_path))
        else:
            held_rows.append(
                _single_image_held_row(
                    item,
                    enhanced=enhanced,
                    verdict=verdict,
                    reason=verdict.reason,
                )
            )

    _write_single_image_manifest(output_dir, manifest_rows, dry_run)
    _write_single_image_held_report(output_dir, held_rows, dry_run)
    return manifest_rows, held_rows

MANIFEST_FIELDS = [
    "case_id", "customer", "case_name", "category",
    "template_tier", "quality_score", "quality_status",
    "artifact_mode", "tier", "dest_path", "source_path",
]


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _manifest_row(item: DeliverableItem, dest_path: Path) -> dict:
    return {
        "case_id": item.case_id,
        "customer": item.customer,
        "case_name": item.case_name,
        "category": item.category,
        "template_tier": item.template_tier,
        "quality_score": item.quality_score,
        "quality_status": item.quality_status,
        "artifact_mode": item.artifact_mode,
        "tier": item.tier,
        "dest_path": str(dest_path),
        "source_path": item.source_path,
    }


def export(
    output_dir: Path,
    dry_run: bool = False,
    db_path: Path = DB_PATH,
    qa_enabled: bool = True,
    single_image_enabled: bool = False,
) -> list[dict]:
    conn = _connect(db_path)
    held: list[HeldBoard] = []
    single_image_rows: list[dict] = []
    single_image_held: list[dict] = []
    try:
        board_qa = _build_board_qa(conn, db_path.parent) if qa_enabled else None
        gate = DeliveryGate(conn, board_qa=board_qa)
        sim_gate = SimulationDeliveryGate(conn)
        screen = gate.screen_deliverables(simulation_gate=sim_gate)
        items = screen.passed
        held = screen.held

        if board_qa is not None:
            print(
                f"\n  🛡  Board QA gate: {len(items)} ship / {len(held)} held for review"
            )
            for hb in held:
                print(
                    f"     ⛔ Case #{hb.case_id:>3d} [{hb.verdict}] "
                    f"{hb.customer}/{hb.case_name[:36]} — {hb.primary_defect[:48]}"
                )

        if not items:
            if held:
                print(
                    f"\n❌ No shippable cases — all {len(held)} candidate(s) held by QA. "
                    "Review delivery_held_review.json, then clear or re-render."
                )
                _write_held_report(output_dir, held, dry_run)
            else:
                print("❌ No deliverable cases found.")
            return []

        print(f"\n{'=' * 60}")
        print(f"  📦 Delivery Export: {len(items)} cases")
        print(f"  📁 Output: {output_dir}")
        print(f"  🕐 Timestamp: {datetime.now(timezone.utc).isoformat()}")
        print(f"{'=' * 60}\n")

        p0_count = sum(1 for it in items if it.tier == "P0")
        p1_count = sum(1 for it in items if it.tier == "P1")
        print(f"  🟢 P0 精品级 (≥{P0_THRESHOLD:.0f}): {p0_count}")
        print(f"  🟡 P1 可交付级 ({P1_THRESHOLD:.0f}-{P0_THRESHOLD - 0.1:.1f}): {p1_count}")
        print()

        if not dry_run:
            output_dir.mkdir(parents=True, exist_ok=True)

        manifest_rows: list[dict] = []
        for item in items:
            dest_path = gate.export(item, output_dir, dry_run=dry_run)
            print(
                f"  {item.tier} Case #{item.case_id:>3d} | [{item.category}] "
                f"{item.customer}/{item.case_name[:40]} | score={item.quality_score}"
            )
            manifest_rows.append(_manifest_row(item, dest_path))

        if single_image_enabled:
            print("\n  🖼  Single-image closeups: enabled")
            single_image_qa = _build_single_image_qa(conn, db_path.parent)
            try:
                single_image_rows, single_image_held = _run_single_image_delivery(
                    items,
                    output_dir,
                    dry_run=dry_run,
                    conn=conn,
                    qa=single_image_qa,
                )
            except Exception as exc:  # noqa: BLE001 - companion artifacts never block boards
                print(f"  ⚠️  Single-image delivery skipped: {type(exc).__name__}: {str(exc)[:160]}")
    finally:
        conn.close()

    csv_path = output_dir / "delivery_manifest.csv"
    json_path = output_dir / "delivery_manifest.json"
    if not dry_run:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
            writer.writeheader()
            writer.writerows(manifest_rows)

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "total_cases": len(manifest_rows),
                    "p0_count": p0_count,
                    "p1_count": p1_count,
                    "min_score": min(d["quality_score"] for d in manifest_rows),
                    "cases": manifest_rows,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    _write_held_report(output_dir, held, dry_run)

    print(f"\n{'=' * 60}")
    print(f"  ✅ Export complete: {len(manifest_rows)} cases")
    if held:
        print(f"  🛡  Held for review: {len(held)} board(s) (not shipped)")
    if single_image_enabled:
        print(
            f"  🖼  Single-image closeups: {len(single_image_rows)} ship / "
            f"{len(single_image_held)} held"
        )
    if not dry_run:
        print(f"  📋 CSV: {csv_path}")
        print(f"  📋 JSON: {json_path}")
    else:
        print("  ℹ️  Dry run — no files copied")
    print(f"{'=' * 60}")
    return manifest_rows


def _qa_enabled_default() -> bool:
    return str(os.environ.get("CASE_WORKBENCH_DELIVERY_QA", "1")).strip().lower() not in {
        "0", "false", "no", "off",
    }


def _single_image_enabled_default() -> bool:
    return str(os.environ.get("CASE_WORKBENCH_SINGLE_IMAGE_DELIVERY", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Export deliverable cases")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument(
        "--no-qa",
        action="store_true",
        help="Disable the fail-closed board QA gate (emergency escape hatch).",
    )
    parser.add_argument(
        "--single-image",
        dest="single_image",
        action="store_true",
        default=None,
        help="Add screened standalone clarity closeups for shipped cases.",
    )
    parser.add_argument(
        "--no-single-image",
        dest="single_image",
        action="store_false",
        help="Disable standalone closeup delivery even if the env flag is set.",
    )
    args = parser.parse_args()
    qa_enabled = _qa_enabled_default() and not args.no_qa
    single_image_enabled = (
        _single_image_enabled_default() if args.single_image is None else args.single_image
    )
    if not qa_enabled:
        print("⚠️  Board QA gate DISABLED — boards ship without VLM screening.")
    export(
        args.output_dir,
        args.dry_run,
        db_path=args.db,
        qa_enabled=qa_enabled,
        single_image_enabled=single_image_enabled,
    )


if __name__ == "__main__":
    main()
