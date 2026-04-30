"""Tests for `POST /api/cases/{id}/render/restore`.

The `archived_at` field is concatenated into a filesystem path
(`<abs_path>/.case-layout-output/<brand>/<template>/render/.history/<archived_at>.jpg`).
The strict `\\d{8}T\\d{6}Z` fullmatch in the route is a load-bearing
path-traversal defense — without it `archived_at="../../etc/passwd"` could
escape the .history directory.
"""
from __future__ import annotations


def test_restore_rejects_path_traversal(client, seed_case):
    case_id = seed_case()
    resp = client.post(
        f"/api/cases/{case_id}/render/restore",
        json={"archived_at": "../../etc/passwd", "brand": "fumei"},
    )
    # Pydantic min_length/max_length=16/20 will reject longer strings before
    # the fullmatch even runs. Either 400 or 422 is an acceptable rejection.
    assert resp.status_code in (400, 422)


def test_restore_rejects_dot_segment_within_length(client, seed_case):
    """`..\\d{6}T\\d{6}Z` is 16 chars long, passes Pydantic length, but must
    still be rejected by the fullmatch regex."""
    case_id = seed_case()
    resp = client.post(
        f"/api/cases/{case_id}/render/restore",
        json={"archived_at": "../20260101T000000Z"[:16], "brand": "fumei"},
    )
    assert resp.status_code == 400
    assert "invalid archived_at" in resp.json()["detail"]


def test_restore_rejects_malformed_format(client, seed_case):
    case_id = seed_case()
    resp = client.post(
        f"/api/cases/{case_id}/render/restore",
        json={"archived_at": "2026-04-29T00:00Z", "brand": "fumei"},
    )
    assert resp.status_code == 400
    assert "invalid archived_at" in resp.json()["detail"]


def test_restore_accepts_well_formed_archived_at_but_404_for_missing_case(client):
    # Well-formed timestamp, case 9999 doesn't exist → 404 case not found.
    resp = client.post(
        "/api/cases/9999/render/restore",
        json={"archived_at": "20260429T120000Z", "brand": "fumei"},
    )
    assert resp.status_code == 404
    assert "case not found" in resp.json()["detail"]


def test_restore_rejects_unsupported_brand(client, seed_case):
    case_id = seed_case()
    resp = client.post(
        f"/api/cases/{case_id}/render/restore",
        json={"archived_at": "20260429T120000Z", "brand": "not-a-real-brand"},
    )
    assert resp.status_code == 400
    assert "unsupported brand" in resp.json()["detail"]


def test_restore_404_when_snapshot_missing_on_disk(client, seed_case, tmp_path):
    """Case exists, archived_at is well-formed, but the .history file doesn't."""
    case_dir = tmp_path / "case-x"
    case_dir.mkdir()
    case_id = seed_case(abs_path=str(case_dir))

    resp = client.post(
        f"/api/cases/{case_id}/render/restore",
        json={"archived_at": "20260429T120000Z", "brand": "fumei"},
    )
    assert resp.status_code == 404
    assert "snapshot not found" in resp.json()["detail"]
