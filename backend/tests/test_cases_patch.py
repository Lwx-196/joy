"""Tests for `PATCH /api/cases/{id}` — verifies revision recording + return shape.

Sister tests to `test_audit_record.py` but exercised through the HTTP layer.
"""
from __future__ import annotations

from backend import db


def test_patch_updates_notes_and_records_revision(client, seed_case):
    case_id = seed_case(notes="orig")

    resp = client.patch(f"/api/cases/{case_id}", json={"notes": "updated"})
    assert resp.status_code == 200
    assert resp.json()["notes"] == "updated"

    with db.connect() as conn:
        rev = conn.execute(
            "SELECT op, source_route FROM case_revisions WHERE case_id = ? ORDER BY id DESC LIMIT 1",
            (case_id,),
        ).fetchone()

    assert rev is not None
    assert rev["op"] == "patch"
    assert rev["source_route"] == f"/api/cases/{case_id}"


def test_patch_no_op_does_not_create_revision(client, seed_case):
    """Sending the same value as currently stored must not pollute the undo stack."""
    case_id = seed_case(notes="same")

    resp = client.patch(f"/api/cases/{case_id}", json={"notes": "same"})
    assert resp.status_code == 200

    with db.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM case_revisions WHERE case_id = ?", (case_id,)
        ).fetchone()
    assert count["n"] == 0


def test_patch_404_for_missing_case(client):
    resp = client.patch("/api/cases/9999", json={"notes": "x"})
    assert resp.status_code == 404


def test_patch_then_undo_round_trip_via_http(client, seed_case):
    case_id = seed_case(notes="orig")

    # Apply a change.
    resp = client.patch(f"/api/cases/{case_id}", json={"notes": "changed"})
    assert resp.status_code == 200
    assert resp.json()["notes"] == "changed"

    # Undo it.
    undo_resp = client.post(f"/api/cases/{case_id}/undo")
    assert undo_resp.status_code == 200
    assert undo_resp.json()["restored"]["notes"] == "orig"

    # Verify the case is back to original via GET.
    get_resp = client.get(f"/api/cases/{case_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["notes"] == "orig"


def test_revisions_endpoint_lists_in_reverse_chronological_order(client, seed_case):
    case_id = seed_case(notes="v0")
    client.patch(f"/api/cases/{case_id}", json={"notes": "v1"})
    client.patch(f"/api/cases/{case_id}", json={"notes": "v2"})

    resp = client.get(f"/api/cases/{case_id}/revisions")
    assert resp.status_code == 200
    revisions = resp.json()["revisions"]
    assert len(revisions) == 2
    # Newest first.
    assert revisions[0]["op"] == "patch"
    assert revisions[1]["op"] == "patch"
