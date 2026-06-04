"""P2.2 — SimulationDeliveryGate boundary tests.

`SimulationDeliveryGate.evaluate(simulation_job_id)` reads:

* `simulation_jobs.audit_json.quality_score` (or the canonical metric field)
* `candidate_lineage.vlm_judge_result_json.agreement_rate` (latest row per job)
* `case-workbench-ai/promotion/manifest.json` (real file; hash bindings
  produced by `backend.scripts.compute_manifest_hashes`)
* `simulation_jobs.audit_json.workflow_name` checked against `manifest.scope`

Any missing / expired / mismatch → `accepted=False, rolled_back=True`.
Manifest unreadable → fail-closed `accepted=False, reasons=['manifest_missing']`.

Tests use the real `temp_db` fixture (no DB mocks) + real on-disk manifest
JSON (no manifest mocks) per CLAUDE.md red-line rule.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from backend import db
from backend.scripts.compute_manifest_hashes import (
    BINDING_NAMES,
    compute_all_bindings,
)
from backend.services.simulation_delivery_gate import (
    DEFAULT_SIMULATION_QUALITY_THRESHOLD,
    DEFAULT_VLM_AGREEMENT_THRESHOLD,
    GateDecision,
    SimulationDeliveryGate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_manifest(
    path: Path,
    *,
    bindings: dict[str, str],
    scope: str = "production",
    promotion_state: str = "p10",
    approver: str = "owner@example.com",
    approved_at: str | None = None,
) -> Path:
    """Write a real manifest.json file to `path` with the given bindings."""
    if approved_at is None:
        approved_at = _now()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "version": "v1.0.0",
        "scope": scope,
        "approver": approver,
        "approved_at": approved_at,
        "expires_at": None,
        "promotion_state": promotion_state,
        "bindings": dict(bindings),
        "rollback_baseline": {
            "manifest_ref": None,
            "captured_at": None,
            "bindings": {name: None for name in BINDING_NAMES},
        },
        "notes": "test manifest",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _seed_simulation_job(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    quality_score: float | None = 0.85,
    workflow_name: str = "local_region_enhance_v1",
    status: str = "done",
    audit_extra: dict[str, Any] | None = None,
) -> int:
    """Insert a simulation_jobs row with audit_json.quality_score + workflow_name."""
    now = _now()
    audit: dict[str, Any] = {
        "workflow_name": workflow_name,
        "provider": "comfyui",
        "policy": {"candidate_only": True},
    }
    if quality_score is not None:
        audit["quality_score"] = quality_score
    if audit_extra:
        audit.update(audit_extra)
    cur = conn.execute(
        """INSERT INTO simulation_jobs
           (case_id, status, focus_targets_json, policy_json, model_plan_json,
            input_refs_json, output_refs_json, watermarked, audit_json,
            review_status, can_publish, created_at, updated_at)
           VALUES (?, ?, '[]', '{}', '{}', '[]', '[]', 1, ?, 'approved', 1, ?, ?)""",
        (case_id, status, json.dumps(audit, ensure_ascii=False), now, now),
    )
    job_id = cur.lastrowid
    conn.commit()
    return job_id


def _seed_lineage(
    conn: sqlite3.Connection,
    *,
    simulation_job_id: int,
    case_id: int,
    agreement_rate: float | None = 0.85,
    attempt: int = 1,
) -> int:
    """Insert a candidate_lineage row carrying the VLM judge agreement."""
    now = _now()
    judge: dict[str, Any] = {"hard_veto": False}
    if agreement_rate is not None:
        judge["agreement_rate"] = agreement_rate
    cur = conn.execute(
        """INSERT INTO candidate_lineage
           (simulation_job_id, case_id, provider, model_name, attempt,
            vlm_judge_result_json, created_at)
           VALUES (?, ?, 'comfyui', 'sdxl-stub', ?, ?, ?)""",
        (simulation_job_id, case_id, attempt, json.dumps(judge, ensure_ascii=False), now),
    )
    lineage_id = cur.lastrowid
    conn.commit()
    return lineage_id


def _real_bindings(repo_root: Path) -> dict[str, str]:
    """Compute real hash bindings against this repo's actual sources."""
    return compute_all_bindings(repo_root)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_root() -> Path:
    # backend/tests/<file>.py -> parents[2] is the worktree root.
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def manifest_with_real_hashes(tmp_path: Path, repo_root: Path) -> Path:
    """A manifest whose bindings match the *current* repo sources."""
    bindings = _real_bindings(repo_root)
    manifest_path = tmp_path / "promotion" / "manifest.json"
    return _write_manifest(manifest_path, bindings=bindings)


