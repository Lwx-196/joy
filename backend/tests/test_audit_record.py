"""Tests for `audit.record_revision`, `snapshot_before`, and `record_after`.

`record_after` has a no-op detection (skip when before == after) — this is
load-bearing because PATCH calls always go through it, and a request that
sends unchanged values must not pollute the undo stack with an empty revision.
"""
from __future__ import annotations

import json

from backend import audit, db


def test_record_revision_inserts_row_with_json_payloads(seed_case):
    case_id = seed_case()
    before = {"notes": "old"}
    after = {"notes": "new"}

    with db.connect() as conn:
        rev_id = audit.record_revision(
            conn, case_id, op="patch", before=before, after=after,
            source_route="/api/test", actor="user",
        )
        row = conn.execute(
            "SELECT * FROM case_revisions WHERE id = ?", (rev_id,)
        ).fetchone()

    assert rev_id > 0
    assert row["case_id"] == case_id
    assert row["op"] == "patch"
    assert row["actor"] == "user"
    assert row["source_route"] == "/api/test"
    assert json.loads(row["before_json"]) == before
    assert json.loads(row["after_json"]) == after
    assert row["undone_at"] is None


def test_record_revision_preserves_unicode(seed_case):
    """`ensure_ascii=False` should keep CJK chars readable in the DB."""
    case_id = seed_case()
    with db.connect() as conn:
        rev_id = audit.record_revision(
            conn, case_id, op="patch",
            before={"notes": "原始备注"},
            after={"notes": "新备注 ✓"},
        )
        row = conn.execute(
            "SELECT before_json, after_json FROM case_revisions WHERE id = ?",
            (rev_id,),
        ).fetchone()

    assert "原始备注" in row["before_json"]
    assert "新备注" in row["after_json"]


def test_snapshot_before_captures_tracked_columns(seed_case):
    case_id = seed_case(category="A", template_tier="standard", notes="hello")
    with db.connect() as conn:
        snapshots = audit.snapshot_before(conn, [case_id])

    assert case_id in snapshots
    snap = snapshots[case_id]
    assert snap["category"] == "A"
    assert snap["template_tier"] == "standard"
    assert snap["notes"] == "hello"
    # Spot check that all TRACKED_COLUMNS keys are present (None for unset).
    for col in audit.TRACKED_COLUMNS:
        assert col in snap


def test_snapshot_before_returns_empty_for_missing_case():
    with db.connect() as conn:
        snapshots = audit.snapshot_before(conn, [9999])
    assert snapshots == {9999: {}}


def test_record_after_skips_no_op_writes(seed_case):
    """If a PATCH didn't actually change tracked columns, no revision should be written."""
    case_id = seed_case(notes="unchanged")

    with db.connect() as conn:
        befores = audit.snapshot_before(conn, [case_id])
        # Don't mutate anything between snapshot_before and record_after.
        ids = audit.record_after(conn, [case_id], befores, op="patch", source_route="/api/test")

    assert ids == [], "no-op write should not produce any revision rows"

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM case_revisions WHERE case_id = ?", (case_id,)
        ).fetchone()
    assert rows["n"] == 0


def test_record_after_writes_revision_when_columns_change(seed_case):
    case_id = seed_case(notes="orig")

    with db.connect() as conn:
        befores = audit.snapshot_before(conn, [case_id])
        conn.execute("UPDATE cases SET notes = ? WHERE id = ?", ("changed", case_id))
        ids = audit.record_after(conn, [case_id], befores, op="patch", source_route="/api/test")

    assert len(ids) == 1

    with db.connect() as conn:
        row = conn.execute(
            "SELECT before_json, after_json, op FROM case_revisions WHERE id = ?",
            (ids[0],),
        ).fetchone()

    assert row["op"] == "patch"
    assert json.loads(row["before_json"])["notes"] == "orig"
    assert json.loads(row["after_json"])["notes"] == "changed"


def test_record_after_handles_batch_with_partial_changes(seed_case):
    """Batch PATCH: only cases that actually changed should get revision rows."""
    cid_changed = seed_case(abs_path="/tmp/c1", notes="orig")
    cid_unchanged = seed_case(abs_path="/tmp/c2", notes="static")

    with db.connect() as conn:
        befores = audit.snapshot_before(conn, [cid_changed, cid_unchanged])
        conn.execute("UPDATE cases SET notes = ? WHERE id = ?", ("changed", cid_changed))
        ids = audit.record_after(
            conn, [cid_changed, cid_unchanged], befores,
            op="batch", source_route="/api/cases/batch",
        )

    assert len(ids) == 1

    with db.connect() as conn:
        row = conn.execute(
            "SELECT case_id FROM case_revisions WHERE id = ?", (ids[0],)
        ).fetchone()
    assert row["case_id"] == cid_changed
