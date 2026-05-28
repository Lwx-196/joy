"""C3.0.1 Ops Readiness Gate — unit tests for the 11 new status fields.

Covers :mod:`backend.services.ops_readiness` and the additive contract on
``GET /api/render/ops/vlm-comfyui/status`` (the ``promotion`` block is new;
the existing ``vlm`` / ``comfyui`` / ``gate`` keys are unchanged).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.services import ops_readiness


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _write_manifest(path: Path, **overrides) -> Path:
    payload = {
        "schema_version": 1,
        "version": "v-test",
        "scope": "production",
        "promotion_state": "shadow",
        "bindings": {},
        "rollback_baseline": {
            "manifest_ref": None,
            "captured_at": None,
            "bindings": {},
        },
    }
    payload.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _seed_render_job(
    conn,
    *,
    case_id: int,
    status: str,
    started_at: datetime | None,
    finished_at: datetime | None,
    output_path: str | None,
    render_mode: str = "ai",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO render_jobs (
            case_id, brand, template, status, enqueued_at,
            started_at, finished_at, output_path, semantic_judge,
            render_mode, meta_json
        )
        VALUES (?, 'b', 'tri-compare', ?, ?, ?, ?, ?, 'auto', ?, '{}')
        """,
        (
            case_id,
            status,
            _iso(datetime.now(timezone.utc) - timedelta(hours=2)),
            _iso(started_at) if started_at else None,
            _iso(finished_at) if finished_at else None,
            output_path,
            render_mode,
        ),
    )
    return int(cur.lastrowid)


def _seed_case(conn) -> int:
    now = _iso(datetime.now(timezone.utc))
    scan_id = conn.execute(
        "INSERT INTO scans (started_at, root_paths, mode) VALUES (?, '[]', 'unit')",
        (now,),
    ).lastrowid
    cur = conn.execute(
        """
        INSERT INTO cases (scan_id, abs_path, category, last_modified, indexed_at)
        VALUES (?, ?, 'standard_face', ?, ?)
        """,
        (scan_id, f"/tmp/case-{scan_id}", now, now),
    )
    return int(cur.lastrowid)


