"""Boundary tests for ``backend.services.promotion_rollback_applier``.

Real data discipline (per ~/.claude/CLAUDE.md):
- Real SQLite via the conftest ``temp_db`` fixture (no mocking).
- Real tmp manifest files on disk (``tmp_path`` fixture).
- ``os.replace`` is observed (not stubbed) via filesystem state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from backend import db
from backend.services.promotion_rollback_applier import (
    REASON_ALREADY_ROLLED_BACK,
    REASON_APPLIED,
    REASON_DRY_RUN,
    REASON_INSUFFICIENT_DATA,
    REASON_MISSING_BASELINE_BINDINGS,
    REASON_MONITORING_PAUSED,
    REASON_NO_MANIFEST,
    REASON_NO_ROLLBACK_NEEDED,
    REC_CONTINUE,
    REC_INSUFFICIENT,
    REC_MONITORING_PAUSED,
    REC_ROLLBACK,
    ROLLED_BACK_STATE,
    REVIEWER,
    apply_rollback_decision,
)

# ---------------------------------------------------------------------------
# Manifest / decision fixture builders
# ---------------------------------------------------------------------------


_VALID_BINDINGS = {
    "vlm_calibration_hash": "sha256:aaaa",
    "production_gate_hash": "sha256:bbbb",
    "ab_report_hash": "sha256:cccc",
    "render_quality_baseline_hash": "sha256:dddd",
}


def _make_manifest(
    *,
    promotion_state: str = "p50",
    bindings: dict[str, str] | None = None,
    rollback_baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "schema_version": 1,
        "version": "v1.0.0",
        "scope": "production",
        "approver": "alice@example.com",
        "approved_at": "2026-05-01T00:00:00+00:00",
        "expires_at": None,
        "promotion_state": promotion_state,
        "bindings": dict(_VALID_BINDINGS) if bindings is None else dict(bindings),
    }
    if rollback_baseline is not None:
        out["rollback_baseline"] = rollback_baseline
    return out


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _make_decision(
    *,
    recommendation: str,
    violations: list[dict[str, Any]] | None = None,
    sample_size: int = 100,
    window_hours: int = 48,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "recommendation": recommendation,
        "within_slo": recommendation == REC_CONTINUE,
        "violations": list(violations or []),
        "evidence": evidence or {"comfyui_failure": {"rate": 0.05, "terminal_total": 80}},
        "window_hours": window_hours,
        "sample_size": sample_size,
        "generated_at": "2026-05-28T00:00:00+00:00",
    }


def _rollback_violation() -> list[dict[str, Any]]:
    return [
        {
            "dimension": "comfyui_failure_rate",
            "actual": 0.20,
            "threshold": 0.05,
            "comparator": "<=",
        },
        {
            "dimension": "vlm_disagreement_rate",
            "actual": 0.18,
            "threshold": 0.10,
            "comparator": "<=",
        },
    ]


# ---------------------------------------------------------------------------
# 1) rec=rollback + apply → manifest rewritten + audit log written
# ---------------------------------------------------------------------------


def test_rollback_apply_writes_manifest_and_audit(
    temp_db: Path, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _make_manifest(promotion_state="p25"))
    decision = _make_decision(
        recommendation=REC_ROLLBACK, violations=_rollback_violation()
    )

    with db.connect() as conn:
        result = apply_rollback_decision(
            decision,
            dry_run=False,
            manifest_path=manifest_path,
            conn=conn,
        )

    assert result["applied"] is True
    assert result["reason"] == REASON_APPLIED
    assert result["dry_run"] is False
    assert result["audit_log_id"] >= 1

    # Manifest on disk should now be rolled_back.
    new_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert new_payload["promotion_state"] == ROLLED_BACK_STATE
    # Active bindings snapshotted into rollback_baseline.bindings.
    assert new_payload["rollback_baseline"]["bindings"] == _VALID_BINDINGS
    assert new_payload["rollback_baseline"]["rolled_back_at"]
    assert new_payload["rollback_baseline"]["rolled_back_from_state"] == "p25"

    # Audit log row exists w/ correct reviewer + reason summary.
    with db.connect() as conn:
        row = conn.execute(
            "SELECT reviewer, endpoint, reason, outcome, http_status, payload_json "
            "FROM ops_audit_log WHERE id = ?",
            (result["audit_log_id"],),
        ).fetchone()
    assert row is not None
    assert row["reviewer"] == REVIEWER
    assert row["endpoint"] == "promotion_rollback_applier.apply"
    assert "comfyui_failure_rate" in row["reason"]
    assert row["outcome"] == "ok"
    assert row["http_status"] == 200
    payload = json.loads(row["payload_json"])
    assert payload["decision_recommendation"] == REC_ROLLBACK
    assert payload["from_state"] == "p25"


# ---------------------------------------------------------------------------
# 2) rec=continue → no file touch
# ---------------------------------------------------------------------------


def test_continue_does_not_touch_manifest(
    temp_db: Path, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "manifest.json"
    original = _make_manifest(promotion_state="p50")
    _write_manifest(manifest_path, original)
    before_text = manifest_path.read_text(encoding="utf-8")

    result = apply_rollback_decision(
        _make_decision(recommendation=REC_CONTINUE),
        dry_run=False,
        manifest_path=manifest_path,
    )

    assert result["applied"] is False
    assert result["reason"] == REASON_NO_ROLLBACK_NEEDED
    assert manifest_path.read_text(encoding="utf-8") == before_text

    # No audit row should have been written.
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM ops_audit_log").fetchone()["n"]
    assert n == 0


# ---------------------------------------------------------------------------
# 3) rec=insufficient_data → no file touch + warning
# ---------------------------------------------------------------------------


def test_insufficient_data_keeps_warning_and_no_touch(
    temp_db: Path, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _make_manifest())
    before_text = manifest_path.read_text(encoding="utf-8")

    result = apply_rollback_decision(
        _make_decision(recommendation=REC_INSUFFICIENT, sample_size=3),
        dry_run=False,
        manifest_path=manifest_path,
    )

    assert result["applied"] is False
    assert result["reason"] == REASON_INSUFFICIENT_DATA
    assert "warning" in result and result["warning"]
    assert manifest_path.read_text(encoding="utf-8") == before_text


# ---------------------------------------------------------------------------
# 4) already rolled_back → not re-applied
# ---------------------------------------------------------------------------


def test_already_rolled_back_is_idempotent(
    temp_db: Path, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(
        manifest_path,
        _make_manifest(
            promotion_state=ROLLED_BACK_STATE,
            rollback_baseline={
                "manifest_ref": "prior-sha",
                "captured_at": "2026-05-20T00:00:00+00:00",
                "bindings": _VALID_BINDINGS,
                "rolled_back_at": "2026-05-20T00:00:00+00:00",
                "rolled_back_from_state": "p100",
            },
        ),
    )
    before_text = manifest_path.read_text(encoding="utf-8")

    result = apply_rollback_decision(
        _make_decision(recommendation=REC_ROLLBACK, violations=_rollback_violation()),
        dry_run=False,
        manifest_path=manifest_path,
    )

    assert result["applied"] is False
    assert result["reason"] == REASON_ALREADY_ROLLED_BACK
    # Filesystem byte-identical to before.
    assert manifest_path.read_text(encoding="utf-8") == before_text


# ---------------------------------------------------------------------------
# 5) atomic write — write goes through tmp + os.replace; failed write
#    leaves the original file intact
# ---------------------------------------------------------------------------


def test_atomic_write_via_tmp_replace(
    temp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "manifest.json"
    original = _make_manifest(promotion_state="p10")
    _write_manifest(manifest_path, original)
    original_bytes = manifest_path.read_bytes()

    # Spy on os.replace to confirm the applier uses the atomic primitive.
    seen: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy_replace(src: Any, dst: Any) -> None:  # type: ignore[override]
        seen.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(
        "backend.services.promotion_rollback_applier.os.replace", spy_replace
    )

    result = apply_rollback_decision(
        _make_decision(recommendation=REC_ROLLBACK, violations=_rollback_violation()),
        dry_run=False,
        manifest_path=manifest_path,
    )
    assert result["applied"] is True
    # Exactly one os.replace call: tmp → real manifest path.
    assert len(seen) == 1
    src, dst = seen[0]
    assert dst == str(manifest_path)
    assert src != str(manifest_path)
    assert src.endswith(".tmp")
    # Tmp file has been moved (no leftover).
    assert not Path(src).exists()

    # Now simulate a write failure on a fresh manifest and confirm the
    # original is untouched.
    other_path = tmp_path / "manifest2.json"
    _write_manifest(other_path, _make_manifest(promotion_state="p10"))
    untouched_bytes = other_path.read_bytes()

    def failing_replace(src: Any, dst: Any) -> None:
        raise OSError("simulated EROFS")

    monkeypatch.setattr(
        "backend.services.promotion_rollback_applier.os.replace", failing_replace
    )

    with pytest.raises(OSError):
        apply_rollback_decision(
            _make_decision(recommendation=REC_ROLLBACK, violations=_rollback_violation()),
            dry_run=False,
            manifest_path=other_path,
        )
    # File on disk is byte-identical to pre-apply (atomic guarantee).
    assert other_path.read_bytes() == untouched_bytes
    # No stray tmp leaked into the dir.
    leftover_tmps = list(tmp_path.glob(".manifest2.json.tmp"))
    assert leftover_tmps == []

    # And the FIRST (successful) write produced different bytes than original.
    assert manifest_path.read_bytes() != original_bytes


# ---------------------------------------------------------------------------
# 6) dry_run=True → no file write, no audit row
# ---------------------------------------------------------------------------


def test_dry_run_no_side_effects(
    temp_db: Path, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _make_manifest(promotion_state="p25"))
    before_bytes = manifest_path.read_bytes()

    result = apply_rollback_decision(
        _make_decision(recommendation=REC_ROLLBACK, violations=_rollback_violation()),
        dry_run=True,
        manifest_path=manifest_path,
    )

    assert result["applied"] is False
    assert result["reason"] == REASON_DRY_RUN
    assert result["dry_run"] is True
    assert result["would_apply"] is True
    assert result["plan"]["from_state"] == "p25"
    assert result["plan"]["to_state"] == ROLLED_BACK_STATE
    # Bytes on disk unchanged.
    assert manifest_path.read_bytes() == before_bytes
    # No audit row written.
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM ops_audit_log").fetchone()["n"]
    assert n == 0


# ---------------------------------------------------------------------------
# 7) ops_audit_log row has reviewer=system/slo_monitor + JSON payloads
# ---------------------------------------------------------------------------


def test_audit_log_shape(temp_db: Path, tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _make_manifest(promotion_state="p100"))
    violations = _rollback_violation()

    with db.connect() as conn:
        result = apply_rollback_decision(
            _make_decision(recommendation=REC_ROLLBACK, violations=violations),
            dry_run=False,
            manifest_path=manifest_path,
            conn=conn,
            request_id="test-req-001",
        )

    assert result["applied"] is True

    with db.connect() as conn:
        row = conn.execute(
            "SELECT request_id, reviewer, endpoint, payload_json, response_json "
            "FROM ops_audit_log WHERE id = ?",
            (result["audit_log_id"],),
        ).fetchone()

    assert row["request_id"] == "test-req-001"
    assert row["reviewer"] == REVIEWER
    assert row["endpoint"].startswith("promotion_rollback_applier")
    payload = json.loads(row["payload_json"])
    response = json.loads(row["response_json"])
    # Payload carries the original violations verbatim (forensics value).
    assert payload["violations"] == violations
    assert payload["from_state"] == "p100"
    assert response["applied"] is True
    assert response["to_state"] == ROLLED_BACK_STATE
    assert response["bindings_snapshot"] == _VALID_BINDINGS


# ---------------------------------------------------------------------------
# 8) Missing rollback_baseline (empty active bindings) → fail-closed reject
# ---------------------------------------------------------------------------


def test_missing_baseline_bindings_is_failclosed(
    temp_db: Path, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "manifest.json"
    # Empty / all-null bindings → cannot snapshot a meaningful baseline.
    _write_manifest(
        manifest_path,
        _make_manifest(
            promotion_state="p25",
            bindings={k: None for k in _VALID_BINDINGS},  # type: ignore[dict-item]
        ),
    )
    before_bytes = manifest_path.read_bytes()

    result = apply_rollback_decision(
        _make_decision(recommendation=REC_ROLLBACK, violations=_rollback_violation()),
        dry_run=False,
        manifest_path=manifest_path,
    )

    assert result["applied"] is False
    assert result["reason"] == REASON_MISSING_BASELINE_BINDINGS
    assert result["error"]
    # Manifest unchanged on disk.
    assert manifest_path.read_bytes() == before_bytes
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM ops_audit_log").fetchone()["n"]
    assert n == 0


# ---------------------------------------------------------------------------
# 9) monitoring_paused recommendation (Agent B will emit) → no-op + warning
# ---------------------------------------------------------------------------


def test_monitoring_paused_recommendation_is_noop(
    temp_db: Path, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _make_manifest(promotion_state="p10"))
    before_bytes = manifest_path.read_bytes()

    result = apply_rollback_decision(
        _make_decision(recommendation=REC_MONITORING_PAUSED, sample_size=12),
        dry_run=False,
        manifest_path=manifest_path,
    )

    assert result["applied"] is False
    assert result["reason"] == REASON_MONITORING_PAUSED
    assert "warning" in result and result["warning"]
    assert manifest_path.read_bytes() == before_bytes


# ---------------------------------------------------------------------------
# 10) Missing manifest file → fail-closed reject (don't synthesize one)
# ---------------------------------------------------------------------------


def test_missing_manifest_file_is_failclosed(
    temp_db: Path, tmp_path: Path
) -> None:
    nonexistent = tmp_path / "does_not_exist.json"
    assert not nonexistent.exists()

    result = apply_rollback_decision(
        _make_decision(recommendation=REC_ROLLBACK, violations=_rollback_violation()),
        dry_run=False,
        manifest_path=nonexistent,
    )

    assert result["applied"] is False
    assert result["reason"] == REASON_NO_MANIFEST
    assert result["error"]
    # We did NOT create the file.
    assert not nonexistent.exists()


# ---------------------------------------------------------------------------
# 11) Accepts an SLOReport dataclass (not just dict)
# ---------------------------------------------------------------------------


def test_accepts_slo_report_dataclass(temp_db: Path, tmp_path: Path) -> None:
    """promotion_slo_monitor.evaluate_window returns an SLOReport. The applier
    must accept it directly (with .to_dict()) — not just plain dict."""
    from backend.services.promotion_slo_monitor import SLOReport

    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _make_manifest(promotion_state="p10"))

    report = SLOReport(
        within_slo=False,
        violations=_rollback_violation(),
        evidence={"comfyui_failure": {"rate": 0.20, "terminal_total": 100}},
        recommendation=REC_ROLLBACK,
        window_hours=48,
        sample_size=100,
    )

    result = apply_rollback_decision(
        report,
        dry_run=True,  # safe — no DB write
        manifest_path=manifest_path,
    )
    assert result["dry_run"] is True
    assert result["would_apply"] is True
    assert result["plan"]["to_state"] == ROLLED_BACK_STATE


# ---------------------------------------------------------------------------
# 12) Unknown recommendation token → no-op + warning (defensive)
# ---------------------------------------------------------------------------


def test_unknown_recommendation_is_noop_with_warning(
    temp_db: Path, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _make_manifest(promotion_state="p25"))
    before_bytes = manifest_path.read_bytes()

    result = apply_rollback_decision(
        _make_decision(recommendation="some_new_state_we_dont_know"),
        dry_run=False,
        manifest_path=manifest_path,
    )

    assert result["applied"] is False
    assert result["reason"] == REASON_NO_ROLLBACK_NEEDED
    assert "warning" in result and "unrecognized" in result["warning"]
    assert manifest_path.read_bytes() == before_bytes


# ---------------------------------------------------------------------------
# 13) CLI smoke — script returns proper exit codes
# ---------------------------------------------------------------------------


def test_cli_dry_run_returns_exit_2_when_rollback_planned(
    temp_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI: with seeded SLO conditions that emit rollback, default --dry-run
    must return EXIT_ROLLBACK (2). End-to-end real data: insert simulation
    failures into the temp DB, then run the script's main()."""
    # Seed enough sim_jobs to push failure rate > 5%
    with db.connect() as conn:
        from backend.tests.test_promotion_slo_monitor import _insert_simulation_job

        for _ in range(20):
            _insert_simulation_job(conn, status="failed", failure_stage="provider_call")
        for _ in range(80):
            _insert_simulation_job(conn, status="done")
        conn.commit()

    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _make_manifest(promotion_state="p25"))

    from backend.scripts import promotion_rollback_check

    code = promotion_rollback_check.main(
        ["--dry-run", "--window", "48", "--manifest", str(manifest_path)]
    )
    captured = capsys.readouterr()
    assert code == 2, f"expected EXIT_ROLLBACK=2 got {code}; stdout={captured.out!r}"
    payload = json.loads(captured.out)
    assert payload["mode"] == "dry_run"
    assert payload["slo"]["recommendation"] == REC_ROLLBACK
    assert payload["applier"]["would_apply"] is True
    # File unchanged because dry-run.
    assert json.loads(manifest_path.read_text())["promotion_state"] == "p25"


