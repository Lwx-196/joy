"""Smoke tests for `/api/evaluations` endpoints."""
from __future__ import annotations


def test_create_evaluation_for_case(client, seed_case):
    case_id = seed_case(abs_path="/tmp/case-eval-1")
    resp = client.post(
        "/api/evaluations",
        json={
            "subject_kind": "case",
            "subject_id": case_id,
            "verdict": "approved",
            "reviewer": "dr-lin",
            "note": "looks good",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["subject_kind"] == "case"
    assert body["subject_id"] == case_id
    assert body["verdict"] == "approved"
    assert body["reviewer"] == "dr-lin"
    assert body["undone_at"] is None

    history = client.get(
        "/api/evaluations",
        params={"subject_kind": "case", "subject_id": case_id},
    ).json()
    assert len(history) == 1
    assert history[0]["id"] == body["id"]


def test_create_evaluation_404_unknown_case(client):
    resp = client.post(
        "/api/evaluations",
        json={
            "subject_kind": "case",
            "subject_id": 9999,
            "verdict": "approved",
            "reviewer": "dr-lin",
        },
    )
    assert resp.status_code == 404
    assert "case 9999 not found" in resp.json()["detail"]


def test_create_evaluation_400_blank_reviewer(client, seed_case):
    case_id = seed_case(abs_path="/tmp/case-eval-blank")
    resp = client.post(
        "/api/evaluations",
        json={
            "subject_kind": "case",
            "subject_id": case_id,
            "verdict": "approved",
            "reviewer": "    ",
        },
    )
    assert resp.status_code == 400
    assert "reviewer cannot be blank" in resp.json()["detail"]


def test_re_evaluate_auto_undoes_prior_active(client, seed_case):
    """Creating a 2nd evaluation for same subject auto-undoes the first."""
    case_id = seed_case(abs_path="/tmp/case-reeval")
    first = client.post(
        "/api/evaluations",
        json={
            "subject_kind": "case",
            "subject_id": case_id,
            "verdict": "approved",
            "reviewer": "dr-lin",
        },
    ).json()
    second = client.post(
        "/api/evaluations",
        json={
            "subject_kind": "case",
            "subject_id": case_id,
            "verdict": "rejected",
            "reviewer": "dr-lin",
        },
    ).json()
    assert first["id"] != second["id"]

    history = client.get(
        "/api/evaluations",
        params={"subject_kind": "case", "subject_id": case_id},
    ).json()
    assert len(history) == 2
    by_id = {row["id"]: row for row in history}
    assert by_id[first["id"]]["undone_at"] is not None
    assert by_id[second["id"]]["undone_at"] is None


def test_undo_evaluation_409_when_already_undone(client, seed_case):
    case_id = seed_case(abs_path="/tmp/case-undo-twice")
    created = client.post(
        "/api/evaluations",
        json={
            "subject_kind": "case",
            "subject_id": case_id,
            "verdict": "approved",
            "reviewer": "dr-lin",
        },
    ).json()
    eval_id = created["id"]
    first_undo = client.post(f"/api/evaluations/{eval_id}/undo")
    assert first_undo.status_code == 200
    assert first_undo.json()["undone_at"] is not None

    second_undo = client.post(f"/api/evaluations/{eval_id}/undo")
    assert second_undo.status_code == 409
    assert "already undone" in second_undo.json()["detail"]
