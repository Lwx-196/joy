"""Tests for `POST /api/cases/{id}/upgrade` and `/rescan`.

These two endpoints both invoke heavy subprocess paths
(`skill_bridge.upgrade_case_to_v3` and `scanner.rescan_one`). The tests
monkeypatch those functions so we exercise the route layer's
404/400/500 mapping + audit revision side-effects without spinning up
mediapipe or touching real case directories.
"""
from __future__ import annotations

from typing import Any


# ----------------------------------------------------------------------
# /upgrade — sync path
# ----------------------------------------------------------------------


def test_upgrade_404_for_missing_case(client, monkeypatch):
    resp = client.post("/api/cases/9999/upgrade", params={"brand": "fumei"})
    assert resp.status_code == 404
    assert "case not found" in resp.json()["detail"]


def test_upgrade_404_when_executor_raises_filenotfound(client, seed_case, monkeypatch):
    """`FileNotFoundError` from the skill bridge → 404 case directory missing."""
    case_id = seed_case()

    def _raise_fnf(*args: Any, **kwargs: Any):
        raise FileNotFoundError("case dir gone")

    monkeypatch.setattr(
        "backend.skill_bridge.upgrade_case_to_v3", _raise_fnf
    )
    resp = client.post(f"/api/cases/{case_id}/upgrade", params={"brand": "fumei"})
    assert resp.status_code == 404
    assert "case directory missing" in resp.json()["detail"]


def test_upgrade_503_when_skill_unavailable(client, seed_case, monkeypatch):
    """`RuntimeError` (skill module missing/broken) → 503."""
    case_id = seed_case()
    monkeypatch.setattr(
        "backend.skill_bridge.upgrade_case_to_v3",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("skill broken")),
    )
    resp = client.post(f"/api/cases/{case_id}/upgrade", params={"brand": "fumei"})
    assert resp.status_code == 503
    assert "skill unavailable" in resp.json()["detail"]


def test_upgrade_500_for_unexpected_skill_failure(client, seed_case, monkeypatch):
    """Any other exception → 500 with skill upgrade failed prefix."""
    case_id = seed_case()
    monkeypatch.setattr(
        "backend.skill_bridge.upgrade_case_to_v3",
        lambda *a, **kw: (_ for _ in ()).throw(KeyError("bad key")),
    )
    resp = client.post(f"/api/cases/{case_id}/upgrade", params={"brand": "fumei"})
    assert resp.status_code == 500
    assert "skill upgrade failed" in resp.json()["detail"]


def test_upgrade_success_returns_case_detail(client, seed_case, monkeypatch):
    """When skill_bridge returns a valid payload, the route writes the new
    fields and returns the refreshed CaseDetail."""
    case_id = seed_case()

    def _fake_upgrade(case_dir: str, brand: str) -> dict[str, Any]:
        return {
            "category": "standard_face",
            "template_tier": "tri",
            "blocking_issues_json": "[]",
            "pose_delta_max": 0.05,
            "sharp_ratio_min": 0.95,
            "meta_extras": {"v3_run_id": "abc123", "skill_status": "ok"},
        }

    monkeypatch.setattr("backend.skill_bridge.upgrade_case_to_v3", _fake_upgrade)
    resp = client.post(f"/api/cases/{case_id}/upgrade", params={"brand": "fumei"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "standard_face"
    assert body["template_tier"] == "tri"


def test_upgrade_records_revision_with_op_upgrade(client, seed_case, monkeypatch):
    """Successful upgrade must write a revision with op='upgrade' so it shows
    up in the case history drawer + can be undone."""
    case_id = seed_case()
    monkeypatch.setattr(
        "backend.skill_bridge.upgrade_case_to_v3",
        lambda *a, **kw: {
            "category": "standard_face",
            "template_tier": "single",
            "blocking_issues_json": "[]",
            "pose_delta_max": None,
            "sharp_ratio_min": None,
            "meta_extras": {"skill_status": "ok"},
        },
    )
    client.post(f"/api/cases/{case_id}/upgrade", params={"brand": "fumei"})
    revs = client.get(f"/api/cases/{case_id}/revisions").json()["revisions"]
    assert any(r["op"] == "upgrade" for r in revs)


# ----------------------------------------------------------------------
# /rescan
# ----------------------------------------------------------------------


def test_rescan_404_for_missing_case(client):
    resp = client.post("/api/cases/9999/rescan")
    assert resp.status_code == 404
    assert "case not found" in resp.json()["detail"]


def test_rescan_400_when_scanner_raises_value_error(client, seed_case, monkeypatch):
    """scanner.rescan_one raises ValueError when the case dir doesn't exist
    on disk; the route maps that to 400 with the original message.
    """
    case_id = seed_case()
    monkeypatch.setattr(
        "backend.scanner.rescan_one",
        lambda conn, cid: (_ for _ in ()).throw(ValueError("case dir missing")),
    )
    resp = client.post(f"/api/cases/{case_id}/rescan")
    assert resp.status_code == 400
    assert "case dir missing" in resp.json()["detail"]


def test_rescan_success_returns_case_detail(client, seed_case, monkeypatch):
    case_id = seed_case()
    monkeypatch.setattr(
        "backend.scanner.rescan_one",
        lambda conn, cid: {"category": "standard_face", "template_tier": "tri"},
    )
    resp = client.post(f"/api/cases/{case_id}/rescan")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == case_id


def test_rescan_records_revision_with_op_rescan_and_actor_scan(
    client, seed_case, monkeypatch
):
    """`record_after` is called with op='rescan' and actor='scan' — the
    rescan revision must be tagged so the UI can distinguish it from
    user-initiated changes.
    """
    case_id = seed_case()

    def _fake_rescan(conn, cid):
        # Write a real change so record_after produces a revision.
        conn.execute(
            "UPDATE cases SET category = 'standard_face', template_tier = 'tri' WHERE id = ?",
            (cid,),
        )
        return {"category": "standard_face", "template_tier": "tri"}

    monkeypatch.setattr("backend.scanner.rescan_one", _fake_rescan)
    client.post(f"/api/cases/{case_id}/rescan")
    revs = client.get(f"/api/cases/{case_id}/revisions").json()["revisions"]
    rescan_revs = [r for r in revs if r["op"] == "rescan"]
    assert len(rescan_revs) == 1
    assert rescan_revs[0]["actor"] == "scan"


def test_rescan_no_op_does_not_create_revision(client, seed_case, monkeypatch):
    """If rescan_one writes no actual changes, record_after should not emit a
    revision — saves the audit log from spam.
    """
    case_id = seed_case()
    monkeypatch.setattr("backend.scanner.rescan_one", lambda conn, cid: None)
    client.post(f"/api/cases/{case_id}/rescan")
    revs = client.get(f"/api/cases/{case_id}/revisions").json()["revisions"]
    assert all(r["op"] != "rescan" for r in revs)
