"""Audit crop_touches_frame override outcomes against latest D6 verdicts.

Read-only R1a panel:

    python -m backend.scripts.audit_crop_override_outcomes \
      --db /path/to/case-workbench.db

The panel joins resolved ``crop_touches_frame`` review tickets to the latest
``board_delivery_qa`` verdict per case through ``render_jobs.case_id``. It is
pure SELECT: the DB connection opens with ``mode=ro`` and ``PRAGMA query_only``.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REASON_CODE = "crop_touches_frame"
BASELINE_CROP_BLOCKER_OVERLAP = (18, 20)
_DB_PATH_ENV = os.environ.get("CASE_WORKBENCH_DB_PATH")
DEFAULT_DB_PATH = (
    Path(_DB_PATH_ENV).expanduser()
    if _DB_PATH_ENV
    else Path(__file__).resolve().parents[2] / "case-workbench.db"
)


@dataclass(frozen=True)
class OverrideTicket:
    ticket_id: int
    case_id: int
    slot: str
    source_filename: str
    reviewer: str
    decided_at: str


@dataclass(frozen=True)
class D6Verdict:
    case_id: int
    render_job_id: int
    verdict: str
    review_status: str
    reviewed_by: str
    reviewed_at: str
    assessed_at: str
    primary_defect: str
    confidence: float | None


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    path = db_path.expanduser().resolve()
    uri = f"{path.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _load_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _fetch_override_tickets(conn: sqlite3.Connection) -> list[OverrideTicket]:
    rows = conn.execute(
        """
        SELECT id, case_id, slot, source_filename, decision_json, resolved_at, updated_at
        FROM review_tickets
        WHERE reason_code = ?
          AND status = 'resolved'
        ORDER BY case_id, resolved_at, id
        """,
        (REASON_CODE,),
    ).fetchall()
    tickets: list[OverrideTicket] = []
    for row in rows:
        decision = _load_json(row["decision_json"])
        tickets.append(
            OverrideTicket(
                ticket_id=int(row["id"]),
                case_id=int(row["case_id"]),
                slot=str(row["slot"] or ""),
                source_filename=str(row["source_filename"] or ""),
                reviewer=str(decision.get("reviewer") or ""),
                decided_at=str(decision.get("decided_at") or row["resolved_at"] or row["updated_at"] or ""),
            )
        )
    return tickets


def _fetch_latest_d6(conn: sqlite3.Connection) -> dict[int, D6Verdict]:
    rows = conn.execute(
        """
        WITH ranked AS (
          SELECT
            rj.case_id,
            rj.id AS render_job_id,
            q.verdict,
            q.review_status,
            q.reviewed_by,
            q.reviewed_at,
            q.assessed_at,
            q.primary_defect,
            q.confidence,
            ROW_NUMBER() OVER (
              PARTITION BY rj.case_id
              ORDER BY q.assessed_at DESC,
                       COALESCE(q.reviewed_at, '') DESC,
                       q.content_hash DESC,
                       q.prompt_version DESC
            ) AS rn
          FROM board_delivery_qa q
          JOIN render_jobs rj ON rj.id = q.job_id
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
        """,
    ).fetchall()
    latest: dict[int, D6Verdict] = {}
    for row in rows:
        latest[int(row["case_id"])] = D6Verdict(
            case_id=int(row["case_id"]),
            render_job_id=int(row["render_job_id"]),
            verdict=str(row["verdict"] or ""),
            review_status=str(row["review_status"] or ""),
            reviewed_by=str(row["reviewed_by"] or ""),
            reviewed_at=str(row["reviewed_at"] or ""),
            assessed_at=str(row["assessed_at"] or ""),
            primary_defect=str(row["primary_defect"] or ""),
            confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        )
    return latest


def _group_tickets(tickets: list[OverrideTicket]) -> dict[int, list[OverrideTicket]]:
    grouped: dict[int, list[OverrideTicket]] = defaultdict(list)
    for ticket in tickets:
        grouped[ticket.case_id].append(ticket)
    return dict(grouped)


def _latest_ticket(tickets: list[OverrideTicket]) -> OverrideTicket:
    return max(tickets, key=lambda item: (item.decided_at, item.ticket_id))


def _case_rows(
    tickets_by_case: dict[int, list[OverrideTicket]],
    d6_by_case: dict[int, D6Verdict],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case_id in sorted(tickets_by_case):
        tickets = tickets_by_case[case_id]
        latest_ticket = _latest_ticket(tickets)
        d6 = d6_by_case.get(case_id)
        rows.append(
            {
                "case_id": case_id,
                "ticket_count": len(tickets),
                "ticket_ids": ",".join(str(ticket.ticket_id) for ticket in tickets),
                "slots": ",".join(sorted({ticket.slot for ticket in tickets if ticket.slot})),
                "override_reviewer": latest_ticket.reviewer,
                "override_decided_at": latest_ticket.decided_at,
                "d6_verdict": d6.verdict if d6 else "never-screened",
                "d6_review_status": d6.review_status if d6 else "",
                "d6_reviewed_by": d6.reviewed_by if d6 else "",
                "d6_reviewed_at": d6.reviewed_at if d6 else "",
                "d6_assessed_at": d6.assessed_at if d6 else "",
                "render_job_id": d6.render_job_id if d6 else None,
                "primary_defect": d6.primary_defect if d6 else "",
                "confidence": d6.confidence if d6 else None,
            }
        )
    return rows


def _summarize(case_rows: list[dict[str, Any]], all_latest_d6: dict[int, D6Verdict]) -> dict[str, Any]:
    verdict_counts = Counter(str(row["d6_verdict"]) for row in case_rows)
    d6_blocker_cases_total = sum(1 for verdict in all_latest_d6.values() if verdict.verdict == "blocker")
    crop_blocker_cases = verdict_counts["blocker"]
    expected_crop, expected_total = BASELINE_CROP_BLOCKER_OVERLAP
    return {
        "reason_code": REASON_CODE,
        "override_case_count": len(case_rows),
        "resolved_ticket_count": sum(int(row["ticket_count"]) for row in case_rows),
        "verdict_counts": dict(sorted(verdict_counts.items())),
        "crop_blocker_cases": crop_blocker_cases,
        "d6_blocker_cases_total": d6_blocker_cases_total,
        "section_1_5_overlap": {
            "actual": f"{crop_blocker_cases}/{d6_blocker_cases_total}",
            "expected": f"{expected_crop}/{expected_total}",
            "matches": crop_blocker_cases == expected_crop and d6_blocker_cases_total == expected_total,
        },
    }


def _table(rows: list[dict[str, Any]]) -> str:
    headers = [
        "case_id",
        "d6_verdict",
        "d6_review_status",
        "override_reviewer",
        "override_decided_at",
        "ticket_count",
        "slots",
        "d6_assessed_at",
        "d6_reviewed_by",
    ]
    widths = {
        header: max(len(header), *(len(str(row.get(header) or "")) for row in rows))
        for header in headers
    }
    lines = ["  ".join(header.ljust(widths[header]) for header in headers)]
    lines.append("  ".join("-" * widths[header] for header in headers))
    for row in rows:
        lines.append("  ".join(str(row.get(header) or "").ljust(widths[header]) for header in headers))
    return "\n".join(lines)


def _markdown_table(rows: list[dict[str, Any]]) -> str:
    headers = [
        "case_id",
        "d6_verdict",
        "d6_review_status",
        "override_reviewer",
        "override_decided_at",
        "ticket_count",
        "slots",
        "d6_assessed_at",
        "d6_reviewed_by",
    ]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        values = [str(row.get(header) or "").replace("|", "/") for header in headers]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def build_report(conn: sqlite3.Connection) -> dict[str, Any]:
    tickets = _fetch_override_tickets(conn)
    tickets_by_case = _group_tickets(tickets)
    latest_d6 = _fetch_latest_d6(conn)
    rows = _case_rows(tickets_by_case, latest_d6)
    return {"summary": _summarize(rows, latest_d6), "cases": rows}


def _print_text_report(report: dict[str, Any], *, markdown: bool) -> None:
    summary = report["summary"]
    overlap = summary["section_1_5_overlap"]
    status = "match" if overlap["matches"] else "drift"
    if markdown:
        print("# crop_touches_frame override -> D6 outcome audit")
        print()
        print("## Summary")
        print()
        print(f"- override cases: {summary['override_case_count']}")
        print(f"- resolved tickets: {summary['resolved_ticket_count']}")
        print(f"- verdict counts: {json.dumps(summary['verdict_counts'], ensure_ascii=False)}")
        print(
            "- section 1.5 blocker overlap: "
            f"{overlap['actual']} (expected {overlap['expected']}, {status})"
        )
        print()
        print("## Cases")
        print()
        print(_markdown_table(report["cases"]))
        return

    print("crop_touches_frame override -> D6 outcome audit")
    print(f"override_cases: {summary['override_case_count']}")
    print(f"resolved_tickets: {summary['resolved_ticket_count']}")
    print(f"verdict_counts: {json.dumps(summary['verdict_counts'], ensure_ascii=False)}")
    print(f"section_1_5_blocker_overlap: {overlap['actual']} expected={overlap['expected']} status={status}")
    print()
    print(_table(report["cases"]))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="path to case-workbench.db")
    parser.add_argument("--format", choices=("table", "markdown", "json"), default="table")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        with _connect_readonly(args.db) as conn:
            report = build_report(conn)
    except sqlite3.Error as exc:
        print(f"DB audit failed: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_text_report(report, markdown=args.format == "markdown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
