"""Tests for `audit.apply_undo` and the `/api/cases/{id}/undo` endpoint.

Covers the core mutation/undo round-trip — the load-bearing behavior that the
30-second undo window in the UI depends on.
"""
from __future__ import annotations

import json

import pytest

from backend import audit, db


def test_apply_undo_restores_tracked_columns(seed_case, insert_revision):
    case_id = seed_case(category="A", template_tier="standard", notes="original")

    # Manually mutate the case row, then record a revision pointing at the old state.
    with db.connect() as conn:
        before = audit._snapshot(conn, case_id)
        conn.execute("UPDATE cases SET notes = ? WHERE id = ?", ("changed", case_id))

    # Record the revision after the mutation (so before_json captures the original state).
    insert_revision(case_id=case_id, op="patch", before=before, after={"notes": "changed"})

    # Apply undo — should roll `notes` back to "original".
    with db.connect() as conn:
        restored = audit.apply_undo(conn, case_id)

    assert restored["notes"] == "original"

    with db.connect() as conn:
        row = conn.execute("SELECT notes FROM cases WHERE id = ?", (case_id,)).fetchone()
    assert row["notes"] == "original"


def test_apply_undo_marks_source_revision_undone(seed_case, insert_revision):
    case_id = seed_case(notes="x")
    with db.connect() as conn:
        before = audit._snapshot(conn, case_id)
        conn.execute("UPDATE cases SET notes = ? WHERE id = ?", ("y", case_id))
    rev_id = insert_revision(case_id=case_id, op="patch", before=before, after={"notes": "y"})

    with db.connect() as conn:
        audit.apply_undo(conn, case_id)
        row = conn.execute(
            "SELECT undone_at FROM case_revisions WHERE id = ?", (rev_id,)
        ).fetchone()

    assert row["undone_at"] is not None


def test_apply_undo_writes_new_undo_revision(seed_case, insert_revision):
    case_id = seed_case(notes="a")
    with db.connect() as conn:
        before = audit._snapshot(conn, case_id)
        conn.execute("UPDATE cases SET notes = ? WHERE id = ?", ("b", case_id))
    insert_revision(case_id=case_id, op="patch", before=before, after={"notes": "b"})

    with db.connect() as conn:
        audit.apply_undo(conn, case_id, source_route="/test/undo")
        rows = conn.execute(
            "SELECT op, source_route FROM case_revisions WHERE case_id = ? ORDER BY id",
            (case_id,),
        ).fetchall()

    ops = [r["op"] for r in rows]
    assert ops == ["patch", "undo"]
    assert rows[1]["source_route"] == "/test/undo"


def test_apply_undo_raises_when_nothing_to_undo(seed_case):
    case_id = seed_case()
    with db.connect() as conn:
        with pytest.raises(ValueError, match="nothing to undo"):
            audit.apply_undo(conn, case_id)


def test_apply_undo_skips_already_undone_revisions(seed_case, insert_revision):
    case_id = seed_case(notes="orig")
    insert_revision(
        case_id=case_id, op="patch",
        before={"notes": "orig"}, after={"notes": "x"},
        undone_at="2026-01-01T00:00:00Z",
    )
    with db.connect() as conn:
        with pytest.raises(ValueError, match="nothing to undo"):
            audit.apply_undo(conn, case_id)


def test_apply_undo_excludes_restore_render_op(seed_case, insert_revision):
    """Stage 12 safety: restore_render must never flow through apply_undo,
    otherwise its non-tracked-column payload would null every tracked field."""
    case_id = seed_case(notes="kept")
    insert_revision(case_id=case_id, op="restore_render", before={}, after={})

    with db.connect() as conn:
        with pytest.raises(ValueError, match="nothing to undo"):
            audit.apply_undo(conn, case_id)

        # Verify the case row wasn't mutated.
        row = conn.execute("SELECT notes FROM cases WHERE id = ?", (case_id,)).fetchone()
    assert row["notes"] == "kept"


# ---- HTTP endpoint --------------------------------------------------------


def test_undo_endpoint_returns_409_when_nothing_to_undo(client, seed_case):
    case_id = seed_case()
    resp = client.post(f"/api/cases/{case_id}/undo")
    assert resp.status_code == 409


def test_undo_endpoint_404_when_case_missing(client):
    resp = client.post("/api/cases/9999/undo")
    assert resp.status_code == 404


def test_undo_endpoint_round_trips(client, seed_case, insert_revision):
    case_id = seed_case(notes="orig")
    with db.connect() as conn:
        before = audit._snapshot(conn, case_id)
        conn.execute("UPDATE cases SET notes = ? WHERE id = ?", ("changed", case_id))
    insert_revision(case_id=case_id, op="patch", before=before, after={"notes": "changed"})

    resp = client.post(f"/api/cases/{case_id}/undo")
    assert resp.status_code == 200
    body = resp.json()
    assert body["undone"] is True
    assert body["case_id"] == case_id
    assert body["restored"]["notes"] == "orig"