def test_cli_continue_returns_exit_0(
    temp_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Healthy SLO + manifest → exit 0 / mode=apply / no manifest mutation."""
    with db.connect() as conn:
        from backend.tests.test_promotion_slo_monitor import (
            _insert_lineage,
            _insert_ops_audit,
            _insert_simulation_job,
        )

        _insert_simulation_job(conn, status="failed", failure_stage="provider_call")
        for _ in range(49):
            _insert_simulation_job(conn, status="done")
        for _ in range(20):
            _insert_lineage(conn, agreement_rate=0.95)
        for i in range(20):
            ep = "POST /api/render/ops/batch-rerun" if i % 2 == 0 else "POST /api/delivery/foo"
            _insert_ops_audit(conn, endpoint=ep, outcome="ok")
        conn.commit()

    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _make_manifest(promotion_state="p50"))

    from backend.scripts import promotion_rollback_check

    code = promotion_rollback_check.main(
        ["--apply", "--window", "48", "--manifest", str(manifest_path)]
    )
    captured = capsys.readouterr()
    assert code == 0, f"expected 0 got {code}; stdout={captured.out!r}"
    payload = json.loads(captured.out)
    assert payload["mode"] == "apply"
    assert payload["slo"]["recommendation"] == REC_CONTINUE
    assert payload["applier"]["applied"] is False
    # Manifest untouched.
    assert json.loads(manifest_path.read_text())["promotion_state"] == "p50"
