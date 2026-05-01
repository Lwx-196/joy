"""Stage B: case_image_overrides table + PATCH endpoint + render injection.

Coverage:
- Schema bootstrap: case_image_overrides table exists after init_schema
- PATCH happy paths: phase only / view only / both
- PATCH validation: bad enum → 400 / non-basename → 400 / no fields → 400
- Clear semantics: empty string clears one dim; both cleared deletes row
- Override merge in GET /api/cases/{id}: manual phase wins over skill phase
- Render injection: render_queue feeds overrides into render_executor.run_render
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest


def _override_count(client) -> int:
    from backend import db

    with db.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM case_image_overrides").fetchone()
    return int(row["n"])


def test_schema_creates_overrides_table(temp_db):
    from backend import db

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='case_image_overrides'"
        ).fetchall()
    assert len(rows) == 1


def test_patch_phase_only_creates_row(client, seed_case):
    case_id = seed_case()
    resp = client.patch(
        f"/api/cases/{case_id}/images/术前1.jpeg",
        json={"manual_phase": "before"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["case_id"] == case_id
    assert data["filename"] == "术前1.jpeg"
    assert data["manual_phase"] == "before"
    assert data["manual_view"] is None
    assert _override_count(client) == 1


def test_patch_view_only_creates_row(client, seed_case):
    case_id = seed_case()
    resp = client.patch(
        f"/api/cases/{case_id}/images/术后1.jpeg",
        json={"manual_view": "front"},
    )
    assert resp.status_code == 200
    assert resp.json()["manual_view"] == "front"


def test_patch_both_fields(client, seed_case):
    case_id = seed_case()
    resp = client.patch(
        f"/api/cases/{case_id}/images/sample.jpg",
        json={"manual_phase": "after", "manual_view": "side"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["manual_phase"] == "after"
    assert body["manual_view"] == "side"


def test_patch_subsequent_partial_update_preserves_other(client, seed_case):
    case_id = seed_case()
    client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_phase": "before", "manual_view": "front"},
    )
    # Update only view, phase should remain
    resp = client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_view": "oblique"},
    )
    body = resp.json()
    assert body["manual_phase"] == "before"
    assert body["manual_view"] == "oblique"


def test_patch_clear_one_dim_keeps_row(client, seed_case):
    case_id = seed_case()
    client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_phase": "before", "manual_view": "front"},
    )
    resp = client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_phase": ""},  # clear phase
    )
    body = resp.json()
    assert body["manual_phase"] is None
    assert body["manual_view"] == "front"
    assert _override_count(client) == 1


def test_patch_clear_both_deletes_row(client, seed_case):
    case_id = seed_case()
    client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_phase": "before", "manual_view": "front"},
    )
    resp = client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_phase": "", "manual_view": ""},
    )
    body = resp.json()
    assert body["manual_phase"] is None
    assert body["manual_view"] is None
    assert _override_count(client) == 0


def test_patch_invalid_phase_returns_400(client, seed_case):
    case_id = seed_case()
    resp = client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_phase": "operative"},  # not in allowed set
    )
    assert resp.status_code == 400
    assert "manual_phase" in resp.json()["detail"]


def test_patch_invalid_view_returns_400(client, seed_case):
    case_id = seed_case()
    resp = client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_view": "back"},
    )
    assert resp.status_code == 400


def test_patch_path_traversal_returns_400(client, seed_case):
    """Inputs that survive httpx URL normalization and reach our handler must
    be rejected with 400. Note: httpx auto-resolves bare '..' / '.' segments
    so those cases get caught at the routing layer, not in our handler — for
    those we accept any 4xx as proof of refusal."""
    case_id = seed_case()
    # encoded / and \ → route layer returns 404; either way we never write a row
    for bad in ["subdir%2Fx.jpg", r"foo%5Cbar.jpg"]:
        resp = client.patch(
            f"/api/cases/{case_id}/images/{bad}",
            json={"manual_phase": "before"},
        )
        assert resp.status_code in (400, 404), f"expected reject for {bad!r}, got {resp.status_code}"
    assert _override_count(client) == 0


def test_patch_no_fields_returns_400(client, seed_case):
    case_id = seed_case()
    resp = client.patch(f"/api/cases/{case_id}/images/x.jpg", json={})
    assert resp.status_code == 400
    assert "no fields" in resp.json()["detail"]


def test_patch_unknown_case_returns_404(client):
    resp = client.patch(
        "/api/cases/9999/images/x.jpg", json={"manual_phase": "before"}
    )
    assert resp.status_code == 404


def test_get_case_merges_override_phase_over_skill(client, seed_case):
    """When skill_image_metadata_json says phase=None, manual override 'before'
    should appear in skill_image_metadata for that filename, with
    phase_override_source='manual'."""
    from backend import db

    case_id = seed_case()
    skill_meta = [
        {
            "filename": "术中1.jpeg",
            "phase": None,  # skill failed to label
            "view_bucket": "front",
            "angle": "front",
        }
    ]
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET skill_image_metadata_json = ? WHERE id = ?",
            (json.dumps(skill_meta, ensure_ascii=False), case_id),
        )
    # Apply manual override
    client.patch(
        f"/api/cases/{case_id}/images/术中1.jpeg",
        json={"manual_phase": "after"},
    )
    resp = client.get(f"/api/cases/{case_id}")
    assert resp.status_code == 200
    items = resp.json()["skill_image_metadata"]
    assert len(items) == 1
    assert items[0]["phase"] == "after"
    assert items[0]["phase_override_source"] == "manual"
    assert items[0]["view_override_source"] is None  # view not overridden


def test_get_case_view_override_replaces_bucket_and_angle(client, seed_case):
    from backend import db

    case_id = seed_case()
    skill_meta = [
        {
            "filename": "x.jpg",
            "phase": "before",
            "view_bucket": "side",
            "angle": "side",
        }
    ]
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET skill_image_metadata_json = ? WHERE id = ?",
            (json.dumps(skill_meta, ensure_ascii=False), case_id),
        )
    client.patch(
        f"/api/cases/{case_id}/images/x.jpg",
        json={"manual_view": "front"},
    )
    items = client.get(f"/api/cases/{case_id}").json()["skill_image_metadata"]
    assert items[0]["view_bucket"] == "front"
    assert items[0]["angle"] == "front"
    assert items[0]["view_override_source"] == "manual"
    assert items[0]["phase_override_source"] is None


def test_render_queue_pulls_overrides_into_run_render(client, seed_case, monkeypatch):
    """Stage B: render_queue._execute_render reads case_image_overrides and
    passes them as kwarg to render_executor.run_render."""
    from backend import db, render_executor, render_queue

    case_id = seed_case(abs_path="/tmp/case-render-ov")
    # Create one override
    client.patch(
        f"/api/cases/{case_id}/images/term.jpg",
        json={"manual_phase": "before", "manual_view": "front"},
    )
    # Insert a queued render job for this case
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, ?, ?, 'queued', ?, 'off')""",
            (case_id, "fumei", "tri-compare", datetime.now(timezone.utc).isoformat()),
        )
        job_id = cur.lastrowid

    captured: dict[str, object] = {}

    def fake_run(case_dir, brand, template, semantic_judge, manual_overrides=None, **kw):
        captured["case_dir"] = case_dir
        captured["manual_overrides"] = manual_overrides
        return {
            "output_path": "/tmp/out.jpg",
            "manifest_path": "/tmp/m.json",
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "case_mode": "tri-compare",
            "effective_templates": [],
            "manual_overrides_applied": list((manual_overrides or {}).keys()),
        }

    monkeypatch.setattr(render_executor, "run_render", fake_run)

    queue = render_queue.RenderQueue()
    queue._execute_render(job_id)

    assert captured["manual_overrides"] == {
        "term.jpg": {"phase": "before", "view": "front"},
    }
    # Verify the job moved to done
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "done"


def test_render_queue_no_overrides_passes_empty_dict(client, seed_case, monkeypatch):
    from backend import db, render_executor, render_queue

    case_id = seed_case(abs_path="/tmp/case-render-no-ov")
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, ?, ?, 'queued', ?, 'off')""",
            (case_id, "fumei", "tri-compare", datetime.now(timezone.utc).isoformat()),
        )
        job_id = cur.lastrowid

    captured: dict[str, object] = {}

    def fake_run(case_dir, brand, template, semantic_judge, manual_overrides=None, **kw):
        captured["manual_overrides"] = manual_overrides
        return {
            "output_path": "/tmp/out.jpg",
            "manifest_path": "/tmp/m.json",
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "case_mode": "tri-compare",
            "effective_templates": [],
            "manual_overrides_applied": [],
        }

    monkeypatch.setattr(render_executor, "run_render", fake_run)
    render_queue.RenderQueue()._execute_render(job_id)
    assert captured["manual_overrides"] == {}
