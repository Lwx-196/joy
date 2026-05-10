"""Tests for cases endpoints not covered elsewhere.

Covers:
  - GET /api/cases/stats — totals + by_category / by_tier / by_review_status / manual_override_count
  - POST /api/cases/batch — empty / 404 / partial-valid / clear_fields / review_status auto-stamp
  - GET /api/cases/{id}/files — path-traversal defense + 404 for missing case/file
  - GET /api/cases/{id}/rename-suggestion — non_labeled vs already-named
"""
from __future__ import annotations


# ----------------------------------------------------------------------
# /stats
# ----------------------------------------------------------------------


def test_stats_empty_db_returns_zero_total(client):
    body = client.get("/api/cases/stats").json()
    assert body["total"] == 0
    assert body["by_category"] == {}
    assert body["by_tier"] == {}
    assert body["manual_override_count"] == 0


def test_stats_aggregates_by_category(client, seed_case):
    seed_case(abs_path="/tmp/stats-a", category="A")
    seed_case(abs_path="/tmp/stats-b", category="A")
    seed_case(abs_path="/tmp/stats-c", category="B")
    body = client.get("/api/cases/stats").json()
    assert body["total"] == 3
    assert body["by_category"]["A"] == 2
    assert body["by_category"]["B"] == 1


def test_stats_review_status_treats_null_as_unreviewed(client, seed_case):
    seed_case(abs_path="/tmp/stats-r1")
    seed_case(abs_path="/tmp/stats-r2")
    body = client.get("/api/cases/stats").json()
    # COALESCE(review_status, 'unreviewed') groups NULLs as 'unreviewed'
    assert body["by_review_status"]["unreviewed"] == 2


# ----------------------------------------------------------------------
# /batch
# ----------------------------------------------------------------------


def test_batch_400_when_case_ids_empty(client):
    resp = client.post(
        "/api/cases/batch", json={"case_ids": [], "update": {"notes": "x"}}
    )
    assert resp.status_code == 400
    assert "case_ids cannot be empty" in resp.json()["detail"]


def test_batch_404_when_no_matching_cases(client):
    resp = client.post(
        "/api/cases/batch",
        json={"case_ids": [9990, 9991], "update": {"notes": "x"}},
    )
    assert resp.status_code == 404
    assert "no matching" in resp.json()["detail"]


