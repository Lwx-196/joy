#!/usr/bin/env python3
"""
Revert the A3 crop_touches_frame batch override.

Usage:
    python3 scripts/revert_a3_batch.py [--dry-run]

What it does:
1. Find all review_tickets where reviewer marker == "a3-batch-crop-override-2026-05-16"
   inside decision_json
2. Reset status resolved -> open, clear decision_json and resolved_at
3. For each affected case, remove the matching entry from
   cases.meta_json.source_group_selection.ticket_decisions

The associated render_jobs created after the override are NOT touched
(physical artifacts already exist on disk; revert only undoes the decision marker).
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "case-workbench.db"
REVIEWER_MARKER = "a3-batch-crop-override-2026-05-16"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing")
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"ERROR: DB not found: {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    tickets = conn.execute(
        """
        SELECT id, case_id, status, reason_code, slot, decision_json
        FROM review_tickets
        WHERE status = 'resolved'
          AND json_extract(decision_json, '$.reviewer') = ?
        ORDER BY id
        """,
        (REVIEWER_MARKER,),
    ).fetchall()

    if not tickets:
        print(f"No tickets found with reviewer={REVIEWER_MARKER}. Nothing to revert.")
        return 0

    print(f"Found {len(tickets)} tickets to revert (reviewer={REVIEWER_MARKER})")
    case_ids = sorted({t["case_id"] for t in tickets})
    print(f"Affected case_ids: {case_ids}")

    if args.dry_run:
        for t in tickets:
            print(f"  would revert ticket {t['id']} (case {t['case_id']}, {t['reason_code']}/{t['slot']})")
        print("DRY-RUN — no changes written.")
        return 0

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cur = conn.cursor()

    # 1) Revert ticket rows
    ticket_ids = [t["id"] for t in tickets]
    placeholders = ",".join("?" for _ in ticket_ids)
    cur.execute(
        f"""
        UPDATE review_tickets
           SET status = 'open',
               decision_json = '{{}}',
               resolved_at = NULL,
               updated_at = ?
         WHERE id IN ({placeholders})
        """,
        [now, *ticket_ids],
    )
    print(f"Reverted {cur.rowcount} ticket rows")

    # 2) Strip ticket_decisions entries from each affected case
    stripped = 0
    for cid in case_ids:
        row = cur.execute("SELECT meta_json FROM cases WHERE id = ?", (cid,)).fetchone()
        if not row or not row["meta_json"]:
            continue
        meta = json.loads(row["meta_json"])
        sgs = meta.get("source_group_selection") or {}
        td = sgs.get("ticket_decisions") or []
        if not td:
            continue
        td_new = [d for d in td if d.get("reviewer") != REVIEWER_MARKER]
        if len(td_new) != len(td):
            sgs["ticket_decisions"] = td_new
            meta["source_group_selection"] = sgs
            cur.execute(
                "UPDATE cases SET meta_json = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False), cid),
            )
            stripped += 1
    print(f"Stripped ticket_decisions entries from {stripped} cases")

    conn.commit()
    conn.close()

    print("\nDone. Note: render_jobs created after the override are NOT removed.")
    print("If you also want to discard those artifacts, do that separately by inspecting:")
    print('   SELECT id, case_id, status, finished_at FROM render_jobs WHERE enqueued_at > "2026-05-16T15:30:00Z" ORDER BY id DESC;')
    return 0


if __name__ == "__main__":
    sys.exit(main())
