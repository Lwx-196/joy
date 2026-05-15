#!/usr/bin/env python3
"""Export all deliverable cases to a unified delivery directory.

Usage:
    python -m backend.scripts.export_delivery_batch [--output-dir ./delivery] [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from backend.services.delivery_gate import (
    DeliverableItem,
    DeliveryGate,
    P0_THRESHOLD,
    P1_THRESHOLD,
)

DB_PATH = Path(__file__).resolve().parent.parent.parent / "case-workbench.db"
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent.parent / "delivery"

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


def export(output_dir: Path, dry_run: bool = False, db_path: Path = DB_PATH) -> list[dict]:
    conn = _connect(db_path)
    try:
        gate = DeliveryGate(conn)
        items = gate.list_deliverables()

        if not items:
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

    print(f"\n{'=' * 60}")
    print(f"  ✅ Export complete: {len(manifest_rows)} cases")
    if not dry_run:
        print(f"  📋 CSV: {csv_path}")
        print(f"  📋 JSON: {json_path}")
    else:
        print(f"  ℹ️  Dry run — no files copied")
    print(f"{'=' * 60}")
    return manifest_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Export deliverable cases")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()
    export(args.output_dir, args.dry_run, db_path=args.db)


if __name__ == "__main__":
    main()