def test_batch_partial_valid_silently_skips_missing(client, seed_case):
    a = seed_case(abs_path="/tmp/batch-a")
    resp = client.post(
        "/api/cases/batch",
        json={"case_ids": [a, 9999], "update": {"notes": "patched"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] == 1
    assert body["case_ids"] == [a]


def test_batch_records_one_revision_per_updated_case(client, seed_case):
    """Each batched case gets its own revision row with op='batch'."""
    a = seed_case(abs_path="/tmp/batch-rev-a")
    b = seed_case(abs_path="/tmp/batch-rev-b")
    client.post(
        "/api/cases/batch",
        json={"case_ids": [a, b], "update": {"notes": "shared"}},
    )
    revs_a = client.get(f"/api/cases/{a}/revisions").json()["revisions"]
    revs_b = client.get(f"/api/cases/{b}/revisions").json()["revisions"]
    assert any(r["op"] == "batch" for r in revs_a)
    assert any(r["op"] == "batch" for r in revs_b)


def test_batch_clear_fields_nulls_target(client, seed_case):
    """clear_fields takes precedence over a value — clearing manual_category
    sets it to NULL even if the payload also includes a non-empty value.
    """
    case_id = seed_case(abs_path="/tmp/batch-clear")
    # First populate the override
    client.post(
        "/api/cases/batch",
        json={"case_ids": [case_id], "update": {"manual_category": "B"}},
    )
    detail = client.get(f"/api/cases/{case_id}").json()
    assert detail["manual_category"] == "B"

    # Now clear it
    client.post(
        "/api/cases/batch",
        json={
            "case_ids": [case_id],
            "update": {"clear_fields": ["manual_category"]},
        },
    )
    after = client.get(f"/api/cases/{case_id}").json()
    assert after["manual_category"] is None


def test_batch_review_status_reviewed_stamps_reviewed_at(client, seed_case):
    case_id = seed_case(abs_path="/tmp/batch-reviewed")
    client.post(
        "/api/cases/batch",
        json={"case_ids": [case_id], "update": {"review_status": "reviewed"}},
    )
    detail = client.get(f"/api/cases/{case_id}").json()
    assert detail["review_status"] == "reviewed"
    assert detail["reviewed_at"] is not None


def test_batch_review_status_pending_clears_reviewed_at(client, seed_case):
    """Setting review_status back to 'pending'/'needs_recheck' must clear the
    reviewed_at timestamp so the case re-enters the review queue.
    """
    case_id = seed_case(abs_path="/tmp/batch-pending")
    client.post(
        "/api/cases/batch",
        json={"case_ids": [case_id], "update": {"review_status": "reviewed"}},
    )
    assert client.get(f"/api/cases/{case_id}").json()["reviewed_at"] is not None

    client.post(
        "/api/cases/batch",
        json={"case_ids": [case_id], "update": {"review_status": "pending"}},
    )
    after = client.get(f"/api/cases/{case_id}").json()
    assert after["review_status"] == "pending"
    assert after["reviewed_at"] is None


# ----------------------------------------------------------------------
# /files (path-traversal defense)
# ----------------------------------------------------------------------


def test_files_404_for_missing_case(client):
    resp = client.get("/api/cases/9999/files", params={"name": "anything.jpg"})
    assert resp.status_code == 404
    assert "case not found" in resp.json()["detail"]


def test_files_rejects_path_traversal(client, seed_case, tmp_path):
    """`name=../../etc/passwd` must be rejected — the resolve() check enforces
    that the target stays inside the case's abs_path tree.
    """
    case_dir = tmp_path / "case-x"
    case_dir.mkdir()
    case_id = seed_case(abs_path=str(case_dir))
    resp = client.get(
        f"/api/cases/{case_id}/files", params={"name": "../../etc/passwd"}
    )
    assert resp.status_code == 400
    assert "invalid path" in resp.json()["detail"]


def test_files_404_for_missing_file_within_case_dir(client, seed_case, tmp_path):
    case_dir = tmp_path / "case-y"
    case_dir.mkdir()
    case_id = seed_case(abs_path=str(case_dir))
    resp = client.get(
        f"/api/cases/{case_id}/files", params={"name": "missing.jpg"}
    )
    assert resp.status_code == 404
    assert "file not found" in resp.json()["detail"]


def test_files_serves_existing_file(client, seed_case, tmp_path):
    case_dir = tmp_path / "case-z"
    case_dir.mkdir()
    target = case_dir / "hello.txt"
    target.write_text("hi", encoding="utf-8")
    case_id = seed_case(abs_path=str(case_dir))

    resp = client.get(f"/api/cases/{case_id}/files", params={"name": "hello.txt"})
    assert resp.status_code == 200
    assert resp.text == "hi"


# ----------------------------------------------------------------------
# /reveal
# ----------------------------------------------------------------------


def test_reveal_opens_case_root(client, seed_case, tmp_path, monkeypatch):
    from backend.routes import cases

    opened: list[list[str]] = []

    def fake_run(args, check):
        opened.append(args)
        assert check is True

    monkeypatch.setattr(cases.subprocess, "run", fake_run)
    case_dir = tmp_path / "case-reveal-root"
    case_dir.mkdir()
    case_id = seed_case(abs_path=str(case_dir))

    resp = client.post(f"/api/cases/{case_id}/reveal", json={"target": "case_root"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"opened": True, "path": str(case_dir.resolve())}
    assert opened == [["open", str(case_dir.resolve())]]


def test_reveal_opens_render_output(client, seed_case, tmp_path, monkeypatch):
    from backend.routes import cases

    opened: list[list[str]] = []
    monkeypatch.setattr(cases.subprocess, "run", lambda args, check: opened.append(args))
    case_dir = tmp_path / "case-reveal-render"
    out_dir = case_dir / ".case-layout-output" / "fumei" / "tri-compare" / "render"
    out_dir.mkdir(parents=True)
    case_id = seed_case(abs_path=str(case_dir))

    resp = client.post(
        f"/api/cases/{case_id}/reveal",
        json={"target": "render_output", "brand": "fumei", "template": "tri-compare"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == str(out_dir.resolve())
    assert opened == [["open", str(out_dir.resolve())]]


def test_reveal_404_for_missing_render_output(client, seed_case, tmp_path, monkeypatch):
    from backend.routes import cases

    monkeypatch.setattr(cases.subprocess, "run", lambda args, check: None)
    case_dir = tmp_path / "case-reveal-missing-output"
    case_dir.mkdir()
    case_id = seed_case(abs_path=str(case_dir))

    resp = client.post(f"/api/cases/{case_id}/reveal", json={"target": "render_output"})

    assert resp.status_code == 404
    assert "render output directory not found" in resp.json()["detail"]


def test_reveal_rejects_invalid_target_and_path_traversal(client, seed_case, tmp_path, monkeypatch):
    from backend.routes import cases

    monkeypatch.setattr(cases.subprocess, "run", lambda args, check: None)
    case_dir = tmp_path / "case-reveal-invalid"
    case_dir.mkdir()
    case_id = seed_case(abs_path=str(case_dir))

    bad_target = client.post(f"/api/cases/{case_id}/reveal", json={"target": "desktop"})
    assert bad_target.status_code == 422

    traversal = client.post(
        f"/api/cases/{case_id}/reveal",
        json={"target": "render_output", "brand": "../../../outside", "template": "x"},
    )
    assert traversal.status_code == 400
    assert "invalid path" in traversal.json()["detail"]


# ----------------------------------------------------------------------
# /rename-suggestion
# ----------------------------------------------------------------------


def test_rename_suggestion_404_for_missing_case(client):
    resp = client.get("/api/cases/9999/rename-suggestion")
    assert resp.status_code == 404


def test_rename_suggestion_returns_noop_for_already_categorized(client, seed_case):
    """Cases with category != 'non_labeled' return command=None and a note."""
    case_id = seed_case(category="standard_face")
    body = client.get(f"/api/cases/{case_id}/rename-suggestion").json()
    assert body["command"] is None
    assert body["affected_count"] == 0
    assert body["dry_run"] is True


def test_rename_suggestion_returns_command_for_non_labeled(client, seed_case):
    """non_labeled with no labeled tokens in image_files → emits a command
    template + lists the unlabeled files (capped at 20)."""
    from backend import db
    import json

    case_id = seed_case(category="non_labeled")
    # Inject meta_json with image_files (the seed_case fixture doesn't fill it).
    files = [f"img-{i}.jpg" for i in range(25)]
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": files}), case_id),
        )
    body = client.get(f"/api/cases/{case_id}/rename-suggestion").json()
    assert body["command"] is not None
    assert body["affected_count"] == 25
    assert len(body["affected_files"]) == 20  # capped
