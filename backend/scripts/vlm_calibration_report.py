"""Daily VLM calibration report.

Pulls recent classifier outputs from ``image_observations`` and runs the
distribution collapse detector; writes a JSON snapshot under
``case-workbench-ai/calibration/YYYY-MM-DD.json`` plus a console summary.

Usage:
    python -m backend.scripts.vlm_calibration_report --days 1
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from backend.db import DB_PATH
from backend.services.vlm_calibration import detect_distribution_collapse


def _records_in_window(conn: sqlite3.Connection, days: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT phase, view, body_part, confidence
          FROM image_observations
         WHERE source = 'vlm_classifier'
           AND created_at >= ?
        """,
        (cutoff,),
    ).fetchall()
    return [
        {"phase": r[0], "view": r[1], "body_part": r[2], "confidence": r[3]}
        for r in rows
    ]


def _write_report(output_dir: Path, today: date, payload: dict) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{today.isoformat()}.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily VLM calibration report")
    parser.add_argument("--days", type=int, default=1, help="window size in days")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("case-workbench-ai/calibration"),
        help="report output directory",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path(DB_PATH),
        help="SQLite DB path override",
    )
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db_path)
    try:
        records = _records_in_window(conn, args.days)
    finally:
        conn.close()

    status = detect_distribution_collapse(records)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": args.days,
        "sample_size": status.sample_size,
        "status": status.status,
        "evidence": [asdict(a) for a in status.evidence],
        "recommendation": status.recommendation,
    }
    target = _write_report(args.output_dir, date.today(), payload)

    print(f"[vlm-calibration] sample={status.sample_size} status={status.status}")
    for alert in status.evidence:
        print(
            f"  - {alert.dimension}: dominant={alert.dominant_class} "
            f"ratio={alert.dominant_ratio} p50={alert.confidence_p50} "
            f"severity={alert.severity}"
        )
    print(f"  recommendation: {status.recommendation}")
    print(f"  report: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
