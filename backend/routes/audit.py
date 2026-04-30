"""Audit log endpoints: list revisions for a case, undo last mutation."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from .. import audit, db

router = APIRouter(prefix="/api/cases", tags=["audit"])


@router.get("/{case_id}/revisions")
def list_revisions(case_id: int, limit: int = 20) -> dict:
    """Return revision history newest-first. Each entry has op, actor,
    changed_at, before/after dicts, undone flag."""
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, case_id, changed_at, actor, op, before_json, after_json,
                   source_route, undone_at
            FROM case_revisions
            WHERE case_id = ?
            ORDER BY changed_at DESC, id DESC
            LIMIT ?
            """,
            (case_id, limit),
        ).fetchall()
    return {
        "revisions": [
            {
                "id": r["id"],
                "case_id": r["case_id"],
                "changed_at": r["changed_at"],
                "actor": r["actor"],
                "op": r["op"],
                "before": json.loads(r["before_json"] or "{}"),
                "after": json.loads(r["after_json"] or "{}"),
                "source_route": r["source_route"],
                "undone_at": r["undone_at"],
            }
            for r in rows
        ]
    }


@router.post("/{case_id}/undo")
def undo_case(case_id: int) -> dict:
    """Reapply the previous state of the most recent active revision."""
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        try:
            restored = audit.apply_undo(conn, case_id, source_route=f"/api/cases/{case_id}/undo")
        except ValueError as e:
            raise HTTPException(409, str(e))
    return {"undone": True, "case_id": case_id, "restored": restored}
