"""Batch VLM validation script — run enhanced classification on all unknown cases."""
from __future__ import annotations

import os
import sys
import json
from datetime import datetime, timezone

os.environ.setdefault(
    "CASE_WORKBENCH_DB_PATH",
    "/Users/a1234/Desktop/案例生成器/case-workbench/case-workbench.db",
)

from backend import db
from backend.services.enhanced_classifier import run_enhanced_classification
from backend.services.vlm_provider import VLMProvider


def main() -> None:
    skip_ids_str = os.environ.get("SKIP_CASE_IDS", "")
    skip_ids = set(int(x) for x in skip_ids_str.split(",") if x.strip())

    provider = VLMProvider(env=dict(os.environ))

    with db.connect() as conn:
        rows = conn.execute("""
            SELECT o.case_id, COUNT(o.id) as cnt,
                   SUM(CASE WHEN o.phase = 'unknown' THEN 1 ELSE 0 END) as unknowns,
                   c.abs_path
            FROM image_observations o
            JOIN cases c ON c.id = o.case_id AND c.trashed_at IS NULL
            GROUP BY o.case_id
            HAVING unknowns > 0
            ORDER BY unknowns DESC
        """).fetchall()

    batch = [r for r in rows if r["case_id"] not in skip_ids]
    total_unknown = sum(r["unknowns"] for r in batch)
    print(f"Cases: {len(batch)} | Unknown images: {total_unknown}")
    print()

    grand = {"total": 0, "resolved": 0, "before": 0, "after": 0, "intraop": 0, "held": 0, "errors": 0}
    all_results = []

    for i, row in enumerate(batch, 1):
        cid = row["case_id"]
        unknowns = row["unknowns"]
        cnt = row["cnt"]
        sys.stdout.write(f"[{i}/{len(batch)}] Case {cid:4d} ({cnt:2d} imgs, {unknowns:2d} unk) ... ")
        sys.stdout.flush()
        try:
            with db.connect() as conn:
                result = run_enhanced_classification(
                    conn, cid, mode="live-no-apply",
                    provider=provider, concurrency=2, timeout=60.0,
                )
            s = result["summary"]
            resolved = unknowns - s["unknown_held"]
            grand["total"] += unknowns
            grand["resolved"] += resolved
            grand["before"] += s["before"]
            grand["after"] += s["after"]
            grand["intraop"] += s["intraop"]
            grand["held"] += s["unknown_held"]

            conflicts = sum(1 for r in result["results"] if r["fusion"].get("agreement") is False)
            conflict_str = f" | {conflicts} conflicts" if conflicts else ""
            pct = resolved / max(unknowns, 1) * 100
            print(f"B={s['before']:2d} A={s['after']:2d} I={s['intraop']:1d} U={s['unknown_held']:2d} | {resolved}/{unknowns} ({pct:.0f}%){conflict_str}")

            all_results.append({
                "case_id": cid,
                "total": cnt,
                "unknowns": unknowns,
                "resolved": resolved,
                "before": s["before"],
                "after": s["after"],
                "intraop": s["intraop"],
                "held": s["unknown_held"],
                "conflicts": conflicts,
            })
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {str(e)[:100]}")
            grand["errors"] += 1

    print(f"\n{'='*60}")
    print(f"GRAND TOTAL")
    print(f"  Unknown images:  {grand['total']}")
    print(f"  Resolved:        {grand['resolved']} ({grand['resolved']/max(grand['total'],1)*100:.1f}%)")
    print(f"  Before:          {grand['before']}")
    print(f"  After:           {grand['after']}")
    print(f"  Intraop:         {grand['intraop']}")
    print(f"  Held (unknown):  {grand['held']}")
    print(f"  Errors:          {grand['errors']}")

    report_path = f"/tmp/vlm-validation-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    with open(report_path, "w") as f:
        json.dump({"summary": grand, "cases": all_results}, f, indent=2)
    print(f"\nReport: {report_path}")


if __name__ == "__main__":
    main()
