"""C3.0.4 — ``ops_audit_log`` archive exporter.

Reads rows from ``ops_audit_log`` whose ``created_at`` is older than
``--min-age-days`` (default 90) and writes them as JSONL into
``<output-dir>/YYYY/MM.jsonl``. The export is **additive** — the source rows
are *not* deleted from the hot DB until legal sign-off on the retention
policy lands (see ``docs/operations/audit-retention.md`` §6).

Idempotency: running the exporter twice with the same arguments produces
byte-identical files (rows sorted by ``id`` ASC, fixed JSON separators).
A ``--overwrite`` flag is required to replace an existing archive shard;
without it the exporter refuses to clobber, which is a safety property
the on-call relies on when re-running after a partial failure.

Restore path: ``--restore --archive <path> --where <sql>`` re-inserts the
matching rows into ``ops_audit_log`` with a fresh ``id`` and a synthetic
``reason`` prefix ``restored_from_archive:`` so the audit trail of the
restoration itself is preserved.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# Columns in ops_audit_log (DB schema is fixed — see backend/db.py).
COLUMNS: tuple[str, ...] = (
    "id",
    "request_id",
    "endpoint",
    "reviewer",
    "reason",
    "payload_json",
    "response_json",
    "outcome",
    "http_status",
    "created_at",
)

RESTORED_PREFIX = "restored_from_archive:"


def _parse_iso(text: str) -> datetime:
    """Tolerant ISO-8601 parse. Naive timestamps are interpreted as UTC."""
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {col: row[col] for col in COLUMNS}


def _bucket_by_month(rows: Iterable[dict[str, Any]]) -> dict[tuple[int, int], list[dict[str, Any]]]:
    """Group rows by (year, month) parsed from ``created_at``."""
    buckets: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        when = _parse_iso(str(row["created_at"]))
        buckets[(when.year, when.month)].append(row)
    return buckets


def _write_shard(path: Path, rows: list[dict[str, Any]], *, overwrite: bool) -> int:
    """Write rows as JSONL to ``path``. Returns the row count written."""
    rows_sorted = sorted(rows, key=lambda r: int(r["id"]))
    if path.exists() and not overwrite:
        raise FileExistsError(
            f"refusing to overwrite {path} (re-run with --overwrite to replace)"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        for row in rows_sorted:
            fp.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            fp.write("\n")
    tmp.replace(path)
    return len(rows_sorted)


def export_rows(
    *,
    conn: sqlite3.Connection,
    output_dir: Path,
    min_age_days: int,
    overwrite: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Export rows from ``ops_audit_log`` older than ``min_age_days`` to
    monthly JSONL shards under ``output_dir``.

    Returns a manifest dict: {months_written, rows_written, shards: [...]}.
    """
    cutoff_dt = (now or datetime.now(timezone.utc)) - timedelta(days=min_age_days)
    cutoff_iso = cutoff_dt.isoformat()
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        f"""
        SELECT {", ".join(COLUMNS)}
        FROM ops_audit_log
        WHERE created_at < ?
        ORDER BY id ASC
        """,
        (cutoff_iso,),
    )
    rows = [_row_to_dict(r) for r in cur.fetchall()]
    buckets = _bucket_by_month(rows)
    shards: list[dict[str, Any]] = []
    for (year, month), bucket_rows in sorted(buckets.items()):
        shard_path = output_dir / f"{year:04d}" / f"{month:02d}.jsonl"
        written = _write_shard(shard_path, bucket_rows, overwrite=overwrite)
        shards.append({"path": str(shard_path), "rows": written})
    return {
        "cutoff_iso": cutoff_iso,
        "rows_written": sum(s["rows"] for s in shards),
        "shards": shards,
    }


def restore_rows(
    *,
    conn: sqlite3.Connection,
    archive: Path,
    where: str | None,
) -> dict[str, Any]:
    """Insert rows from a JSONL archive back into ``ops_audit_log`` with a
    fresh ``id`` and a ``restored_from_archive:`` reason prefix.

    The ``where`` filter is a Python eval-able dict-style match like
    ``request_id="req-abc"`` (split on ``=`` once, comma-separated for
    multiple predicates). Each row's matching column value must equal the
    quoted literal exactly. Anything beyond simple equality is intentionally
    not supported — the exporter is a backstop, not a query engine.
    """
    if not archive.exists():
        raise FileNotFoundError(f"archive not found: {archive}")
    predicates: list[tuple[str, str]] = []
    if where:
        for clause in where.split(","):
            clause = clause.strip()
            if not clause:
                continue
            if "=" not in clause:
                raise ValueError(f"unsupported where clause: {clause!r}")
            key, raw_val = clause.split("=", 1)
            key = key.strip()
            val = raw_val.strip().strip('"').strip("'")
            if key not in COLUMNS:
                raise ValueError(f"unknown column in where: {key!r}")
            predicates.append((key, val))
    inserted = 0
    with archive.open(encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if predicates:
                match = all(str(row.get(k)) == v for k, v in predicates)
                if not match:
                    continue
            old_reason = row.get("reason") or ""
            new_reason = f"{RESTORED_PREFIX}{old_reason}"
            conn.execute(
                """
                INSERT INTO ops_audit_log
                  (request_id, endpoint, reviewer, reason,
                   payload_json, response_json, outcome, http_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("request_id"),
                    row["endpoint"],
                    row["reviewer"],
                    new_reason,
                    row.get("payload_json"),
                    row.get("response_json"),
                    row["outcome"],
                    int(row["http_status"]),
                    row["created_at"],
                ),
            )
            inserted += 1
    conn.commit()
    return {"archive": str(archive), "restored_rows": inserted, "predicates": predicates}


def _open_default_db() -> sqlite3.Connection:
    """Default DB lookup: <repo>/case-workbench.db. Tests inject explicitly."""
    repo_root = Path(__file__).resolve().parents[2]
    db_path = repo_root / "case-workbench.db"
    return sqlite3.connect(db_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("case-workbench-archive/ops_audit"),
        help="root directory for exported JSONL shards",
    )
    parser.add_argument(
        "--min-age-days",
        type=int,
        default=90,
        help="only export rows whose created_at is older than this many days",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing shard files (default: refuse and exit non-zero)",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help="restore mode: read archive shard and re-insert matching rows",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        help="archive JSONL to restore from (required when --restore)",
    )
    parser.add_argument(
        "--where",
        type=str,
        default=None,
        help='restore filter, e.g. \'request_id="req-abc123"\'',
    )
    args = parser.parse_args(argv)

    conn = _open_default_db()
    try:
        if args.restore:
            if not args.archive:
                parser.error("--archive is required with --restore")
            result = restore_rows(conn=conn, archive=args.archive, where=args.where)
        else:
            result = export_rows(
                conn=conn,
                output_dir=args.output_dir,
                min_age_days=args.min_age_days,
                overwrite=args.overwrite,
            )
    finally:
        conn.close()
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
