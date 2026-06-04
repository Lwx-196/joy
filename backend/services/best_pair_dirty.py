"""Dirty marker for cached best-pair candidates."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_FIELDS_ALWAYS_DIRTY = frozenset({"manual_phase", "manual_view"})


def _candidate_name(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, dict):
        text = str(value.get("filename") or value.get("render_filename") or "").strip()
    else:
        text = ""
    return text or None


def _candidate_filenames(candidates_json: str | None) -> set[str]:
    try:
        data = json.loads(candidates_json or "[]")
    except (TypeError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    out: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        for key in ("before", "after"):
            name = _candidate_name(item.get(key))
            if name:
                out.add(name)
                out.add(Path(name).name)
    return out


def _should_mark_dirty(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    filename: str,
    changed_fields: Iterable[str],
) -> bool:
    fields = {str(field) for field in (changed_fields or ()) if str(field)}
    if not fields:
        return False
    if fields & _FIELDS_ALWAYS_DIRTY:
        return True
    row = conn.execute(
        "SELECT candidates_json FROM case_best_pairs WHERE case_id = ?",
        (case_id,),
    ).fetchone()
    if row is None:
        return False
    candidates_json = row["candidates_json"] if isinstance(row, sqlite3.Row) else row[0]
    names = _candidate_filenames(candidates_json)
    return filename in names or Path(filename).name in names


def mark_best_pair_dirty(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    filename: str,
    changed_fields: Iterable[str],
) -> bool:
    """Flip an existing best-pair cache row to dirty when an override matters."""
    if not _should_mark_dirty(
        conn,
        case_id=case_id,
        filename=filename,
        changed_fields=changed_fields,
    ):
        return False
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        UPDATE case_best_pairs
        SET status = 'dirty',
            source_version = source_version + 1,
            updated_at = ?
        WHERE case_id = ?
        """,
        (now, case_id),
    )
    return cur.rowcount > 0

