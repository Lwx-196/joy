"""Boundary tests for ``backend.services.promotion_rollback_applier``.

Real data discipline (per ~/.claude/CLAUDE.md):
- Real SQLite via the conftest ``temp_db`` fixture (no mocking).
- Real tmp manifest files on disk (``tmp_path`` fixture).
- ``os.replace`` is observed (not stubbed) via filesystem state.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from backend import db
from backend.services.promotion_rollback_applier import (
    AUDIT_OUTCOME_STOP_LOSS_HALT_ALERT,
    REASON_ALREADY_ROLLED_BACK,
    REASON_APPLIED,
    REASON_CONCURRENT_APPLY,
    REASON_DRY_RUN,
    REASON_INSUFFICIENT_DATA,
    REASON_MISSING_BASELINE_BINDINGS,
    REASON_MONITORING_PAUSED,
    REASON_NO_MANIFEST,
    REASON_NO_ROLLBACK_NEEDED,
    REASON_STOP_LOSS_HALT_ALERT,
    REC_CONTINUE,
    REC_INSUFFICIENT,
    REC_MONITORING_PAUSED,
    REC_ROLLBACK,
    REC_STOP_LOSS_HALT,
    ROLLBACK_LOCK_FILENAME,
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
    rollback_forensics: dict[str, Any] | None = None,
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
    if rollback_forensics is not None:
        out["rollback_forensics"] = rollback_forensics
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
    # Wave 3 H-3: completed id is back-compat; full pair under audit_log_ids.
    assert result["audit_log_id"] >= 1
    assert "audit_log_ids" in result
    assert result["audit_log_ids"]["rollback_started"] >= 1
    assert result["audit_log_ids"]["rollback_completed"] == result["audit_log_id"]
    assert (
        result["audit_log_ids"]["rollback_started"]
        < result["audit_log_ids"]["rollback_completed"]
    )

    # Manifest on disk should now be rolled_back.
    new_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert new_payload["promotion_state"] == ROLLED_BACK_STATE
    # Wave 3 H-2: active bindings snapshotted into rollback_forensics.failed_snapshot,
    # NOT rollback_baseline (which carries the revert target).
    assert new_payload["rollback_forensics"]["failed_snapshot"]["bindings"] == _VALID_BINDINGS
    assert new_payload["rollback_forensics"]["failed_snapshot"]["rolled_back_at"]
    assert new_payload["rollback_forensics"]["failed_snapshot"]["from_state"] == "p25"
    # Wave 3 H-2: rollback_baseline preserved (untouched by applier — it is
    # the revert-target schema, reserved for revert tooling).
    assert "rollback_baseline" not in new_payload

    # Wave 3 H-3: audit-first → two rows share request_id correlation id.
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, reviewer, endpoint, reason, outcome, http_status, "
            "       payload_json, request_id "
            "FROM ops_audit_log "
            "WHERE request_id = ? ORDER BY id ASC",
            (result["request_id"],),
        ).fetchall()
    assert len(rows) == 2, f"expected exactly 2 audit rows; got {len(rows)}"
    started, completed = rows[0], rows[1]
    assert started["outcome"] == "rollback_started"
    assert started["http_status"] == 202
    assert completed["outcome"] == "rollback_completed"
    assert completed["http_status"] == 200
    for row in (started, completed):
        assert row["reviewer"] == REVIEWER
        assert row["endpoint"] == "promotion_rollback_applier.apply"
        assert "comfyui_failure_rate" in row["reason"]
    payload = json.loads(completed["payload_json"])
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
            # rollback_baseline preserved as the prior-approved-healthy
            # revert target (Wave 3 H-2 semantic split).
            rollback_baseline={
                "manifest_ref": "prior-sha",
                "captured_at": "2026-05-20T00:00:00+00:00",
                "bindings": _VALID_BINDINGS,
            },
            # rollback_forensics.failed_snapshot is what the *previous* apply
            # would have written.
            rollback_forensics={
                "failed_snapshot": {
                    "bindings": _VALID_BINDINGS,
                    "from_state": "p100",
                    "rolled_back_at": "2026-05-20T00:00:00+00:00",
                    "manifest_ref": None,
                    "captured_at": None,
                },
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
# 13) Wave 3 P0.5 H-3 — manifest write fails after audit-first → aborted row
#     written, started row preserved, manifest untouched, OSError raised.
# ---------------------------------------------------------------------------


def test_audit_aborted_row_on_manifest_write_failure(
    temp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """audit-first invariant: when atomic manifest write fails AFTER the
    rollback_started INSERT, we MUST insert a correlated rollback_aborted
    row (so forensics can pair them by request_id) and leave the manifest
    bytes untouched. The raise surface lets the caller exit 1."""
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _make_manifest(promotion_state="p25"))
    pre_bytes = manifest_path.read_bytes()

    def failing_replace(src: Any, dst: Any) -> None:
        raise OSError("simulated ENOSPC during atomic rename")

    monkeypatch.setattr(
        "backend.services.promotion_rollback_applier.os.replace", failing_replace
    )

    with pytest.raises(OSError):
        apply_rollback_decision(
            _make_decision(recommendation=REC_ROLLBACK, violations=_rollback_violation()),
            dry_run=False,
            manifest_path=manifest_path,
            request_id="test-req-aborted",
        )

    # Manifest untouched on disk (atomic guarantee).
    assert manifest_path.read_bytes() == pre_bytes

    # Audit table: exactly TWO rows with the correlation id, ordered
    # started → aborted (no completed row).
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT outcome, http_status, response_json "
            "FROM ops_audit_log WHERE request_id = ? ORDER BY id ASC",
            ("test-req-aborted",),
        ).fetchall()
    assert [r["outcome"] for r in rows] == ["rollback_started", "rollback_aborted"]
    assert rows[0]["http_status"] == 202
    assert rows[1]["http_status"] == 500
    aborted_response = json.loads(rows[1]["response_json"])
    assert aborted_response["phase"] == "rollback_aborted"
    assert "ENOSPC" in aborted_response["error"]
    assert aborted_response["correlation_started_id"] >= 1


# ---------------------------------------------------------------------------
# 14) Wave 3 P0.5 H-4 — concurrent fcntl.flock holder defers cleanly
# ---------------------------------------------------------------------------


def test_concurrent_apply_in_progress_yields_clean_noop(
    temp_db: Path, tmp_path: Path
) -> None:
    """Simulate another process holding the rollback sentinel lock: the
    applier MUST return ``concurrent_apply_in_progress`` and write neither
    the manifest nor any audit row (the holder owns the audit trail)."""
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _make_manifest(promotion_state="p50"))
    pre_bytes = manifest_path.read_bytes()

    lock_path = manifest_path.parent / ROLLBACK_LOCK_FILENAME
    # Acquire the lock in this test process FD — applier in same process
    # (POSIX advisory locks are per-FD), so the applier's separate fd will
    # see the lock held.
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        result = apply_rollback_decision(
            _make_decision(
                recommendation=REC_ROLLBACK, violations=_rollback_violation()
            ),
            dry_run=False,
            manifest_path=manifest_path,
            request_id="test-req-locked",
        )
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    assert result["applied"] is False
    assert result["reason"] == REASON_CONCURRENT_APPLY
    assert result["error"]
    # Manifest untouched.
    assert manifest_path.read_bytes() == pre_bytes
    # No audit row written under the deferred request_id.
    with db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM ops_audit_log WHERE request_id = ?",
            ("test-req-locked",),
        ).fetchone()["n"]
    assert n == 0


def test_apply_creates_lock_file_in_manifest_dir(
    temp_db: Path, tmp_path: Path
) -> None:
    """Sanity: a successful apply leaves the sentinel lock file on disk in
    the manifest's parent directory (it is intentionally kept so subsequent
    crons can lock it; flock state is per-FD, not persisted to inode)."""
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _make_manifest(promotion_state="p25"))

    apply_rollback_decision(
        _make_decision(recommendation=REC_ROLLBACK, violations=_rollback_violation()),
        dry_run=False,
        manifest_path=manifest_path,
    )

    expected_lock = manifest_path.parent / ROLLBACK_LOCK_FILENAME
    assert expected_lock.exists()


# ---------------------------------------------------------------------------
# 15) CLI smoke — script returns proper exit codes
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
    """Healthy SLO + manifest → exit 0 / mode=apply / no manifest mutation.

    Wave 4 W4-1: the checked-in slo_thresholds.json is placeholder
    (manual_seed) which now emits a `baseline_unmeasured` violation and
    flips the SLO to rollback. Inject a calibrated-provenance thresholds
    JSON via --thresholds so this test exercises the genuine
    healthy-traffic continue path.
    """
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

    # Calibrated provenance overlay so baseline_unmeasured does not fire.
    calibrated_thresholds = tmp_path / "calibrated_slo.json"
    calibrated_thresholds.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "thresholds": {
                    "comfyui_failure_rate_max": 0.05,
                    "vlm_disagreement_rate_max": 0.10,
                    "vlm_judge_missing_rate_max": 0.10,
                    "delivery_gate_rejection_rate_multiplier_max": 1.05,
                    "pre_render_gate_blocker_multiplier_max": 1.10,
                },
                "baseline": {
                    "delivery_gate_rejection_rate": 0.10,
                    "pre_render_gate_blocker_count": 5,
                },
                "baseline_provenance": {
                    "measured_at": datetime.now(timezone.utc).isoformat(),
                    "window_hours": 24,
                    "sample_size": 100,
                    "computed_by": "calibrate_cli",
                    "computed_at_main_sha": "wave4-test",
                },
                "minimum_sample_size": 30,
                "default_window_hours": 48,
            }
        ),
        encoding="utf-8",
    )

    from backend.scripts import promotion_rollback_check

    code = promotion_rollback_check.main(
        [
            "--apply",
            "--window",
            "48",
            "--manifest",
            str(manifest_path),
            "--thresholds",
            str(calibrated_thresholds),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, f"expected 0 got {code}; stdout={captured.out!r}"
    payload = json.loads(captured.out)
    assert payload["mode"] == "apply"
    assert payload["slo"]["recommendation"] == REC_CONTINUE
    assert payload["applier"]["applied"] is False
    # Manifest untouched.
    assert json.loads(manifest_path.read_text())["promotion_state"] == "p50"


# ---------------------------------------------------------------------------
# K-5 — STOP_LOSS_HALT recommendation: audit-only, manifest unchanged
# ---------------------------------------------------------------------------


def test_k5_stop_loss_halt_does_not_revert_manifest(
    temp_db: Path, tmp_path: Path
) -> None:
    """K-5: when SLO monitor emits STOP_LOSS_HALT (灰度流量长时间不足),
    the applier MUST NOT mutate the manifest. It writes an audit alert
    row only — operator review decides next step.

    Pre-K-5 the monitor would emit ROLLBACK for stale paused windows,
    triggering a real auto-rollback on the basis of insufficient signal
    rather than a true SLO violation. K-5 introduces STOP_LOSS_HALT as
    a distinct, audit-only intermediate state.
    """
    manifest_path = tmp_path / "manifest.json"
    pre_payload = _make_manifest(promotion_state="p25")
    _write_manifest(manifest_path, pre_payload)
    pre_bytes = manifest_path.read_bytes()

    decision = _make_decision(
        recommendation=REC_STOP_LOSS_HALT,
        violations=[
            {
                "dimension": "monitoring_paused_stale",
                "actual_days": 9.1,
                "threshold_days": 7,
                "comparator": "<=",
                "context": {"promotion_state": "p25"},
            }
        ],
        sample_size=4,
    )
    result = apply_rollback_decision(
        decision, dry_run=False, manifest_path=manifest_path
    )

    # Manifest must be byte-identical to the pre-state — K-5 forbids mutation.
    assert manifest_path.read_bytes() == pre_bytes
    # Result surfaces the audit-only outcome.
    assert result["applied"] is False
    assert result["reason"] == REASON_STOP_LOSS_HALT_ALERT
    assert result["recommendation"] == REC_STOP_LOSS_HALT
    assert "warning" in result and "operator review" in result["warning"]
    # Audit log row exists and has the right outcome.
    audit_id = result.get("audit_log_id")
    assert isinstance(audit_id, int) and audit_id > 0

    with db.connect() as conn:
        row = conn.execute(
            "SELECT outcome, reason FROM ops_audit_log WHERE id = ?",
            (audit_id,),
        ).fetchone()
    assert row is not None
    assert row["outcome"] == AUDIT_OUTCOME_STOP_LOSS_HALT_ALERT


def test_k5_stop_loss_halt_dry_run_writes_no_audit_row(
    temp_db: Path, tmp_path: Path
) -> None:
    """K-5: dry_run STOP_LOSS_HALT mirrors dry_run ROLLBACK semantics —
    no DB write, no manifest write, would_apply=True for preview."""
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(manifest_path, _make_manifest(promotion_state="p10"))
    pre_bytes = manifest_path.read_bytes()

    with db.connect() as conn:
        pre_count = conn.execute("SELECT COUNT(*) AS n FROM ops_audit_log").fetchone()["n"]

    result = apply_rollback_decision(
        _make_decision(recommendation=REC_STOP_LOSS_HALT, sample_size=3),
        dry_run=True,
        manifest_path=manifest_path,
    )

    assert manifest_path.read_bytes() == pre_bytes
    assert result["dry_run"] is True
    assert result["would_apply"] is True
    assert result["applied"] is False
    assert result["reason"] == REASON_STOP_LOSS_HALT_ALERT

    with db.connect() as conn:
        post_count = conn.execute("SELECT COUNT(*) AS n FROM ops_audit_log").fetchone()["n"]
    assert post_count == pre_count, "dry_run must NOT write an audit row"