def _seed_audit(
    conn,
    *,
    outcome: str,
    request_id: str = "req-test",
    reason: str | None = None,
    http_status: int = 200,
    created_at: datetime | None = None,
) -> int:
    when = _iso(created_at or datetime.now(timezone.utc))
    cur = conn.execute(
        """
        INSERT INTO ops_audit_log
          (request_id, endpoint, reviewer, reason,
           payload_json, response_json, outcome, http_status, created_at)
        VALUES (?, ?, 'system/slo_monitor', ?, '{}', '{}', ?, ?, ?)
        """,
        (
            request_id,
            "promotion_rollback_applier.apply",
            reason,
            outcome,
            http_status,
            when,
        ),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Field 1-2: manifest_state + bucket_exposure
# ---------------------------------------------------------------------------


def test_compute_status_missing_manifest_defaults_to_shadow(
    client, temp_db: Path, tmp_path: Path, monkeypatch
) -> None:
    """No manifest on disk → fail-closed shadow + 0% exposure."""
    monkeypatch.setattr(
        "backend.services.promotion_manifest_loader.DEFAULT_MANIFEST_PATH",
        tmp_path / "missing.json",
    )
    from backend import db

    with db.connect() as conn:
        out = ops_readiness.compute_promotion_status(
            conn, probe_comfyui=False
        )
    assert out["manifest_state"] == "shadow"
    assert out["bucket_exposure_pct"] == 0


@pytest.mark.parametrize(
    "state, pct",
    [
        ("shadow", 0),
        ("p10", 10),
        ("p25", 25),
        ("p50", 50),
        ("p100", 100),
        ("rolled_back", 0),
    ],
)
def test_bucket_exposure_table_matches_documented_states(
    client, temp_db: Path, tmp_path: Path, monkeypatch, state: str, pct: int
) -> None:
    mp = _write_manifest(tmp_path / "m.json", promotion_state=state)
    monkeypatch.setattr(
        "backend.services.promotion_manifest_loader.DEFAULT_MANIFEST_PATH", mp
    )
    from backend import db

    with db.connect() as conn:
        out = ops_readiness.compute_promotion_status(
            conn, probe_comfyui=False
        )
    assert out["manifest_state"] == state
    assert out["bucket_exposure_pct"] == pct


def test_unknown_state_falls_back_to_shadow_zero(
    client, temp_db: Path, tmp_path: Path, monkeypatch
) -> None:
    """Manifest with non-VALID_STATES state → fail-closed shadow (loader contract)."""
    mp = _write_manifest(tmp_path / "m.json", promotion_state="wibble")
    monkeypatch.setattr(
        "backend.services.promotion_manifest_loader.DEFAULT_MANIFEST_PATH", mp
    )
    from backend import db

    with db.connect() as conn:
        out = ops_readiness.compute_promotion_status(
            conn, probe_comfyui=False
        )
    assert out["manifest_state"] == "shadow"
    assert out["bucket_exposure_pct"] == 0


# ---------------------------------------------------------------------------
# Field 3-5: SLO recommendation + sample_size + violations
# ---------------------------------------------------------------------------


def test_slo_block_carries_recommendation_and_sample(
    client, temp_db: Path, tmp_path: Path, monkeypatch
) -> None:
    """Empty DB + shadow → insufficient_data + 0 sample size."""
    mp = _write_manifest(tmp_path / "m.json", promotion_state="shadow")
    monkeypatch.setattr(
        "backend.services.promotion_manifest_loader.DEFAULT_MANIFEST_PATH", mp
    )
    from backend import db

    with db.connect() as conn:
        out = ops_readiness.compute_promotion_status(
            conn, probe_comfyui=False
        )
    # shadow + empty DB → insufficient_data (legacy semantics, see
    # promotion_slo_monitor.evaluate_window Wave 3 C-1 triage).
    assert out["slo_recommendation"] == "insufficient_data"
    assert out["sample_size"] == 0
    assert out["minimum_sample_size"] > 0  # default 30
    assert isinstance(out["violations"], list)


# ---------------------------------------------------------------------------
# Field 6: baseline_freshness
# ---------------------------------------------------------------------------


def test_baseline_freshness_reports_age_days_when_captured_at_set(
    client, temp_db: Path, tmp_path: Path, monkeypatch
) -> None:
    captured = datetime.now(timezone.utc) - timedelta(days=12, hours=3)
    mp = _write_manifest(
        tmp_path / "m.json",
        promotion_state="p10",
        bindings={"vlm_calibration_hash": "sha256:abc"},
        rollback_baseline={
            "manifest_ref": "abc1234",
            "captured_at": _iso(captured),
            "bindings": {"vlm_calibration_hash": "sha256:prior"},
        },
    )
    monkeypatch.setattr(
        "backend.services.promotion_manifest_loader.DEFAULT_MANIFEST_PATH", mp
    )
    from backend import db

    with db.connect() as conn:
        out = ops_readiness.compute_promotion_status(
            conn, probe_comfyui=False
        )
    bf = out["baseline_freshness"]
    assert bf["bindings_present"] is True
    assert bf["rollback_baseline_captured_at"] is not None
    # Within a tight tolerance of 12 days
    assert 11.5 < bf["rollback_baseline_age_days"] < 12.5


def test_baseline_freshness_handles_null_captured_at(
    client, temp_db: Path, tmp_path: Path, monkeypatch
) -> None:
    mp = _write_manifest(tmp_path / "m.json")
    monkeypatch.setattr(
        "backend.services.promotion_manifest_loader.DEFAULT_MANIFEST_PATH", mp
    )
    from backend import db

    with db.connect() as conn:
        out = ops_readiness.compute_promotion_status(
            conn, probe_comfyui=False
        )
    bf = out["baseline_freshness"]
    assert bf["bindings_present"] is False
    assert bf["rollback_baseline_captured_at"] is None
    assert bf["rollback_baseline_age_days"] is None


# ---------------------------------------------------------------------------
# Field 7: ComfyUI live probe
# ---------------------------------------------------------------------------


def test_comfyui_probe_skipped_when_flag_false(
    client, temp_db: Path
) -> None:
    from backend import db

    with db.connect() as conn:
        out = ops_readiness.compute_promotion_status(
            conn, probe_comfyui=False
        )
    probe = out["comfyui_live_probe"]
    assert probe["reachable"] is None
    assert probe["skipped"] is True


def test_comfyui_probe_unreachable_returns_error_block(
    client, temp_db: Path
) -> None:
    """Point at an unreachable URL → reachable=False + error field present."""
    from backend import db

    with db.connect() as conn:
        out = ops_readiness.compute_promotion_status(
            conn,
            probe_comfyui=True,
            comfyui_base_url="http://127.0.0.1:1",  # port 1 is reserved
        )
    probe = out["comfyui_live_probe"]
    assert probe["reachable"] is False
    assert "error" in probe
    assert probe["base_url"] == "http://127.0.0.1:1"


def test_comfyui_probe_success_parses_queue_lengths(
    client, temp_db: Path, monkeypatch
) -> None:
    """Stub urllib to return a ComfyUI-shaped /queue payload."""

    class _StubResp:
        status = 200

        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self, _n: int = 0) -> bytes:
            return self._body

        def __enter__(self) -> "_StubResp":
            return self

        def __exit__(self, *exc) -> None:
            return None

    def _fake_urlopen(url, timeout):  # noqa: ARG001
        body = json.dumps(
            {"queue_running": [1, 2], "queue_pending": [3, 4, 5]}
        ).encode("utf-8")
        return _StubResp(body)

    monkeypatch.setattr(
        "backend.services.ops_readiness._urllib_request.urlopen", _fake_urlopen
    )
    from backend import db

    with db.connect() as conn:
        out = ops_readiness.compute_promotion_status(
            conn,
            probe_comfyui=True,
            comfyui_base_url="http://stub:9999",
        )
    probe = out["comfyui_live_probe"]
    assert probe["reachable"] is True
    assert probe["http_status"] == 200
    assert probe["queue_running"] == 2
    assert probe["queue_pending"] == 3


