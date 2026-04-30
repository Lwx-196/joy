"""Tests for `backend.audit.latest_active_revision`.

The exclusion set in `latest_active_revision` is a load-bearing safety control:
op kinds whose `before_json` doesn't fit `TRACKED_COLUMNS` (render/undo_render,
evaluate/undo_evaluate, restore_render) must NOT be returned, otherwise
`apply_undo` would null every tracked column. The `restore_render` exclusion
specifically was the Stage 12 safety fix.
"""
from __future__ import annotations

from backend import audit, db


def test_latest_active_revision_returns_most_recent_patch(seed_case, insert_revision):
    case_id = seed_case()
    insert_revision(case_id=case_id, op="patch")
    second = insert_revision(case_id=case_id, op="patch")

    with db.connect() as conn:
        rev = audit.latest_active_revision(conn, case_id)

    assert rev is not None
    assert rev["id"] == second
    assert rev["op"] == "patch"


def test_latest_active_revision_excludes_restore_render(seed_case, insert_revision):
    """Stage 12 safety fix: `restore_render` rows must not flow through apply_undo."""
    case_id = seed_case()
    patch_rev = insert_revision(case_id=case_id, op="patch")
    insert_revision(case_id=case_id, op="restore_render")  # newer, must be skipped

    with db.connect() as conn:
        rev = audit.latest_active_revision(conn, case_id)

    assert rev is not None, "should fall back to the older patch row"
    assert rev["id"] == patch_rev
    assert rev["op"] == "patch"


def test_latest_active_revision_excludes_render_and_undo_render(seed_case, insert_revision):
    case_id = seed_case()
    patch_rev = insert_revision(case_id=case_id, op="patch")
    insert_revision(case_id=case_id, op="render")
    insert_revision(case_id=case_id, op="undo_render")

    with db.connect() as conn:
        rev = audit.latest_active_revision(conn, case_id)

    assert rev is not None
    assert rev["id"] == patch_rev


def test_latest_active_revision_excludes_evaluate_and_undo_evaluate(seed_case, insert_revision):
    case_id = seed_case()
    patch_rev = insert_revision(case_id=case_id, op="patch")
    insert_revision(case_id=case_id, op="evaluate")
    insert_revision(case_id=case_id, op="undo_evaluate")

    with db.connect() as conn:
        rev = audit.latest_active_revision(conn, case_id)

    assert rev is not None
    assert rev["id"] == patch_rev


def test_latest_active_revision_excludes_undo(seed_case, insert_revision):
    case_id = seed_case()
    patch_rev = insert_revision(case_id=case_id, op="patch")
    insert_revision(case_id=case_id, op="undo")

    with db.connect() as conn:
        rev = audit.latest_active_revision(conn, case_id)

    assert rev is not None
    assert rev["id"] == patch_rev


def test_latest_active_revision_skips_undone_rows(seed_case, insert_revision):
    case_id = seed_case()
    older = insert_revision(case_id=case_id, op="patch")
    insert_revision(case_id=case_id, op="patch", undone_at="2026-01-01T00:00:00Z")

    with db.connect() as conn:
        rev = audit.latest_active_revision(conn, case_id)

    assert rev is not None
    assert rev["id"] == older


def test_latest_active_revision_returns_none_when_all_excluded(seed_case, insert_revision):
    case_id = seed_case()
    insert_revision(case_id=case_id, op="render")
    insert_revision(case_id=case_id, op="restore_render")

    with db.connect() as conn:
        rev = audit.latest_active_revision(conn, case_id)

    assert rev is None


def test_latest_active_revision_returns_none_for_case_with_no_revisions(seed_case):
    case_id = seed_case()

    with db.connect() as conn:
        rev = audit.latest_active_revision(conn, case_id)

    assert rev is None
