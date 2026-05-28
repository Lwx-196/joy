"""C3.0.3 — Tests for the alerting wiring.

Covers:
  * `compile_alert_from_slo_report` — recommendation → severity mapping +
    benign short-circuit.
  * `compile_alert_from_applier_result` — outcome / reason mapping.
  * `fire_alert` / `ack_alert` / `resolve_alert` lifecycle — every row goes
    to ops_audit_log sharing one correlation_id.
  * StubChannel — append-only JSONL, surface errors via {"ok": false}.
  * HTTP layer: POST /alerts/fire / ack / resolve end-to-end through the
    FastAPI router on a per-test SQLite DB.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.services import ops_alerting


# ---------------------------------------------------------------------------
# Compile — SLO report
# ---------------------------------------------------------------------------


def test_compile_from_slo_returns_none_when_recommendation_continue() -> None:
    report = {"recommendation": "continue", "sample_size": 200, "violations": []}
    assert ops_alerting.compile_alert_from_slo_report(report) is None


@pytest.mark.parametrize(
    "rec, expected_severity",
    [
        ("rollback", ops_alerting.SEVERITY_CRITICAL),
        ("stop_loss_halt", ops_alerting.SEVERITY_CRITICAL),
        ("insufficient_data", ops_alerting.SEVERITY_WARN),
        ("monitoring_paused", ops_alerting.SEVERITY_WARN),
        ("some_unknown_token", ops_alerting.SEVERITY_INFO),
    ],
)
def test_compile_from_slo_severity_mapping(rec: str, expected_severity: str) -> None:
    report = {
        "recommendation": rec,
        "sample_size": 5,
        "window_hours": 24,
        "violations": [{"dimension": "comfyui_failure_rate", "delta": 0.1}],
        "evidence": {"promotion_state": "p10"},
        "notes": "synthetic",
    }
    event = ops_alerting.compile_alert_from_slo_report(report)
    assert event is not None
    assert event.severity == expected_severity
    assert event.source == ops_alerting.SOURCE_SLO
    assert event.detail["recommendation"] == rec
    assert event.detail["promotion_state"] == "p10"
    assert event.detail["violations"] == [
        {"dimension": "comfyui_failure_rate", "delta": 0.1}
    ]


def test_compile_from_slo_preserves_caller_supplied_correlation_id() -> None:
    event = ops_alerting.compile_alert_from_slo_report(
        {"recommendation": "rollback", "violations": []}, correlation_id="custom-1"
    )
    assert event is not None
    assert event.correlation_id == "custom-1"


# ---------------------------------------------------------------------------
# Compile — applier result
# ---------------------------------------------------------------------------


def test_compile_from_applier_skips_benign_outcomes() -> None:
    for reason in (
        "no_rollback_needed",
        "insufficient_data",
        "monitoring_paused",
        "already_rolled_back",
        "dry_run",
        "baseline_unmeasured_only_no_real_breach",
    ):
        assert ops_alerting.compile_alert_from_applier_result(
            {"reason": reason, "outcome": "ok"}
        ) is None


@pytest.mark.parametrize(
    "result, expected_severity",
    [
        ({"reason": "rollback_applied", "outcome": "rollback_completed"}, "critical"),
        ({"reason": "stop_loss_halt_alert", "outcome": "stop_loss_halt_alert"}, "critical"),
        ({"reason": "invalid_manifest_state", "outcome": "error"}, "critical"),
        ({"reason": "some_warn_token", "outcome": "warn"}, "warn"),
    ],
)
def test_compile_from_applier_severity_mapping(
    result: dict, expected_severity: str
) -> None:
    event = ops_alerting.compile_alert_from_applier_result(result)
    assert event is not None
    assert event.severity == expected_severity
    assert event.source == ops_alerting.SOURCE_APPLIER


def test_compile_from_applier_uses_result_request_id_as_correlation() -> None:
    event = ops_alerting.compile_alert_from_applier_result(
        {
            "request_id": "rb-abc-123",
            "reason": "rollback_applied",
            "outcome": "rollback_completed",
        }
    )
    assert event is not None
    assert event.correlation_id == "rb-abc-123"


# ---------------------------------------------------------------------------
# StubChannel
# ---------------------------------------------------------------------------


def test_stub_channel_appends_jsonl(tmp_path: Path) -> None:
    target = tmp_path / "alerts.jsonl"
    channel = ops_alerting.StubChannel(target)
    event = ops_alerting.AlertEvent(
        correlation_id="cid-1",
        source=ops_alerting.SOURCE_SLO,
        severity=ops_alerting.SEVERITY_CRITICAL,
        title="t",
        detail={"k": "v"},
        fired_at=datetime.now(timezone.utc).isoformat(),
    )
    r1 = channel.send(event)
    r2 = channel.send(event)
    assert r1["ok"] is True
    assert r2["ok"] is True
    lines = target.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert all(p["correlation_id"] == "cid-1" for p in parsed)


def test_stub_channel_handles_oserror_gracefully(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "x"
    channel = ops_alerting.StubChannel(target)

    def _boom(*args, **kwargs):  # noqa: ARG001
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", _boom)
    event = ops_alerting.AlertEvent(
        correlation_id="c",
        source=ops_alerting.SOURCE_SLO,
        severity="critical",
        title="t",
        detail={},
        fired_at="2026-05-29T00:00:00+00:00",
    )
    result = channel.send(event)
    assert result["ok"] is False
    assert "OSError" in (result.get("error") or "")


# ---------------------------------------------------------------------------
# Lifecycle through fire_alert / ack_alert / resolve_alert
# ---------------------------------------------------------------------------


def test_fire_alert_writes_audit_row_with_correlation_id(
    client, temp_db: Path, tmp_path: Path
) -> None:
    from backend import db

    event = ops_alerting.compile_alert_from_slo_report(
        {
            "recommendation": "rollback",
            "violations": [{"dimension": "comfyui_failure_rate"}],
            "sample_size": 50,
            "window_hours": 24,
        }
    )
    assert event is not None
    channel = ops_alerting.StubChannel(tmp_path / "alerts.jsonl")
    with db.connect() as conn:
        response = ops_alerting.fire_alert(event, conn=conn, channels=[channel])
        rows = conn.execute(
            "SELECT request_id, endpoint, outcome FROM ops_audit_log "
            "WHERE endpoint = ?",
            (ops_alerting.ENDPOINT_FIRE,),
        ).fetchall()
    assert response["dispatched"] is True
    assert len(rows) == 1
    assert rows[0]["request_id"] == event.correlation_id
    assert rows[0]["outcome"] == "ok"


def test_fire_alert_dispatch_false_skips_channels(
    client, temp_db: Path, tmp_path: Path
) -> None:
    """dispatch=False must NOT call any channel — used in dry-run mode."""
    from backend import db

    sent: list = []

    class _RecordingChannel:
        name = "rec"

        def send(self, ev):
            sent.append(ev)
            return {"ok": True, "http_status": 200, "error": None, "channel": "rec"}

    event = ops_alerting.compile_alert_from_slo_report(
        {"recommendation": "rollback", "violations": [], "sample_size": 50}
    )
    assert event is not None
    with db.connect() as conn:
        response = ops_alerting.fire_alert(
            event, conn=conn, channels=[_RecordingChannel()], dispatch=False
        )
    assert sent == []
    assert response["dispatched"] is False
    # Audit row still landed
    with db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM ops_audit_log WHERE endpoint = ?",
            (ops_alerting.ENDPOINT_FIRE,),
        ).fetchone()["n"]
    assert n == 1


def test_fire_alert_rejects_unknown_source(client, temp_db: Path) -> None:
    bad_event = ops_alerting.AlertEvent(
        correlation_id="c",
        source="random_source",
        severity="critical",
        title="x",
        detail={},
        fired_at="2026-05-29T00:00:00+00:00",
    )
    from backend import db

    with db.connect() as conn, pytest.raises(ValueError):
        ops_alerting.fire_alert(bad_event, conn=conn, channels=[], dispatch=False)


def test_ack_and_resolve_share_correlation_id(
    client, temp_db: Path, tmp_path: Path
) -> None:
    """Full alert/ack/resolve cycle — all three rows must share request_id."""
    from backend import db

    event = ops_alerting.compile_alert_from_slo_report(
        {"recommendation": "rollback", "violations": [], "sample_size": 100}
    )
    assert event is not None
    channel = ops_alerting.StubChannel(tmp_path / "alerts.jsonl")
    with db.connect() as conn:
        ops_alerting.fire_alert(event, conn=conn, channels=[channel])
        ops_alerting.ack_alert(
            conn=conn,
            correlation_id=event.correlation_id,
            operator="on-call-primary",
            note="seen at 09:14",
        )
        ops_alerting.resolve_alert(
            conn=conn,
            correlation_id=event.correlation_id,
            operator="on-call-primary",
            note="manifest reverted to shadow",
        )
        rows = conn.execute(
            """
            SELECT endpoint, outcome, reason, reviewer
            FROM ops_audit_log
            WHERE request_id = ?
            ORDER BY id ASC
            """,
            (event.correlation_id,),
        ).fetchall()
    endpoints = [r["endpoint"] for r in rows]
    assert endpoints == [
        ops_alerting.ENDPOINT_FIRE,
        ops_alerting.ENDPOINT_ACK,
        ops_alerting.ENDPOINT_RESOLVE,
    ]
    assert all(r["reviewer"] == ops_alerting.REVIEWER_SYSTEM for r in rows)


# ---------------------------------------------------------------------------
# HTTP endpoint integration tests
# ---------------------------------------------------------------------------


def test_post_alerts_fire_compiles_and_records(client, temp_db: Path) -> None:
    resp = client.post(
        "/api/render/ops/alerts/fire",
        json={
            "source": "slo_report",
            "slo_report": {
                "recommendation": "rollback",
                "violations": [{"dimension": "comfyui_failure_rate"}],
                "sample_size": 50,
                "window_hours": 24,
            },
            "reviewer": "operator-1",
            "reason": "synthetic test fire",
            "dispatch": False,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["alert_compiled"] is True
    cid = body["correlation_id"]
    assert isinstance(cid, str) and cid

    # Audit row landed
    from backend import db

    with db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM ops_audit_log WHERE request_id = ?",
            (cid,),
        ).fetchone()["n"]
    # We expect 2 rows: one from fire_alert (alert_fired) + one from the
    # endpoint wrapper write (POST /alerts/fire) — both share correlation_id
    # only when the wrapper request_id matches. Wrapper uses its own request
    # id (uuid), so audit row count keyed by correlation_id is 1.
    assert n == 1


def test_post_alerts_fire_rejects_unknown_source(client, temp_db: Path) -> None:
    resp = client.post(
        "/api/render/ops/alerts/fire",
        json={
            "source": "random_source",
            "reviewer": "operator-1",
            "dispatch": False,
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert "source must be one of" in body["detail"]["error"]


def test_post_alerts_fire_requires_payload_for_chosen_source(
    client, temp_db: Path
) -> None:
    resp = client.post(
        "/api/render/ops/alerts/fire",
        json={
            "source": "slo_report",
            "reviewer": "operator-1",
            "dispatch": False,
        },
    )
    assert resp.status_code == 400, resp.text
    assert "slo_report required" in resp.json()["detail"]["error"]


def test_post_alerts_lifecycle_full_cycle(client, temp_db: Path) -> None:
    fire = client.post(
        "/api/render/ops/alerts/fire",
        json={
            "source": "slo_report",
            "slo_report": {
                "recommendation": "rollback",
                "violations": [{"dimension": "vlm_disagreement_rate"}],
                "sample_size": 60,
            },
            "reviewer": "operator-1",
            "dispatch": False,
        },
    )
    cid = fire.json()["correlation_id"]
    ack = client.post(
        f"/api/render/ops/alerts/{cid}/ack",
        json={"operator": "on-call-primary", "note": "seen"},
    )
    assert ack.status_code == 200, ack.text
    assert ack.json()["stage"] == "acked"
    resolve = client.post(
        f"/api/render/ops/alerts/{cid}/resolve",
        json={"operator": "on-call-primary", "note": "manifest rolled back"},
    )
    assert resolve.status_code == 200, resolve.text
    assert resolve.json()["stage"] == "resolved"

    from backend import db

    with db.connect() as conn:
        endpoints = [
            r["endpoint"]
            for r in conn.execute(
                "SELECT endpoint FROM ops_audit_log WHERE request_id = ? ORDER BY id ASC",
                (cid,),
            ).fetchall()
        ]
    assert endpoints == [
        ops_alerting.ENDPOINT_FIRE,
        ops_alerting.ENDPOINT_ACK,
        ops_alerting.ENDPOINT_RESOLVE,
    ]


def test_post_alerts_fire_benign_payload_returns_alert_compiled_false(
    client, temp_db: Path
) -> None:
    resp = client.post(
        "/api/render/ops/alerts/fire",
        json={
            "source": "slo_report",
            "slo_report": {"recommendation": "continue", "violations": []},
            "reviewer": "operator-1",
            "dispatch": False,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["alert_compiled"] is False
    assert "benign" in body["note"]


def test_post_alerts_fire_applier_source(client, temp_db: Path) -> None:
    resp = client.post(
        "/api/render/ops/alerts/fire",
        json={
            "source": "rollback_applier",
            "applier_result": {
                "reason": "rollback_applied",
                "outcome": "rollback_completed",
                "request_id": "rb-cid-987",
                "from_state": "p10",
                "to_state": "rolled_back",
                "recommendation": "rollback",
            },
            "reviewer": "operator-1",
            "dispatch": False,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["alert_compiled"] is True
    assert body["correlation_id"] == "rb-cid-987"
    assert body["event"]["severity"] == "critical"
    assert body["event"]["source"] == "rollback_applier"