# ---------------------------------------------------------------------------
# Field 8: render_latency_p50_p95
# ---------------------------------------------------------------------------


def test_render_latency_p50_p95_grouped_by_mode(
    client, temp_db: Path
) -> None:
    from backend import db

    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        cid = _seed_case(conn)
        # 5 ai jobs, durations 1s, 2s, 3s, 4s, 100s → p50=3, p95~80.8
        for i, secs in enumerate((1, 2, 3, 4, 100), start=1):
            started = now - timedelta(hours=1)
            finished = started + timedelta(seconds=secs)
            _seed_render_job(
                conn,
                case_id=cid,
                status="done",
                started_at=started,
                finished_at=finished,
                output_path=f"/tmp/out-{i}.jpg",
                render_mode="ai",
            )
        # 1 best-pair job, 10s
        started = now - timedelta(hours=1)
        finished = started + timedelta(seconds=10)
        _seed_render_job(
            conn,
            case_id=cid,
            status="done",
            started_at=started,
            finished_at=finished,
            output_path="/tmp/out-bp.jpg",
            render_mode="best-pair",
        )
        out = ops_readiness.compute_promotion_status(
            conn, probe_comfyui=False
        )
    latency = out["render_latency"]
    by_mode = latency["by_render_mode"]
    assert by_mode["ai"]["count"] == 5
    assert by_mode["ai"]["p50_seconds"] == pytest.approx(3.0, abs=0.01)
    # p95 linear-interp on [1,2,3,4,100] → idx=3.8 → 4 + 0.8*(100-4) = 80.8
    assert by_mode["ai"]["p95_seconds"] == pytest.approx(80.8, abs=0.1)
    assert by_mode["best-pair"]["count"] == 1
    assert by_mode["best-pair"]["p50_seconds"] == pytest.approx(10.0, abs=0.01)


def test_render_latency_excludes_unfinished_and_pre_window_rows(
    client, temp_db: Path
) -> None:
    from backend import db

    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        cid = _seed_case(conn)
        # In-window done with timestamps → counted
        _seed_render_job(
            conn,
            case_id=cid,
            status="done",
            started_at=now - timedelta(hours=1),
            finished_at=now - timedelta(minutes=55),
            output_path="/tmp/a.jpg",
            render_mode="ai",
        )
        # status=running → excluded
        _seed_render_job(
            conn,
            case_id=cid,
            status="running",
            started_at=now - timedelta(minutes=30),
            finished_at=None,
            output_path=None,
            render_mode="ai",
        )
        # 3 days ago → outside 24h window
        _seed_render_job(
            conn,
            case_id=cid,
            status="done",
            started_at=now - timedelta(days=3),
            finished_at=now - timedelta(days=3) + timedelta(seconds=30),
            output_path="/tmp/old.jpg",
            render_mode="ai",
        )
        out = ops_readiness.compute_promotion_status(
            conn, probe_comfyui=False
        )
    by_mode = out["render_latency"]["by_render_mode"]
    assert by_mode.get("ai", {}).get("count", 0) == 1


# ---------------------------------------------------------------------------
# Field 9: silent_fail_count
# ---------------------------------------------------------------------------


