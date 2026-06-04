"""P2.2 — DeliveryGate.list_deliverables UNION with simulation_jobs.

After P2.2, `DeliveryGate.list_deliverables()` returns
* Existing real render_job-backed deliverables (unchanged, BC), AND
* simulation_job-backed deliverables IF `SimulationDeliveryGate.evaluate(job)`
  returns `accepted=True`.

If `SimulationDeliveryGate` is not injected (e.g. legacy call sites), the
behaviour collapses to the pre-P2.2 baseline — only render_jobs visible.

Tests use the real `temp_db` fixture + real on-disk manifest/output files.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend import db
from backend.scripts.compute_manifest_hashes import (
    BINDING_NAMES,
    compute_all_bindings,
)
from backend.services.delivery_gate import DeliveryGate
from backend.services.simulation_delivery_gate import SimulationDeliveryGate


# ---------------------------------------------------------------------------
# Helpers (duplicated minimally from test_delivery_gate.py / test_sim_gate)
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_output_file(tmp_path: Path, name: str) -> str:
    fp = tmp_path / name
    fp.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return str(fp)


def _seed_render(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    output_path: str,
    quality_score: float,
    can_publish: int = 1,
) -> int:
    now = _now()
    cur = conn.execute(
        """INSERT INTO render_jobs
           (case_id, brand, template, status, enqueued_at, output_path)
           VALUES (?, 'brand-a', 'tri-compare', 'done', ?, ?)""",
        (case_id, now, output_path),
    )
    job_id = cur.lastrowid
    conn.execute(
        """INSERT INTO render_quality
           (render_job_id, quality_status, quality_score, can_publish,
            artifact_mode, blocking_count, warning_count, metrics_json,
            created_at, updated_at)
           VALUES (?, 'done', ?, ?, 'real_layout', 0, 0, '{}', ?, ?)""",
        (job_id, quality_score, can_publish, now, now),
    )
    conn.commit()
    return job_id


def _seed_simulation(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    output_path: str,
    quality_score: float = 0.92,
    agreement_rate: float = 0.92,
    workflow_name: str = "local_region_enhance_v1",
) -> int:
    now = _now()
    audit = {
        "workflow_name": workflow_name,
        "quality_score": quality_score,
    }
    cur = conn.execute(
        """INSERT INTO simulation_jobs
           (case_id, status, focus_targets_json, policy_json, model_plan_json,
            input_refs_json, output_refs_json, watermarked, audit_json,
            review_status, can_publish, created_at, updated_at)
           VALUES (?, 'done', '[]', '{}', '{}', '[]', ?, 1, ?,
                   'approved', 1, ?, ?)""",
        (
            case_id,
            json.dumps([{"kind": "ai_after_simulation", "path": output_path}]),
            json.dumps(audit, ensure_ascii=False),
            now,
            now,
        ),
    )
    sim_id = cur.lastrowid
    judge = {"agreement_rate": agreement_rate, "hard_veto": False}
    conn.execute(
        """INSERT INTO candidate_lineage
           (simulation_job_id, case_id, provider, model_name, attempt,
            vlm_judge_result_json, created_at)
           VALUES (?, ?, 'comfyui', 'sdxl-stub', 1, ?, ?)""",
        (sim_id, case_id, json.dumps(judge, ensure_ascii=False), now),
    )
    conn.commit()
    return sim_id


def _write_real_manifest(tmp_path: Path, repo_root: Path) -> Path:
    bindings = compute_all_bindings(repo_root)
    manifest = {
        "schema_version": 1,
        "version": "v1.0.0",
        "scope": "production",
        "approver": "owner@example.com",
        "approved_at": _now(),
        "expires_at": None,
        "promotion_state": "p10",
        "bindings": dict(bindings),
        "rollback_baseline": {
            "manifest_ref": None,
            "captured_at": None,
            "bindings": {name: None for name in BINDING_NAMES},
        },
        "notes": "test",
    }
    path = tmp_path / "promotion" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# BC: pure render_jobs path unchanged
# ---------------------------------------------------------------------------


def test_bc_pure_render_jobs_unchanged(temp_db: Path, seed_case, tmp_path: Path) -> None:
    """No simulation gate injected → behave exactly like pre-P2.2."""
    case_id = seed_case(abs_path="/cases/customer-a/c-render")
    out = _make_output_file(tmp_path, "render.jpg")
    with db.connect() as conn:
        _seed_render(conn, case_id=case_id, output_path=out, quality_score=95.0)
        items = DeliveryGate(conn).list_deliverables()
    assert len(items) == 1
    assert items[0].quality_score == 95.0
    assert items[0].case_id == case_id
    assert items[0].source_path == out


def test_bc_render_jobs_dedup_per_case_still_holds(
    temp_db: Path, seed_case, tmp_path: Path
) -> None:
    case_id = seed_case(abs_path="/cases/customer-b/multi")
    out = _make_output_file(tmp_path, "render.jpg")
    with db.connect() as conn:
        _seed_render(conn, case_id=case_id, output_path=out, quality_score=80.0)
        _seed_render(conn, case_id=case_id, output_path=out, quality_score=99.0)
        items = DeliveryGate(conn).list_deliverables()
    assert len(items) == 1
    assert items[0].quality_score == 99.0


# ---------------------------------------------------------------------------
# UNION: simulation accepted → appears in list
# ---------------------------------------------------------------------------


def test_simulation_accepted_appears_in_list(
    temp_db: Path, seed_case, tmp_path: Path, repo_root: Path
) -> None:
    case_id = seed_case(abs_path="/cases/customer-c/sim-win")
    out = _make_output_file(tmp_path, "sim.jpg")
    manifest_path = _write_real_manifest(tmp_path, repo_root)
    with db.connect() as conn:
        _seed_simulation(conn, case_id=case_id, output_path=out)
        sim_gate = SimulationDeliveryGate(
            conn, manifest_path=manifest_path, repo_root=repo_root
        )
        items = DeliveryGate(conn).list_deliverables(simulation_gate=sim_gate)
    assert len(items) == 1, [it.case_id for it in items]
    assert items[0].case_id == case_id
    assert items[0].source_path == out
    # tag simulation-origin so downstream consumers can branch:
    assert items[0].artifact_mode == "ai_after_simulation"


def test_simulation_rejected_does_not_appear(
    temp_db: Path, seed_case, tmp_path: Path, repo_root: Path
) -> None:
    """Sim with low quality_score → rejected → not in list."""
    case_id = seed_case(abs_path="/cases/customer-d/sim-lowq")
    out = _make_output_file(tmp_path, "sim.jpg")
    manifest_path = _write_real_manifest(tmp_path, repo_root)
    with db.connect() as conn:
        _seed_simulation(conn, case_id=case_id, output_path=out, quality_score=0.40)
        sim_gate = SimulationDeliveryGate(
            conn, manifest_path=manifest_path, repo_root=repo_root
        )
        items = DeliveryGate(conn).list_deliverables(simulation_gate=sim_gate)
    assert items == []


def test_mixed_render_and_simulation_both_visible(
    temp_db: Path, seed_case, tmp_path: Path, repo_root: Path
) -> None:
    case_render = seed_case(abs_path="/cases/customer-a/render-only")
    case_sim = seed_case(abs_path="/cases/customer-b/sim-only")
    out_r = _make_output_file(tmp_path, "r.jpg")
    out_s = _make_output_file(tmp_path, "s.jpg")
    manifest_path = _write_real_manifest(tmp_path, repo_root)
    with db.connect() as conn:
        _seed_render(conn, case_id=case_render, output_path=out_r, quality_score=92.0)
        _seed_simulation(conn, case_id=case_sim, output_path=out_s)
        sim_gate = SimulationDeliveryGate(
            conn, manifest_path=manifest_path, repo_root=repo_root
        )
        items = DeliveryGate(conn).list_deliverables(simulation_gate=sim_gate)
    cids = {it.case_id for it in items}
    assert cids == {case_render, case_sim}
    by_case = {it.case_id: it for it in items}
    assert by_case[case_render].artifact_mode == "real_layout"
    assert by_case[case_sim].artifact_mode == "ai_after_simulation"


def test_render_wins_when_both_present_for_same_case(
    temp_db: Path, seed_case, tmp_path: Path, repo_root: Path
) -> None:
    """Render_jobs (real artifact) wins over simulation_jobs for the same case;
    we keep only one DeliverableItem per case (render preferred)."""
    case_id = seed_case(abs_path="/cases/customer-e/both")
    out_r = _make_output_file(tmp_path, "r.jpg")
    out_s = _make_output_file(tmp_path, "s.jpg")
    manifest_path = _write_real_manifest(tmp_path, repo_root)
    with db.connect() as conn:
        _seed_render(conn, case_id=case_id, output_path=out_r, quality_score=95.0)
        _seed_simulation(conn, case_id=case_id, output_path=out_s)
        sim_gate = SimulationDeliveryGate(
            conn, manifest_path=manifest_path, repo_root=repo_root
        )
        items = DeliveryGate(conn).list_deliverables(simulation_gate=sim_gate)
    assert len(items) == 1
    assert items[0].source_path == out_r
    assert items[0].artifact_mode == "real_layout"


def test_simulation_missing_output_file_skipped(
    temp_db: Path, seed_case, tmp_path: Path, repo_root: Path
) -> None:
    case_id = seed_case(abs_path="/cases/customer-f/ghost")
    ghost = str(tmp_path / "ghost.jpg")  # never created
    manifest_path = _write_real_manifest(tmp_path, repo_root)
    with db.connect() as conn:
        _seed_simulation(conn, case_id=case_id, output_path=ghost)
        sim_gate = SimulationDeliveryGate(
            conn, manifest_path=manifest_path, repo_root=repo_root
        )
        items = DeliveryGate(conn).list_deliverables(simulation_gate=sim_gate)
    assert items == []


def test_list_deliverables_signature_keeps_zero_arg_form(
    temp_db: Path, seed_case, tmp_path: Path
) -> None:
    """BC: 0-arg `list_deliverables()` still works (no simulation gate)."""
    case_id = seed_case(abs_path="/cases/customer-x/legacy")
    out = _make_output_file(tmp_path, "r.jpg")
    with db.connect() as conn:
        _seed_render(conn, case_id=case_id, output_path=out, quality_score=88.0)
        items = DeliveryGate(conn).list_deliverables()
    assert len(items) == 1
    assert items[0].case_id == case_id


def test_simulation_highest_quality_wins_when_multiple_sims(
    temp_db: Path, seed_case, tmp_path: Path, repo_root: Path
) -> None:
    """H-3 hardening (nyquist M-3): same case has multiple sim_jobs —
    ORDER BY quality_score DESC must surface the highest-quality candidate,
    not the most-recently-updated one. Pre-hardening bug shipped the
    latest-but-lower-quality (e.g. 0.71 over 0.95).
    """
    case_id = seed_case(abs_path="/cases/customer-q/multi-sim")
    out_high = _make_output_file(tmp_path, "high.jpg")
    out_latest_low = _make_output_file(tmp_path, "low.jpg")
    manifest_path = _write_real_manifest(tmp_path, repo_root)
    with db.connect() as conn:
        # Insert highest-quality FIRST (older updated_at), then lower-quality (newer).
        _seed_simulation(
            conn, case_id=case_id, output_path=out_high, quality_score=0.95
        )
        _seed_simulation(
            conn, case_id=case_id, output_path=out_latest_low, quality_score=0.71
        )
        sim_gate = SimulationDeliveryGate(
            conn, manifest_path=manifest_path, repo_root=repo_root
        )
        items = DeliveryGate(conn).list_deliverables(simulation_gate=sim_gate)
    assert len(items) == 1
    assert items[0].source_path == out_high, (
        f"Expected highest-quality sim ({out_high}) to win; "
        f"got {items[0].source_path} — ORDER BY regression"
    )
