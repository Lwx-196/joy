"""Best-pair REST endpoint coverage."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mk_case(seed_case, tmp_path: Path, *, image_files: list[str] | None = None) -> tuple[int, Path]:
    case_dir = tmp_path / f"case-{id(image_files)}"
    case_dir.mkdir()
    for name in image_files or []:
        target = case_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"real-source")
    case_id = seed_case(abs_path=str(case_dir))
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": image_files or []}, ensure_ascii=False), case_id),
        )
    return int(case_id), case_dir


def _seed_ready(case_id: int, *, candidates: list[dict], fingerprint: str = "fp") -> None:
    with db.connect() as conn:
        now = _now()
        conn.execute(
            """INSERT INTO case_best_pairs
                 (case_id, status, candidates_json, candidates_fingerprint,
                  source_version, scanned_at, updated_at)
               VALUES (?, 'ready', ?, ?, 0, ?, ?)""",
            (case_id, json.dumps(candidates, ensure_ascii=False), fingerprint, now, now),
        )


def test_get_best_pair_pending_when_no_row(client, seed_case, tmp_path: Path) -> None:
    case_id, _ = _mk_case(seed_case, tmp_path)

    resp = client.get(f"/api/cases/{case_id}/best-pair")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["candidates"] == []
    assert body["current_selection"] is None


def test_get_best_pair_missing_case_404(client) -> None:
    resp = client.get("/api/cases/999999/best-pair")

    assert resp.status_code == 404


def test_post_compute_returns_result(client, seed_case, tmp_path: Path) -> None:
    case_id, _ = _mk_case(seed_case, tmp_path)

    with patch(
        "backend.services.best_pair_service.compute_best_pair",
        return_value={"status": "skipped", "skipped_reason": "no_phase_labels", "candidates": []},
    ) as mocked:
        resp = client.post(f"/api/cases/{case_id}/best-pair/compute")

    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"
    mocked.assert_called_once_with(case_id)


def test_post_select_writes_overrides(client, seed_case, tmp_path: Path) -> None:
    case_id, _ = _mk_case(seed_case, tmp_path, image_files=["b.jpg", "a.jpg"])
    _seed_ready(case_id, candidates=[{"before": "b.jpg", "after": "a.jpg", "delta_deg": 1.0}], fingerprint="fp")

    resp = client.post(
        f"/api/cases/{case_id}/best-pair/select",
        json={"before": "b.jpg", "after": "a.jpg", "fingerprint": "fp"},
    )

    assert resp.status_code == 200
    assert resp.json()["selection_id"] > 0
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT filename, manual_phase FROM case_image_overrides WHERE case_id = ? ORDER BY filename",
            (case_id,),
        ).fetchall()
    assert [(row["filename"], row["manual_phase"]) for row in rows] == [("a.jpg", "after"), ("b.jpg", "before")]


def test_post_select_with_view_writes_slot_lock(client, seed_case, tmp_path: Path) -> None:
    case_id, _ = _mk_case(seed_case, tmp_path, image_files=["front-b.jpg", "front-a.jpg"])
    _seed_ready(
        case_id,
        candidates=[{"view": "front", "before": "front-b.jpg", "after": "front-a.jpg", "delta_deg": 0.5}],
        fingerprint="fp-slot",
    )

    resp = client.post(
        f"/api/cases/{case_id}/best-pair/select",
        json={"view": "front", "before": "front-b.jpg", "after": "front-a.jpg", "fingerprint": "fp-slot"},
    )

    assert resp.status_code == 200
    with db.connect() as conn:
        row = conn.execute("SELECT meta_json FROM cases WHERE id = ?", (case_id,)).fetchone()
        selection = conn.execute(
            "SELECT view FROM case_best_pair_selections WHERE id = ?",
            (resp.json()["selection_id"],),
        ).fetchone()
    meta = json.loads(row["meta_json"])
    lock = meta["source_group_selection"]["locked_slots"]["front"]
    assert selection["view"] == "front"
    assert lock["before"] == {"case_id": case_id, "filename": "front-b.jpg"}
    assert lock["after"] == {"case_id": case_id, "filename": "front-a.jpg"}


def test_post_select_stale_fingerprint_409(client, seed_case, tmp_path: Path) -> None:
    case_id, _ = _mk_case(seed_case, tmp_path, image_files=["b.jpg", "a.jpg"])
    _seed_ready(case_id, candidates=[{"before": "b.jpg", "after": "a.jpg", "delta_deg": 1.0}], fingerprint="real")

    resp = client.post(
        f"/api/cases/{case_id}/best-pair/select",
        json={"before": "b.jpg", "after": "a.jpg", "fingerprint": "old"},
    )

    assert resp.status_code == 409


def test_post_render_returns_best_pair_job(client, seed_case, tmp_path: Path, no_job_pool) -> None:
    case_id, _ = _mk_case(seed_case, tmp_path, image_files=["b.jpg", "a.jpg"])
    _seed_ready(case_id, candidates=[{"before": "b.jpg", "after": "a.jpg", "delta_deg": 1.0}], fingerprint="fp-render")
    select_resp = client.post(
        f"/api/cases/{case_id}/best-pair/select",
        json={"before": "b.jpg", "after": "a.jpg", "fingerprint": "fp-render"},
    )
    selection_id = select_resp.json()["selection_id"]

    resp = client.post(f"/api/cases/{case_id}/best-pair/render")

    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    with db.connect() as conn:
        row = conn.execute(
            "SELECT render_mode, best_pair_selection_id, candidates_fingerprint_snapshot FROM render_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert row["render_mode"] == "best-pair"
    assert row["best_pair_selection_id"] == selection_id
    assert row["candidates_fingerprint_snapshot"] == "fp-render"


def test_post_render_without_selection_400(client, seed_case, tmp_path: Path) -> None:
    case_id, _ = _mk_case(seed_case, tmp_path)

    resp = client.post(f"/api/cases/{case_id}/best-pair/render")

    assert resp.status_code == 400
    assert "no_current_selection" in resp.json()["detail"]


def test_batch_compute_submit_and_status(client) -> None:
    with patch("backend.workers.best_pair_compute_queue.COMPUTE_QUEUE.submit_batch", return_value="batch-1") as submit:
        resp = client.post("/api/cases/best-pair/batch-compute", json={"case_ids": [3, 1, 2]})

    assert resp.status_code == 202
    assert resp.json() == {"batch_id": "batch-1", "queued": 3}
    submit.assert_called_once_with([3, 1, 2])

    with patch(
        "backend.workers.best_pair_compute_queue.COMPUTE_QUEUE.status",
        return_value={"batch_id": "batch-1", "total": 3, "done": 1, "failed": 0, "errors": [], "status": "running"},
    ):
        status_resp = client.get("/api/cases/best-pair/batch-compute/batch-1")

    assert status_resp.status_code == 200
    assert status_resp.json()["done"] == 1


def test_batch_compute_status_404(client) -> None:
    with patch("backend.workers.best_pair_compute_queue.COMPUTE_QUEUE.status", return_value=None):
        resp = client.get("/api/cases/best-pair/batch-compute/nope")

    assert resp.status_code == 404