# ---------------------------------------------------------------------------
# Boundary tests (6 required)
# ---------------------------------------------------------------------------


def test_accept_when_all_checks_pass(
    temp_db: Path,
    seed_case,
    repo_root: Path,
    manifest_with_real_hashes: Path,
) -> None:
    """All four checks pass → accepted=True, rolled_back=False."""
    case_id = seed_case(abs_path="/cases/customer/win")
    with db.connect() as conn:
        job_id = _seed_simulation_job(
            conn, case_id=case_id, quality_score=0.92, workflow_name="local_region_enhance_v1"
        )
        _seed_lineage(conn, simulation_job_id=job_id, case_id=case_id, agreement_rate=0.92)
        gate = SimulationDeliveryGate(
            conn,
            manifest_path=manifest_with_real_hashes,
            repo_root=repo_root,
        )
        decision = gate.evaluate(job_id)
    assert isinstance(decision, GateDecision)
    assert decision.accepted is True, decision.reasons
    assert decision.rolled_back is False
    assert decision.manifest_state in {"p10", "p25", "p50", "p100", "shadow"}
    assert decision.reasons == []


def test_reject_when_manifest_missing(
    temp_db: Path,
    seed_case,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    """Manifest file does not exist → fail-closed reject."""
    case_id = seed_case(abs_path="/cases/customer/no-manifest")
    with db.connect() as conn:
        job_id = _seed_simulation_job(conn, case_id=case_id, quality_score=0.95)
        _seed_lineage(conn, simulation_job_id=job_id, case_id=case_id, agreement_rate=0.95)
        gate = SimulationDeliveryGate(
            conn,
            manifest_path=tmp_path / "does-not-exist.json",
            repo_root=repo_root,
        )
        decision = gate.evaluate(job_id)
    assert decision.accepted is False
    assert "manifest_missing" in decision.reasons
    # fail-closed: rolled_back semantic = "we refused to advance, treat as
    # not-deliverable"; we use rolled_back=True to signal explicit halt.
    assert decision.rolled_back is True
    assert decision.manifest_state == "missing"


def test_reject_when_manifest_hash_mismatch(
    temp_db: Path,
    seed_case,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    """Manifest hash binding differs from current source hash → reject + rolled_back."""
    case_id = seed_case(abs_path="/cases/customer/hashdrift")
    # Build a manifest with a deliberately-wrong vlm_calibration_hash:
    bindings = _real_bindings(repo_root)
    bindings["vlm_calibration_hash"] = "sha256:" + ("0" * 64)
    manifest_path = _write_manifest(tmp_path / "promotion" / "manifest.json", bindings=bindings)

    with db.connect() as conn:
        job_id = _seed_simulation_job(conn, case_id=case_id, quality_score=0.95)
        _seed_lineage(conn, simulation_job_id=job_id, case_id=case_id, agreement_rate=0.95)
        gate = SimulationDeliveryGate(
            conn, manifest_path=manifest_path, repo_root=repo_root
        )
        decision = gate.evaluate(job_id)
    assert decision.accepted is False
    assert any("hash_mismatch" in r for r in decision.reasons), decision.reasons
    assert decision.rolled_back is True
    # Evidence should pinpoint which binding drifted:
    assert "vlm_calibration_hash" in json.dumps(decision.evidence)


def test_reject_when_workflow_out_of_scope(
    temp_db: Path,
    seed_case,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    """Job workflow not in manifest.scope (e.g. staging scope, prod workflow)."""
    case_id = seed_case(abs_path="/cases/customer/scopebad")
    # Real hashes but scope=staging:
    bindings = _real_bindings(repo_root)
    manifest_path = _write_manifest(
        tmp_path / "promotion" / "manifest.json",
        bindings=bindings,
        scope="staging",
    )
    with db.connect() as conn:
        # Default scope check: production-only workflow + staging manifest → reject
        job_id = _seed_simulation_job(
            conn, case_id=case_id, quality_score=0.95,
            workflow_name="local_region_enhance_v1",
        )
        _seed_lineage(conn, simulation_job_id=job_id, case_id=case_id, agreement_rate=0.95)
        gate = SimulationDeliveryGate(
            conn,
            manifest_path=manifest_path,
            repo_root=repo_root,
            allowed_scopes={"production"},  # explicit scope filter
        )
        decision = gate.evaluate(job_id)
    assert decision.accepted is False
    assert any("scope" in r for r in decision.reasons), decision.reasons
    assert decision.rolled_back is True


def test_reject_when_simulation_quality_below_threshold(
    temp_db: Path,
    seed_case,
    repo_root: Path,
    manifest_with_real_hashes: Path,
) -> None:
    """audit_json.quality_score < threshold → reject."""
    case_id = seed_case(abs_path="/cases/customer/lowqual")
    with db.connect() as conn:
        # Below default 0.7 threshold:
        job_id = _seed_simulation_job(conn, case_id=case_id, quality_score=0.55)
        _seed_lineage(conn, simulation_job_id=job_id, case_id=case_id, agreement_rate=0.95)
        gate = SimulationDeliveryGate(
            conn,
            manifest_path=manifest_with_real_hashes,
            repo_root=repo_root,
            simulation_quality_threshold=DEFAULT_SIMULATION_QUALITY_THRESHOLD,
        )
        decision = gate.evaluate(job_id)
    assert decision.accepted is False
    assert any("simulation_quality" in r for r in decision.reasons), decision.reasons
    assert decision.rolled_back is True


def test_reject_when_vlm_agreement_below_threshold(
    temp_db: Path,
    seed_case,
    repo_root: Path,
    manifest_with_real_hashes: Path,
) -> None:
    """latest candidate_lineage.vlm_judge_result_json.agreement_rate < threshold."""
    case_id = seed_case(abs_path="/cases/customer/badjudge")
    with db.connect() as conn:
        job_id = _seed_simulation_job(conn, case_id=case_id, quality_score=0.95)
        _seed_lineage(conn, simulation_job_id=job_id, case_id=case_id, agreement_rate=0.40)
        gate = SimulationDeliveryGate(
            conn,
            manifest_path=manifest_with_real_hashes,
            repo_root=repo_root,
            vlm_agreement_threshold=DEFAULT_VLM_AGREEMENT_THRESHOLD,
        )
        decision = gate.evaluate(job_id)
    assert decision.accepted is False
    assert any("vlm_agreement" in r for r in decision.reasons), decision.reasons
    assert decision.rolled_back is True


# ---------------------------------------------------------------------------
# Extra coverage
# ---------------------------------------------------------------------------


def test_missing_simulation_job_returns_reject(
    temp_db: Path,
    repo_root: Path,
    manifest_with_real_hashes: Path,
) -> None:
    """Unknown job_id → graceful reject (not crash)."""
    with db.connect() as conn:
        gate = SimulationDeliveryGate(
            conn, manifest_path=manifest_with_real_hashes, repo_root=repo_root
        )
        decision = gate.evaluate(999_999)
    assert decision.accepted is False
    assert any("simulation_job_not_found" in r for r in decision.reasons)
    # Job-not-found is also rolled_back: there's nothing to deliver.
    assert decision.rolled_back is True


def test_missing_vlm_judge_lineage_is_treated_as_missing_evidence(
    temp_db: Path,
    seed_case,
    repo_root: Path,
    manifest_with_real_hashes: Path,
) -> None:
    """No candidate_lineage row → no VLM evidence → reject (fail-closed)."""
    case_id = seed_case(abs_path="/cases/customer/nojudge")
    with db.connect() as conn:
        job_id = _seed_simulation_job(conn, case_id=case_id, quality_score=0.95)
        gate = SimulationDeliveryGate(
            conn, manifest_path=manifest_with_real_hashes, repo_root=repo_root
        )
        decision = gate.evaluate(job_id)
    assert decision.accepted is False
    assert any("vlm_agreement_missing" in r for r in decision.reasons), decision.reasons
    assert decision.rolled_back is True
