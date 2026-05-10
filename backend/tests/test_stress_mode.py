from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_db_path_can_be_configured_by_env(tmp_path: Path):
    target = tmp_path / "stress.db"
    code = "from backend import db; print(db.DB_PATH)"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "CASE_WORKBENCH_DB_PATH": str(target)},
        text=True,
        capture_output=True,
        check=True,
    )
    assert proc.stdout.strip() == str(target.resolve())


def test_stress_status_reports_isolated_paths(client, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CASE_WORKBENCH_STRESS_MODE", "1")
    monkeypatch.setenv("CASE_WORKBENCH_STRESS_RUN_ID", "test-run")
    monkeypatch.setenv("CASE_WORKBENCH_OUTPUT_ROOT", str(tmp_path / "out"))
    from backend import db

    resp = client.get("/api/stress/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stress_mode"] is True
    assert body["stress_run_id"] == "test-run"
    assert body["db_path"] == str(db.DB_PATH)
    assert body["output_root"] == str((tmp_path / "out").resolve())
    assert body["cases"]["total"] == 0


def test_stress_mode_blocks_destructive_routes(client, monkeypatch):
    monkeypatch.setenv("CASE_WORKBENCH_STRESS_MODE", "1")

    assert client.post("/api/cases/1/render/undo").status_code == 403
    assert client.post(
        "/api/cases/1/render/restore",
        json={"brand": "fumei", "template": "tri-compare", "archived_at": "20250101T000000Z"},
    ).status_code == 403
    assert client.post("/api/cases/trash", json={"case_ids": [1]}).status_code == 403
    assert client.post("/api/cases/1/images/trash", json={"filename": "x.jpg"}).status_code == 403
    assert client.post("/api/cases/1/images/restore", json={"trash_path": "x.jpg"}).status_code == 403


def test_audit_revision_is_tagged_in_stress_mode(client, seed_case, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CASE_WORKBENCH_STRESS_MODE", "1")
    monkeypatch.setenv("CASE_WORKBENCH_STRESS_RUN_ID", "audit-run")
    monkeypatch.setenv("CASE_WORKBENCH_OUTPUT_ROOT", str(tmp_path / "out"))
    case_id = seed_case(abs_path=str(tmp_path / "case"))
    resp = client.patch(f"/api/cases/{case_id}", json={"notes": "stress note"})
    assert resp.status_code == 200

    from backend import db

    with db.connect() as conn:
        row = conn.execute(
            "SELECT after_json FROM case_revisions WHERE case_id = ? ORDER BY id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
    after = json.loads(row["after_json"])
    assert after["_stress"]["enabled"] is True
    assert after["_stress"]["run_id"] == "audit-run"


def test_render_runner_inline_script_compiles():
    from backend import render_executor

    compile(render_executor._build_render_runner(), "<render-runner>", "exec")