def test_silent_fail_counts_done_with_null_output_within_window(
    client, temp_db: Path
) -> None:
    from backend import db

    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        cid = _seed_case(conn)
        # Silent fail: done + output_path NULL, in window
        _seed_render_job(
            conn,
            case_id=cid,
            status="done",
            started_at=now - timedelta(hours=1),
            finished_at=now - timedelta(minutes=58),
            output_path=None,
            render_mode="ai",
        )
        # Silent fail: done + output_path empty string
        _seed_render_job(
            conn,
            case_id=cid,
            status="done",
            started_at=now - timedelta(hours=1),
            finished_at=now - timedelta(minutes=57),
            output_path="",
            render_mode="ai",
        )
        # Healthy: done + path
        _seed_render_job(
            conn,
            case_id=cid,
            status="done",
            started_at=now - timedelta(hours=1),
            finished_at=now - timedelta(minutes=56),
            output_path="/tmp/ok.jpg",
            render_mode="ai",
        )
        # failed → excluded (different surface)
        _seed_render_job(
            conn,
            case_id=cid,
            status="failed",
            started_at=now - timedelta(hours=1),
            finished_at=now - timedelta(minutes=55),
            output_path=None,
            render_mode="ai",
        )
        out = ops_readiness.compute_promotion_status(
            conn, probe_comfyui=False
        )
    assert out["silent_fail"]["count"] == 2
    assert out["silent_fail"]["window_hours"] == 24


# ---------------------------------------------------------------------------
# Field 10: rollback_applier_last
# ---------------------------------------------------------------------------


def test_rollback_applier_last_returns_latest_by_id(
    client, temp_db: Path
) -> None:
    from backend import db

    with db.connect() as conn:
        _seed_audit(
            conn,
            outcome="rollback_started",
            request_id="req-1",
            reason="violation_threshold",
        )
        _seed_audit(
            conn,
            outcome="rollback_completed",
            request_id="req-1",
            reason="rollback_applied",
        )
        # Unrelated audit — must NOT be picked
        _seed_audit(
            conn,
            outcome="ok",
            request_id="req-other",
            reason="batch_rerun",
        )
        out = ops_readiness.compute_promotion_status(
            conn, probe_comfyui=False
        )
    last = out["rollback_applier_last"]
    assert last["last_outcome"] == "rollback_completed"
    assert last["last_request_id"] == "req-1"
    assert last["last_reason"] == "rollback_applied"


def test_rollback_applier_last_returns_null_block_when_no_rows(
    client, temp_db: Path
) -> None:
    from backend import db

    with db.connect() as conn:
        out = ops_readiness.compute_promotion_status(
            conn, probe_comfyui=False
        )
    last = out["rollback_applier_last"]
    assert last["last_outcome"] is None
    assert last["last_run_at"] is None
    assert last["last_request_id"] is None


# ---------------------------------------------------------------------------
# Endpoint contract — additive only
# ---------------------------------------------------------------------------


def test_status_endpoint_adds_promotion_block_without_breaking_legacy_keys(
    client, temp_db: Path
) -> None:
    """Existing top-level keys must remain; ``promotion`` added on top."""
    resp = client.get(
        "/api/render/ops/vlm-comfyui/status?probe_comfyui=false"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # legacy keys still present
    for k in ("days", "vlm", "comfyui", "gate"):
        assert k in body, f"legacy top-level {k!r} missing"
    # new block present
    assert "promotion" in body
    promo = body["promotion"]
    expected = {
        "manifest_state",
        "bucket_exposure_pct",
        "slo_recommendation",
        "sample_size",
        "minimum_sample_size",
        "violations",
        "baseline_freshness",
        "comfyui_live_probe",
        "render_latency",
        "silent_fail",
        "rollback_applier_last",
        "computed_at",
        "schema_version",
    }
    missing = expected - set(promo.keys())
    assert not missing, f"promotion block missing keys: {missing}"


def test_status_endpoint_probe_comfyui_query_flag_honored(
    client, temp_db: Path
) -> None:
    """When probe_comfyui=false the probe block must be skipped."""
    resp = client.get(
        "/api/render/ops/vlm-comfyui/status?probe_comfyui=false"
    )
    body = resp.json()
    probe = body["promotion"]["comfyui_live_probe"]
    assert probe["skipped"] is True
    assert probe["reachable"] is None


def test_status_endpoint_slo_window_hours_query_passes_through(
    client, temp_db: Path
) -> None:
    """SLO block surfaces the configured window_hours."""
    resp = client.get(
        "/api/render/ops/vlm-comfyui/status?probe_comfyui=false&slo_window_hours=6"
    )
    body = resp.json()
    assert body["promotion"]["slo_window_hours"] == 6
