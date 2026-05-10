"""Audit log for case mutations — feeds the 30-second undo window.

Design:
- Any mutation that touches a row in `cases` (PATCH, batch update, merge, rescan,
  on-demand v3 upgrade) calls `record_revision` BEFORE applying changes, with the
  current row snapshot as `before` and the post-change snapshot as `after`.
- Undo means: take the most recent revision for that case, write a new revision
  with op='undo', and copy `before_json` back into `cases`.
- Multiple cases can be touched in one logical operation (batch / merge); each
  gets its own revision row, but they share a `group_id` via the same `op` and
  `changed_at` window — for now we just write one row per case and the undo
  endpoint operates one case at a time.
- Snapshots only include the columns we actually mutate via API. We don't snapshot
  the immutable scanner-managed columns (abs_path, scan_id, indexed_at) — those
  aren't user-editable through this layer.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import stress

# Columns the audit layer treats as user-mutable. If a future PATCH grows the
# surface area, add the column name here and the snapshot logic will pick it up.
TRACKED_COLUMNS: tuple[str, ...] = (
    "manual_category",
    "manual_template_tier",
    "manual_blocking_issues_json",
    "notes",
    "tags_json",
    "review_status",
    "reviewed_at",
    "customer_id",
    "held_until",
    "hold_reason",
    # The scanner-side columns are also tracked because B3's single-case rescan
    # and C2's v3 upgrade rewrite them — and we want undo to roll those back too.
    "category",
    "template_tier",
    "blocking_issues_json",
    "pose_delta_max",
    "sharp_ratio_min",
    "source_count",
    "labeled_count",
    "meta_json",
    # Stage A: skill 透传缓存,upgrade 写、undo 也要回滚
    "skill_image_metadata_json",
    "skill_blocking_detail_json",
    "skill_warnings_json",
)


def _snapshot(conn: sqlite3.Connection, case_id: int) -> dict[str, Any]:
    """Capture the tracked-columns subset of a case row as a dict."""
    cols = ",".join(TRACKED_COLUMNS)
    row = conn.execute(
        f"SELECT {cols} FROM cases WHERE id = ?", (case_id,)
    ).fetchone()
    if row is None:
        return {}
    out: dict[str, Any] = {}
    for col in TRACKED_COLUMNS:
        try:
            out[col] = row[col]
        except (IndexError, KeyError):
            out[col] = None
    return out


def record_revision(
    conn: sqlite3.Connection,
    case_id: int,
    op: str,
    before: dict[str, Any],
    after: dict[str, Any],
    source_route: str | None = None,
    actor: str = "user",
) -> int:
    """Insert one revision row. Returns the revision id."""
    now = datetime.now(timezone.utc).isoformat()
    before = stress.tag_payload(before)
    after = stress.tag_payload(after)
    cur = conn.execute(
        """
        INSERT INTO case_revisions
            (case_id, changed_at, actor, op, before_json, after_json, source_route)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            case_id,
            now,
            actor,
            op,
            json.dumps(before, ensure_ascii=False),
            json.dumps(after, ensure_ascii=False),
            source_route,
        ),
    )
    return cur.lastrowid or 0


def snapshot_before(conn: sqlite3.Connection, case_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Capture pre-state for a batch — call before applying mutations."""
    return {cid: _snapshot(conn, cid) for cid in case_ids}


def record_after(
    conn: sqlite3.Connection,
    case_ids: list[int],
    befores: dict[int, dict[str, Any]],
    op: str,
    source_route: str | None = None,
    actor: str = "user",
) -> list[int]:
    """Capture post-state and write one revision per case."""
    ids: list[int] = []
    for cid in case_ids:
        before = befores.get(cid, {})
        after = _snapshot(conn, cid)
        if before == after:
            # No-op write — don't pollute the undo stack.
            continue
        ids.append(record_revision(conn, cid, op, before, after, source_route=source_route, actor=actor))
    return ids


def latest_active_revision(
    conn: sqlite3.Connection, case_id: int
) -> sqlite3.Row | None:
    """Most recent revision that hasn't been undone, for this case.

    Excludes render / undo_render, evaluate / undo_evaluate, and restore_render
    — those go through their own undo paths (`RenderQueue.undo_render()`, the
    evaluations route's undo handler, or — for restore_render — re-restore via
    POST /api/cases/{id}/render/restore on `previous_archived_at`) instead of
    `apply_undo()`, because their before/after payloads don't fit TRACKED_COLUMNS;
    routing them through apply_undo would null every tracked column.
    """
    return conn.execute(
        """
        SELECT * FROM case_revisions
        WHERE case_id = ?
          AND undone_at IS NULL
          AND op NOT IN ('undo', 'render', 'undo_render', 'evaluate', 'undo_evaluate', 'restore_render')
        ORDER BY changed_at DESC, id DESC
        LIMIT 1
        """,
        (case_id,),
    ).fetchone()


def apply_undo(conn: sqlite3.Connection, case_id: int, source_route: str | None = None) -> dict[str, Any]:
    """
    Roll a single case back to its previous state by reapplying `before_json`
    of the latest active revision. Records a new revision with op='undo' and
    marks the source revision as undone.

    Returns the restored snapshot dict (the new "current" state of the case).
    Raises ValueError if there's nothing to undo.
    """
    rev = latest_active_revision(conn, case_id)
    if rev is None:
        raise ValueError("nothing to undo")

    before_state = json.loads(rev["before_json"] or "{}")
    if not before_state:
        raise ValueError("revision has empty before_state")

    # Snapshot the current state — this becomes the `before` of the undo revision.
    current = _snapshot(conn, case_id)

    # Build dynamic UPDATE that restores all tracked columns to before_state values.
    sets: list[str] = []
    values: list[Any] = []
    for col in TRACKED_COLUMNS:
        sets.append(f"{col} = ?")
        values.append(before_state.get(col))
    values.append(case_id)
    conn.execute(f"UPDATE cases SET {', '.join(sets)} WHERE id = ?", values)

    # Write the undo revision: before = current state pre-undo, after = restored state.
    record_revision(
        conn,
        case_id,
        op="undo",
        before=current,
        after=before_state,
        source_route=source_route,
        actor="user",
    )

    # Mark the source revision as undone.
    conn.execute(
        "UPDATE case_revisions SET undone_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), rev["id"]),
    )

    return before_state
