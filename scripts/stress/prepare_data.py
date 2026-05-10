#!/usr/bin/env python3
"""Prepare a real-data stress DB and cloned writable case directories."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path


SKIP_DIRS = {
    ".case-workbench-trash",
    ".case-workbench-simulation-inputs",
    "__pycache__",
}


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {name for name in names if name in SKIP_DIRS or name == ".DS_Store"}


def _select_case_ids(conn: sqlite3.Connection, requested: str | None, limit: int) -> list[int]:
    if requested:
        return list(dict.fromkeys(int(item.strip()) for item in requested.split(",") if item.strip()))
    ids: list[int] = []
    row = conn.execute("SELECT id FROM cases WHERE id = 126 AND trashed_at IS NULL").fetchone()
    if row:
        ids.append(126)
    for row in conn.execute(
        """
        SELECT id
        FROM cases
        WHERE trashed_at IS NULL
          AND id != 126
        ORDER BY
          CASE WHEN source_count IS NULL OR source_count = 0 THEN 1 ELSE 0 END,
          id ASC
        LIMIT ?
        """,
        (max(0, limit - len(ids)),),
    ).fetchall():
        ids.append(int(row["id"]))
    return ids[:limit]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--dest-db", required=True)
    parser.add_argument("--cases-root", required=True)
    parser.add_argument("--case-ids", default="")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    source_db = Path(args.source_db).expanduser().resolve()
    dest_db = Path(args.dest_db).expanduser().resolve()
    cases_root = Path(args.cases_root).expanduser().resolve()
    if not source_db.is_file():
        raise SystemExit(f"source DB not found: {source_db}")
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_db, dest_db)
    cases_root.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(dest_db)
    conn.row_factory = sqlite3.Row
    selected = _select_case_ids(conn, args.case_ids or None, max(1, args.limit))
    cloned: list[dict[str, str | int]] = []
    for case_id in selected:
        row = conn.execute(
            "SELECT id, abs_path FROM cases WHERE id = ? AND trashed_at IS NULL",
            (case_id,),
        ).fetchone()
        if not row:
            continue
        source_dir = Path(str(row["abs_path"])).expanduser().resolve()
        if not source_dir.is_dir():
            continue
        target_dir = cases_root / f"case-{case_id}-{source_dir.name}"
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(source_dir, target_dir, ignore=_ignore)
        conn.execute(
            "UPDATE cases SET abs_path = ?, original_abs_path = COALESCE(original_abs_path, ?) WHERE id = ?",
            (str(target_dir), str(source_dir), case_id),
        )
        old_prefix = str(source_dir)
        new_prefix = str(target_dir)
        for table, columns in {
            "render_jobs": ("output_path", "manifest_path"),
        }.items():
            for column in columns:
                conn.execute(
                    f"""
                    UPDATE {table}
                    SET {column} = REPLACE({column}, ?, ?)
                    WHERE case_id = ?
                      AND {column} IS NOT NULL
                      AND {column} LIKE ?
                    """,
                    (old_prefix, new_prefix, case_id, f"{old_prefix}%"),
                )
        cloned.append(
            {
                "case_id": case_id,
                "source_abs_path": str(source_dir),
                "stress_abs_path": str(target_dir),
            }
        )
    conn.commit()
    conn.close()

    print(
        json.dumps(
            {
                "source_db": str(source_db),
                "dest_db": str(dest_db),
                "cases_root": str(cases_root),
                "selected_case_ids": [item["case_id"] for item in cloned],
                "cloned": cloned,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
